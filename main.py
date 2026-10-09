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

# 依赖自举：必须在导入第三方库之前执行。用系统 Python / uv 托管的 Python
# 直接运行本脚本时，会自动接入项目自带 .venv 中的依赖（见 runtime.py 说明）。
import runtime
runtime.ensure_runtime(__file__)
import qrcode  # noqa: E402
import uvicorn  # noqa: E402
from netutils import find_free_port, get_lan_ip, list_lan_ips  # noqa: E402
from store import (LOG_DIR, LOG_MAX_BYTES, LOG_PATH,  # noqa: E402
                   UPLOAD_DIR, create_app, disk_free, human_size)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 打包成 exe 后 __file__ 指向临时解压目录，必须改用 exe 所在目录
if getattr(sys, "frozen", False):
    BASE_DIR: str = os.path.dirname(os.path.abspath(sys.executable))

QR_PATH = os.path.join(BASE_DIR, "lan_qrcode.png")
SERVER_IP = "127.0.0.1"   # 启动后写入真实内网 IP
SERVER_PORT = 8000        # 启动后写入真实端口

# 上传限速下拉可选值（MB/s），0 = 无限制；与网页端的 SPEED_CHOICES 保持一致
SPEED_CHOICES_MB = (0, 1, 2, 5, 10, 20, 50, 100)


def ensure_std_streams():
    """打包成无控制台 exe 后 stdout/stderr 为 None，print 会抛异常，这里兜底。"""
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


class _Tee:
    """把输出同时送到原控制台流与日志文件。

    只接管 write/flush，其余属性（reconfigure / fileno / isatty …）转发给
    原流，避免破坏解释器或第三方库对 stdout 的既有假设。
    """

    def __init__(self, stream, log):
        self._stream = stream
        self._log = log

    def write(self, text):
        try:
            self._stream.write(text)
        except Exception:
            pass
        try:
            self._log.write(text)
        except Exception:
            return 0
        return len(text)

    def flush(self):
        for target in (self._stream, self._log):
            try:
                target.flush()
            except Exception:
                pass

    def __getattr__(self, name):
        return getattr(self._stream, name)


