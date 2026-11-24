#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Winlator Frost 25W Automated Patcher (corrected for brunodev85/winlator-app main @ v11.2)

针对真实上游 brunodev85/winlator-app（main 分支，2026-11 状态，versionName "11.2"）
的安全、幂等二次定制引擎。相对手册 3.1 节初版修正了以下失效点：

  1. Box64 FROST_25W 预设三处注入全部修正：
     - Box64Preset.java 常量（保留 PERFORMANCE 锚点）。
     - Box64PresetManager.getEnvVars 分支：按上游实际风格 envVars.put("BOX64_...")。
     - Box64PresetManager.getPresets 追加：手册正则因 context.getString(R.string.xxx)
       嵌套括号而匹配不到，这里改为行级定位，并使用字符串字面量 "Frost 25W Extreme"。
     - 所有 Java 写盘前仍做 braces_balanced() 校验，失败整体回滚。
  2. FileProvider：上游 Java 硬编码的是大写 "com.winlator.FileProvider"（手册误写成小写
     "com.winlator.fileprovider" 导致漏替换、运行时崩溃）。这里大小写两种形态都同步为
     "<package_id>.FileProvider"，与 Manifest 中 applicationId 动态占位符严格一致。
  3. 主题：上游 res 下没有 values/colors.xml（颜色内联在 values/styles.xml 的
     AppThemeDark 中）。有 colors.xml 时沿用原逻辑；否则只改 AppThemeDark 样式块内已存在
     的 <item>，绝不改动 AppThemeLight。
  4. arrays.xml：只补充上游缺失的 1600x720 (20:9) / 2400x1080 (20:9)；1440x900、1920x1080
     已存在则跳过；video_memory_size_entries 上游已有 6 GB / 8 GB，不再重复添加
     6144 MB / 8192 MB。ET.indent 格式化后回写，保证 XML 合法。
  5. Gradle：保留 applicationId / versionName / release 块 / androidResources noCompress /
     lint 逻辑，并额外向 gradle.properties 末尾追加
     org.gradle.jvmargs=-Xmx4g -Dfile.encoding=UTF-8（已存在则跳过，其他内容不动）。
  6. 全模块幂等：重复运行时全部 SKIP 且不落盘；任何匹配失败只 WARN/SKIP，绝不异常中断，
     进程退出码恒为 0。

