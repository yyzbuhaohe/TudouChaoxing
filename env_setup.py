# -*- coding: utf-8 -*-
"""土豆刷学习通 · 环境检测与依赖安装

只用标准库，所以「一行依赖都没装」的情况下也能跑起来。

设计原则（重要）：
1. 所有下载都走国内镜像，永不依赖 pypi.org 能不能连上；
2. 镜像一个不通就换下一个，直到装成功，不把选择权丢给用户；
3. Python 本体也提供国内镜像下载（华为云 / npmmirror），官网打不开照样能装。

命令行用法：
    python env_setup.py --check                 # 只看状态，缺什么
    python env_setup.py --install               # 自动选最快的镜像装依赖
    python env_setup.py --install --web         # 连网页版(flask/celery)一起装
    python env_setup.py --install --mirror tsinghua
    python env_setup.py --download-python       # 从国内镜像下 Python 并静默安装
"""
import argparse
import json
import os
import platform
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))

# Windows 控制台默认是 GBK，写 emoji / 生僻符号会直接抛 UnicodeEncodeError 把程序打断。
# 这里统一转成 utf-8 并允许替换，装环境的时候不能因为一个符号崩掉。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 导入名 -> (pip 包名, 中文说明, 是否必需)
MODULES = [
    ("requests", "requests", "HTTP 请求（登录 / 刷课）", True),
    ("pyaes", "pyaes", "超星密码 AES 加密", True),
    ("bs4", "beautifulsoup4", "解析超星返回的 HTML", True),
    ("lxml", "lxml", "HTML 解析引擎（bs4 用）", True),
    ("loguru", "loguru", "日志输出", True),
    ("fontTools", "fonttools", "超星字形反爬解密", True),
    ("openai", "openai", "大模型答题（DeepSeek 兼容接口）", True),
    ("httpx", "httpx", "大模型请求底层", True),
    ("nacl", "pynacl", "云端一键部署（加密 GitHub Secrets）", True),
    ("truststore", "truststore", "认系统证书（装了加速器也能连 GitHub）", True),
    ("flask", "flask", "网页版界面（可选）", False),
    ("celery", "celery", "网页版异步队列（可选）", False),
]

# 国内镜像。顺序即兜底顺序：第一个最快不一定最稳，所以全都不通才报错。
MIRRORS = [
    ("huawei", "华为云", "https://repo.huaweicloud.com/repository/pypi/simple"),
    ("aliyun", "阿里云", "https://mirrors.aliyun.com/pypi/simple/"),
    ("tsinghua", "清华 TUNA", "https://pypi.tuna.tsinghua.edu.cn/simple"),
    ("tencent", "腾讯云", "https://mirrors.cloud.tencent.com/pypi/simple"),
    ("ustc", "中科大", "https://mirrors.ustc.edu.cn/pypi/simple"),
    ("pypi", "官方 PyPI", "https://pypi.org/simple"),
]

# Python 安装包（国内镜像）。华为云与 npmmirror 都长期提供 Windows 官方安装包。
PYTHON_VERSION = "3.12.7"
PYTHON_URLS = [
    ("华为云", "https://mirrors.huaweicloud.com/python/{v}/python-{v}-amd64.exe"),
    ("npmmirror", "https://registry.npmmirror.com/-/binary/python/{v}/python-{v}-amd64.exe"),
]


def _host(url):
    """从 URL 里取主机名，给 pip 的 --trusted-host 用（公司网/TLS 拦截时会用到）"""
    m = re.match(r"https?://([^/]+)", url or "")
    return m.group(1) if m else ""


def python_exe():
    """返回带控制台的 python.exe。

    GUI 是 pythonw.exe 启动的，pythonw 没有控制台，虽然能用管道收输出，
    但个别 pip 版本在 pythonw 下会吞掉错误信息，所以这里换回 python.exe。
    """
    exe = sys.executable or "python"
    base = os.path.basename(exe).lower()
    if base.startswith("pythonw"):
        cand = os.path.join(os.path.dirname(exe), "python.exe")
        if os.path.exists(cand):
            return cand
    return exe


def python_info():
    return {
        "exe": python_exe(),
        "version": platform.python_version(),
        "bits": 64 if sys.maxsize > 2 ** 32 else 32,
        "store": "WindowsApps" in (sys.executable or ""),
        "ok": sys.version_info >= (3, 8),
    }


