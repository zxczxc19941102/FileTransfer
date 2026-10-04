"""
局域网文件互传工具（电脑端服务程序）
==================================

功能概述：
    1. 电脑端运行本程序，自动获取本机局域网 IP、随机分配可用端口，启动本地 HTTP 文件服务；
    2. 自动生成二维码（内容为 http://内网IP:端口），在 tkinter 窗口展示（无 tkinter 时打印到控制台）；
    3. 手机扫码打开网页即可上传文件（支持多文件、带上传进度）；
    4. 上传完成后自动生成局域网下载链接；
    5. 网页端展示所有设备上传的全部文件，点击文件即触发下载；
    6. 全部通信仅发生在局域网内，不做公网穿透、不使用任何第三方服务器。

技术栈：FastAPI + uvicorn + qrcode（+ tkinter 简易 GUI），前端页面内嵌在本文件中。

安装依赖：pip install -r requirements.txt
运行：    python main.py            # 自动 IP + 随机端口 + GUI
          python main.py --port 8080 --no-gui
"""

import argparse
import os
import random
import re
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from urllib.parse import quote

import qrcode
import uvicorn
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from starlette.concurrency import run_in_threadpool

# ==========================================================================
# 一、全局配置
# ==========================================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))  # 程序所在目录
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")  # 文件保存目录（需求：同目录 uploads）
QR_PATH = os.path.join(BASE_DIR, "lan_qrcode.png")  # 二维码图片路径
MAX_UPLOAD_MB = 2048  # 单文件大小上限（MB），防止超大文件占用磁盘
CHUNK = 1024 * 1024  # 读写块大小 1MB

FILE_RECORDS: dict = {}  # 已上传文件记录：{文件ID: 文件信息}
RECORD_LOCK = threading.Lock()  # 多设备并发上传时保护记录表
ILLEGAL_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')  # Windows 非法文件名字符

# ==========================================================================
# 二、前端网页（内嵌 HTML，不依赖任何前端工程，手机 / 电脑同一页面）
# ==========================================================================

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>局域网文件互传</title>
<style>
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;
     background:#0f1220;color:#eef1ff}