设计约束：仅使用 Python 3 标准库；默认参数适配上文所述上游 v11.2。
"""

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Optional, Tuple
from xml.sax.saxutils import escape as xml_escape


PATCHER_VERSION = "2.0"
FROST_PRESET_NAME = "Frost 25W Extreme"
FROST_RESOLUTIONS = (
    "1600x720 (20:9)",
    "2400x1080 (20:9)",
)
UPSTREAM_RESOLUTIONS = (
    "1440x900 (16:10)",
    "1920x1080 (16:9)",
)
# 无 colors.xml 时，只针对 AppThemeDark 块内已存在的 item 做替换。
# 上游 AppThemeDark 当前包含：colorAccent / colorPrimary / colorPrimaryDark /
# colorPrimarySurface / colorSecondarySurface。
FROST_STYLE_ITEMS = (
    ("colorAccent", "#00E5FF"),
    ("colorPrimary", "#00B8D4"),
    ("colorPrimaryDark", "#080D18"),
    ("colorPrimarySurface", "#0B0F19"),
    ("colorSecondarySurface", "#0F172A"),
)
# 上游若将来补上 colors.xml，则沿用手册 3.1 节的配色键逻辑。
FROST_COLORS_PALETTE = {
    "colorPrimary": "#00B8D4",
    "colorPrimaryDark": "#080D18",
    "colorAccent": "#00E5FF",
    "window_background_color": "#0B0F19",
    "navigation_view_background_color": "#0F172A",
}
GRADLE_PROPERTIES_JVMARGS = "org.gradle.jvmargs=-Xmx4g -Dfile.encoding=UTF-8"
DISPLAY_METHOD_MARKER = "applyFrost25WDisplayOptimization"
# 用拼接构造，避免源码里出现需要转义的应用 ID 占位符字面量。
APPLICATION_ID_PLACEHOLDER = chr(36) + "{applicationId}"


# ---------------------------------------------------------------------------
# 基础设施：报告 / IO / Java 大括号平衡校验
# ---------------------------------------------------------------------------
class PatchReport:
    def __init__(self) -> None:
        self.items: List[Tuple[str, str, str]] = []

    def ok(self, module: str, msg: str) -> None:
        self.items.append(("OK", module, msg))
        print(f"  [OK]   {module}: {msg}")

    def warn(self, module: str, msg: str) -> None:
        self.items.append(("WARN", module, msg))
        print(f"  [WARN] {module}: {msg}")

    def skip(self, module: str, msg: str) -> None:
        self.items.append(("SKIP", module, msg))
        print(f"  [SKIP] {module}: {msg}")

    def summary(self) -> None:
        print("\n" + "=" * 68)
        print(" Winlator Frost 25W Patch Summary")
        print("=" * 68)
        ok_cnt = sum(1 for s, _, _ in self.items if s == "OK")
        warn_cnt = sum(1 for s, _, _ in self.items if s == "WARN")
        skip_cnt = sum(1 for s, _, _ in self.items if s == "SKIP")
        for status, mod, msg in self.items:
            print(f"  [{status:4s}] {mod:<26s} | {msg}")
        print("-" * 68)
        print(f"  Total: {ok_cnt} applied, {skip_cnt} skipped, {warn_cnt} warnings")
        print("=" * 68 + "\n")


def emit(report: PatchReport, status: str, module: str, msg: str) -> None:
    if status == "OK":
        report.ok(module, msg)
    elif status == "WARN":
        report.warn(module, msg)
    else:
        report.skip(module, msg)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_text(path: Path, content: str) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)


def find_single(root: Path, filename: str) -> Optional[Path]:
    matches = sorted(root.rglob(filename))
    return matches[0] if matches else None


def braces_balanced(java_src: str) -> bool:
    """检查 Java 源码大括号是否平衡（逐字符扫描，跳过注释与字符串/字符字面量）。

    比手册里简单的正则清洗更稳：上游 XServerDisplayActivity 含大量字符串与换行，
    逐字符状态机不会把字符串里的 "//" 或大括号误当成代码。
    """
    depth = 0
    i = 0
    n = len(java_src)
    state = "code"
    while i < n:
        ch = java_src[i]
        nxt = java_src[i + 1] if i + 1 < n else ""
        if state == "code":
            if ch == "/" and nxt == "/":
                state = "line_comment"
                i += 2
                continue
            if ch == "/" and nxt == "*":
                state = "block_comment"
                i += 2
                continue
            if ch == '"':
                state = "string"
                i += 1
                continue
            if ch == "'":
                state = "char"
                i += 1
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth < 0:
                    return False
            i += 1
        elif state == "line_comment":
            if ch == "\n":
                state = "code"
            i += 1
        elif state == "block_comment":
            if ch == "*" and nxt == "/":
                state = "code"
                i += 2
                continue
            i += 1
        elif state == "string":
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                state = "code"
            i += 1
        else:  # char
            if ch == "\\":
                i += 2
                continue
            if ch == "'":
                state = "code"
            i += 1
    return depth == 0


# ---------------------------------------------------------------------------
# 1. Gradle 构建与包名/资源优化补丁（app/build.gradle）
# ---------------------------------------------------------------------------
def patch_app_gradle(
    repo_root: Path, package_id: str, version_suffix: str, report: PatchReport
) -> None:
    gradle_path = repo_root / "app" / "build.gradle"
    if not gradle_path.exists():
        report.warn("Gradle", "未找到 app/build.gradle，跳过 Gradle 补丁")
        return

    src = read_text(gradle_path)
    original = src

    # 1.1 applicationId（仅替换 applicationId，namespace 保持 com.winlator 不动）
    app_id_match = re.search(r"(applicationId\s+)(['\"])([^'\"]+)\2", src)
    if app_id_match is None:
        report.warn("Gradle.applicationId", "未找到 applicationId 字段，保持原值")
    elif app_id_match.group(3) == package_id:
        report.skip("Gradle.applicationId", f"applicationId 已是 {package_id}")
    else:
        src = (
            src[: app_id_match.start(3)]
            + package_id
            + src[app_id_match.end(3) :]
        )
        report.ok(
            "Gradle.applicationId",
            f"已设置 applicationId={package_id}（namespace 'com.winlator' 保持不变）",
        )

    # 1.2 versionName 追加版本后缀（幂等：已含 -suffix 则跳过）
    version_match = re.search(r"(versionName\s+)(['\"])([^'\"]+)\2", src)
    if not version_suffix:
        report.skip("Gradle.versionName", "未指定 --version-suffix，保持原版本号")
    elif version_match is None:
        report.warn("Gradle.versionName", "未找到 versionName 字段，跳过版本后缀")
    elif version_match.group(3).endswith("-" + version_suffix):
        report.skip(
            "Gradle.versionName",
            f"versionName 已含 -{version_suffix}（当前 {version_match.group(3)}）",
        )
    else:
        new_version = version_match.group(3) + "-" + version_suffix
        src = (
            src[: version_match.start(3)]
            + new_version
            + src[version_match.end(3) :]
        )
        report.ok("Gradle.versionName", f"已追加版本标识：{new_version}")

    # 1.3 缺 release 构建块时注入，并锁定 arm64-v8a（上游 buildTypes 只有 debug）
    if re.search(r"\brelease\s*\{", src):
        report.skip("Gradle.releaseBuild", "已存在 release 构建块，跳过注入")
    else:
        build_types_match = re.search(r"(buildTypes\s*\{)", src)
        if build_types_match is None:
            report.warn("Gradle.releaseBuild", "未找到 buildTypes 块，跳过 release 注入")
        else:
            release_block = (
                "\n        release {\n"
                "            minifyEnabled false\n"
                "            ndk {\n"
                "                abiFilters 'arm64-v8a'\n"
                "            }\n"
                "        }\n"
            )
            src = (
                src[: build_types_match.end(1)]
                + release_block
                + src[build_types_match.end(1) :]
            )
            report.ok(
                "Gradle.releaseBuild",
                "已注入 release 构建块（minifyEnabled false + ndk abiFilters 'arm64-v8a'）",
            )

    # 1.4 大型镜像资产免二次压缩（AGP 8.4.2 的 androidResources.noCompress(String...) 有效）
    if "noCompress" in src:
        report.skip("Gradle.noCompress", "已配置 noCompress，跳过")
    else:
        android_match = re.search(r"android\s*\{", src)
        if android_match is None:
            report.warn("Gradle.noCompress", "未找到 android {} 块，跳过 noCompress 注入")
        else:
            no_compress_block = (
                "\n    androidResources {\n"
                "        noCompress 'txz', 'tzst', 'xz', 'zst', 'tar'\n"
                "    }\n"
            )
            src = (
                src[: android_match.end()]
                + no_compress_block
                + src[android_match.end() :]
            )
            report.ok(
                "Gradle.noCompress",
                "已对 .txz/.tzst/.xz/.zst/.tar 镜像启用 androidResources.noCompress",
            )

    # 1.5 lint：上游已有 lintOptions { checkReleaseBuilds false }，存在则跳过
    if "lintOptions" in src or re.search(r"\blint\s*\{", src):
        report.skip("Gradle.lint", "已存在 lintOptions/lint 配置，保持上游原样")
    else:
        android_match = re.search(r"android\s*\{", src)
        if android_match is None:
            report.warn("Gradle.lint", "未找到 android {} 块，跳过 lint 注入")
        else:
            lint_block = (
                "\n    lint {\n"
                "        abortOnError false\n"
                "        checkReleaseBuilds false\n"
                "    }\n"
            )
            src = (
                src[: android_match.end()] + lint_block + src[android_match.end() :]
            )
            report.ok("Gradle.lint", "已注入 lint { abortOnError false; checkReleaseBuilds false }")

    if src != original:
        write_text(gradle_path, src)


# ---------------------------------------------------------------------------
# 1b. Gradle 全局 JVM 参数（gradle.properties）
# ---------------------------------------------------------------------------
def patch_gradle_properties(repo_root: Path, report: PatchReport) -> None:
    properties_path = repo_root / "gradle.properties"
    if not properties_path.exists():
        report.skip("GradleProperties", "未找到 gradle.properties，跳过 jvmargs 配置")
        return

    src = read_text(properties_path)
    if "org.gradle.jvmargs" in src:
        report.skip("GradleProperties.jvmargs", "已存在 org.gradle.jvmargs，保持原值")
        return

    new_src = src
    if new_src and not new_src.endswith("\n"):
        new_src += "\n"
    new_src += GRADLE_PROPERTIES_JVMARGS + "\n"
    write_text(properties_path, new_src)
    report.ok("GradleProperties.jvmargs", f"已追加 {GRADLE_PROPERTIES_JVMARGS}（其他内容未改动）")


# ---------------------------------------------------------------------------
# 2. AndroidManifest.xml 与 FileProvider 全链路同步
# ---------------------------------------------------------------------------
def patch_manifest_and_providers(repo_root: Path, package_id: str, report: PatchReport) -> None:
    manifest_path = repo_root / "app" / "src" / "main" / "AndroidManifest.xml"
    if not manifest_path.exists():
        report.warn("Manifest", "未找到 AndroidManifest.xml，跳过 Manifest 补丁")
        return

    src = read_text(manifest_path)
    original = src

    # 2.1 FileProvider authorities -> applicationId 动态占位符（上游实际是大写 F）
    if re.search(r'android:authorities="com\.winlator', src):
        src, n_auth = re.subn(
            r'android:authorities="com\.winlator([^"]*)"',
            lambda m: 'android:authorities="' + APPLICATION_ID_PLACEHOLDER + m.group(1) + '"',
            src,
        )
        report.ok(
            "Manifest.Authorities",
            f"已将 {n_auth} 处 FileProvider authorities 改为 "
            + APPLICATION_ID_PLACEHOLDER
            + f" 动态占位符（运行时解析为 {package_id}.FileProvider）",
        )
    elif APPLICATION_ID_PLACEHOLDER in src:
        report.skip(
            "Manifest.Authorities",
            "FileProvider authorities 已使用 applicationId 动态占位符",
        )
    else:
        report.warn("Manifest.Authorities", "未找到 FileProvider authorities 声明，跳过")

    # 2.2 大堆内存 / 硬件加速 / 原生库解压（存在则不重复加）
    app_match = re.search(r"<application\b[^>]*>", src, flags=re.DOTALL)
    if app_match is None:
        report.warn("Manifest.PerfFlags", "未匹配到 <application> 标签，跳过性能属性注入")
    else:
        app_tag = app_match.group(0)
        wanted_attrs = (
            ("android:largeHeap", "true"),
            ("android:hardwareAccelerated", "true"),
            ("android:extractNativeLibs", "true"),
        )
        missing = [f'{name}="{value}"' for name, value in wanted_attrs if name + "=" not in app_tag]
        if missing:
            insertion = "\n" + "\n".join("        " + attr for attr in missing)
            new_tag = app_tag.replace("<application", "<application" + insertion, 1)
            src = src.replace(app_tag, new_tag, 1)
            report.ok(
                "Manifest.PerfFlags",
                "已补充 " + "、".join(attr.split("=")[0] for attr in missing) + "（已存在的属性不重复添加）",
            )
        else:
            report.skip("Manifest.PerfFlags", "largeHeap / hardwareAccelerated / extractNativeLibs 均已存在")

    if src != original:
        write_text(manifest_path, src)

    # 2.3 Java 侧 FileProvider Authority 同步（大写 F 与小写两种形态都覆盖）
    java_root = repo_root / "app" / "src" / "main" / "java"
    patched_java: List[str] = []
    total_replacements = 0
    if java_root.exists():
        for java_file in sorted(java_root.rglob("*.java")):
            jtxt = read_text(java_file)
            new_jtxt = jtxt
            replaced_here = 0
            for old_authority in ('"com.winlator.FileProvider"', '"com.winlator.fileprovider"'):
                occurrences = new_jtxt.count(old_authority)
                if occurrences:
                    new_jtxt = new_jtxt.replace(old_authority, f'"{package_id}.FileProvider"')
                    replaced_here += occurrences
            if new_jtxt != jtxt:
                write_text(java_file, new_jtxt)
                patched_java.append(java_file.name)
                total_replacements += replaced_here

    if patched_java:
        report.ok(
            "Java.FileProvider",
            f"已同步 {total_replacements} 处硬编码 Authority -> {package_id}.FileProvider"
            f"（文件：{', '.join(sorted(patched_java))}）",
        )
    else:
        report.skip(
            "Java.FileProvider",
            '未发现旧的 "com.winlator.FileProvider"/"com.winlator.fileprovider"（已同步或上游已改）',
        )


# ---------------------------------------------------------------------------
# 3. 品牌名称与寒冰赛博暗黑主题（strings.xml + colors.xml / styles.xml）
# ---------------------------------------------------------------------------
def patch_branding_and_theme(repo_root: Path, app_name: str, report: PatchReport) -> None:
    res_dir = repo_root / "app" / "src" / "main" / "res"
    if not res_dir.exists():
        report.warn("Resources", "未找到 res 目录，跳过品牌与主题补丁")
        return

    # 3.1 全语言 strings.xml 的 app_name
    escaped_name = xml_escape(app_name)
    app_name_pattern = re.compile(r'(<string\s+name="app_name"[^>]*>)[^<]*(</string>)')
    found_langs = 0
    updated_langs = 0
    for sfile in sorted(res_dir.glob("values*/strings.xml")):
        txt = read_text(sfile)
        new_txt, n = app_name_pattern.subn(
            lambda m: m.group(1) + escaped_name + m.group(2), txt
        )
        if not n:
            continue
        found_langs += 1
        if new_txt != txt:
            write_text(sfile, new_txt)
            updated_langs += 1
    if updated_langs:
        report.ok(
            "Branding.AppName",
            f"已在 {updated_langs} 个语言包中更新 app_name 为 '{app_name}'",
        )
    elif found_langs:
        report.skip("Branding.AppName", f"{found_langs} 个语言包的 app_name 已是 '{app_name}'")
    else:
        report.warn("Branding.AppName", '未找到 <string name="app_name"> 条目，跳过应用名修改')

    # 3.2 主题：优先 colors.xml 原逻辑；上游无 colors.xml 时改 styles.xml 的 AppThemeDark
    colors_files = sorted(res_dir.glob("values*/colors.xml"))
    if colors_files:
        total_colors = 0
        written_colors_files = 0
        for cfile in colors_files:
            txt = read_text(cfile)
            new_txt = txt
            replaced_here = 0
            for key, hex_val in FROST_COLORS_PALETTE.items():
                pattern = re.compile(rf'(<color\s+name="{key}"\s*>)[^<]*(</color>)')
                new_txt, n = pattern.subn(
                    lambda m, v=hex_val: m.group(1) + v + m.group(2), new_txt
                )
                replaced_here += n
            if new_txt != txt:
                write_text(cfile, new_txt)
                written_colors_files += 1
            total_colors += replaced_here
        if written_colors_files:
            report.ok(
                "Theme.FrostCyber",
                f"已通过 colors.xml 应用寒冰赛博配色（替换 {total_colors} 处主题色值）",
            )
        else:
            report.skip("Theme.FrostCyber", "colors.xml 中未匹配到可替换颜色键，保留原版主题")
        return

    styles_path = res_dir / "values" / "styles.xml"
    if not styles_path.exists():
        report.warn("Theme.FrostCyber", "未找到 colors.xml 与 values/styles.xml，跳过主题补丁")
        return

    txt = read_text(styles_path)
    block_match = re.search(
        r'<style\s+name="AppThemeDark"[^>]*>.*?</style>', txt, flags=re.DOTALL
    )
    if block_match is None:
        report.warn("Theme.FrostCyber", "未找到 AppThemeDark 样式块，跳过主题补丁")
        return

    block = block_match.group(0)
    new_block = block
    replaced_items = 0
    for key, value in FROST_STYLE_ITEMS:
        pattern = re.compile(rf'(<item\s+name="{key}"\s*>)[^<]*(</item>)')
        new_block, n = pattern.subn(
            lambda m, v=value: m.group(1) + v + m.group(2), new_block
        )
        replaced_items += n

    if new_block != block:
        write_text(
            styles_path,
            txt[: block_match.start()] + new_block + txt[block_match.end() :],
        )
        report.ok(
            "Theme.FrostCyber",
            "未发现 colors.xml，已就地修改 values/styles.xml 的 AppThemeDark"
            f"（替换 {replaced_items} 个颜色项：#00E5FF/#00B8D4/#080D18/#0B0F19/#0F172A；AppThemeLight 未改动）",
        )
    else:
        report.skip("Theme.FrostCyber", "AppThemeDark 已为寒冰赛博配色，无需修改")


# ---------------------------------------------------------------------------
# 4. 高分辨率选项扩展（values/arrays.xml）
# ---------------------------------------------------------------------------
def patch_arrays_xml(repo_root: Path, report: PatchReport) -> None:
    arrays_path = repo_root / "app" / "src" / "main" / "res" / "values" / "arrays.xml"
    if not arrays_path.exists():
        report.skip("Arrays", "未找到 values/arrays.xml，跳过高分屏/显存扩展")
        return

    try:
        tree = ET.parse(arrays_path)
        root = tree.getroot()
    except ET.ParseError as exc:
        report.warn("Arrays", f"arrays.xml 解析失败，已跳过: {exc}")
        return

    added_res = 0
    added_vram = 0
    screen_array_found = False
    video_array_found = False
    video_builtin = False

    for arr in root.findall("string-array"):
        name = arr.attrib.get("name", "")
        items = [el.text.strip() for el in arr.findall("item") if el.text and el.text.strip()]

        if "screen_size" in name and "entries" in name:
            screen_array_found = True
            for spec in FROST_RESOLUTIONS:
                prefix = spec.split(" ")[0]
                if any(item.startswith(prefix) for item in items):
                    continue
                item_el = ET.SubElement(arr, "item")
                item_el.text = spec
                items.append(spec)
                added_res += 1

        if "video_memory_size" in name and "entries" in name:
            video_array_found = True
            has_6gb = any(re.match(r"^6\s*GB$", item, re.IGNORECASE) for item in items)
            has_8gb = any(re.match(r"^8\s*GB$", item, re.IGNORECASE) for item in items)
            if has_6gb and has_8gb:
                video_builtin = True
            else:
                for label, pattern in (("6 GB", r"^6\s*GB$"), ("8 GB", r"^8\s*GB$")):
                    if not any(re.match(pattern, item, re.IGNORECASE) for item in items):
                        item_el = ET.SubElement(arr, "item")
                        item_el.text = label
                        items.append(label)
                        added_vram += 1

    if not screen_array_found:
        report.warn("Arrays.Resolution", "未找到 screen_size_entries，跳过分辨率扩展")
    elif added_res:
        report.ok(
            "Arrays.Resolution",
            "新增 " + str(added_res) + " 个高刷分辨率（1600x720 (20:9) / 2400x1080 (20:9)）；"
            "1440x900 与 1920x1080 上游已存在，不重复添加",
        )
    else:
        report.skip("Arrays.Resolution", "1600x720/2400x1080 已存在，无需重复添加")

    if not video_array_found:
        report.warn("Arrays.VideoMemory", "未找到 video_memory_size_entries，跳过显存扩展")
    elif video_builtin:
        report.skip(
            "Arrays.VideoMemory",
            "上游界面已内置 6 GB / 8 GB 显存选项，不重复添加 6144 MB / 8192 MB",
        )
    elif added_vram:
        report.ok(
            "Arrays.VideoMemory",
            f"新增 {added_vram} 个显存档位（6 GB / 8 GB，未使用 6144 MB/8192 MB 形式）",
        )
    else:
        report.skip("Arrays.VideoMemory", "6 GB / 8 GB 显存选项已存在，无需修改")

    if added_res or added_vram:
        if hasattr(ET, "indent"):
            ET.indent(tree, space="    ")
        tree.write(arrays_path, encoding="utf-8", xml_declaration=True)


# ---------------------------------------------------------------------------
# 5. 高端 RTX 显卡 PCI ID 注入（assets/gpu_cards.json）
# ---------------------------------------------------------------------------
def patch_gpu_cards(repo_root: Path, report: PatchReport) -> None:
    assets_dir = repo_root / "app" / "src" / "main" / "assets"
    gpu_path = find_single(assets_dir, "gpu_cards.json")
    if gpu_path is None:
        report.skip("Assets.GPUCards", "未找到 assets/gpu_cards.json，跳过显卡 ID 注入")
        return

    try:
        cards = json.loads(read_text(gpu_path))
    except json.JSONDecodeError as exc:
        report.warn("Assets.GPUCards", f"JSON 解析失败，已跳过: {exc}")
        return

    if not isinstance(cards, list) or not cards or not isinstance(cards[0], dict):
        report.skip("Assets.GPUCards", "gpu_cards.json 结构非预期（非对象数组），跳过")
        return

    sample_keys = list(cards[0].keys())
    k_name = next((k for k in sample_keys if "name" in k.lower()), None)
    k_dev = next((k for k in sample_keys if "device" in k.lower()), None)
    k_ven = next((k for k in sample_keys if "vendor" in k.lower()), None)
    if not k_name or not k_dev or not k_ven:
        report.warn("Assets.GPUCards", "gpu_cards.json 字段名非预期，安全跳过")
        return

    existing_names = {
        str(card.get(k_name, "")).strip().lower()
        for card in cards
        if isinstance(card, dict)
    }
    rtx_cards = [
        # 0x2684 / 0x10DE
        {k_name: "NVIDIA GeForce RTX 4090", k_dev: 9860, k_ven: 4318},
        # 0x2704 / 0x10DE
        {k_name: "NVIDIA GeForce RTX 4080", k_dev: 9988, k_ven: 4318},
        # 0x2786 / 0x10DE
        {k_name: "NVIDIA GeForce RTX 4070", k_dev: 10118, k_ven: 4318},
    ]

    added = 0
    for card in rtx_cards:
        if card[k_name].lower() not in existing_names:
            cards.append(card)
            existing_names.add(card[k_name].lower())
            added += 1

    if added:
        write_text(gpu_path, json.dumps(cards, indent=4, ensure_ascii=False) + "\n")
        report.ok(
            "Assets.GPUCards",
            f"已注入 {added} 款 RTX 40 系显卡伪装 ID（RTX 4090/4080/4070，vendor 4318）",
        )
    else:
        report.skip("Assets.GPUCards", "RTX 40 系显卡伪装条目已存在")


# ---------------------------------------------------------------------------
# 6. 25W 冰封极限 Box64 预设注入（Box64Preset.java + Box64PresetManager.java）
# ---------------------------------------------------------------------------
def patch_box64_frost_preset(repo_root: Path, report: PatchReport) -> None:
    java_root = repo_root / "app" / "src" / "main" / "java"
    preset_file = find_single(java_root, "Box64Preset.java")
    mgr_file = find_single(java_root, "Box64PresetManager.java")
    if preset_file is None or mgr_file is None:
        report.skip("Box64.Frost25W", "未找到 Box64Preset.java / Box64PresetManager.java，跳过")
        return

    preset_src = read_text(preset_file)
    mgr_src = read_text(mgr_file)
    original_preset = preset_src
    original_mgr = mgr_src

    # (status, module, message)，等写盘校验通过后再统一打印，保证回滚时日志不误导
    actions: List[Tuple[str, str, str]] = []

    # 6.1 Box64Preset.java 常量
    if re.search(r"\bFROST_25W\b", preset_src):
        actions.append(("SKIP", "Box64.PresetConst", "Box64Preset.FROST_25W 常量已存在"))
    else:
        lines = preset_src.splitlines(keepends=True)
        inserted = False
        for idx, line in enumerate(lines):
            if re.search(r"public\s+static\s+final\s+String\s+PERFORMANCE\s*=", line):
                indent = line[: len(line) - len(line.lstrip())]
                lines.insert(
                    idx + 1,
                    f'{indent}public static final String FROST_25W = "FROST_25W";\n',
                )
                inserted = True
                break
        if inserted:
            preset_src = "".join(lines)
            actions.append(
                (
                    "OK",
                    "Box64.PresetConst",
                    '已注入 public static final String FROST_25W = "FROST_25W";',
                )
            )
        else:
            actions.append(
                (
                    "WARN",
                    "Box64.PresetConst",
                    "未匹配到 PERFORMANCE 常量声明，安全跳过常量注入",
                )
            )

    # 6.2 getEnvVars 环境变量分支（上游风格：envVars.put("BOX64_DYNAREC_...", "...")）
    if re.search(r"id\.equals\(\s*Box64Preset\.FROST_25W\s*\)", mgr_src):
        actions.append(("SKIP", "Box64.EnvVars", "getEnvVars 中的 FROST_25W 分支已存在"))
    else:
        perf_match = re.search(
            r"if\s*\(\s*id\.equals\(\s*Box64Preset\.PERFORMANCE\s*\)\s*\)\s*\{",
            mgr_src,
        )
        if perf_match is None:
            actions.append(
                (
                    "WARN",
                    "Box64.EnvVars",
                    "未匹配到 PERFORMANCE 环境变量分支（方法结构可能已变），安全跳过",
                )
            )
        else:
            uses_prefix = bool(
                re.search(r'envVars\.put\(\s*prefix\s*\+\s*"_DYNAREC_', mgr_src)
            )
            key_prefix = 'prefix + "' if uses_prefix else '"BOX64'
            env_pairs = (
                ("_DYNAREC_SAFEFLAGS", "0"),
                ("_DYNAREC_FASTNAN", "1"),
                ("_DYNAREC_FASTROUND", "1"),
                ("_DYNAREC_X87DOUBLE", "0"),
                ("_DYNAREC_BIGBLOCK", "3"),
                ("_DYNAREC_STRONGMEM", "0"),
                ("_DYNAREC_FORWARD", "512"),
                ("_DYNAREC_CALLRET", "1"),
                ("_DYNAREC_WAIT", "1"),
                ("_AVX", "1"),
                ("_MAXCPU", "8"),
            )
            body = "\n".join(
                f'            envVars.put({key_prefix}{suffix}", "{value}");'
                for suffix, value in env_pairs
            )
            frost_branch = (
                "if (id.equals(Box64Preset.FROST_25W)) {\n"
                + body
                + "\n        }\n        else "
            )
            mgr_src = (
                mgr_src[: perf_match.start()]
                + frost_branch
                + mgr_src[perf_match.start() :]
            )
            style_desc = "prefix 拼接" if uses_prefix else '字面量 "BOX64_*"'
            actions.append(
                (
                    "OK",
                    "Box64.EnvVars",
                    "已注入 FROST_25W 环境变量分支（"
                    + style_desc
                    + "；SAFEFLAGS=0/BIGBLOCK=3/STRONGMEM=0/FORWARD=512/"
                    "CALLRET=1/AVX=1/MAXCPU=8）",
                )
            )

    # 6.3 getPresets 追加 FROST 预设（行级定位，规避 context.getString(...) 嵌套括号）
    if FROST_PRESET_NAME in mgr_src:
        actions.append(
            ("SKIP", "Box64.PresetsList", f'getPresets 中已追加 "{FROST_PRESET_NAME}"')
        )
    else:
        lines = mgr_src.splitlines(keepends=True)
        inserted = False
        for idx, line in enumerate(lines):
            if "presets.add" in line and "Box64Preset.PERFORMANCE" in line:
                indent = line[: len(line) - len(line.lstrip())]
                lines.insert(
                    idx + 1,
                    f'{indent}presets.add(new Box64Preset(Box64Preset.FROST_25W, "{FROST_PRESET_NAME}"));\n',
                )
                inserted = True
                break
        if inserted:
            mgr_src = "".join(lines)
            actions.append(
                (
                    "OK",
                    "Box64.PresetsList",
                    "已在 getPresets 中追加 "
                    f'presets.add(new Box64Preset(Box64Preset.FROST_25W, "{FROST_PRESET_NAME}"));',
                )
            )
        else:
            actions.append(
                (
                    "WARN",
                    "Box64.PresetsList",
                    "未匹配到 getPresets 中的 PERFORMANCE 列表项，安全跳过",
                )
            )

    if preset_src == original_preset and mgr_src == original_mgr:
        for status, module, msg in actions:
            emit(report, status, module, msg)
        return

    if not (braces_balanced(preset_src) and braces_balanced(mgr_src)):
        report.warn(
            "Box64.Frost25W",
            "大括号平衡校验未通过，已回滚全部 Box64 修改（两个 Java 文件均未写盘）",
        )
        return

    write_text(preset_file, preset_src)
    write_text(mgr_file, mgr_src)
    for status, module, msg in actions:
        emit(report, status, module, msg)


# ---------------------------------------------------------------------------
# 7. 原生高刷锁定 + 全面屏挖孔适配（XServerDisplayActivity.java）
# ---------------------------------------------------------------------------
FROST_DISPLAY_HELPER = """
    private void applyFrost25WDisplayOptimization() {
        try {
            android.view.Window window = getWindow();
            if (window == null) return;
            window.addFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
            android.view.WindowManager.LayoutParams lp = window.getAttributes();
            if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.P) {
                lp.layoutInDisplayCutoutMode =
                        android.view.WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES;
            }
            if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.M) {
                android.view.Display display = null;
                if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.R) {
                    display = getDisplay();
                }
                if (display == null) {
                    display = getWindowManager().getDefaultDisplay();
                }
                if (display != null) {
                    android.view.Display.Mode current = display.getMode();
                    android.view.Display.Mode bestMode = current;
                    float maxRefresh = current != null ? current.getRefreshRate() : 60.0f;
                    for (android.view.Display.Mode mode : display.getSupportedModes()) {
                        boolean sameRes = (current == null) ||
                                (mode.getPhysicalWidth() == current.getPhysicalWidth()
                                        && mode.getPhysicalHeight() == current.getPhysicalHeight());
                        if (sameRes && mode.getRefreshRate() > maxRefresh) {
                            maxRefresh = mode.getRefreshRate();
                            bestMode = mode;
                        }
                    }
                    if (bestMode != null) {
                        lp.preferredDisplayModeId = bestMode.getModeId();
                        lp.preferredRefreshRate = bestMode.getRefreshRate();
                    }
                }
            }
            window.setAttributes(lp);
        } catch (Throwable ignored) {
        }
    }