def run(cmd, timeout=300, cwd=None):
    """跑一个命令，返回 (退出码, 合并后的输出)"""
    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd or ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True, encoding="utf-8", errors="ignore",
        )
    except FileNotFoundError:
        return 127, "找不到命令: " + " ".join(cmd)
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        return 124, "命令超时（%s 秒）" % timeout
    return proc.returncode, out or ""


def pip_ready():
    code, out = run([python_exe(), "-m", "pip", "--version"], timeout=90)
    return code == 0, (out or "").strip()


def ensure_pip(log=print):
    """pip 缺失时用 ensurepip 就地补上 —— 这一步离线，不需要联网"""
    ok, ver = pip_ready()
    if ok:
        return True, ver
    log("[环境] 没找到 pip，尝试用 Python 自带的 ensurepip 补一个（不用联网）…")
    for args in (["--upgrade", "--default-pip"], ["--upgrade"], []):
        code, out = run([python_exe(), "-m", "ensurepip"] + args, timeout=300)
        if code == 0:
            ok, ver = pip_ready()
            if ok:
                log("[环境] pip 已补齐: " + ver)
                return True, ver
        else:
            log("[环境] ensurepip 失败: " + (out or "").strip()[:200])
    return False, ""


def missing_modules(modules=None):
    """检查哪些包没装（find_spec 不会真的 import，速度快且不会触发副作用）"""
    import importlib.util

    result = []
    for imp, pip_name, desc, required in (modules or MODULES):
        try:
            found = importlib.util.find_spec(imp) is not None
        except (ImportError, ValueError, AttributeError):
            found = False
        result.append({
            "import": imp, "pip": pip_name, "desc": desc,
            "required": required, "installed": found,
        })
    return result


def check_all(include_web=True):
    info = python_info()
    ok, ver = pip_ready()
    info["pip"] = ver if ok else ""
    info["pip_ok"] = ok
    info["modules"] = [m for m in missing_modules() if m["required"] or include_web]
    info["missing"] = [m["pip"] for m in info["modules"] if not m["installed"]]
    info["missing_required"] = [m["pip"] for m in info["modules"] if m["required"] and not m["installed"]]
    return info


def mirror_latency(url, timeout=6.0):
    """测一个镜像的响应时间；不通返回 None"""
    probe = url.rstrip("/") + "/requests/"
    req = urllib.request.Request(probe, headers={"User-Agent": "pip/24.0"})
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read(256)
        return round((time.time() - start) * 1000)
    except Exception:
        return None


def rank_mirrors(log=print, test=True):
    """返回 [(key, 名称, url, 延迟ms)]，最快的排前面；不通的排最后但仍然保留做兜底。"""
    ranked = []
    for key, name, url in MIRRORS:
        ms = mirror_latency(url) if test else None
        ranked.append((key, name, url, ms))
    live = [m for m in ranked if m[3] is not None]
    dead = [m for m in ranked if m[3] is None]
    live.sort(key=lambda m: m[3])
    if log and test:
        if live:
            log("[镜像] 可用: " + "  ".join("%s %dms" % (m[1], m[3]) for m in live))
        if dead:
            log("[镜像] 暂时不通（已跳过）: " + "  ".join(m[1] for m in dead))
    return live + dead


def resolve_mirror(choice="auto", log=print):
    """把用户选的镜像名解析成 (名称, url)。choice=auto 时自动测速。"""
    if choice and choice not in ("auto", "自动"):
        for key, name, url in MIRRORS:
            if choice in (key, name):
                return name, url
    ranked = rank_mirrors(log=log, test=True)
    if ranked:
        return ranked[0][1], ranked[0][2]
    return MIRRORS[0][1], MIRRORS[0][2]


def _pip_cmd(packages=None, req_file=None, mirror_url="", extra=None):
    cmd = [python_exe(), "-m", "pip", "install", "--disable-pip-version-check",
           "--no-input", "--retries", "3", "--timeout", "30", "--prefer-binary"]
    if req_file:
        cmd += ["-r", req_file]
    if packages:
        cmd += list(packages)
    if mirror_url:
        host = _host(mirror_url)
        cmd += ["-i", mirror_url]
        if host:
            cmd += ["--trusted-host", host]
    if extra:
        cmd += list(extra)
    return cmd


