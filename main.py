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
from store import UPLOAD_DIR, create_app, disk_free, human_size

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
    """电脑端窗口。

    包含：二维码、局域网信息、已接收文件列表（IP / 计算机名 / MAC + 右键删除记录）、
    本机上传区（进度 / 速度 / 暂停继续 / 取消）、未完成任务提示，
    关闭时若有上传任务会两次确认。
    """
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    from PIL import Image, ImageTk

    from store import (MEMORY_PEAK, human_size, local_machine_info,
                       read_all_meta, scan_pending_uploads)
    from uploader import UploadTask

    root = tk.Tk()
    root.title("局域网文件传输工具 - 电脑端")
    root.geometry("860x760")
    root.configure(bg="#0f1220")
    machine = local_machine_info()

    # ---------------------------------------------------------- 顶部信息
    tk.Label(root, text="局域网文件传输工具", bg="#0f1220", fg="#eef1ff",
             font=("微软雅黑", 14, "bold")).pack(pady=(10, 2))
    tk.Label(root, text=f"手机与电脑连同一 WiFi，扫码访问：{url}",
             bg="#0f1220", fg="#9aa3c7", font=("微软雅黑", 9)).pack()
    tk.Label(root,
             text=f"本机：{machine['name']}    IP：{url.split('//')[1].split(':')[0]}"
                  f"    MAC：{machine['mac'] or '未知'}",
             bg="#0f1220", fg="#9aa3c7", font=("微软雅黑", 9)).pack(pady=(2, 4))

    img = Image.open(qr_path)
    img = img.resize((170, 170), Image.LANCZOS)
    photo = ImageTk.PhotoImage(img)
    tk.Label(root, image=photo, bg="white", bd=0).pack(pady=2)

    # ---------------------------------------------------------- 已接收文件
    tk.Label(root, text="已接收文件（双击下载到本机，右键删除记录）",
             bg="#0f1220", fg="#eef1ff",
             font=("微软雅黑", 10, "bold")).pack(anchor="w", padx=14, pady=(6, 2))

    frame = tk.Frame(root, bg="#0f1220")
    frame.pack(fill="both", expand=True, padx=14)
    cols = ("name", "size", "time", "ip", "pcname", "mac")
    tree = ttk.Treeview(frame, columns=cols, show="headings", height=9)
    headers = (("文件名", 190), ("大小", 80), ("接收时间", 105),
               ("IP地址", 100), ("计算机名", 110), ("MAC地址", 125))
    for col, (title, width) in zip(cols, headers):
        tree.heading(col, text=title)
        tree.column(col, width=width, anchor="w")
    vsb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=vsb.set)
    tree.pack(side="left", fill="both", expand=True)
    vsb.pack(side="right", fill="y")

    def refresh_tree():
        for item in tree.get_children():
            tree.delete(item)
        for rec in read_all_meta():
            tree.insert("", "end", iid=rec["id"], values=(
                rec["name"], human_size(rec["size"]), rec["uploaded_at"],
                rec.get("client_ip") or "-", rec.get("client_name") or "-",
                rec.get("client_mac") or "-"))

    def on_double_click(_event):
        sel = tree.selection()
        if sel:
            webbrowser.open(f"http://127.0.0.1:{port}/files/{sel[0]}")

    def on_right_click(event):
        """右键菜单：仅删除任务记录，保留本地文件。"""
        sel = tree.selection()
        if not sel:
            return
        menu = tk.Menu(root, tearless=0)
        menu.add_command(label="删除任务记录（保留文件）",
                         command=lambda: delete_record(sel[0]))
        menu.add_command(label="下载到本机",
                         command=lambda: webbrowser.open(
                             f"http://127.0.0.1:{port}/files/{sel[0]}"))
        menu.post(event.x_root, event.y_root)

    def delete_record(file_id: str):
        rec = next((r for r in read_all_meta() if r["id"] == file_id), None)
        if not rec:
            return
        if not messagebox.askyesno("删除任务记录",
                                   f"仅删除任务记录，不删除文件：\n{rec['name']}\n\n确定删除吗？"):
            return
        try:
            os.remove(os.path.join(os.path.dirname(os.path.dirname(
                os.path.dirname(rec["path"]))), ".meta", f"{file_id}.json"))
        except OSError as exc:
            messagebox.showerror("删除失败", str(exc))
            return
        refresh_tree()

    tree.bind("<Double-1>", on_double_click)
    tree.bind("<Button-3>", on_right_click)
    tree.bind("<Button-2>", on_right_click)
    # ---------------------------------------------------------- 本机上传区
    tk.Label(root, text="从本机上传（分片 · 断点续传 · 可暂停继续）",
             bg="#0f1220", fg="#eef1ff",
             font=("微软雅黑", 10, "bold")).pack(anchor="w", padx=14, pady=(8, 2))

    up_frame = ttk.Frame(root)
    up_frame.pack(fill="x", padx=14)
    up_tree = ttk.Treeview(up_frame, columns=("name", "size", "pct", "speed", "state"),
                            show="headings", height=4)
    up_cols = (("name", 190), ("size", 80), ("pct", 70), ("speed", 90), ("state", 90))
    for col, (title, width) in zip(("name", "size", "pct", "speed", "state"), up_cols):
        up_tree.heading(col, text=title)
        up_tree.column(col, width=width, anchor="w")
    up_tree.pack(side="left", fill="both", expand=True)

    tasks = {}          # iid -> UploadTask

    STATE_TEXT = {"running": "上传中", "paused": "已暂停", "done": "已完成",
                  "failed": "失败", "canceled": "已取消"}

    def add_task(path: str):
        task = UploadTask(path, port)
        iid = str(abs(hash(path)) % 10 ** 8)
        tasks[iid] = task
        up_tree.insert("", "end", iid=iid, values=(
            task.name, human_size(task.size), "0%", "-", "已暂停"))

        def on_progress(uploaded, size, speed, status):
            root.after(0, lambda: _update_row(iid, uploaded, size, speed, status))

        task.on_progress = on_progress
        task.on_done = lambda t: root.after(0, refresh_tree)

    def _update_row(iid, uploaded, size, speed, status):
        if not up_tree.exists(iid):
            return
        pct = (uploaded / size * 100) if size else 0
        speed_text = f"{speed:.1f} MB/s" if status == "running" else "-"
        up_tree.item(iid, values=(tasks[iid].name, human_size(size),
                                  f"{pct:.1f}%", speed_text,
                                  STATE_TEXT.get(status, status)))

    def pick_files():
        for path in filedialog.askopenfilenames(title="选择要上传的文件"):
            add_task(path)

    def task_action(action: str):
        sel = up_tree.selection()
        if not sel:
            return
        for iid in sel:
            task = tasks.get(iid)
            if not task:
                continue
            if action == "start":
                task.start()
            elif action == "pause":
                task.pause()
            elif action == "resume":
                task.resume()
            elif action == "cancel":
                if messagebox.askyesno("取消上传", "取消并清除该任务的已上传分片？"):
                    task.cancel()
                    up_tree.delete(iid)
                    tasks.pop(iid, None)
            elif action == "restart":
                task.restart()
                up_tree.item(iid, values=(task.name, human_size(task.size),
                                          "0%", "-", "已暂停"))
    # ---------------------------------------------------------- 未完成任务
    pending = scan_pending_uploads()
    if pending:
        text = "上次有未完成的上传（已暂停）：" + "、".join(
            f"{p['name']} {p['offset']}/{human_size(p['size'])}" for p in pending[:3])
        tk.Label(root, text=text + ("…" if len(pending) > 3 else ""),
                 bg="#3a2f14", fg="#ffcc66", font=("微软雅黑", 9),
                 wraplength=820, justify="left").pack(fill="x", padx=14, pady=(6, 0))

    # ---------------------------------------------------------- 底部按钮
    bar = tk.Frame(root, bg="#0f1220")
    bar.pack(pady=8)

    ttk.Button(bar, text="选择文件", command=pick_files).pack(side="left", padx=4)
    ttk.Button(bar, text="开始/继续", command=lambda: task_action("start")).pack(side="left", padx=4)
    ttk.Button(bar, text="暂停", command=lambda: task_action("pause")).pack(side="left", padx=4)
    ttk.Button(bar, text="取消", command=lambda: task_action("cancel")).pack(side="left", padx=4)
    ttk.Button(bar, text="重传", command=lambda: task_action("restart")).pack(side="left", padx=4)
    ttk.Button(bar, text="刷新列表", command=refresh_tree).pack(side="left", padx=4)
    ttk.Button(bar, text="打开网页", command=lambda: webbrowser.open(url)).pack(side="left", padx=4)
    ttk.Button(bar, text="打开文件夹",
               command=lambda: os.startfile(UPLOAD_DIR) if os.name == "nt"
               else subprocess.Popen(["xdg-open", UPLOAD_DIR])).pack(side="left", padx=4)
    ttk.Button(bar, text="退出", command=root.destroy).pack(side="left", padx=4)

    status_line = tk.Label(root, text="", bg="#0f1220", fg="#7f88ad", font=("微软雅黑", 8))
    status_line.pack(pady=(0, 6))

    # ---------------------------------------------------------- 关闭确认
    def has_running_task() -> bool:
        return any(t.status == "running" for t in tasks.values())

    def on_close():
        """有上传任务时，关闭需要两次确认。"""
        if has_running_task():
            if not messagebox.askyesno(
                    "仍有上传任务进行中",
                    "还有文件正在上传。\n\n第一次确认：仍要关闭程序吗？\n"
                    "（已上传的分片会保留，下次可断点续传）"):
                return
            if not messagebox.askyesno(
                    "再次确认",
                    "真的要关闭吗？\n\n第二次确认：关闭后本机上传将中断，"
                    "未完成的分片会保留在服务器上。"):
                return
        server.should_exit = True
        for task in tasks.values():
            if task.status == "running":
                task.pause()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)

    def tick():
        refresh_tree()
        root.after(2000, tick)

    refresh_tree()
    root.after(500, tick)
    root.mainloop()


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