"""


def patch_display_high_refresh(repo_root: Path, report: PatchReport) -> None:
    java_root = repo_root / "app" / "src" / "main" / "java"
    act_file = find_single(java_root, "XServerDisplayActivity.java")
    if act_file is None:
        report.skip("Display.HighRefresh", "未找到 XServerDisplayActivity.java，跳过")
        return

    src = read_text(act_file)
    if DISPLAY_METHOD_MARKER in src:
        report.skip("Display.HighRefresh", "高刷优化方法已注入，跳过")
        return

    # 优先在 onCreate 方法体内定位 setContentView 调用（真实上游仅此一处）
    on_create_match = re.search(r"public\s+void\s+onCreate\s*\([^)]*\)\s*\{", src)
    search_start = on_create_match.end() if on_create_match else 0
    scv_match = re.search(r"(setContentView\s*\([^;]+\)\s*;)", src[search_start:])
    if scv_match is None:
        report.warn("Display.HighRefresh", "未匹配到 setContentView 调用，安全跳过")
        return
    insert_pos = search_start + scv_match.end(1)

    new_src = src[:insert_pos] + "\n        applyFrost25WDisplayOptimization();" + src[insert_pos:]

    last_brace = new_src.rfind("}")
    if last_brace == -1:
        report.warn("Display.HighRefresh", "Java 文件结构异常（找不到类结束大括号），安全跳过")
        return
    new_src = new_src[:last_brace] + FROST_DISPLAY_HELPER + "\n}\n"

    if not braces_balanced(new_src):
        report.warn("Display.HighRefresh", "大括号平衡校验未通过，已回滚修改（未写盘）")
        return

    write_text(act_file, new_src)
    report.ok(
        "Display.HighRefresh",
        "已注入原生高刷模式锁定（120Hz/144Hz）+ 挖孔全屏 + 屏幕常亮，并插入 onCreate 调用",
    )


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Apply Winlator Frost 25W custom patches (corrected for upstream v11.2)"
    )
    parser.add_argument("--root", default=".", help="Path to winlator-app repository root")
    parser.add_argument("--app-name", default="Winlator Frost 25W", help="Custom application label")
    parser.add_argument(
        "--package-id", default="com.winlator.frost25w", help="Custom Android applicationId"
    )
    parser.add_argument(
        "--version-suffix", default="frost25w", help="Version suffix string"
    )
    args = parser.parse_args()

    repo_root = Path(args.root).resolve()
    print(f"\n>>> Starting Winlator Frost 25W Patcher v{PATCHER_VERSION} on: {repo_root}")
    print(f"    App Name   : {args.app_name}")
    print(f"    Package ID : {args.package_id}")
    print(f"    Version    : suffix={args.version_suffix}\n")

    report = PatchReport()

    modules = [
        (
            "Gradle",
            lambda: patch_app_gradle(
                repo_root, args.package_id, args.version_suffix, report
            ),
        ),
        ("GradleProperties", lambda: patch_gradle_properties(repo_root, report)),
        (
            "Manifest",
            lambda: patch_manifest_and_providers(repo_root, args.package_id, report),
        ),
        (
            "BrandingTheme",
            lambda: patch_branding_and_theme(repo_root, args.app_name, report),
        ),
        ("Arrays", lambda: patch_arrays_xml(repo_root, report)),
        ("GPUCards", lambda: patch_gpu_cards(repo_root, report)),
        ("Box64", lambda: patch_box64_frost_preset(repo_root, report)),
        ("Display", lambda: patch_display_high_refresh(repo_root, report)),
    ]

    for module_name, module_func in modules:
        try:
            module_func()
        except Exception as exc:  # 任何单模块异常都不允许中断整包编译
            report.warn(
                module_name,
                f"未预期异常，已安全跳过且不影响退出码: {type(exc).__name__}: {exc}",
            )

    report.summary()
    return 0


if __name__ == "__main__":
    sys.exit(main())