def pip_install(packages=None, req_file=None, mirror_url="", log=print, timeout=900):
    """装一批包，边装边把输出喂给 log。返回 (是否成功, 合并输出)"""
    cmd = _pip_cmd(packages, req_file, mirror_url)
    log("$ " + " ".join(cmd))
    try:
        proc = subprocess.Popen(
            cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True, encoding="utf-8", errors="ignore", bufsize=1,
        )
    except FileNotFoundError:
        return False, "找不到 python: " + python_exe()
    chunks = []
    start = time.time()
    for line in proc.stdout:
        chunks.append(line)
        log(line.rstrip())
        if time.time() - start > timeout:
            proc.kill()
            return False, "安装超时"
    code = proc.wait()
    out = "".join(chunks)
    return code == 0, out


def install_all(log=print, mirror="auto", include_web=False, packages=None, req_file=None):
    """完整安装流程：保 pip → 选镜像 → 装；失败就换镜像重来，最后退回 --user。"""
    info = python_info()
    if info["store"]:
        log("[环境] 当前 Python 是应用商店占位版，装不了包。请用「下载并安装 Python」按钮装一个正式版。")
        return False
    if not info["ok"]:
        log("[环境] Python 版本太低（%s），至少要 3.8。" % info["version"])
        return False

    ok, _ = ensure_pip(log)
    if not ok:
        log("[环境] pip 补不上，改用「下载并安装 Python」重装一个干净的 Python。")
        return False

    if req_file is None and packages is None:
        req_file = os.path.join(ROOT, "requirements-web.txt" if include_web else "requirements.txt")
        if not os.path.exists(req_file):
            req_file = None
    if req_file is None and packages is None:
        packages = [m["pip"] for m in missing_modules() if m["required"] or include_web]

    ranked = rank_mirrors(log=log, test=True)
    if mirror and mirror not in ("auto", "自动"):
        picked = resolve_mirror(mirror, log=None)
        ranked = [m for m in ranked if m[1] == picked[0]] + [m for m in ranked if m[1] != picked[0]]

    last_out = ""
    for key, name, url, ms in ranked:
        log("\n[环境] 用 %s 安装…" % name)
        ok, out = pip_install(packages=packages, req_file=req_file, mirror_url=url, log=log)
        last_out = out or last_out
        if ok:
            log("[环境] 安装完成（镜像: %s）" % name)
            return True
        log("[环境] %s 没装上，换下一个镜像…" % name)

    # 全都不行，最后试一次 --user（公司电脑没有写系统目录权限时常见）
    log("\n[环境] 所有镜像都失败，最后用 --user 再试一次…")
    ok, out = pip_install(packages=packages, req_file=req_file,
                          mirror_url=MIRRORS[0][2], log=log, )
    if not ok:
        log("[环境] 换 --user 重试")
        ok, out = pip_install(packages=packages, req_file=req_file,
                              mirror_url=MIRRORS[0][2], log=log, extra=["--user"])
    if ok:
        log("[环境] 安装完成（--user 模式）")
        return True
    log("[环境] 依赖没装上。最后的报错：\n" + (out or last_out or "")[-1500:])
    return False


def _download(url, dest, log=print):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp, open(dest, "wb") as fp:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        last = 0
        while True:
            chunk = resp.read(262144)
            if not chunk:
                break
            fp.write(chunk)
            done += len(chunk)
            if total and done - last > 8 * 1024 * 1024:
                last = done
                log("  已下载 %.1f / %.1f MB" % (done / 1048576.0, total / 1048576.0))
        return done


def download_python(log=print, version=PYTHON_VERSION, silent=True):
    """从国内镜像下一个 Windows 官方 Python 安装包并静默安装（不需要管理员）。"""
    if os.name != "nt":
        log("[环境] 这个功能只对 Windows 有效。其它系统请用包管理器装 python3。")
        return False
    dest = os.path.join(os.environ.get("TEMP") or ROOT, "python-%s-amd64.exe" % version)
    for name, tpl in PYTHON_URLS:
        url = tpl.format(v=version)
        log("[环境] 从 %s 下载: %s" % (name, url))
        try:
            size = _download(url, dest, log=log)
        except Exception as exc:  # noqa: BLE001 - 换下一个镜像继续
            log("[环境] %s 下载失败: %s" % (name, exc))
            continue
        if size < 5 * 1024 * 1024:
            log("[环境] 下载的文件太小（%d 字节），可能不是安装包，换下一个镜像" % size)
            continue
        log("[环境] 下载完成: %s（%.1f MB），开始静默安装…" % (dest, size / 1048576.0))
        args = [dest]
        if silent:
            args += ["/quiet", "InstallAllUsers=0", "PrependPath=1",
                     "Include_pip=1", "Include_test=0", "Include_launcher=1"]
        code, out = run(args, timeout=1800)
        if code == 0:
            log("[环境] Python %s 安装完成。关掉配置工具重新打开一次，再点「检查环境」。" % version)
            return True
        log("[环境] 安装包返回 %s。可以手动双击 %s 安装。" % (code, dest))
        return False
    log("[环境] 国内镜像都没下来。手动装一个也行：控制面板搜「Python」或用 微软商店 装 Python 3.12。")
    return False


