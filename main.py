"""
局域网文件传输工具（电脑端服务程序）
======================================

功能：
    1. 自动获取本机局域网 IP、随机分配空闲端口，启动 HTTP 服务；
    2. 自动生成访问地址二维码，并在 tkinter 窗口中展示（二维码 + IP + 端口 + 文件列表）；
    3. 手机 / 电脑扫码打开网页，使用 TUS 分片协议上传，支持 100G 级超大文件与断点续传；
    4. 上传全程流式写盘，内存占用恒定；上传前校验磁盘空间，完成后计算 SHA256；
    5. 页面展示所有设备上传的文件，点击即可流式下载。

依赖：FastAPI + uvicorn + tuspyserver + qrcode + pillow
运行：python main.py            （默认 GUI 窗口）
      python main.py --no-gui  （仅控制台）
"""
import argparse
import os
import subprocess
import sys
import threading
import time
import webbrowser

import qrcode
import uvicorn

from netutils import find_free_port, get_lan_ip, list_lan_ips
from store import (UPLOAD_DIR, create_app, disk_free, human_size, read_all_meta)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 打包成 exe 后 __file__ 指向临时解压目录，必须改用 exe 所在目录
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))

QR_PATH = os.path.join(BASE_DIR, "lan_qrcode.png")
SERVER_IP = "127.0.0.1"   # 启动后写入真实内网 IP
SERVER_PORT = 8000        # 启动后写入真实端口


def ensure_std_streams():
    """打包成无控制台 exe 后 stdout/stderr 为 None，print 会抛异常，这里兜底。"""
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


def make_qrcode(text: str) -> str:
    """生成二维码 PNG（容错等级 M，扫码更稳）。"""
    qr = qrcode.QRCode(box_size=8, border=2)
    qr.add_data(text)
    qr.make(fit=True)
    qr.make_image(fill_color="black", back_color="white").save(QR_PATH)
    return QR_PATH


def print_qrcode_text(url: str):
    """控制台用字符打印二维码（无 tkinter 时的备选方案）。"""
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    try:
        qr.print_ascii(invert=True)
    except UnicodeEncodeError:
        pass


def start_server(host: str, port: int):
    """后台线程启动 uvicorn，返回 (server, thread)。

    超时时间已按长时间传输调大：
        timeout_keep_alive  保持连接 120 秒（分片之间的空闲容忍）
        h11_max_incomplete_event_size 放宽单个请求体上限（32MB 分片不被拒）
    """
    config = uvicorn.Config(
        app, host=host, port=port, log_level="warning", access_log=False,
        timeout_keep_alive=120,
        http="httptools",          # 大文件流式传输必须用 httptools，h11 会在超大请求体上断连
        limit_concurrency=None,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True, name="http-server")
    thread.start()
    return server, thread

# ==========================================================================
# tkinter 窗口：二维码 + IP/端口 + 已接收文件列表
# ==========================================================================


