"""把 android/ 目录编译成可安装的 APK —— 不依赖 Gradle / Android Studio。

依赖（可用环境变量覆盖）：
  JAVA_HOME      JDK 17         默认 <workspace>/.toolchain/jdk
  ANDROID_HOME   Android SDK    默认 <workspace>/.toolchain/sdk
                需含 platforms/android-34 与 build-tools/34.0.0

用法：python android/build_apk.py
产物：radio-app/dist-android/CloudRadio.apk
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

AND = Path(__file__).resolve().parent
APP = AND.parent
WS = APP.parent
TC = WS / ".toolchain"

JAVA_HOME = Path(os.environ.get("JAVA_HOME") or TC / "jdk")
SDK = Path(os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or TC / "sdk")
BT_VER = os.environ.get("BUILD_TOOLS_VERSION", "34.0.0")
PLATFORM = os.environ.get("ANDROID_PLATFORM", "android-34")

WIN = os.name == "nt"
EXE = ".exe" if WIN else ""
BT = SDK / "build-tools" / BT_VER
ANDROID_JAR = SDK / "platforms" / PLATFORM / "android.jar"
JAVA = JAVA_HOME / "bin" / f"java{EXE}"
JAVAC = JAVA_HOME / "bin" / f"javac{EXE}"
KEYTOOL = JAVA_HOME / "bin" / f"keytool{EXE}"

OUT = AND / "out"
DIST = APP / "dist-android"
APK_NAME = "CloudRadio.apk"

KS_DIR = AND / "keystore"
KS_FILE = KS_DIR / "cloudradio.jks"
KS_ALIAS = "cloudradio"
KS_PASS = "cloudradio2026"

PKG = "com.cloudradio.app"
APP_LABEL = "云收音机"
VERSION_CODE = "1"
VERSION_NAME = "1.0.0"


def die(msg: str) -> None:
    print(f"\n[失败] {msg}")
    sys.exit(1)


def run(cmd: list, desc: str, cwd: Path | None = None, quiet: bool = False) -> str:
    print(f"[{desc}] {' '.join(str(c) for c in cmd)[:160]}")
    proc = subprocess.run(
        [str(c) for c in cmd],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        print(out[-4000:])
        die(f"{desc} 失败（退出码 {proc.returncode}）")
    if not quiet and out.strip():
        tail = "\n".join(out.strip().splitlines()[-6:])
        print(tail)
    return out


def check_env() -> None:
    missing = []
    if not JAVA.exists():
        missing.append(f"JDK（未找到 {JAVA}）")
    if not ANDROID_JAR.exists():
        missing.append(f"Android 平台（未找到 {ANDROID_JAR}）")
    for tool in (f"aapt2{EXE}", f"zipalign{EXE}"):
        if not (BT / tool).exists():
            missing.append(f"build-tools 组件 {tool}")
    if missing:
        print("构建环境不完整：")
        for m in missing:
            print("  -", m)
        print("\n安装 SDK 组件：")
        print(f'  "{SDK / "cmdline-tools" / "latest" / "bin" / "sdkmanager.bat"}" '
              f'"platforms;{PLATFORM}" "build-tools;{BT_VER}"')
        sys.exit(1)


def ensure_keystore() -> None:
    if KS_FILE.exists():
        return
    KS_DIR.mkdir(parents=True, exist_ok=True)
    run([
        KEYTOOL, "-genkeypair", "-v",
        "-keystore", KS_FILE,
        "-alias", KS_ALIAS,
        "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000",
        "-storepass", KS_PASS, "-keypass", KS_PASS,
        "-dname", "CN=CloudRadio, OU=Personal, O=CloudRadio, L=Beijing, ST=Beijing, C=CN",
    ], "生成签名密钥", quiet=True)
    (KS_DIR / "keystore-info.txt").write_text(
        "别名 alias : %s\n密码 pass  : %s\n有效期    : 10000 天\n\n"
        "这个文件用于给 APK 签名。覆盖安装新版本必须使用同一个密钥，\n"
        "否则需要先卸载旧版。请备份整个 keystore 目录。\n" % (KS_ALIAS, KS_PASS),
        encoding="utf-8",
    )


def main() -> None:
    check_env()
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    DIST.mkdir(parents=True, exist_ok=True)

    res_zip = OUT / "res.zip"
    unsigned = OUT / "app-unsigned.apk"
    gen = OUT / "gen"
    classes = OUT / "classes"
    dex_dir = OUT / "dex"
    aligned = OUT / "app-aligned.apk"
    final = DIST / APK_NAME

    run([BT / f"aapt2{EXE}", "compile", "--dir", AND / "res", "-o", res_zip], "编译资源")

    run([
        BT / f"aapt2{EXE}", "link",
        "-o", unsigned,
        "-I", ANDROID_JAR,
        "--manifest", AND / "AndroidManifest.xml",
        "-R", res_zip,
        "--java", gen,
        "--auto-add-overlay",
        "--min-sdk-version", "26",
        "--target-sdk-version", "34",
        "--version-code", VERSION_CODE,
        "--version-name", VERSION_NAME,
    ], "链接资源与清单")

    sources = [str(p) for p in (AND / "src").rglob("*.java")]
    r_java = [str(p) for p in gen.rglob("R.java")]
    if not sources or not r_java:
        die("未找到 Java 源文件或 R.java")

    run([
        JAVAC, "--release", "11", "-encoding", "UTF-8",
        "-classpath", ANDROID_JAR,
        "-d", classes,
        *sources, *r_java,
    ], f"编译 Java（{len(sources)} 个源文件）")

    class_files = [str(p) for p in classes.rglob("*.class")]
    d8_jar = BT / "lib" / "d8.jar"
    run([
        JAVA, "-cp", d8_jar, "com.android.tools.r8.D8",
        "--release", "--min-api", "26",
        "--lib", ANDROID_JAR,
        "--output", dex_dir,
        *class_files,
    ], f"转换为 dex（{len(class_files)} 个 class）")

    dex_file = dex_dir / "classes.dex"
    if not dex_file.exists():
        die("d8 未生成 classes.dex")

    with_dex = OUT / "app-with-dex.apk"
    shutil.copy2(unsigned, with_dex)
    with zipfile.ZipFile(with_dex, "a", zipfile.ZIP_DEFLATED) as z:
        z.write(dex_file, "classes.dex")
    print(f"[写入 classes.dex] {dex_file.stat().st_size / 1024:.0f} KB")

    run([BT / f"zipalign{EXE}", "-f", "4", with_dex, aligned], "对齐字节")

    ensure_keystore()
    run([
        JAVA, "-jar", BT / "lib" / "apksigner.jar", "sign",
        "--ks", KS_FILE,
        "--ks-key-alias", KS_ALIAS,
        "--ks-pass", f"pass:{KS_PASS}",
        "--key-pass", f"pass:{KS_PASS}",
        "--min-sdk-version", "26",
        "--out", final,
        aligned,
    ], "签名 APK", quiet=True)

    verify = run([
        JAVA, "-jar", BT / "lib" / "apksigner.jar", "verify",
        "--min-sdk-version", "26", "--print-certs", final,
    ], "校验签名")

    badging = run([BT / f"aapt2{EXE}", "dump", "badging", final], "读取 APK 信息")
    label = "?"
    for line in badging.splitlines():
        if line.startswith("application-label"):
            label = line.split(":", 1)[1].strip().strip("'")
        if line.startswith("package:"):
            print("  " + line.strip())
    print(f"  应用名：{label}")
    print(f"\n完成：{final}  ({final.stat().st_size / 1024 / 1024:.1f} MB)")


if __name__ == "__main__":
    main()
