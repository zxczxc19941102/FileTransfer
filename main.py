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
    BASE_DIR: str = os.path.dirname(os.path.abspath(sys.executable))

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

    包含：二维码、局域网信息、已接收文件列表（右键菜单：下载 / 删除任务 / 打开）、
    本机上传任务列表（持久化 + 右键：继续执行 / 下载 / 暂停 / 删除），
    关闭时若有上传任务会两次确认。
    """
    import json
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    from PIL import Image, ImageTk

    from store import (human_size, list_active_uploads, local_machine_info,
                       meta_path, read_all_meta, scan_pending_uploads)
    from uploader import LOCAL_TASK_FILE, UploadTask, load_local_tasks, save_local_tasks

    root = tk.Tk()
    root.title("局域网文件传输工具 - 电脑端")
    root.geometry("900x780")
    root.configure(bg="#0f1220")
    machine = local_machine_info()
    # 上传任务的浏览器端 uid（用于把本机任务与网页"继续"联动）
    WEB_UID = {}

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
    img = img.resize((160, 160), Image.LANCZOS)
    photo = ImageTk.PhotoImage(img)
    tk.Label(root, image=photo, bg="white", bd=0).pack(pady=2)

    # ---------------------------------------------------------- 已接收文件
    tk.Label(root, text="已接收文件（其他设备上传时这里会实时显示进度；左键选中，右键：下载 / 删除）",
             bg="#0f1220", fg="#eef1ff",
             font=("微软雅黑", 10, "bold")).pack(anchor="w", padx=14, pady=(6, 2))

    frame = tk.Frame(root, bg="#0f1220")
    frame.pack(fill="both", expand=True, padx=14)
    cols = ("name", "size", "state", "time", "ip", "pcname", "mac")
    tree = ttk.Treeview(frame, columns=cols, show="headings", height=8)
    headers = (("文件名", 175), ("大小", 80), ("状态", 105), ("接收时间", 100),
               ("IP地址", 100), ("计算机名", 100), ("MAC地址", 120))
    for col, (title, width) in zip(cols, headers):
        tree.heading(col, text=title)
        tree.column(col, width=width, anchor="w")
    vsb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=vsb.set)
    tree.pack(side="left", fill="both", expand=True)
    vsb.pack(side="right", fill="y")

    live_info = {}          # uid -> 活跃上传信息（传输中任务）

    # 本机身份：用 127.0.0.1 或本机局域网 IP（如 192.168.1.100）打开页面，
    # 都算「本机发起的任务」。否则用局域网 IP 访问自己时，自己传的文件
    # 会被误判成"其他设备"而无法暂停/删除。
    self_ips = {"127.0.0.1", "::1", "localhost"}
    try:
        self_ips.add(get_lan_ip())
        self_ips.update(list_lan_ips())
    except Exception:
        pass
    try:
        self_mac = (local_machine_info() or {}).get("mac") or ""
    except Exception:
        self_mac = ""

    def local_uids() -> set:
        """本机上传器占用的任务 ID（只有这些任务允许本机暂停/删除）。"""
        out = set()
        for rec in tasks.values():
            u = rec["task"]._uid()
            if u:
                out.add(u)
        return out

    def is_mine(uid: str) -> bool:
        """该任务是否由本机发起（本机发起 = 可操作；他人发起 = 只读）。

        依次按三种依据判断：本机上传器持有的 uid、网卡 MAC 一致、
        来源 IP 属于本机任一网卡地址。MAC 判断最可靠（多网卡也不误判）。
        """
        if uid in local_uids():
            return True
        info = live_info.get(uid) or {}
        if self_mac and (info.get("client_mac") or "").upper() == self_mac.upper():
            return True
        return (info.get("client_ip") or "").strip() in self_ips

    def refresh_tree():
        for item in tree.get_children():
            tree.delete(item)
        # 其他设备正在上传的任务：显示实时进度（其它设备/网页端上传时同步可见）
        try:
            for a in list_active_uploads():
                pct = min(100.0, a["uploaded"] / a["size"] * 100) if a["size"] else 0.0
                who = a.get("client_name") or a.get("client_ip") or "未知设备"
                uid = a["uid"]
                live_info[uid] = a
                mine = is_mine(uid)
                tree.insert("", "end", iid="live_" + uid, values=(
                    a["name"], human_size(a["size"], ), f"上传中 {pct:.2f}%",
                    ("本机上传" if mine else "传输中…"),
                    a.get("client_ip") or "-", who, a.get("client_mac") or "-"))
        except Exception:
            pass
        for rec in read_all_meta():
            tree.insert("", "end", iid=rec["id"], values=(
                rec["name"], human_size(rec["size"]), "已完成", rec["uploaded_at"],
                rec.get("client_ip") or "-", rec.get("client_name") or "-",
                rec.get("client_mac") or "-"))

    def api_call(method: str, path: str):
        """调用本机服务接口。

        请求走 127.0.0.1，因此在服务端看来属于"本机"，对本机发起的
        任务拥有操作权限；他人的任务会被 403 拒绝。
        """
        import http.client
        import json as _json
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
            conn.request(method, path)
            resp = conn.getresponse()
            body = resp.read()
            conn.close()
            data = _json.loads(body.decode("utf-8")) if body else {}
            if resp.status >= 400:
                return False, data.get("detail", f"HTTP {resp.status}")
            return True, data
        except Exception as exc:
            return False, str(exc)

    def rec_path(file_id: str) -> str:
        for r in read_all_meta():
            if r["id"] == file_id:
                return r.get("path", "")
        return ""

    def pause_remote(uid: str):
        """请求暂停传输中的任务（通过心跳回执同步到对方的页面/程序）。"""
        ok, msg = api_call("POST", f"/api/pause/{uid}")
        if ok:
            messagebox.showinfo("已请求暂停", "已通知对方：任务将在其下次心跳时暂停。")
        else:
            messagebox.showwarning("无法暂停", str(msg))

    def resume_remote(uid: str):
        """请求继续传输中的任务。"""
        ok, msg = api_call("POST", f"/api/resume/{uid}")
        if ok:
            messagebox.showinfo("已请求继续", "已通知对方继续上传。")
        else:
            messagebox.showwarning("无法继续", str(msg))

    def drop_remote(uid: str):
        """删除传输中的任务（清除其已上传的分片）。"""
        if not messagebox.askyesno("删除任务",
                "删除该上传任务？\n已上传的分片数据也会被清除，且不可恢复。"):
            return
        ok, msg = api_call("DELETE", f"/api/pending/{uid}")
        if ok:
            messagebox.showinfo("已删除", "该上传任务已删除")
        else:
            messagebox.showwarning("删除失败", str(msg))

    def on_rec_right_click(event):
        """已接收文件：右键菜单。

        传输中的任务（live_ 前缀）也支持暂停 / 继续 / 删除，
        且**仅允许操作本机发起的任务**；他人任务只读。
        """
        iid = tree.identify_row(event.y)
        if not iid:
            return
        tree.selection_set(iid)
        menu = tk.Menu(root, tearoff=0)
        if iid.startswith("live_"):
            uid = iid[5:]
            info = live_info.get(uid) or {}
            who = info.get("client_ip") or "其它设备"
            if is_mine(uid):
                menu.add_command(label="暂停上传", command=lambda: pause_remote(uid))
                menu.add_command(label="继续上传", command=lambda: resume_remote(uid))
                menu.add_separator()
                menu.add_command(label="删除任务（清除分片）", command=lambda: drop_remote(uid))
            else:
                menu.add_command(label="等待上传完成…", state="disabled")
                menu.add_separator()
                menu.add_command(label=f"只能操作自己的任务（该任务来自 {who}）",
                                 state="disabled")
            menu.post(event.x_root, event.y_root)
            return
        menu.add_command(label="下载到本机",
                         command=lambda: webbrowser.open(f"http://127.0.0.1:{port}/files/{iid}"))
        menu.add_command(label="下载并选择保存位置",
                         command=lambda: save_as(iid))
        menu.add_separator()
        menu.add_command(label="打开文件", command=lambda: open_path(rec_path(iid)))
        menu.add_command(label="打开所在文件夹",
                         command=lambda: reveal_path(rec_path(iid)))
        menu.add_separator()
        menu.add_command(label="删除任务（保留文件）", command=lambda: delete_record(iid))
        menu.post(event.x_root, event.y_root)

    def save_as(file_id: str):
        """下载并选择保存位置。"""
        import urllib.request
        src = rec_path(file_id)
        if not src or not os.path.isfile(src):
            messagebox.showerror("下载失败", "文件不存在")
            return
        name = os.path.basename(src)
        dest = filedialog.asksaveasfilename(initialfile=name,
                                            defaultextension=os.path.splitext(name)[1])
        if not dest:
            return
        try:
            urllib.request.urlretrieve(f"http://127.0.0.1:{port}/files/{file_id}", dest)
            messagebox.showinfo("下载完成", f"已保存到：\n{dest}")
        except Exception as exc:
            messagebox.showerror("下载失败", str(exc))

    def open_path(path: str):
        if path and os.path.isfile(path):
            (os.startfile(path) if os.name == "nt"
             else subprocess.Popen(["xdg-open", path]))

    def reveal_path(path: str):
        if path and os.path.isfile(path):
            folder = os.path.dirname(path)
            if os.name == "nt":
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            else:
                subprocess.Popen(["xdg-open", folder])

    def delete_record(file_id: str):
        rec = next((r for r in read_all_meta() if r["id"] == file_id), None)
        if not rec:
            return
        if not messagebox.askyesno("删除任务",
                                   f"仅删除任务记录，不删除文件：\n{rec['name']}\n\n确定删除吗？"):
            return
        try:
            # 直接用元数据模块的路径函数，避免手工按层数剥离目录出错
            # （原先多剥了一层 dirname，拼出的是 f:\meta\xxx.json）
            os.remove(meta_path(file_id))
        except FileNotFoundError:
            pass  # 记录已不存在，等同于删除成功
        except OSError as exc:
            messagebox.showerror("删除失败", str(exc))
            return
        refresh_tree()

    # 单击仅选中高亮；双击不做任何动作
    tree.bind("<Button-3>", on_rec_right_click)
    tree.bind("<Button-2>", on_rec_right_click)
    # ---------------------------------------------------------- 本机上传任务
    tk.Label(root, text="从本机上传（分片 · 断点续传 · 可暂停继续；右键：继续 / 下载 / 暂停 / 删除）",
             bg="#0f1220", fg="#eef1ff",
             font=("微软雅黑", 10, "bold")).pack(anchor="w", padx=14, pady=(8, 2))

    up_frame = ttk.Frame(root)
    up_frame.pack(fill="x", padx=14)
    up_tree = ttk.Treeview(up_frame, columns=("name", "size", "pct", "speed", "state"),
                            show="headings", height=5)
    for col, (title, width) in zip(("name", "size", "pct", "speed", "state"),
                                   (("name", 180), ("size", 85), ("pct", 75),
                                    ("speed", 95), ("state", 95))):
        up_tree.heading(col, text=title)
        up_tree.column(col, width=width, anchor="w")
    up_tree.pack(side="left", fill="both", expand=True)

    tasks = {}          # iid -> {"task": UploadTask, "path": str, "state": str}
    STATE_TEXT = {"running": "上传中", "waiting": "排队中", "paused": "已暂停",
                  "done": "已完成", "failed": "失败", "canceled": "已取消",
                  "interrupted": "已中断"}

    def task_state_text(t):
        if t.status == "running":
            return "上传中"
        if t.status in ("paused",):
            return "已暂停"
        if t.status == "failed":
            return "失败"
        if t.status == "done":
            return "已完成"
        if t.status == "canceled":
            return "已取消"
        return "排队中"

    def pct_text(uploaded, size):
        """双精度百分比，保留两位小数。"""
        if not size:
            return "0.00%"
        return f"{min(100.0, uploaded / size * 100):.2f}%"

    def row_values(rec):
        return (rec["name"], human_size(rec["size"]), rec.get("pct", "0.00%"),
                rec.get("speed", "-"), STATE_TEXT.get(rec.get("state", "waiting"), "排队中"))

    def upsert_row(iid, rec):
        if up_tree.exists(iid):
            up_tree.item(iid, values=row_values(rec))
        else:
            up_tree.insert("", "end", iid=iid, values=row_values(rec))

    def add_task(path: str, state: str = "waiting", uploaded: int = 0):
        """新建（或恢复）一个本机上传任务。"""
        task = UploadTask(path, port)
        iid = task.iid
        tasks[iid] = {"task": task, "path": path, "state": state, "uploaded": uploaded}

        def on_progress(up, size, speed, st):
            root.after(0, lambda: _update_row(iid, up, size, speed, st))
            root.after(0, lambda: persist_tasks())

        task.on_progress = on_progress
        task.on_done = lambda t: root.after(0, refresh_tree)
        upsert_row(iid, {"name": task.name, "size": task.size,
                         "pct": pct_text(uploaded, task.size),
                         "speed": "-", "state": state})
        return task

    def _update_row(iid, uploaded, size, speed, status):
        rec = tasks.get(iid)
        if not rec or not up_tree.exists(iid):
            return
        rec["uploaded"] = uploaded
        rec["state"] = task_state_text_str(status)
        upsert_row(iid, {"name": rec["task"].name, "size": size,
                         "pct": pct_text(uploaded, size),
                         "speed": f"{speed:.1f} MB/s" if status == "running" else "-",
                         "state": rec["state"]})

    def task_state_text_str(status):
        return {"running": "上传中", "paused": "已暂停", "done": "已完成",
                "failed": "失败", "canceled": "已取消"}.get(status, "排队中")

    def persist_tasks():
        """把本机上传任务持久化，重启程序后仍可续传。"""
        data = []
        for iid, rec in tasks.items():
            t = rec["task"]
            if t.status in ("done", "canceled"):
                continue
            data.append({
                "iid": iid, "path": rec["path"], "name": t.name,
                "size": t.size, "mtime": os.path.getmtime(rec["path"]) if os.path.isfile(rec["path"]) else 0,
                "uploaded": max(t.uploaded, rec.get("uploaded", 0)),
                "state": rec["state"], "speed": 0.0,
            })
        save_local_tasks(data)
    # -------- 本机上传任务：右键菜单（任务 5）
    def continue_task(iid: str):
        """按绝对路径自动读取原文件并断点续传（任务 3，程序端不弹选择框）。"""
        rec = tasks.get(iid)
        if not rec:
            return
        path, size, mtime = rec["path"], rec["task"].size, rec.get("mtime", 0)
        if not os.path.isfile(path):
            if not messagebox.askyesno(
                    "原文件不存在",
                    f"找不到：\n{path}\n\n原文件可能已被移动或删除，是否重新选择？"):
                return
            picked = filedialog.askopenfilename(title="重新选择文件")
            if not picked:
                return
            path = picked
        else:
            # 校验大小/修改时间，避免续错文件
            st = os.stat(path)
            if size and st.st_size != size:
                if not messagebox.askyesno("文件已变化",
                        f"文件大小与记录不一致：\n记录 {human_size(size)}\n"
                        f"当前 {human_size(st.st_size)}\n\n是否按新文件重新上传？"):
                    return
        task = add_task(path, state="waiting", uploaded=rec.get("uploaded", 0))
        task.start()

    def download_task(iid: str):
        """下载任务：已完成则下载成品，未完成则提示可下载已接收分片。"""
        rec = tasks.get(iid)
        if not rec:
            return
        name = rec["task"].name
        # 优先在已接收文件里找同名成品
        hit = next((r for r in read_all_meta() if r["name"] == name), None)
        if hit:
            webbrowser.open(f"http://127.0.0.1:{port}/files/{hit['id']}")
            return
        # 未完成：给出已上传分片信息
        pcts = pct_text(rec.get("uploaded", 0), rec["task"].size)
        if messagebox.askyesno("任务未完成",
                f"该任务尚未完成（{pcts}）。\n\n服务端已保存 "
                f"{human_size(rec.get('uploaded', 0))} 分片。\n是否仍要下载已接收的文件列表？"):
            refresh_tree()

    def pause_task(iid: str):
        rec = tasks.get(iid)
        if rec:
            rec["task"].pause()
            _update_row(iid, rec["task"].uploaded, rec["task"].size, 0, "paused")
            persist_tasks()

    def delete_task(iid: str):
        rec = tasks.get(iid)
        if not rec:
            return
        if not messagebox.askyesno("删除任务", f"删除上传任务：\n{rec['task'].name}\n\n仅删除任务记录，不删除原文件。"):
            return
        rec["task"].cancel()
        tasks.pop(iid, None)
        if up_tree.exists(iid):
            up_tree.delete(iid)
        persist_tasks()

    def on_up_right_click(event):
        iid = up_tree.identify_row(event.y)
        if not iid:
            return
        up_tree.selection_set(iid)
        menu = tk.Menu(root, tearoff=0)
        menu.add_command(label="继续执行", command=lambda: continue_task(iid))
        menu.add_command(label="下载", command=lambda: download_task(iid))
        menu.add_separator()
        menu.add_command(label="暂停", command=lambda: pause_task(iid))
        menu.add_command(label="删除任务", command=lambda: delete_task(iid))
        menu.post(event.x_root, event.y_root)

    up_tree.bind("<Button-3>", on_up_right_click)
    up_tree.bind("<Button-2>", on_up_right_click)

    # -------- 启动时恢复历史任务
    for item in load_local_tasks():
        path = item.get("path", "")
        if path and os.path.isfile(path):
            add_task(path, state=item.get("state", "paused"),
                     uploaded=item.get("uploaded", 0))
        elif path:
            upsert_row(item.get("iid", path), {
                "name": item.get("name", os.path.basename(path)),
                "size": item.get("size", 0),
                "pct": pct_text(item.get("uploaded", 0), item.get("size", 0)),
                "speed": "-", "state": "已中断（文件不存在）"})

    def pick_files():
        for path in filedialog.askopenfilenames(title="选择要上传的文件"):
            add_task(path).start()

    def start_selected():
        for iid in up_tree.selection():
            if iid in tasks:
                tasks[iid]["task"].start()
                _update_row(iid, tasks[iid]["task"].uploaded,
                            tasks[iid]["task"].size, 0, "running")

    def pause_selected():
        for iid in up_tree.selection():
            pause_task(iid)

    def cancel_selected():
        for iid in up_tree.selection():
            delete_task(iid)
    # ---------------------------------------------------------- 未完成任务提示
    pending = scan_pending_uploads()
    if pending:
        text = "上次有未完成的上传（网页端可继续）：" + "、".join(
            f"{p['name']} {pct_text(p['offset'], p['size'])}" for p in pending[:3])
        tk.Label(root, text=text + ("…" if len(pending) > 3 else ""),
                 bg="#3a2f14", fg="#ffcc66", font=("微软雅黑", 9),
                 wraplength=850, justify="left").pack(fill="x", padx=14, pady=(6, 0))

    # ---------------------------------------------------------- 底部按钮
    bar = tk.Frame(root, bg="#0f1220")
    bar.pack(pady=8)
    ttk.Button(bar, text="选择文件", command=pick_files).pack(side="left", padx=4)
    ttk.Button(bar, text="开始/继续", command=start_selected).pack(side="left", padx=4)
    ttk.Button(bar, text="暂停", command=pause_selected).pack(side="left", padx=4)
    ttk.Button(bar, text="取消/删除", command=cancel_selected).pack(side="left", padx=4)
    ttk.Button(bar, text="刷新列表", command=refresh_tree).pack(side="left", padx=4)
    ttk.Button(bar, text="打开网页", command=lambda: webbrowser.open(url)).pack(side="left", padx=4)
    ttk.Button(bar, text="打开文件夹",
               command=lambda: os.startfile(os.path.join(os.path.dirname(
                   os.path.abspath(__file__)), "uploads")) if os.name == "nt"
               else subprocess.Popen(["xdg-open", os.path.join(
                   os.path.dirname(os.path.abspath(__file__)), "uploads")])).pack(side="left", padx=4)
    ttk.Button(bar, text="退出", command=root.destroy).pack(side="left", padx=4)

    # ---------------------------------------------------------- 关闭确认
    def has_running_task() -> bool:
        return any(r["task"].status == "running" for r in tasks.values())

    def on_close():
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
        persist_tasks()
        for rec in tasks.values():
            if rec["task"].status == "running":
                rec["task"].pause()
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