def setup_file_log() -> str:
    """把 stdout/stderr 同时写入 logs/app.log，返回日志路径（失败返回空串）。

    以 --noconsole 打包运行时没有控制台可看，出问题只能靠这份日志排查。
    超过 LOG_MAX_BYTES 时轮转为 app.log.1，只保留一份历史，避免无限增长。
    """
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        if os.path.isfile(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
            os.replace(LOG_PATH, LOG_PATH + ".1")
        # 行缓冲：日志要能实时查看，不能等缓冲区写满
        log = open(LOG_PATH, "a", encoding="utf-8", errors="replace", buffering=1)
    except OSError:
        return ""
    log.write(f"\n{'=' * 60}\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
              f"程序启动 pid={os.getpid()}\n{'=' * 60}\n")
    sys.stdout = _Tee(sys.stdout, log)
    sys.stderr = _Tee(sys.stderr, log)
    return LOG_PATH


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


def sync_treeview(tree, rows) -> None:
    """把 Treeview 内容增量同步为 rows（[(iid, values), ...]），保持顺序与选中状态。

    为什么不能用「先 delete 全部再 insert」
    --------------------------------------
    本窗口每 2 秒刷新一次列表。若每次把行删光重建，用户刚选中的行会被销毁、
    重建后不再处于选中态，表现为"选中后一两秒就自动失去高亮"，右键也因此
    经常点不到目标行。

    这里改为按 iid 做差分：消失的行才删除、已有的行只改数值、顺序用 move
    校正，选中状态与滚动位置都不会被打断。
    """
    wanted = {iid for iid, _ in rows}
    for iid in tree.get_children():
        if iid not in wanted:
            tree.delete(iid)                 # 任务已结束 / 记录已删除，才移除该行
    for index, (iid, values) in enumerate(rows):
        values = tuple(str(v) for v in values)
        if tree.exists(iid):
            if tuple(tree.item(iid, "values")) != values:
                tree.item(iid, values=values)
            if tree.index(iid) != index:
                tree.move(iid, "", index)
        else:
            tree.insert("", index, iid=iid, values=values)


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
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    from PIL import Image, ImageTk

    from store import (LOG_DIR, PARALLEL_LIMIT_MAX, SETTINGS_PATH,
                       SPEED_LIMIT_MIN, UPLOAD_DIR, human_size,
                       list_active_uploads, local_machine_info, meta_path,
                       read_all_meta, scan_pending_uploads)
    from uploader import UploadTask, load_local_tasks, save_local_tasks

    root = tk.Tk()
    root.title("局域网文件传输工具 - 电脑端")
    root.geometry("900x890")
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
    img = img.resize((160, 160), Image.LANCZOS)
    photo = ImageTk.PhotoImage(img)
    tk.Label(root, image=photo, bg="white", bd=0).pack(pady=2)

    # ---------------------------------------------------------- 已接收文件
    tk.Label(root, text="已接收文件（Ctrl / Shift 可多选，按 Delete 删除记录；右键：下载 / 删除）",
             bg="#0f1220", fg="#eef1ff",
             font=("微软雅黑", 10, "bold")).pack(anchor="w", padx=14, pady=(6, 2))

    frame = tk.Frame(root, bg="#0f1220")
    frame.pack(fill="both", expand=True, padx=14)
    cols = ("name", "size", "state", "time", "ip", "pcname", "mac")
    # selectmode="extended"：支持 Ctrl / Shift 多选，配合 Delete 键批量删记录
    tree = ttk.Treeview(frame, columns=cols, show="headings", height=8,
                        selectmode="extended")
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

    # 界面异常提示（不再用 except: pass 静默吞掉刷新异常）
    status_var = tk.StringVar(value="")
    last_error_ts = [0.0]

    def report_gui_error(message: str, once_seconds: float = 15.0):
        """把界面异常显示在窗口底部并打印到控制台（限流，避免每 2 秒刷屏）。"""
        status_var.set("⚠ " + message)
        now = time.time()
        if now - last_error_ts[0] >= once_seconds:
            last_error_ts[0] = now
            print(f"[界面错误] {message}", flush=True)

    _menus: list = []

    def popup_menu(menu, x: int, y: int):
        """弹出右键菜单，并销毁上一次的菜单，避免 Tk 子控件无限累积。"""
        for old in _menus:
            try:
                old.destroy()
            except tk.TclError:
                pass
        _menus[:] = [menu]
        menu.post(x, y)

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
        """刷新「已接收文件」列表（增量同步，不打断用户选中状态）。

        注意必须走 sync_treeview 增量更新：本函数由 tick() 每 2 秒调用一次，
        若每次把行删光重建，用户选中的行会被销毁，表现为"选中后一两秒
        自动失去高亮"。
        """
        rows = []
        # 其他设备正在上传的任务：显示实时进度（其它设备/网页端上传时同步可见）
        try:
            for a in list_active_uploads():
                pct = min(100.0, a["uploaded"] / a["size"] * 100) if a["size"] else 0.0
                who = a.get("client_name") or a.get("client_ip") or "未知设备"
                uid = a["uid"]
                live_info[uid] = a
                mine = is_mine(uid)
                rows.append(("live_" + uid, (
                    a["name"], human_size(a["size"]), f"上传中 {pct:.2f}%",
                    ("本机上传" if mine else "传输中…"),
                    a.get("client_ip") or "-", who, a.get("client_mac") or "-")))
        except Exception as exc:
            report_gui_error(f"刷新传输中列表失败：{exc}")
        for rec in read_all_meta():
            rows.append((rec["id"], (
                rec["name"], human_size(rec["size"]), "已完成", rec["uploaded_at"],
                rec.get("client_ip") or "-", rec.get("client_name") or "-",
                rec.get("client_mac") or "-")))
        sync_treeview(tree, rows)

    def api_call(method: str, path: str, payload=None):
        """调用本机服务接口。

        请求走 127.0.0.1，因此在服务端看来属于"本机"，对本机发起的
        任务拥有操作权限；他人的任务会被 403 拒绝。
        payload 非空时以 JSON 形式发送（用于写设置等接口）。
        """
        import http.client
        import json as _json
        try:
            body = None
            headers = {}
            if payload is not None:
                body = _json.dumps(payload).encode("utf-8")
                headers["Content-Type"] = "application/json"
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            conn.close()
            data = _json.loads(raw.decode("utf-8")) if raw else {}
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

    def stop_local_task_by_uid(uid: str) -> bool:
        """该 uid 若属于本机上传器，就真正停掉它（停线程 + 清服务端分片）。"""
        for iid, rec in list(tasks.items()):
            if rec["task"]._uid() != uid:
                continue
            rec["task"].cancel()
            tasks.pop(iid, None)
            if up_tree.exists(iid):
                up_tree.delete(iid)
            persist_tasks()
            return True
        return False

    def drop_remote(uid: str):
        """删除传输中的任务（清除已上传分片）。

        本机上传器发起的任务必须连本地上传线程一起停掉：只删服务端分片的话，
        上传器遇到 404 会自动重建任务、从头再传一遍，等于没删掉。
        """
        if not messagebox.askyesno("删除任务",
                "删除该上传任务？\n已上传的分片数据也会被清除，且不可恢复。"):
            return
        if stop_local_task_by_uid(uid):
            refresh_tree()
            messagebox.showinfo("已删除", "该上传任务已停止，服务端分片已清除")
            return
        ok, msg = api_call("DELETE", f"/api/pending/{uid}")
        if ok:
            messagebox.showinfo("已删除", "该上传任务已删除")
        else:
            messagebox.showwarning("删除失败", str(msg))
        refresh_tree()

    def on_rec_right_click(event):
        """已接收文件：右键菜单。

        传输中的任务（live_ 前缀）也支持暂停 / 继续 / 删除，
        且**仅允许操作本机发起的任务**；他人任务只读。
        """
        iid = tree.identify_row(event.y)
        if not iid:
            return
        # 右键落在已选中的行上时保留现有多选，否则才把选中项改成该行；
        # 无脑 selection_set 会把用户刚做的多选清成一个。
        if iid not in tree.selection():
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
            popup_menu(menu, event.x_root, event.y_root)
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
        # 与 Delete 键共用同一个入口：都是对「当前选中项」批量删记录
        menu.add_command(label="删除记录（保留文件）", command=delete_selected_records)
        popup_menu(menu, event.x_root, event.y_root)

    def save_as(file_id: str):
        """下载并选择保存位置。

        显式禁用代理：Clash 等系统代理环境下，urllib 可能继承 HTTP_PROXY
        环境变量，把本该走 127.0.0.1 的下载请求发到代理上。
        同时改成流式拷贝，避免 urlretrieve 的临时文件与内存开销。
        """
        import shutil
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
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f"http://127.0.0.1:{port}/files/{file_id}", timeout=60) as resp, \
                    open(dest, "wb") as out:
                shutil.copyfileobj(resp, out, 1024 * 1024)
            messagebox.showinfo("下载完成", f"已保存到：\n{dest}")
        except Exception as exc:
            messagebox.showerror("下载失败", str(exc))

    def open_path(path: str):
        if path and os.path.isfile(path):
            (os.startfile(path) if os.name == "nt"
             else subprocess.Popen(["xdg-open", path]))

    def open_folder(path: str, select: str = ""):
        """在系统文件管理器中打开目录；select 有值且该文件存在时顺便选中它。

        目录不存在会先创建：用户点「日志目录」时可能还没有生成任何日志。
        """
        if not path:
            return
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            pass
        if os.name != "nt":
            subprocess.Popen(["xdg-open", path])
        elif select and os.path.isfile(select):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(select)])
        else:
            os.startfile(path)

    def reveal_path(path: str):
        """在资源管理器中定位并选中该文件。"""
        if path and os.path.isfile(path):
            open_folder(os.path.dirname(path), path)

    def delete_selected_records():
        """批量删除选中的下载记录（只删记录，保留 uploads 里的文件）。

        入口有两个：列表里按 Delete 键，或右键菜单「删除记录（保留文件）」。
        两者共用本函数，行为完全一致。

        传输中的任务（live_ 前缀）不在这里处理：它要么属于本机上传器、
        要么属于其它设备，删掉记录没有意义，请走右键的「删除任务」。
        记录文件缺失（.meta 里已不存在）同样视为删除成功，保证幂等。
        """
        meta = {r["id"]: r for r in read_all_meta()}
        ids = [iid for iid in tree.selection()
               if not iid.startswith("live_") and iid in meta]
        if not ids:
            return
        names = [meta[iid]["name"] for iid in ids]
        preview = "、".join(names[:5]) + ("…" if len(names) > 5 else "")
        if not messagebox.askyesno(
                "删除记录",
                f"仅删除 {len(ids)} 条任务记录，不删除文件：\n{preview}\n\n确定删除吗？"):
            return
        failed = []
        for iid in ids:
            try:
                os.remove(meta_path(iid))
            except FileNotFoundError:
                pass                    # 记录已不存在，等同于删除成功
            except OSError as exc:
                failed.append(f"{meta[iid]['name']}：{exc}")
        refresh_tree()
        if failed:
            messagebox.showerror("部分删除失败", "\n".join(failed[:5]))

    # 单击仅选中高亮；双击不做任何动作；Delete 批量删除选中的记录
    tree.bind("<Button-3>", on_rec_right_click)
    tree.bind("<Button-2>", on_rec_right_click)
    tree.bind("<Delete>", lambda _event: delete_selected_records())
    # ---------------------------------------------------------- 本机上传任务
    # 并行任务数：默认 32（服务端硬上限），改动后写回服务端，网页端 2 秒内同步
    ok, cfg = api_call("GET", "/api/settings")
    parallel_max = PARALLEL_LIMIT_MAX
    parallel = {"value": 0}
    if ok and isinstance(cfg, dict):
        parallel_max = int(cfg.get("limit") or PARALLEL_LIMIT_MAX)
        parallel["value"] = int(cfg.get("max_parallel") or 0)
    if not 1 <= parallel["value"] <= parallel_max:
        parallel["value"] = parallel_max

    # 上传限速：0 = 无限制，非 0 时最小 1 MB/s；与并行数一样写回服务端共用
    speed = {"value": 0}
    if ok and isinstance(cfg, dict):
        speed["value"] = int(cfg.get("speed_limit") or 0)
    speed_items = [(0, "无限制")] + [(n << 20, f"{n} MB/s")
                                     for n in SPEED_CHOICES_MB[1:]]
    speed_by_label = {label: value for value, label in speed_items}
    speed_labels = [label for _value, label in speed_items]
    if speed["value"] not in speed_by_label.values():
        # 服务端存着一个不在下拉列表里的值（例如被接口直接设成 3 MB/s）：
        # 临时补进候选项，免得下拉框显示空白让人以为限速没了
        extra = f"{speed['value'] / 1048576:g} MB/s"
        speed_labels.insert(1, extra)
        speed_by_label[extra] = speed["value"]

    up_head = tk.Frame(root, bg="#0f1220")
    up_head.pack(fill="x", padx=14, pady=(8, 2))
    tk.Label(up_head, text="从本机上传（分片 · 断点续传 · 可暂停继续；右键：继续 / 下载 / 暂停 / 删除）",
             bg="#0f1220", fg="#eef1ff",
             font=("微软雅黑", 10, "bold")).pack(side="left")

    up_opts = tk.Frame(root, bg="#0f1220")
    up_opts.pack(fill="x", padx=14)
    tk.Label(up_opts, text="并行任务数", bg="#0f1220", fg="#9aa3c7",
             font=("微软雅黑", 9)).pack(side="left")
    parallel_box = ttk.Combobox(up_opts, width=4, state="readonly",
                                justify="center",
                                values=[str(n) for n in range(1, parallel_max + 1)])
    parallel_box.set(str(parallel["value"]))
    parallel_box.pack(side="left", padx=(6, 16))
    parallel_box.bind("<<ComboboxSelected>>", lambda _e: on_parallel_change())

    tk.Label(up_opts, text="上传限速", bg="#0f1220", fg="#9aa3c7",
             font=("微软雅黑", 9)).pack(side="left")
    speed_box = ttk.Combobox(up_opts, width=9, state="readonly",
                             justify="center", values=speed_labels)
    speed_box.set(next(label for label, value in speed_by_label.items()
                       if value == speed["value"]))
    speed_box.pack(side="left", padx=(6, 0))
    speed_box.bind("<<ComboboxSelected>>", lambda _e: on_speed_change())

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

    tasks = {}          # iid -> {"task": UploadTask, "path": str, "state": str, ...}
    # 任务状态：内部统一用英文枚举存储（只在显示时翻译成中文），
    # 写盘 / 读盘 / 查表用同一套键，不会再出现"状态永远显示排队中"。
    STATE_TEXT = {"running": "上传中", "waiting": "排队中", "paused": "已暂停",
                  "done": "已完成", "failed": "失败", "canceled": "已取消",
                  "interrupted": "已中断"}
    LEGACY_STATE = {"上传中": "running", "排队中": "waiting", "已暂停": "paused",
                    "已完成": "done", "失败": "failed", "已取消": "canceled"}

    def norm_state(value, default: str = "waiting") -> str:
        """归一化状态：兼容历史版本写下的中文状态，保证旧配置也能正确加载。"""
        if not value:
            return default
        if value in STATE_TEXT:
            return value
        return LEGACY_STATE.get(str(value), default)

    def pct_text(uploaded, size):
        """双精度百分比，保留两位小数。"""
        if not size:
            return "0.00%"
        return f"{min(100.0, uploaded / size * 100):.2f}%"

    def row_values(rec):
        return (rec["name"], human_size(rec["size"]), rec.get("pct", "0.00%"),
                rec.get("speed", "-"),
                STATE_TEXT.get(rec.get("state") or "waiting", "排队中"))

    def upsert_row(iid, rec):
        if up_tree.exists(iid):
            up_tree.item(iid, values=row_values(rec))
        else:
            up_tree.insert("", "end", iid=iid, values=row_values(rec))

    # ---------------------------------------------------------- 并行任务调度
    # 本机上传同样受并行上限约束：超出名额的任务显示「排队中」，
    # 有任务完成 / 暂停 / 删除时自动顶上。数值由上面的下拉框控制，
    # 并写回服务端供网页端同步（见 on_parallel_change）。
    def running_tasks() -> int:
        return sum(1 for r in tasks.values() if r.get("state") == "running")

    def pump_local():
        """按当前上限启动排队中的本机上传任务，先到先传。"""
        for iid, rec in list(tasks.items()):
            if running_tasks() >= parallel["value"]:
                break
            if rec.get("state") != "waiting":
                continue
            rec["state"] = "running"
            rec["task"].start()
            _update_row(iid, rec["task"].uploaded, rec["task"].size, 0, "running")

    def apply_parallel_limit():
        """上限调小：列表靠前的任务保留上传，超出的转回「排队中」。"""
        kept = 0
        for iid, rec in list(tasks.items()):
            state = rec.get("state")
            if state in ("done", "canceled", "failed"):
                continue
            kept += 1
            if kept > parallel["value"] and state == "running":
                rec["task"].pause()
                rec["state"] = "waiting"
                _update_row(iid, rec["task"].uploaded,
                            rec["task"].size, 0, "waiting")
        pump_local()

    def on_parallel_change():
        """下拉框改动：写回服务端（网页端会自动同步），并重排本机队列。

        数值没真正变化时直接返回：避免任何杂散的 <<ComboboxSelected>>
        事件把设置重新写一遍（导致用户看到的数字与实际不符，或产生多余磁盘写）。
        """
        try:
            value = int(parallel_box.get())
        except ValueError:
            return
        value = max(1, min(parallel_max, value))
        if value == parallel["value"]:
            return
        parallel["value"] = value
        ok, msg = api_call("POST", "/api/settings",
                           {"max_parallel": parallel["value"]})
        apply_parallel_limit()
        status_var.set(f"并行任务数已设为 {parallel['value']}，网页端会自动同步"
                       if ok else f"⚠ 并行任务数未能同步到服务端：{msg}")

    def on_speed_change():
        """限速下拉框改动：写回服务端（网页端会自动同步），并作用于全部本机任务。

        正在上传的任务也会立即生效——限速值由发送循环每轮读取，
        不需要停下来重启任务。数值没变化时直接返回，避免重复写盘。
        """
        value = speed_by_label.get(speed_box.get())
        if value is None or value == speed["value"]:
            return
        speed["value"] = value
        ok, msg = api_call("POST", "/api/settings", {"speed_limit": value})
        for rec in tasks.values():
            rec["task"].limit_bps = value
        status_var.set(f"上传限速已设为 {speed_box.get()}，网页端会自动同步"
                       if ok else f"⚠ 上传限速未能同步到服务端：{msg}")

    def sync_settings_from_server():
        """跟随服务端设置：网页端改过并行数 / 限速时，程序端也要跟着变。

        与网页端每 2 秒轮询 /api/settings 对称。缺了这一步，两端各持一份
        副本，会出现"程序端写着 32、实际按 2 跑"这类不一致。
        """
        ok, cfg = api_call("GET", "/api/settings")
        if not ok or not isinstance(cfg, dict):
            return
        value = int(cfg.get("max_parallel") or 0)
        limit = int(cfg.get("speed_limit") or 0)
        if value and value != parallel["value"]:
            parallel["value"] = max(1, min(parallel_max, value))
            parallel_box.set(str(parallel["value"]))
            apply_parallel_limit()
            status_var.set(f"并行任务数已同步为 {parallel['value']}")
        if limit != speed["value"]:
            speed["value"] = limit
            for rec in tasks.values():
                rec["task"].limit_bps = limit
            label = next((lbl for lbl, val in speed_by_label.items()
                          if val == limit), None)
            if label is None:
                label = f"{limit / 1048576:g} MB/s"
                speed_by_label[label] = limit
                speed_box.configure(values=list(speed_by_label))
            speed_box.set(label)
            status_var.set(f"上传限速已同步为 {label}")

    def add_task(path: str, state: str = "waiting", uploaded: int = 0, iid: str = ""):
        """新建（或恢复）一个本机上传任务；传入 iid 表示复用原行，不新增条目。"""
        task = UploadTask(path, port)
        task.limit_bps = speed["value"]      # 新建任务沿用当前的限速设置
        iid = iid or task.iid
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0.0
        tasks[iid] = {"task": task, "path": path, "state": norm_state(state),
                      "uploaded": uploaded, "mtime": mtime}

        def on_progress(up, size, speed, st):
            root.after(0, lambda: _update_row(iid, up, size, speed, st))
            root.after(0, lambda: persist_tasks())

        task.on_progress = on_progress
        # 任务结束后让出名额，排队的任务顶上
        task.on_done = lambda t: (root.after(0, refresh_tree),
                                  root.after(0, pump_local))
        upsert_row(iid, {"name": task.name, "size": task.size,
                         "pct": pct_text(uploaded, task.size),
                         "speed": "-", "state": norm_state(state)})
        return task

    def _update_row(iid, uploaded, size, speed, status):
        """刷新一行任务：状态按英文枚举写入，显示时再翻译成中文。"""
        rec = tasks.get(iid)
        if not rec or not up_tree.exists(iid):
            return
        status = norm_state(status, "waiting")
        rec["uploaded"] = uploaded
        rec["state"] = status
        upsert_row(iid, {"name": rec["task"].name, "size": size,
                         "pct": pct_text(uploaded, size),
                         "speed": f"{speed:.1f} MB/s" if status == "running" else "-",
                         "state": status})
        # 失败必须让用户看见（否则界面只显示"排队中"，错误被静默吞掉）
        if status == "failed" and not rec.get("error_shown"):
            rec["error_shown"] = True
            messagebox.showerror("上传失败",
                                 f"{rec['task'].name}\n\n{rec['task'].error or '未知错误'}")
        # 任务一旦不再占用名额（完成 / 失败 / 暂停 / 取消），让排队的任务顶上
        if status != "running":
            root.after(0, pump_local)

    def persist_tasks():
        """把本机上传任务持久化，重启程序后仍可续传（状态存英文枚举）。"""
        data = []
        for iid, rec in tasks.items():
            t = rec["task"]
            if t.status in ("done", "canceled"):
                continue
            data.append({
                "iid": iid, "path": rec["path"], "name": t.name,
                "size": t.size,
                "mtime": os.path.getmtime(rec["path"]) if os.path.isfile(rec["path"]) else 0,
                "uploaded": max(t.uploaded, rec.get("uploaded", 0)),
                "state": norm_state(rec.get("state"), "paused"), "speed": 0.0,
            })
        save_local_tasks(data)
    # -------- 本机上传任务：右键菜单
    def continue_task(iid: str):
        """继续执行：复用同一行、同一个任务对象续传，不再新增任务行。

        - 文件在、大小未变 → 直接 start()，沿用原 uid 与服务端断点；
        - 文件被移动/删除，或大小已变 → 用户确认后替换本行任务（不新增条目）；
        - 只有修改时间变化 → 提示后仍可继续。
        """
        rec = tasks.get(iid)
        if not rec:
            return
        task = rec["task"]
        path = rec["path"]
        if not os.path.isfile(path):
            if not messagebox.askyesno(
                    "原文件不存在",
                    f"找不到：\n{path}\n\n原文件可能已被移动或删除，是否重新选择？"):
                return
            picked = filedialog.askopenfilename(title="重新选择文件")
            if not picked:
                return
            if task.status == "running":
                task.pause()
            tasks.pop(iid, None)
            add_task(picked, state="waiting", uploaded=0, iid=iid)
            pump_local()
            return
        st = os.stat(path)
        if task.size and st.st_size != task.size:
            if not messagebox.askyesno("文件已变化",
                    f"文件大小与记录不一致：\n记录 {human_size(task.size)}\n"
                    f"当前 {human_size(st.st_size)}\n\n是否按新文件重新上传？"):
                return
            if task.status == "running":
                task.pause()
            tasks.pop(iid, None)
            add_task(path, state="waiting", uploaded=0, iid=iid)
            pump_local()
            return
        if rec.get("mtime") and abs(st.st_mtime - rec["mtime"]) > 1:
            if not messagebox.askyesno(
                    "文件已被修改",
                    "文件修改时间与记录不一致，可能不是同一个文件。\n\n"
                    "仍要从断点继续上传吗？"):
                return
        if rec.get("state") == "running":
            return                      # 本来就在传，避免重复启动
        rec["state"] = "waiting"
        _update_row(iid, task.uploaded, task.size, 0, "waiting")
        pump_local()                    # 有名额立刻开始，否则排队等下一位

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
        pump_local()                    # 名额空出来，让排队的任务顶上

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
        popup_menu(menu, event.x_root, event.y_root)

    up_tree.bind("<Button-3>", on_up_right_click)
    up_tree.bind("<Button-2>", on_up_right_click)

    # -------- 启动时恢复历史任务
    for item in load_local_tasks():
        path = item.get("path", "")
        if path and os.path.isfile(path):
            # 上次退出时还在上传的任务，恢复后一律停在「已暂停」，
            # 由用户点「继续」重新入队，避免程序一启动就偷偷跑满带宽
            state = norm_state(item.get("state"), "paused")
            add_task(path, state="paused" if state == "running" else state,
                     uploaded=item.get("uploaded", 0))
        elif path:
            upsert_row(item.get("iid", path), {
                "name": item.get("name", os.path.basename(path)),
                "size": item.get("size", 0),
                "pct": pct_text(item.get("uploaded", 0), item.get("size", 0)),
                "speed": "-", "state": "已中断（文件不存在）"})

    def pick_files():
        for path in filedialog.askopenfilenames(title="选择要上传的文件"):
            add_task(path)
        pump_local()          # 按并行上限放行，超出的显示「排队中」

    def start_selected():
        """「开始/继续」：选中项回到队列，由调度器按名额启动。"""
        for iid in up_tree.selection():
            rec = tasks.get(iid)
            if rec and rec.get("state") != "running":
                rec["state"] = "waiting"
                _update_row(iid, rec["task"].uploaded,
                            rec["task"].size, 0, "waiting")
        pump_local()

    def pause_selected():
        for iid in up_tree.selection():
            pause_task(iid)

    def cancel_selected():
        for iid in up_tree.selection():
            delete_task(iid)
    # ---------------------------------------------------------- 未完成任务提示
    # include_active=True：正在传输中的任务也要列出来，否则只查"已停止"的
    # 那些，会出现任务在两个列表里都查不到的错觉
    pending = scan_pending_uploads(include_active=True)
    if pending:
        names = "、".join(f"{p['name']} {pct_text(p['offset'], p['size'])}"
                          for p in pending)
        tk.Label(root, text=f"共 {len(pending)} 个未完成的上传（网页端或本机可继续）：{names}",
                 bg="#3a2f14", fg="#ffcc66", font=("微软雅黑", 9),
                 wraplength=850, justify="left").pack(fill="x", padx=14, pady=(6, 0))

    # ---------------------------------------------------------- 底部按钮
    # 界面异常提示条（刷新出错、上传失败等会在这里显示，不再静默吞掉）
    tk.Label(root, textvariable=status_var, bg="#0f1220", fg="#ffcc66",
             font=("微软雅黑", 9), anchor="w", justify="left",
             wraplength=860).pack(fill="x", padx=14)

    def open_upload_dir():
        """打开接收目录。

        必须用 store.UPLOAD_DIR：打包成 exe 后 __file__ 指向 PyInstaller 的
        临时解压目录，会打开一个不存在的位置。
        """
        open_folder(UPLOAD_DIR)

    # ---------------------------------------------------------- 目录链接
    # 直接显示真实路径并可点击打开；配置文件（.settings.json）与接收到的
    # 文件同在接收目录，点「配置文件」会在资源管理器中选中它。
    tk.Label(root, text="目录（点击即可打开）", bg="#0f1220", fg="#9aa3c7",
             font=("微软雅黑", 9)).pack(anchor="w", padx=14, pady=(4, 0))
    dirs_row = tk.Frame(root, bg="#0f1220")
    dirs_row.pack(fill="x", padx=14)
    for caption, shown, folder, select in (
            ("接收 / 配置目录", UPLOAD_DIR, UPLOAD_DIR, ""),
            ("配置文件", SETTINGS_PATH, UPLOAD_DIR, SETTINGS_PATH),
            ("日志", LOG_PATH, LOG_DIR, "")):
        link = tk.Label(dirs_row, text=f"{caption}：{shown}", bg="#0f1220",
                        fg="#8fb0ff", font=("微软雅黑", 9, "underline"),
                        cursor="hand2", anchor="w")
        link.pack(anchor="w")
        link.bind("<Button-1>",
                  lambda _e, f=folder, s=select: open_folder(f, s))

    bar = tk.Frame(root, bg="#0f1220")
    bar.pack(pady=8)
    ttk.Button(bar, text="选择文件", command=pick_files).pack(side="left", padx=4)
    ttk.Button(bar, text="开始/继续", command=start_selected).pack(side="left", padx=4)
    ttk.Button(bar, text="暂停", command=pause_selected).pack(side="left", padx=4)
    ttk.Button(bar, text="取消/删除", command=cancel_selected).pack(side="left", padx=4)
    ttk.Button(bar, text="刷新列表", command=refresh_tree).pack(side="left", padx=4)
    ttk.Button(bar, text="打开网页", command=lambda: webbrowser.open(url)).pack(side="left", padx=4)
    ttk.Button(bar, text="打开文件夹", command=open_upload_dir).pack(side="left", padx=4)
    # 「退出」必须走完整关闭流程（确认弹窗 + 暂停任务 + 持久化），
    # 直接 root.destroy() 会跳过全部收尾动作
    ttk.Button(bar, text="退出", command=lambda: on_close()).pack(side="left", padx=4)

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
        """定时刷新「已接收文件」列表。

        必须保证任何情况下都能重新排期：早期直接调用 refresh_tree，
        一旦它抛异常（例如 uploads\\.meta 被删掉），下面的 root.after 就
        执行不到，自动刷新会**永久停摆**——表现出来就是"本地文件删了，
        客户端列表也不再更新"。
        """
        try:
            refresh_tree()
            sync_settings_from_server()
        except Exception as exc:
            report_gui_error(f"刷新界面失败：{exc}")
        finally:
            root.after(2000, tick)

    try:
        refresh_tree()
    except Exception as exc:
        report_gui_error(f"首次读取文件列表失败：{exc}")
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
    # 之后所有 print 都会同时落到 logs/app.log，便于无控制台时排查
    log_path = setup_file_log()

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
    print(f"  日志文件   : {log_path or '（写入失败，日志不可用）'}")
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