def run_gui(url: str, qr_path: str, port: int, server) -> None:
    """电脑端窗口。文件列表双击即在本机浏览器下载。"""
    import tkinter as tk
    from tkinter import ttk

    from PIL import Image, ImageTk

    root = tk.Tk()
    root.title("局域网文件传输工具 - 电脑端")
    root.geometry("580x700")
    root.configure(bg="#0f1220")

    tk.Label(root, text="局域网文件传输工具", bg="#0f1220", fg="#eef1ff",
             font=("微软雅黑", 14, "bold")).pack(pady=(12, 2))
    tk.Label(root, text="手机与电脑连同一 WiFi，扫码访问：" + url,
             bg="#0f1220", fg="#9aa3c7", font=("微软雅黑", 9)).pack()
    tk.Label(root, text=f"接收目录：{UPLOAD_DIR}    剩余空间：{human_size(disk_free(UPLOAD_DIR))}",
             bg="#0f1220", fg="#9aa3c7", font=("微软雅黑", 9)).pack(pady=(2, 6))

    img = Image.open(qr_path)
    img = img.resize((210, 210), Image.LANCZOS)
    photo = ImageTk.PhotoImage(img)
    tk.Label(root, image=photo, bg="white", bd=0).pack(pady=4)

    tk.Label(root, text="已接收文件（双击下载到本机）", bg="#0f1220", fg="#eef1ff",
             font=("微软雅黑", 10, "bold")).pack(anchor="w", padx=16, pady=(6, 2))

    frame = tk.Frame(root, bg="#0f1220")
    frame.pack(fill="both", expand=True, padx=16)
    tree = ttk.Treeview(frame, columns=("name", "size", "time"), show="headings", height=10)
    for col, title, width in zip(("name", "size", "time"),
                                 ("文件名", "大小", "接收时间"), (300, 90, 130)):
        tree.heading(col, text=title)
        tree.column(col, width=width, anchor="w")
    scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=scroll.set)
    tree.pack(side="left", fill="both", expand=True)
    scroll.pack(side="right", fill="y")

    def refresh_tree():
        for item in tree.get_children():
            tree.delete(item)
        for rec in read_all_meta():
            tree.insert("", "end", values=(rec["name"], human_size(rec["size"]),
                                           rec["uploaded_at"]))

    def on_double_click(_event):
        selected = tree.selection()
        if selected:
            file_id = tree.item(selected[0], "values")[0]
            webbrowser.open(f"http://127.0.0.1:{port}/files/{file_id}")

    tree.bind("<Double-1>", on_double_click)

    bar = tk.Frame(root, bg="#0f1220")
    bar.pack(pady=10)
    ttk.Button(bar, text="刷新列表", command=refresh_tree).pack(side="left", padx=5)
    ttk.Button(bar, text="打开网页", command=lambda: webbrowser.open(url)).pack(side="left", padx=5)
    ttk.Button(bar, text="打开文件夹",
               command=lambda: os.startfile(UPLOAD_DIR) if os.name == "nt"
               else subprocess.Popen(["xdg-open", UPLOAD_DIR])).pack(side="left", padx=5)
    ttk.Button(bar, text="退出", command=root.destroy).pack(side="left", padx=5)

    def on_close():
        """关闭窗口时同步关闭 web 服务，避免进程残留。"""
        server.should_exit = True
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    refresh_tree()

    def tick():
        refresh_tree()
        root.after(2000, tick)

    root.after(2000, tick)
    root.mainloop()


# ==========================================================================
# 程序入口
# ==========================================================================


def main() -> None:
    global SERVER_IP, SERVER_PORT

    ensure_std_streams()  # 必须在任何输出之前
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="局域网文件传输工具（TUS 分片上传）")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址，默认 0.0.0.0（所有网卡）")
    parser.add_argument("--port", type=int, default=0, help="端口，0 = 随机分配空闲端口")
    parser.add_argument("--no-gui", action="store_true", help="不打开窗口，仅控制台输出二维码")
    parser.add_argument("--no-browser", action="store_true", help="启动时不自动打开浏览器")
    args = parser.parse_args()

    global app
    app = create_app()  # 创建目录并装配 TUS 路由

    SERVER_PORT = find_free_port(args.port)
    SERVER_IP = get_lan_ip()
    url = f"http://{SERVER_IP}:{SERVER_PORT}"
    qr_path = make_qrcode(url)

    print("=" * 60)
    print("  局域网文件传输工具已启动（TUS 分片上传 / 支持 100G）")
    print("=" * 60)
    print(f"  访问地址   : {url}")
    print(f"  接收目录   : {UPLOAD_DIR}")
    print(f"  剩余磁盘   : {human_size(disk_free(UPLOAD_DIR))}")
    others = [ip for ip in list_lan_ips() if ip != SERVER_IP]
    if others:
        print(f"  其它网卡 IP: {', '.join(others)}")
    print("-" * 60)
    if args.no_gui:
        print()
        print_qrcode_text(url)
    print("  手机连接同一 WiFi 后扫码即可上传；关闭窗口或 Ctrl+C 停止服务。")
    print("=" * 60)

    server, thread = start_server(args.host, SERVER_PORT)
    time.sleep(0.8)
    if not getattr(server, "started", False):
        print(f"[错误] 服务启动失败，端口可能被占用：{args.host}:{SERVER_PORT}")

    if not args.no_browser:
        threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()

    gui_ok = False
    if not args.no_gui:
        try:
            run_gui(url, qr_path, SERVER_PORT, server)
            gui_ok = True
        except Exception as exc:
            print(f"[提示] 无法启动图形窗口（{exc}），已切换为控制台模式。")
            print_qrcode_text(url)
    if not gui_ok:
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n正在关闭服务…")

    server.should_exit = True
    thread.join(timeout=5)
    print("服务已停止。")


app = None  # 由 main() 装配后供 uvicorn 使用

if __name__ == "__main__":
    main()