"""
运行环境自举
=============
让 ``python main.py`` / ``python run.py`` 在**任意解释器、任意一台电脑**上都能直接跑起来，
包括刚 ``git clone`` 下来、还没有虚拟环境的新电脑。

为什么需要
----------
项目依赖装在自带的 ``.venv`` 里。若用系统 Python 或 uv 托管的 Python 直接执行本
脚本，会在 ``import qrcode`` 处抛 ``ModuleNotFoundError``；而 uv 托管的 Python 被
标记为「外部管理」（PEP 668），pip 会拒绝安装，也不建议强行写入。

本模块在**导入任何第三方库之前**被调用，按以下顺序处理：

1. 已打包成 exe（依赖已内置），或当前解释器依赖齐全 -> 什么也不做；
2. 项目里没有 ``.venv``（新电脑首次运行）-> 用当前解释器建一个，
   并按 requirements.txt 把依赖装好；
3. ``.venv`` 存在但依赖没装全 -> 往里面补装；
4. 装好后：Python 次版本号一致（如都是 3.12，二进制扩展的 cp312 ABI 兼容）
   就把它的 site-packages 接入 ``sys.path``，当前进程立刻可用；不一致则用
   ``.venv`` 的解释器重新运行本脚本。

不会修改系统 Python 环境；安装过程有明确输出，失败时给出可复制执行的手动命令。
"""
import os
import subprocess
import sys

# 需要能成功导入的第三方模块（PIL 即 pillow，模块名与包名不同）
REQUIRED = ("qrcode", "uvicorn", "fastapi", "httptools", "tuspyserver", "PIL")
# 防止自动引导无限递归
MARKER = "LAN_FT_BOOTSTRAPPED"
# 官方源太慢时的备选镜像
MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"


def missing_modules() -> list:
    """当前解释器缺失的依赖模块名。"""
    import importlib.util

    return [name for name in REQUIRED if importlib.util.find_spec(name) is None]


def venv_dir(base: str) -> str:
    return os.path.join(base, ".venv")


def venv_python(base: str) -> str:
    """项目 .venv 的解释器路径（仅拼路径，不判断是否存在）。"""
    if os.name == "nt":
        return os.path.join(base, ".venv", "Scripts", "python.exe")
    return os.path.join(base, ".venv", "bin", "python")


def venv_site_packages(base: str) -> str:
    """项目 .venv 的 site-packages 目录；不存在时返回空字符串。"""
    if os.name == "nt":
        path = os.path.join(base, ".venv", "Lib", "site-packages")
    else:
        lib = os.path.join(base, ".venv", "lib")
        if not os.path.isdir(lib):
            return ""
        # 目录名带版本号，形如 python3.12
        for name in sorted(os.listdir(lib)):
            if name.startswith("python3"):
                path = os.path.join(lib, name, "site-packages")
                break
        else:
            return ""
    return path if os.path.isdir(path) else ""


def venv_version(base: str) -> tuple:
    """从 .venv/pyvenv.cfg 读出该环境的 Python 版本，如 (3, 12)；读不到返回 ()。"""
    try:
        with open(os.path.join(base, ".venv", "pyvenv.cfg"),
                  "r", encoding="utf-8", errors="replace") as fp:
            for line in fp:
                if line.strip().lower().startswith("version"):
                    text = line.split("=", 1)[1].strip()
                    return tuple(int(p) for p in text.split(".")[:2] if p.isdigit())
    except (OSError, ValueError, IndexError):
        pass
    return ()


def _venv_has_deps(python: str) -> bool:
    """用 .venv 自己的解释器确认依赖是否已装全。"""
    try:
        return subprocess.call([python, "-c", "import " + ", ".join(REQUIRED)],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL) == 0
    except OSError:
        return False


def _create_venv(base: str, python: str) -> bool:
    """用当前解释器创建 .venv（版本必然与当前一致）。"""
    print("[环境] 未找到虚拟环境，正在创建 .venv（首次运行只需一次）…", flush=True)
    try:
        code = subprocess.call([sys.executable, "-m", "venv", venv_dir(base)])
    except OSError as exc:
        print(f"[环境] 创建虚拟环境失败：{exc}")
        return False
    if code != 0 or not os.path.isfile(python):
        print("[环境] 创建虚拟环境失败：当前解释器可能缺少 venv 模块")
        return False
    return True