.wrap{max-width:900px;margin:0 auto;padding:18px 14px 50px}
h1{font-size:20px;margin:0 0 4px}
.sub{color:#98a1c4;font-size:13px;margin:0 0 16px}
.card{background:#181c30;border:1px solid #2b3152;border-radius:12px;padding:16px;margin-bottom:14px}
.box{border:2px dashed #3a4372;border-radius:12px;padding:26px 14px;text-align:center;cursor:pointer}
.box:hover{background:#1d2338}
.row{display:flex;align-items:center;gap:10px;padding:10px;border-radius:10px;background:#1e2338;margin-top:9px}
.row .n{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:14px}
.row .s{color:#98a1c4;font-size:12px}
.bar{height:6px;background:#2b3152;border-radius:6px;overflow:hidden;margin-top:6px}
.bar>i{display:block;height:100%;width:0;background:#3f7cff;transition:width .2s}
a{color:#8fb0ff}
table{width:100%;border-collapse:collapse}
th,td{padding:9px 6px;border-bottom:1px solid #2b3152;font-size:13.5px;text-align:left}
th{color:#98a1c4;font-size:12px;font-weight:600}
tr{cursor:pointer}
tr:hover{background:#1e2338}
.empty{color:#98a1c4;text-align:center;padding:24px 0;font-size:13px}
</style>
</head>
<body>
<div class="wrap">
  <h1>局域网文件互传</h1>
  <p class="sub" id="info">正在连接电脑端服务…</p>

  <!-- 上传区：手机 / 电脑均可使用 -->
  <div class="card">
    <div class="box" id="box">
      <div style="font-size:34px">&#128196;</div>
      <div style="margin-top:8px">点击选择文件上传（可多选）</div>
      <div class="s" id="tip">单个文件上限 __MAX__ MB</div>
    </div>
    <input type="file" id="pick" multiple hidden>
    <div id="up"></div>
  </div>

  <!-- 全部已上传文件列表（所有设备上传的文件都在这里） -->
  <div class="card">
    <b>全部文件（点击任意一行即可下载）</b>
    <table>
      <thead><tr><th>文件名</th><th>大小</th><th>接收时间</th><th>来源</th></tr></thead>
      <tbody id="rows"></tbody>
    </table>
    <div class="empty" id="empty">暂无文件</div>
  </div>
</div>

<script>
const pick = document.getElementById('pick'), box = document.getElementById('box');
pick.onchange = () => { [...pick.files].forEach(upload); pick.value = ''; };
box.onclick = () => pick.click();

function fmt(n){
  const u = ['B','KB','MB','GB'];
  let i = 0;
  while(n >= 1024 && i < u.length-1){ n /= 1024; i++; }
  return (i ? n.toFixed(1) : n) + ' ' + u[i];
}

/* 上传单个文件：XHR 以显示真实进度 */
function upload(f){
  const row = document.createElement('div');
  row.className = 'row';
  row.innerHTML = '<div class="n">' + f.name + '</div><div class="s">0%</div>'
                + '<div class="bar"><i></i></div>';
  document.getElementById('up').appendChild(row);
  const s = row.querySelector('.s'), bar = row.querySelector('.bar > i');

  const fd = new FormData();
  fd.append('file', f, f.name);
  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/api/upload');
  xhr.upload.onprogress = (e) => {
    if (!e.lengthComputable) return;
    const p = e.loaded / e.total * 100;
    bar.style.width = p + '%';
    s.textContent = p.toFixed(0) + '%';
  };
  xhr.onload = () => {
    try {
      const d = JSON.parse(xhr.responseText);
      if (d.ok) { s.textContent = '完成'; refresh(); }
      else s.textContent = '失败：' + (d.error || xhr.status);
    } catch (e) { s.textContent = '失败'; }
  };
  xhr.onerror = () => { s.textContent = '失败：网络错误'; };
  xhr.send(fd);
}

/* 刷新文件列表 */
async function refresh(){
  const r = await fetch('/api/files');
  const d = await r.json();
  document.getElementById('rows').innerHTML = d.files.map(f =>
    '<tr onclick="location.href=\'/files/' + encodeURIComponent(f.id) + '\'">'
    + '<td>' + esc(f.name) + '</td><td>' + fmt(f.size) + '</td>'
    + '<td>' + f.time + '</td><td>' + (f.from || '-') + '</td></tr>').join('');
  document.getElementById('empty').style.display = d.files.length ? 'none' : 'block';
  document.getElementById('info').textContent =
    '已接收 ' + d.files.length + ' 个文件，共 ' + fmt(d.total) + '　|　电脑地址 ' + location.origin;
}

function esc(s){
  return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}
refresh();
setInterval(refresh, 3000);   // 每 3 秒自动刷新，新文件刷新页面即可看到
</script>
</body>
</html>
"""

# ==========================================================================
# 三、工具函数：内网 IP / 端口 / 二维码
# ==========================================================================


def get_lan_ip() -> str:
    """自动获取本机局域网 IP（无需用户填写）。

    原理：UDP 套接字"连接"到公网地址时不会真正发包，但操作系统会按路由表
    选出将要使用的网卡，从而拿到该网卡的 IP；失败时再回退到网卡枚举。
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.settimeout(0.3)
        probe.connect(("8.8.8.8", 80))
        ip = probe.getsockname()[0]
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    finally:
        probe.close()
    for ip in list_lan_ips():  # 回退：枚举网卡
        return ip
    return "127.0.0.1"


def list_lan_ips() -> list:
    """枚举所有可用于局域网访问的 IPv4（私有网段优先，多网卡/有线+WiFi 都能列出）。"""
    found = []

    def add(ip):
        if ip and not ip.startswith("127.") and ip not in found:
            found.append(ip)

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            add(info[4][0])
    except OSError:
        pass
    if sys.platform == "win32":  # Windows 下 ipconfig 结果最可靠
        try:
            out = subprocess.run(["ipconfig"], capture_output=True, text=True,
                                 timeout=8, creationflags=0x08000000).stdout
            for line in out.splitlines():
                if "IPv4" in line:
                    part = line.split(":")[-1].strip()
                    if part[:1].isdigit():
                        add(part)
        except Exception:
            pass
    else:
        try:
            out = subprocess.run(["hostname", "-I"], capture_output=True,
                                 text=True, timeout=5).stdout
            for ip in out.split():
                add(ip)
        except Exception:
            pass
    found.sort(key=lambda ip: 0 if ip.startswith(("192.168.", "10.", "172.")) else 1)
    return found


def find_free_port(preferred: int = 0) -> int:
    """自动寻找可用端口：指定端口被占用则提示；未指定则在高位端口中随机分配。"""
    if preferred:
        if is_port_free(preferred):
            return preferred
        raise SystemExit(f"[错误] 端口 {preferred} 已被占用，请换一个端口或去掉 --port 参数。")
    for _ in range(200):
        port = random.randint(20000, 60000)
        if is_port_free(port):
            return port
    sock = socket.socket()  # 兜底：让系统分配
    sock.bind(("", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def is_port_free(port: int) -> bool:
    """判断端口是否可被本程序占用。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


# ==========================================================================
# 四、文件记录与存储
# ==========================================================================


def clean_name(name: str) -> str:
    """清洗上传文件名：去掉路径与非法字符，保留中文。"""
    name = os.path.basename(name or "").strip().strip(".")
    name = ILLEGAL_NAME.sub("_", name)
    if len(name) > 100:
        stem, ext = os.path.splitext(name)
        name = stem[: 100 - len(ext)] + ext
    return name or "unnamed"


def build_record(path: str, source: str = "") -> dict:
    """为已保存的文件生成一条记录（ID = 8位随机串_文件名，方便识别）。"""
    stat = os.stat(path)
    name = os.path.basename(path)
    return {
        "id": f"{random_hex(4)}_{name}",
        "name": name,
        "path": path,
        "size": stat.st_size,
        "time": datetime.fromtimestamp(stat.st_mtime).strftime("%m-%d %H:%M:%S"),
        "from": source,
    }


def random_hex(length: int = 4) -> str:
    """生成指定字节数的随机十六进制字符串（用于文件 ID）。"""
    return os.urandom(length).hex()


def load_existing_files() -> list:
    """扫描 uploads 目录，把历史文件补进记录表（程序重启后列表不丢失）。"""
    loaded = []
    for name in sorted(os.listdir(UPLOAD_DIR)):
        path = os.path.join(UPLOAD_DIR, name)
        if os.path.isfile(path):
            loaded.append(build_record(path, source="历史文件"))
    return loaded


async def save_upload(upload: UploadFile, source: str) -> dict:
    """把上传流分块写入磁盘，超过大小上限立即中止并删除半成品。

    使用分块读写而不是一次读入内存，几 GB 的大文件也不会把内存撑爆。
    """
    filename = clean_name(upload.filename)
    # 同名文件自动加序号，避免互相覆盖
    final_name, seq = filename, 1
    while os.path.exists(os.path.join(UPLOAD_DIR, final_name)):
        stem, ext = os.path.splitext(filename)
        final_name = f"{stem}({seq}){ext}"
        seq += 1
    path = os.path.join(UPLOAD_DIR, final_name)

    limit = MAX_UPLOAD_MB * 1024 * 1024
    written = 0
    try:
        with open(path, "wb") as fp:
            while True:
                block = await upload.read(CHUNK)  # 每次 1MB
                if not block:
                    break
                written += len(block)
                if written > limit:
                    raise ValueError(f"文件超过 {MAX_UPLOAD_MB} MB 上限")
                await run_in_threadpool(fp.write, block)
    except Exception:
        if os.path.exists(path):  # 出错时清理未完成的文件
            os.remove(path)
        raise
    finally:
        await upload.close()

    record = build_record(path, source)
    with RECORD_LOCK:
        FILE_RECORDS[record["id"]] = record
    print(f"[接收] {final_name}  {human_size(written)}  来自 {source}")
    return record


def human_size(num: float) -> str:
    """把字节数转换为易读文本。"""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024 or unit == "TB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"


# ==========================================================================
# 五、后端 HTTP 服务（FastAPI）
# ==========================================================================

app = FastAPI(title="局域网文件互传工具", docs_url=None, redoc_url=None)


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    """首页：上传表单 + 全部文件列表（手机与电脑看到的是同一个页面）。"""
    return HTML_PAGE.replace("__MAX__", str(MAX_UPLOAD_MB))


@app.post("/api/upload")
async def api_upload(request: Request, file: UploadFile = File(...)):
    """上传接口：支持任意设备同时上传，完成后返回局域网下载链接。"""
    source = request.client.host if request.client else "未知设备"
    try:
        record = await save_upload(file, source)
    except ValueError as exc:  # 超过大小上限
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    link = download_url(record)  # 自动生成局域网下载链接
    print(f"[链接] {link}")
    return {"ok": True, "file": record, "link": link}


@app.get("/api/files")
async def api_files(request: Request):
    """文件列表接口：返回所有设备上传的全部文件。"""
    with RECORD_LOCK:
        files = sorted(FILE_RECORDS.values(), key=lambda r: r["time"], reverse=True)
        total = sum(f["size"] for f in files)
    base = f"http://{request.url.hostname}:{request.url.port or 80}"
    for item in files:  # 补上下载链接，方便其它设备直接使用
        item["link"] = f"{base}/files/{quote(item['id'], safe='')}"
    return {"files": files, "total": total, "count": len(files)}


@app.get("/files/{file_id}")
async def download(file_id: str):
    """下载接口：点击文件即触发下载，响应头 attachment 让浏览器直接保存。"""
    record = FILE_RECORDS.get(file_id)
    if not record or not os.path.isfile(record["path"]):
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(record["path"], filename=record["name"], media_type="application/octet-stream")


def download_url(record: dict) -> str:
    """用本机局域网 IP 拼出可分享的下载链接（文件名做 URL 编码，兼容中文与特殊字符）。"""
    return f"http://{SERVER_IP}:{SERVER_PORT}/files/{quote(record['id'], safe='')}"


# ==========================================================================
# 六、二维码生成
# ==========================================================================


def make_qrcode(text: str) -> str:
    """生成二维码图片并返回路径（保存到程序目录，方便 GUI 显示）。"""
    qr = qrcode.QRCode(box_size=8, border=2)  # 容错等级默认 M，扫码更稳
    qr.add_data(text)
    qr.make(fit=True)
    qr.make_image(fill_color="black", back_color="white").save(QR_PATH)
    return QR_PATH


def print_qrcode_text(url: str) -> None:
    """在控制台用字符打印二维码（GUI 不可用时的备选方案）。"""
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    try:
        qr.print_ascii(invert=True)
    except UnicodeEncodeError:  # 老终端编码不支持时忽略
        pass


# ==========================================================================
# 七、tkinter 简易窗口（二维码 + IP/端口 + 文件列表）
# ==========================================================================


def run_gui(url: str, qr_path: str, port: int, server) -> None:
    """电脑端窗口：展示二维码图片、局域网地址，以及所有已上传文件（双击下载）。"""
    import tkinter as tk
    from PIL import Image, ImageTk
    from tkinter import ttk

    root = tk.Tk()
    root.title("局域网文件互传工具 - 电脑端")
    root.geometry("560x640")
    root.configure(bg="#0f1220")

    # ---- 顶部信息 ----
    tk.Label(root, text="局域网文件互传工具", bg="#0f1220", fg="#eef1ff",
             font=("微软雅黑", 14, "bold")).pack(pady=(14, 2))
    tk.Label(root, text=f"手机与电脑需连接同一 WiFi，扫码访问：{url}",
             bg="#0f1220", fg="#9aa3c7", font=("微软雅黑", 9)).pack()

    # ---- 二维码图片 ----
    img = Image.open(qr_path)
    img = img.resize((220, 220), Image.LANCZOS)
    photo = ImageTk.PhotoImage(img)
    tk.Label(root, image=photo, bg="white", bd=0).pack(pady=10)

    # ---- 文件列表 ----
    tk.Label(root, text="已接收文件（双击下载到本机）", bg="#0f1220", fg="#eef1ff",
             font=("微软雅黑", 10, "bold")).pack(anchor="w", padx=16, pady=(4, 2))
    frame = tk.Frame(root, bg="#0f1220")
    frame.pack(fill="both", expand=True, padx=16)
    cols = ("name", "size", "time", "from")
    tree = ttk.Treeview(frame, columns=cols, show="headings", height=12)
    for col, title, width in zip(cols, ("文件名", "大小", "接收时间", "来源"), (240, 80, 110, 100)):
        tree.heading(col, text=title)
        tree.column(col, width=width, anchor="w")
    scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=scroll.set)
    tree.pack(side="left", fill="both", expand=True)
    scroll.pack(side="right", fill="y")

    def refresh_tree():
        """定时刷新列表，新上传的文件自动出现。"""
        for item in tree.get_children():
            tree.delete(item)
        with RECORD_LOCK:
            records = sorted(FILE_RECORDS.values(), key=lambda r: r["time"], reverse=True)
        for rec in records:
            tree.insert("", "end", values=(rec["name"], human_size(rec["size"]),
                                           rec["time"], rec["from"]))

    def on_double_click(_event):
        """双击某行即在本机浏览器下载该文件。"""
        selected = tree.selection()
        if selected:
            rec = FILE_RECORDS.get(tree.item(selected[0], "values")[0])
            if rec:
                webbrowser.open(f"http://127.0.0.1:{port}/files/{rec['id']}")

    tree.bind("<Double-1>", on_double_click)

    # ---- 底部按钮 ----
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
        """每 1.5 秒刷新一次列表（等价于页面自动刷新）。"""
        refresh_tree()
        root.after(1500, tick)

    root.after(1500, tick)
    root.mainloop()


# ==========================================================================
# 八、程序入口：启动服务 + 二维码 + GUI，退出时自动关闭服务
# ==========================================================================

SERVER_IP = "127.0.0.1"  # 启动后写入真实内网 IP，供生成下载链接使用
SERVER_PORT = 8000  # 启动后写入真实端口


def start_server(host: str, port: int):
    """在后台线程启动 uvicorn 服务，返回 server 对象以便优雅关闭。"""
    config = uvicorn.Config(app, host=host, port=port, log_level="warning",
                            access_log=False, timeout_keep_alive=30)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True, name="http-server")
    thread.start()
    return server, thread


def main() -> None:
    global SERVER_IP, SERVER_PORT, MAX_UPLOAD_MB

    # Windows 控制台按 UTF-8 输出，避免中文乱码
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="局域网文件互传工具（电脑端服务程序）")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址，默认 0.0.0.0（所有网卡）")
    parser.add_argument("--port", type=int, default=0, help="端口，默认 0 = 随机分配空闲端口")
    parser.add_argument("--max-size", type=int, default=MAX_UPLOAD_MB, help="单文件大小上限 MB")
    parser.add_argument("--no-gui", action="store_true", help="不打开窗口，仅控制台输出二维码")
    parser.add_argument("--no-browser", action="store_true", help="启动时不自动打开浏览器")
    args = parser.parse_args()
    MAX_UPLOAD_MB = max(1, args.max_size)

    # 1. 准备接收目录（不存在则自动创建）
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    # 2. 恢复历史文件记录
    for rec in load_existing_files():
        FILE_RECORDS[rec["id"]] = rec

    # 3. 自动分配端口 + 自动获取局域网 IP
    SERVER_PORT = find_free_port(args.port)
    SERVER_IP = get_lan_ip()
    url = f"http://{SERVER_IP}:{SERVER_PORT}"

    # 4. 生成二维码（内容为 http://内网IP:端口）
    qr_path = make_qrcode(url)

    print("=" * 58)
    print("  局域网文件互传工具已启动")
    print("=" * 58)
    print(f"  局域网地址 : {url}")
    print(f"  上传/列表  : {url}/")
    print(f"  文件保存目录: {UPLOAD_DIR}")
    print(f"  单文件上限  : {MAX_UPLOAD_MB} MB")
    others = [ip for ip in list_lan_ips() if ip != SERVER_IP]
    if others:
        print(f"  其它网卡 IP: {', '.join(others)}（若手机打不开可换这些 IP 访问）")
    print("-" * 58)
    print("  手机连接同一 WiFi 后，用相机扫描窗口中的二维码即可上传文件。")
    if args.no_gui:
        print()
        print_qrcode_text(url)
    print("  关闭窗口或按 Ctrl+C 即可停止服务。")
    print("=" * 58)

    # 5. 启动 HTTP 服务（后台线程）
    server, thread = start_server(args.host, SERVER_PORT)
    time.sleep(0.6)  # 等待端口就绪
    if not getattr(server, "started", False):
        print(f"[错误] 服务启动失败，端口可能被占用：{args.host}:{SERVER_PORT}")

    if not args.no_browser:
        threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()

    # 6. 显示 GUI（无 tkinter 或 --no-gui 时停留在控制台）
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
            while True:  # 前台等待，直到 Ctrl+C
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n正在关闭服务…")

    # 7. 退出时自动关闭 web 服务
    server.should_exit = True
    thread.join(timeout=5)
    print("服务已停止。")


if __name__ == "__main__":
    main()


