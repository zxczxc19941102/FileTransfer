r"""一键打包：单文件 + 无控制台窗口 + UPX 压缩 + 数字签名。

用法（在项目目录下）：
    .venv\Scripts\python.exe build.py

也可以直接用系统 Python / uv 托管的 Python 跑（`python build.py`）：
本脚本会自己换用装了 PyInstaller 的解释器，见 ensure_pyinstaller()。

注：上面那行路径里的反斜杠必须放在「原始字符串」里（本行开头加了前缀 r），
    否则 Python 会报 SyntaxWarning: invalid escape sequence '\S'。
"""
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
UPX = os.path.join(ROOT, "tools", "upx-5.2.1-win64")


def run(cmd):
    print("+", " ".join(str(c) for c in cmd), flush=True)
    return subprocess.call([str(c) for c in cmd], cwd=ROOT)


def venv_python() -> str:
    """项目 .venv 的解释器路径（仅拼路径，不判断是否存在）。"""
    if os.name == "nt":
        return os.path.join(ROOT, ".venv", "Scripts", "python.exe")
    return os.path.join(ROOT, ".venv", "bin", "python")


def has_pyinstaller(python: str) -> bool:
    try:
        return subprocess.call([python, "-c", "import PyInstaller"],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL) == 0
    except OSError:
        return False


def ensure_pyinstaller() -> None:
    """确保打包用的解释器里有 PyInstaller，否则换一个再重跑本脚本。

    为什么需要：用系统 Python 或 uv 托管的 Python 直接跑 ``python build.py``
    时，那个解释器里没有 PyInstaller（uv 的解释器还被标成「外部管理」，
    pip 会拒绝写入），于是 PyInstaller 直接报
    ``No module named PyInstaller`` + 一句没头没脑的「打包失败」。
    打包依赖本来就该装在项目自带的 .venv 里，这里换过去重跑即可。
    """
    if has_pyinstaller(sys.executable):
        return
    venv = venv_python()
    if os.path.isfile(venv) and has_pyinstaller(venv):
        print(f"[环境] 当前解释器（{sys.executable}）没有 PyInstaller，"
              f"改用项目 .venv 重跑…", flush=True)
        sys.exit(subprocess.call([venv, os.path.abspath(__file__),
                                  *sys.argv[1:]], cwd=ROOT))
    raise SystemExit(
        "[环境] 找不到可用的 PyInstaller。\n"
        f"       当前解释器：{sys.executable}\n"
        f"       项目 .venv：{venv}"
        f"{'' if os.path.isfile(venv) else '（不存在）'}\n"
        "       请先安装到项目 .venv 再重试：\n"
        f'           "{venv}" -m pip install pyinstaller '
        "-i https://pypi.tuna.tsinghua.edu.cn/simple")


def main():
    ensure_pyinstaller()
    args = [sys.executable, "-m", "PyInstaller",
            "-F", "-n", "FileTransfer",
            "--noconsole", "--clean", "--noconfirm"]
    if os.path.exists(os.path.join(UPX, "upx.exe")):
        args += ["--upx-dir", UPX]
        print("[UPX] 压缩已启用 ->", UPX)
    else:
        print("[UPX] 未找到 upx.exe，本次跳过压缩")
    # tus.min.js 必须打进 exe：页面把它内联进去，缺了就只能回退到外网 CDN
    args += ["--add-data", "tus.min.js" + os.pathsep + ".",
             "--collect-all", "uvicorn",
             "--collect-all", "fastapi",
             "--collect-all", "starlette",
             "--collect-all", "pydantic",
             "--collect-all", "anyio",
             "--collect-all", "tuspyserver",
             "--hidden-import", "tkinter",
             "--hidden-import", "PIL",
             "--hidden-import", "httptools",
             "--hidden-import", "encodings.idna",
             # 入口用 run.py 而不是 main.py：两者功能与参数完全一致，区别只在
             # 退出行为——run.py 注册了快速退出钩子，避开"关窗口瞬间服务端正在
             # 给大文件算 SHA256，线程池的退出钩子会一直等它算完（可能十几分钟）"
             # 的问题。exe 是 --noconsole，用户看不到任何提示，进程却一直挂在任务
             # 管理器里占 CPU，比命令行下更莫名其妙。run.py 的 docstring 也写明
             # "pyinstaller 打包后同样有效"。
             "run.py"]
    if run(args) != 0:
        print("打包失败")
        sys.exit(1)

    ps = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"]
    if run(ps + ["make_cert.ps1"]) != 0:
        print("[提示] 证书脚本返回非零；证书可能已存在（属正常），也可能是生成失败。")
    # 签名必须成功：刚写出的 exe 常被杀软/索引进程短暂占用，签名会报
    # "being used by another process"。实测确实会偶发，所以重试一次；
    # 仍然失败就**以非零退出**——旧版只是打印，脚本照样报「完成」，
    # 结果交付一个没签名的 exe（本机双击就会弹「未知发布者」）。
    for attempt in (1, 2):
        if run(ps + ["sign.ps1"]) == 0:
            break
        print(f"[警告] 第 {attempt} 次签名失败，{3} 秒后重试…")
        time.sleep(3)
    else:
        print("签名失败：exe 未签名。请关闭占用它的进程后手动执行：")
        print("    powershell -NoProfile -ExecutionPolicy Bypass -File sign.ps1")
        sys.exit(1)

    exe = os.path.join(ROOT, "dist", "FileTransfer.exe")
    if os.path.exists(exe):
        print("完成:", exe, os.path.getsize(exe), "字节")
    else:
        print("未找到产物", exe)


if __name__ == "__main__":
    main()