def _install_requirements(base: str, python: str) -> bool:
    """把 requirements.txt 装进 .venv；官方源失败时退回国内镜像重试。"""
    req = os.path.join(base, "requirements.txt")
    if not os.path.isfile(req):
        print(f"[环境] 找不到 {req}，无法自动安装依赖")
        return False
    cmd = [python, "-m", "pip", "install", "-r", req,
           "--disable-pip-version-check"]
    print("[环境] 正在安装依赖（首次约 1~2 分钟，请耐心等待）…", flush=True)
    if subprocess.call(cmd) == 0:
        return True
    print("[环境] 官方源安装失败，改用国内镜像重试…", flush=True)
    return subprocess.call(cmd + ["-i", MIRROR]) == 0


def _reexec(python: str, script: str) -> None:
    """用 .venv 的解释器重新运行本脚本（仅在版本不一致时走到这里）。"""
    env = dict(os.environ, **{MARKER: "1"})
    proc = subprocess.Popen([python, script, *sys.argv[1:]], env=env)
    if os.name == "nt":
        # Ctrl+C 会同时送给父子两个进程：父进程忽略掉，让子进程独占处理，
        # 免得父进程再抛一次 KeyboardInterrupt、打出一段看着像失败的堆栈
        import signal
        try:
            signal.signal(signal.SIGINT, signal.SIG_IGN)
        except (ValueError, OSError):
            pass
    sys.exit(proc.wait())


def _guide(base: str, script: str, missing: list, reason: str) -> None:
    """自动引导失败时，给出可直接复制执行的手动命令。"""
    python = venv_python(base)
    req = os.path.join(base, "requirements.txt")
    raise SystemExit(
        f"[环境] 当前 Python 缺少依赖：{'、'.join(missing)}\n"
        f"       当前解释器：{sys.executable}\n"
        f"       原因：{reason}\n"
        f"       请手动执行下面两条命令补齐（也可直接换用 .venv 里的解释器运行）：\n"
        f'           "{python}" -m pip install -r "{req}"\n'
        f'           "{python}" "{script}"\n')


def ensure_runtime(entry: str) -> None:
    """入口脚本调用：确保后续 import 第三方库能成功。

    ``entry`` 为入口脚本绝对路径（main.py / run.py），仅用于定位项目目录。
    """
    if getattr(sys, "frozen", False):
        return                       # PyInstaller 打包后依赖已在包内

    missing = missing_modules()
    if not missing:
        return                       # 当前解释器环境完好

    if os.environ.get(MARKER):
        _guide(os.path.dirname(os.path.abspath(entry)),
               os.path.abspath(entry), missing,
               "自动引导后依赖仍不完整")   # 不再递归

    base = os.path.dirname(os.path.abspath(entry))
    script = os.path.abspath(entry)
    python = venv_python(base)
    site = venv_site_packages(base)

    # ---- 1) 没有 .venv（新电脑 clone 后的首次运行）：建一个 ----
    if not os.path.isfile(python):
        if not _create_venv(base, python):
            _guide(base, script, missing, "无法自动创建虚拟环境")
        site = venv_site_packages(base)

    # ---- 2) 依赖没装全：补装 ----
    if not _venv_has_deps(python) and not _install_requirements(base, python):
        _guide(base, script, missing, "依赖安装失败，请检查网络后重试")

    # ---- 3) 次版本号一致：直接把 .venv 的包接入当前进程，无需重开进程 ----
    if site and venv_version(base) == tuple(sys.version_info[:2]):
        sys.path.insert(0, site)
        if not missing_modules():
            print(f"[环境] 当前解释器缺少依赖，已自动改用项目 .venv 中的包：{site}",
                  flush=True)
            return

    # ---- 4) .venv 是别的 Python 版本建的：换它的解释器重跑 ----
    _reexec(python, script)