def _report_compact(info, log=print):
    ok = sum(1 for m in info["modules"] if m["installed"])
    total = len(info["modules"])
    log("Python  " + info["version"] + "  " + str(info["bits"]) + "-bit   " + info["exe"])
    if info["store"]:
        log("[!] 应用商店占位版 Python，装不了包，点「下载并安装 Python」")
    log("pip     " + (_short_pip(info["pip"]) or "缺失（安装时自动用 ensurepip 补上）"))
    cells = []
    for m in info["modules"]:
        flag = "[OK]" if m["installed"] else ("[缺]" if m["required"] else "[--]")
        cells.append("%s %s%s" % (flag, m["pip"], "" if m["required"] else " 可选"))
    width = max(len(c) for c in cells) + 3
    for i in range(0, len(cells), 2):
        log("".join(c.ljust(width) for c in cells[i:i + 2]).rstrip())
    if info["missing_required"]:
        log("")
        log("还缺 %d 个必需包: %s" % (len(info["missing_required"]), ", ".join(info["missing_required"])))
        log("点下面的「一键安装依赖」，会自己挑最快的国内镜像。")
    else:
        log("")
        log("已装 %d/%d，依赖齐全，可以直接开跑。" % (ok, total))
    return info


def _short_pip(text):
    """把 pip 版本信息压成 'pip 26.2.2'，完整路径太长会把界面撑破"""
    m = re.match(r"(pip\s+[0-9][\w.\-]*)", text or "")
    return m.group(1) if m else (text or "")


def report(info=None, log=print, compact=False):
    """打印检查结果。

    compact=True 时压成十行以内（给配置工具的窗口用，窗口高度有限）；
    命令行默认详细模式，一列一个包 + 中文说明。
    """
    info = info or check_all()
    if compact:
        return _report_compact(info, log)
    ok = sum(1 for m in info["modules"] if m["installed"])
    total = len(info["modules"])
    log("Python  " + info["version"] + "  " + str(info["bits"]) + "-bit  " + info["exe"])
    if info["store"]:
        log("[!] 这是应用商店的占位 Python，装不了包，需要点「下载并安装 Python」")
    log("pip     " + (_short_pip(info["pip"]) or "缺失（一键安装会自动用 ensurepip 补上）"))
    log("-" * 46)
    for m in info["modules"]:
        flag = "[OK] " if m["installed"] else ("[缺] " if m["required"] else "[可选]")
        log("%-8s %s" % (flag, m["pip"] + "  " + m["desc"]))
    log("-" * 46)
    log("已装 %d/%d" % (ok, total))
    if info["missing_required"]:
        log("还缺 %d 个必需包: %s" % (len(info["missing_required"]), ", ".join(info["missing_required"])))
        log("点下面的「一键安装依赖」就行，会自动挑最快的国内镜像。")
    else:
        log("必需依赖齐了，可以 python main.py -c config.ini 开跑。")
    return info


def main(argv=None):
    parser = argparse.ArgumentParser(description="土豆刷学习通 · 环境检测 / 依赖安装")
    parser.add_argument("--check", action="store_true", help="只看状态")
    parser.add_argument("--install", action="store_true", help="安装依赖（自动选最快镜像）")
    parser.add_argument("--web", action="store_true", help="连网页版依赖(flask/celery)一起装")
    parser.add_argument("--mirror", default="auto", help="auto / huawei / aliyun / tsinghua / tencent / ustc / pypi")
    parser.add_argument("--download-python", action="store_true", help="从国内镜像下载并安装 Python")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出检查结果（给配置工具用）")
    args = parser.parse_args(argv)

    if args.download_python:
        return 0 if download_python() else 1
    if args.install:
        ok = install_all(include_web=args.web, mirror=args.mirror)
        report()
        return 0 if ok else 1
    info = check_all(include_web=True)
    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=1))
    else:
        report(info)
    return 0 if not info["missing_required"] else 1


if __name__ == "__main__":
    sys.exit(main())
