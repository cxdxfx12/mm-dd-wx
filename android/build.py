#!/usr/bin/env python3
"""手工编译手机端采集 App —— 不依赖 Gradle。

为什么不用 Gradle：
  Gradle 构建要联网拉 Android Gradle Plugin 和一堆依赖，而这台机器的网络分区很怪
  （`dl.google.com` 通、`github.com` 网页域名不通）。手工走 aapt2 + javac + d8 +
  zipalign + apksigner 这条链，工具全部来自 build-tools，装好之后**完全离线可编译**，
  改一行 Java 重打个包只要几秒。

代价只有一个：不能用 AndroidX、不能用第三方库。所以这个工程的 Java 源码刻意
只依赖 android.jar 里的系统 API，界面也用系统控件拼。

用法::

    python android/build.py              # 编译并签名，产物在 android/dist/
    python android/build.py --clean      # 先清干净再编译

工具链位置（默认 D:/android-build）可用环境变量 DDWX_ANDROID_HOME 覆盖，
目录结构约定::

    <home>/jdk-*/                       Microsoft OpenJDK 17
    <home>/sdk/build-tools/33.0.2/      aapt2 / d8 / zipalign / apksigner
    <home>/sdk/platforms/android-33/    android.jar
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
ROOT = PROJECT.parent

APP_ID = "com.hbdxm.ddwx"
BUILD_TOOLS = "33.0.2"
PLATFORM = "android-33"

VERSION_CODE = "5"
VERSION_NAME = "0.4.2"

# 自签名用的口令。这是**演示/自用**签名，不是发布凭据 —— 仓库里放口令是可接受的，
# 因为它唯一的作用是让 Android 愿意装这个包，不保护任何机密。
KS_PASS = "ddwxddwx"
KS_ALIAS = "ddwx"
KS_FILE = PROJECT / "debug.keystore"


def die(msg: str) -> "NoReturn":  # type: ignore[valid-type]
    print("构建失败：" + msg, file=sys.stderr)
    raise SystemExit(1)


def find_home() -> Path:
    env = os.environ.get("DDWX_ANDROID_HOME", "").strip()
    home = Path(env) if env else Path("D:/android-build")
    if not home.is_dir():
        die(f"工具链目录不存在：{home}\n"
            "  请先装 JDK 与 Android build-tools，或设置 DDWX_ANDROID_HOME。\n"
            "  参见 android/README.md 的「准备编译环境」一节。")
    return home


def find_jdk(home: Path) -> Path:
    cands = sorted([p for p in home.glob("jdk*") if (p / "bin" / "javac.exe").is_file()],
                   key=lambda p: p.name, reverse=True)
    if not cands:
        die(f"{home} 下没找到 JDK（要含 bin/javac.exe）")
    return cands[0]


def find_sdk(home: Path) -> tuple[Path, Path, Path]:
    sdk = home / "sdk"
    bt = sdk / "build-tools" / BUILD_TOOLS
    plat = sdk / "platforms" / PLATFORM
    android_jar = plat / "android.jar"
    for p, what in ((bt, "build-tools"), (plat, "platforms")):
        if not p.is_dir():
            die(f"缺少 {what}：{p}\n"
                f"  用 sdkmanager 装：sdkmanager \"build-tools;{BUILD_TOOLS}\" "
                f"\"platforms;{PLATFORM}\"")
    if not android_jar.is_file():
        die(f"缺少 android.jar：{android_jar}")
    return sdk, bt, android_jar


class Runner:
    """跑外部命令。统一注入 JAVA_HOME/PATH，并把输出透传给用户。"""

    def __init__(self, jdk: Path) -> None:
        self.jdk = jdk
        self.env = dict(os.environ)
        self.env["JAVA_HOME"] = str(jdk)
        self.env["PATH"] = str(jdk / "bin") + os.pathsep + self.env.get("PATH", "")
        # d8 默认会尝试读 ~/.android；给它一个明确的目录，避免首次运行时的杂音
        self.env.setdefault("ANDROID_SDK_HOME", str(PROJECT / ".android"))

    def run(self, args: list[str], *, quiet: bool = False) -> None:
        pretty = " ".join(args)
        print("  $ " + (pretty if len(pretty) < 160 else pretty[:160] + " …"))
        out = subprocess.run(args, env=self.env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, cwd=str(ROOT))
        text = out.stdout.decode("utf-8", "replace")
        if out.returncode != 0:
            print(text, file=sys.stderr)
            die(f"命令失败（退出码 {out.returncode}）：{args[0]}")
        if not quiet and text.strip():
            for line in text.strip().splitlines()[:20]:
                print("    " + line)

    def capture(self, args: list[str]) -> str:
        """跑命令并**只要输出**（不打到屏幕）。给「回读产物做断言」这类用途。"""
        out = subprocess.run(args, env=self.env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, cwd=str(ROOT))
        return out.stdout.decode("utf-8", "replace")

    def which(self, name: str) -> str:
        for d in (self.jdk / "bin",):
            for ext in (".exe", ".bat", ""):
                p = d / (name + ext)
                if p.is_file():
                    return str(p)
        return name


def java_sources() -> list[Path]:
    src = PROJECT / "src"
    files = sorted(src.rglob("*.java"))
    if not files:
        die(f"{src} 下没有 Java 源码")
    return files


def build_res(r: Runner, bt: Path, build: Path) -> Path:
    res = PROJECT / "res"
    if not res.is_dir():
        die(f"缺少资源目录：{res}")
    res_zip = build / "res.zip"
    r.run([str(bt / "aapt2.exe"), "compile", "--dir", str(res), "-o", str(res_zip)], quiet=True)
    return res_zip


def link_res(r: Runner, bt: Path, android_jar: Path, res_zip: Path, build: Path) -> Path:
    gen = build / "gen"
    gen.mkdir(parents=True, exist_ok=True)
    apk = build / "base.apk"
    # ⚠ 版本号**以 AndroidManifest.xml 为准**。下面这两个参数 aapt2 实际不认
    # （2026-10-20 实测：清单里写了具体值时参数被完全忽略；清单留空时
    # versionCode 直接变空、versionName 变 "0"）。留在这里只是因为无害、
    # 且换 build-tools 后行为可能变回来。
    # 真正防漂移的是 sign() 里的 verify_version()：打完包回读 APK 与这里的
    # 常量比对，对不上就构建失败 —— 光靠「记得改两处」已经漂移过一次。
    #
    # 注意 res.zip 必须作为**位置参数**传，不能用 -R。
    # aapt2 里 -R 是「覆盖层(overlay)资源」的语义：用 -R 时它会去比对「被覆盖的
    # 那份资源」，找不到就报 "resource string/xxx does not override an existing
    # resource"。踩过一次，报错信息完全指不到真正的原因。
    #
    # -0 arsc：resources.arsc 必须**不压缩**。Android 11 起（targetSdk 30+）系统
    # 会直接拒绝安装 arsc 被 deflate 的包，报 "resources.arsc ... must be uncompressed"。
    # zipalign 只能对齐、不能解压，所以必须在 link 阶段就压对。
    r.run([str(bt / "aapt2.exe"), "link",
           "-o", str(apk),
           "-I", str(android_jar),
           "--manifest", str(PROJECT / "AndroidManifest.xml"),
           "--version-code", VERSION_CODE,
           "--version-name", VERSION_NAME,
           str(res_zip),
           "--java", str(gen),
           "-0", "arsc"], quiet=True)
    if not apk.is_file():
        die("aapt2 link 没有产出 APK")
    return apk


def compile_java(r: Runner, android_jar: Path, gen: Path, build: Path) -> Path:
    classes = build / "classes"
    classes.mkdir(parents=True, exist_ok=True)
    r_java = sorted(gen.rglob("R.java"))
    if not r_java:
        die("aapt2 没有生成 R.java —— 资源里可能有拼错的东西")

    args = [r.which("javac"),
            # 中文 Windows 上 javac 默认用 GBK 输出诊断信息，在 UTF-8 终端里全是乱码。
            # 强制成英文，报错才看得懂；只影响诊断文案，不影响产物。
            "-J-Duser.language=en", "-J-Duser.country=US",
            "-encoding", "UTF-8",
            "-source", "8", "-target", "8",
            # 用 android.jar 当引导类路径，避免 JDK 自带的 java.* 混进 dex
            "-bootclasspath", str(android_jar),
            "-nowarn",
            "-d", str(classes)]
    args += [str(p) for p in java_sources()]
    args += [str(p) for p in r_java]
    r.run(args, quiet=True)

    jar = build / "classes.jar"
    r.run([r.which("jar"), "cf", str(jar), "-C", str(classes), "."], quiet=True)
    return jar


def dex(r: Runner, bt: Path, android_jar: Path, jar: Path, build: Path) -> Path:
    out = build / "dex"
    out.mkdir(parents=True, exist_ok=True)
    # 不调 d8.bat。build-tools 里的 d8.bat 是照着 JDK 8 写的，第 109 行用了
    #   -Djava.ext.dirs=...
    # 这个选项在 JDK 9 就被删除，JDK 17 上直接 "Could not create the Java Virtual
    # Machine"。绕开办法就是自己调主类：脚本真正干的事不过是
    #   java -cp <build-tools>/lib/d8.jar com.android.tools.r8.D8 ...
    # （apksigner.bat 用的是 -jar，没这个问题，可以照常调。）
    d8_jar = bt / "lib" / "d8.jar"
    if not d8_jar.is_file():
        die(f"缺少 {d8_jar}")
    r.run([r.which("java"),
           "-Xmx1024m",
           "-cp", str(d8_jar),
           "com.android.tools.r8.D8",
           "--release",
           "--lib", str(android_jar),
           "--min-api", "24",
           "--output", str(out),
           str(jar)], quiet=True)
    dexfile = out / "classes.dex"
    if not dexfile.is_file():
        die("d8 没有产出 classes.dex")
    return dexfile


def merge_dex(base_apk: Path, dexfile: Path, build: Path) -> Path:
    """把 classes.dex 塞进 aapt2 产出的 APK。

    aapt2 link 只负责资源和清单，不管代码；d8 只产 dex，不会打包。
    中间这个拼装步骤按传统是 dx/d8 之外的活儿，APK 本质就是个 zip，所以直接合并。
    """
    out = build / "app-unsigned.apk"
    with zipfile.ZipFile(base_apk, "r") as src, \
            zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            # resources.arsc 保持不压缩，跟 link 阶段的 -0 arsc 对齐
            comp = zipfile.ZIP_STORED if item.filename == "resources.arsc" \
                else zipfile.ZIP_DEFLATED
            dst.writestr(item.filename, data, compress_type=comp)
        dst.write(dexfile, "classes.dex")
    return out


def ensure_keystore(r: Runner) -> Path:
    if KS_FILE.is_file():
        return KS_FILE
    print("· 生成自签名证书（首次编译会做一次）")
    r.run([r.which("keytool"),
           "-genkeypair",
           "-keystore", str(KS_FILE),
           "-alias", KS_ALIAS,
           "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000",
           "-storepass", KS_PASS, "-keypass", KS_PASS,
           "-dname", "CN=ddwx, OU=hbdxm, O=hbdxm, L=Hangzhou, ST=Zhejiang, C=CN"],
          quiet=True)
    if not KS_FILE.is_file():
        die("生成签名证书失败")
    return KS_FILE


def verify_version(r: Runner, bt: Path, apk: Path) -> None:
    """回读 APK 里**真正**的 versionCode / versionName，与常量对不上就构建失败。

    为什么非查不可：文件名是按常量拼的，看起来一切都对；但 aapt2 link 有个坑 ——
    清单里若写了具体的 versionCode/versionName，它会**忽略** --version-code /
    --version-name 两个参数，以清单为准。2026-10-20 就是这么翻的车：
    0.4.0、0.4.1 两轮产物文件名都新，包内却一直是 0.3.0 / 3，
    手机上显示的、PackageManager 读到的、上报给网关的全是旧值。

    只靠「改清单记得同步」不牢 —— 两处漂移正是这么来的。所以留一条断言。
    """
    out = r.capture([str(bt / "aapt2.exe"), "dump", "badging", str(apk)])
    head = next((ln for ln in out.splitlines() if ln.startswith("package:")), "")
    got_code = re.search(r"versionCode='([^']*)'", head)
    got_name = re.search(r"versionName='([^']*)'", head)
    code = got_code.group(1) if got_code else "(读不到)"
    name = got_name.group(1) if got_name else "(读不到)"
    if code != VERSION_CODE or name != VERSION_NAME:
        die(f"版本号没进包：常量是 {VERSION_NAME}/{VERSION_CODE}，"
            f"APK 里却是 {name}/{code}。\n"
            f"  多半是 AndroidManifest.xml 里写死了 versionCode/versionName —— "
            f"aapt2 会以清单为准、忽略 --version-* 参数。\n"
            f"  清单里那两个属性要留成占位（0），改版本只改本文件顶部常量。")
    print(f"  版本校验：{name}（versionCode {code}）")


def sign(r: Runner, bt: Path, unsigned: Path, build: Path) -> Path:
    dist = PROJECT / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    aligned = build / "app-aligned.apk"
    r.run([str(bt / "zipalign.exe"), "-f", "-p", "4", str(unsigned), str(aligned)], quiet=True)

    signed = dist / f"ddwx-collector-{VERSION_NAME}.apk"
    if signed.exists():
        signed.unlink()
    r.run([str(bt / "apksigner.bat"), "sign",
           "--ks", str(KS_FILE),
           "--ks-pass", "pass:" + KS_PASS,
           "--key-pass", "pass:" + KS_PASS,
           "--v1-signing-enabled", "true",
           "--v2-signing-enabled", "true",
           "--out", str(signed),
           str(aligned)], quiet=True)
    r.run([str(bt / "apksigner.bat"), "verify", "--print-certs", str(signed)], quiet=True)
    verify_version(r, bt, signed)
    return signed


def main() -> int:
    ap = argparse.ArgumentParser(description="编译手机端采集 App")
    ap.add_argument("--clean", action="store_true", help="先删除 build 目录")
    ap.add_argument("--keep-build", action="store_true", help="保留 build 中间产物")
    args = ap.parse_args()

    home = find_home()
    jdk = find_jdk(home)
    sdk, bt, android_jar = find_sdk(home)
    build = PROJECT / "build"

    print(f"· 工具链  {home}")
    print(f"· JDK     {jdk.name}")
    print(f"· SDK     {sdk}  (build-tools {BUILD_TOOLS}, {PLATFORM})")

    if args.clean and build.exists():
        shutil.rmtree(build, ignore_errors=True)
    build.mkdir(parents=True, exist_ok=True)

    r = Runner(jdk)
    print("· 编译资源")
    res_zip = build_res(r, bt, build)
    print("· 链接资源与清单")
    base_apk = link_res(r, bt, android_jar, res_zip, build)
    print("· 编译 Java")
    jar = compile_java(r, android_jar, build / "gen", build)
    print("· 生成 dex")
    dexfile = dex(r, bt, android_jar, jar, build)
    print("· 拼装 APK")
    unsigned = merge_dex(base_apk, dexfile, build)
    print("· 签名")
    ensure_keystore(r)
    signed = sign(r, bt, unsigned, build)

    size = signed.stat().st_size / 1024.0
    print()
    print(f"✔ 产物：{signed}")
    print(f"  体积：{size:.1f} KB")
    print("  安装：把 APK 拷到手机（USB 选「传输文件」→ 拖进任意文件夹）后在手机上点开安装。")

    if not args.keep_build:
        # 只留签名产物：build/ 里有几百个中间文件，留着只会干扰后续查看
        shutil.rmtree(build, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
