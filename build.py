"""一键打包：单文件 + 无控制台窗口 + UPX 压缩 + 数字签名。

用法（在项目目录下）：
    .venv\Scripts\python.exe build.py
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
UPX = os.path.join(ROOT, "tools", "upx-5.2.1-win64")


def run(cmd):
    print("+", " ".join(str(c) for c in cmd), flush=True)
    return subprocess.call([str(c) for c in cmd], cwd=ROOT)


def main():
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
             "main.py"]
    if run(args) != 0:
        print("打包失败")
        sys.exit(1)

    ps = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"]
    run(ps + ["make_cert.ps1"])
    run(ps + ["sign.ps1"])

    exe = os.path.join(ROOT, "dist", "FileTransfer.exe")
    if os.path.exists(exe):
        print("完成:", exe, os.path.getsize(exe), "字节")
    else:
        print("未找到产物", exe)


if __name__ == "__main__":
    main()