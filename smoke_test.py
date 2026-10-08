"""冒烟测试：全链路验证局域网文件传输工具能否正常启动与工作。

覆盖范围
--------
1. 服务启动（真实入口 main.py，无 GUI / 无浏览器）
2. 页面与所有读接口（/、/api/files、/api/space、/api/pending、/api/active、
   /api/machine、/api/device、/verify）
3. TUS 分片上传：创建 → 多分片 PATCH → HEAD 查偏移 → 完成 → SHA256 落库
4. 断点续传：传一半后 HEAD 偏移正确、/api/pending 能查到断点，再续传完成
5. 下载：/files/{id} 流式下载后与源文件 SHA256 逐字节一致
6. 控制接口：/api/active 心跳、/api/pause、/api/resume、DELETE /api/pending
7. 磁盘预检：声明超大 Upload-Length 应返回 507
8. 并行任务数设置：默认值 / 设置 / 越界拒绝 / 持久化
9. 多个未完成任务必须**全部**返回（回归用例）
10. 目录信息接口：路径齐全、目录真实存在；打开目录接口只放行白名单
11. 本地文件删除后的列表同步：磁盘文件删掉后接口不再返回该条目；
    .meta 目录整个缺失时读取/清理均不得抛异常（回归用例）
12. 本机上传器 UploadTask（GUI 上传用的客户端）完整走通一次上传
13. 局域网监听：默认 --host=0.0.0.0 时经**局域网 IP** 可达、横幅与二维码用该 IP；
    同时模拟 Clash 系统代理（HTTP_PROXY/ALL_PROXY 指向不存在的端口），
    验证经局域网 IP 的上传、下载与本机上传器均不受影响
14. 优雅关闭：should_exit 后监听线程正常停止，无残留
15. run.py 推荐入口同样可启动并服务
16. 图形界面：真实创建 tkinter 窗口 + 渲染二维码 + Treeview 增量刷新不丢选中
17. 日志检查：服务端日志中不含 Traceback / ERROR，且启动输出已落盘到 logs/app.log

说明：脚本刻意在子进程里开启 UTF-8 模式（PYTHONUTF8=1，Python 3.15 起为
默认），这是系统命令输出解码最容易出问题的配置，可验证其健壮性。

用法：.venv\\Scripts\\python.exe smoke_test.py
"""
import base64
import hashlib
import http.client
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import traceback  # noqa: F401  保留：出错时便于人工排查
import urllib.parse

BASE = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE, "uploads")
META_DIR = os.path.join(UPLOAD_DIR, ".meta")
TUS_DIR = os.path.join(UPLOAD_DIR, ".tus")
LOG_PATH = os.path.join(BASE, "smoke_server.log")
TMP_DIR = os.path.join(BASE, "__smoke_tmp")

PORT = 0
PASS, FAIL = [], []


def ok(label, extra=""):
    PASS.append(label)
    print(f"  [PASS] {label}" + (f"  ({extra})" if extra else ""))


def bad(label, extra=""):
    FAIL.append(f"{label}: {extra}")
    print(f"  [FAIL] {label}" + (f"  -> {extra}" if extra else ""))


def check(label, cond, extra=""):
    (ok if cond else bad)(label, extra)
    return bool(cond)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def req(method, path, body=None, headers=None, timeout=30,
        host="127.0.0.1", port=None):
    """发一个 HTTP 请求，返回 (status, headers_dict, bytes)。

    host / port 可覆盖，用于验证「经局域网 IP 访问」而非仅回环。
    """
    conn = http.client.HTTPConnection(host, port or PORT, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, data
    finally:
        try:
            conn.close()
        except OSError:
            pass


def jreq(method, path, body=None, headers=None, timeout=30):
    status, hdrs, raw = req(method, path, body, headers, timeout)
    try:
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        data = {}
    return status, hdrs, data


def sha256_file(path: str) -> str:
    d = hashlib.sha256()
    with open(path, "rb") as fp:
        while True:
            b = fp.read(4 * 1024 * 1024)
            if not b:
                break
            d.update(b)
    return d.hexdigest()


def make_blob(path: str, size: int) -> str:
    """造一个可校验的伪随机文件（避免全 0 掩盖偏移错位问题）。"""
    with open(path, "wb") as fp:
        written = 0
        seed = 12345
        while written < size:
            n = min(65536, size - written)
            chunk = bytearray(n)
            for i in range(n):
                seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
                chunk[i] = seed >> 16 & 0xFF
            fp.write(chunk)
            written += n
    return sha256_file(path)


def meta_header(name: str, ftype="application/octet-stream") -> str:
    return (f"filename {base64.b64encode(name.encode()).decode()},"
            f"filetype {base64.b64encode(ftype.encode()).decode()}")


PREFIX = "冒烟测试"


def purge_artifacts() -> int:
    """清理测试产物（按文件名前缀扫描，不依赖元数据是否还在）。

    关键：DELETE /api/record/{id} 只删记录、**保留** uploads 里的实际文件，
    所以只清元数据会留下同名文件，下一轮上传就被 unique_path 改名成
    ``xxx(1).bin``，导致按名字匹配的断言全部失配。
    """
    n = 0
    for folder in (UPLOAD_DIR, META_DIR, TUS_DIR):
        if not os.path.isdir(folder):
            continue
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            if not os.path.isfile(path):
                continue
            # 文件名本身带前缀的（uploads 里的成品文件 / .tus 里的分片数据）
            hit = name.startswith(PREFIX)
            # 元数据文件名是随机 ID，真正的业务名在 JSON 内部：
            #   .meta/*.json -> name       .tus/*.info -> metadata.filename
            # 只在这两个目录里读内层字段：uploads 根目录下还放着程序自己的
            # 状态文件 .local_tasks.json（内容是一个数组），不能当成元数据解析。
            inner_json = folder in (META_DIR, TUS_DIR) and \
                name.endswith((".json", ".info"))
            if not hit and inner_json:
                try:
                    with open(path, encoding="utf-8") as fp:
                        data = json.load(fp)
                    if isinstance(data, dict):
                        md = data.get("metadata")
                        inner = (md.get("filename") if isinstance(md, dict)
                                 else "") or data.get("name") or ""
                        hit = str(inner).startswith(PREFIX)
                except (OSError, ValueError):
                    hit = False
            if hit:
                try:
                    os.remove(path)
                    n += 1
                except OSError:
                    pass
    return n


def t_graceful_shutdown():
    """进程内验证服务端优雅关闭路径（should_exit → 监听线程停止）。"""
    print("\n== 12. 服务优雅关闭 ==")
    sys.path.insert(0, BASE)
    import main as entry

    entry.app = entry.create_app()
    port = free_port()
    server, thread = entry.start_server("127.0.0.1", port)

    started = False
    for _ in range(60):
        if getattr(server, "started", False):
            started = True
            break
        time.sleep(0.2)
    check("start_server 启动成功", started)

    global PORT
    keep, PORT = PORT, port
    try:
        status, _, raw = req("GET", "/")
        check("进程内服务可正常响应请求",
              status == 200 and "局域网文件传输" in raw.decode("utf-8", "replace"),
              f"HTTP {status}")
    finally:
        PORT = keep

    server.should_exit = True
    thread.join(timeout=15)
    check("should_exit 后监听线程已停止（无残留）", not thread.is_alive())


# ==========================================================================
# 测试步骤
# ==========================================================================

def t_tus_upload_validated(section: str):
    """完整分片上传 + 下载校验（核心链路）。"""
    print(f"\n== {section} ==")
    src = os.path.join(TMP_DIR, "冒烟测试-完整上传.bin")
    size = 1 * 1024 * 1024 + 12345
    digest = make_blob(src, size)

    status, hdrs, _ = req("POST", "/api/upload/", body=b"", headers={
        "Tus-Resumable": "1.0.0", "Upload-Length": str(size),
        "Upload-Metadata": meta_header(os.path.basename(src))})
    if not check("POST 创建上传返回 201", status == 201, f"HTTP {status}"):
        return
    loc = hdrs.get("location", "")
    check("响应带 Location", bool(loc), loc)
    uid = loc.rstrip("/").rsplit("/", 1)[-1]
    path = f"/api/upload/{uid}"

    # 分片上传，中途 HEAD 校验偏移，末片前再查一次
    chunk = 400 * 1024
    with open(src, "rb") as fp:
        offset = 0
        idx = 0
        while offset < size:
            block = fp.read(min(chunk, size - offset))
            status, h2, _ = req("PATCH", path, body=block, headers={
                "Tus-Resumable": "1.0.0", "Upload-Offset": str(offset),
                "Content-Type": "application/offset+octet-stream"}, timeout=60)
            if status not in (200, 204):
                bad(f"第 {idx + 1} 片 PATCH", f"HTTP {status}")
                return
            new_off = int(h2.get("upload-offset", -1))
            if new_off != offset + len(block):
                bad(f"第 {idx + 1} 片偏移回执", f"期望 {offset + len(block)}，实际 {new_off}")
                return
            offset = new_off
            idx += 1
            # 传输中间态：HEAD 应能报出已接收偏移（断点续传依赖此接口）
            if offset < size:
                status, h2, _ = req("HEAD", path, headers={"Tus-Resumable": "1.0.0"})
                if status != 200 or int(h2.get("upload-offset", -1)) != offset:
                    bad("传输中 HEAD 偏移不正确",
                        f"HTTP {status} offset={h2.get('upload-offset')} 期望={offset}")
                    return
    ok(f"分片上传完成（{idx} 片，{size} 字节），传输中 HEAD 偏移始终正确")

    # 任务完成后资源被回收（TUS termination），HEAD 返回 404 属预期行为
    status, _, _ = req("HEAD", path, headers={"Tus-Resumable": "1.0.0"})
    check("完成后任务资源已回收（HEAD 404）", status == 404, f"HTTP {status}")

    # 等待服务端异步完成（重命名 + SHA256）
    file_id = None
    deadline = time.time() + 60
    while time.time() < deadline:
        _, _, data = jreq("GET", "/api/files")
        hit = next((f for f in data.get("files", [])
                    if f["name"] == os.path.basename(src)), None)
        if hit:
            file_id = hit["id"]
            break
        time.sleep(0.4)
    if not check("完成后出现在 /api/files", bool(file_id)):
        return
    ok("异步哈希与元数据落库", f"id={file_id}")

    status, _, data = jreq("GET", f"/verify/{file_id}")
    check("SHA256 校验一致（服务端）",
          status == 200 and data.get("ok") is True,
          f"HTTP {status} recorded={str(data.get('recorded'))[:16]}")

    # 下载并与源文件逐字节比对
    status, hdrs, raw = req("GET", f"/files/{file_id}", timeout=60)
    got = hashlib.sha256(raw).hexdigest()
    check("下载内容 SHA256 与源文件一致",
          status == 200 and got == digest,
          f"HTTP {status} len={len(raw)}/{size}")

    # 清理（统一由 main() 末尾按文件名前缀回收）
    jreq("DELETE", f"/api/record/{file_id}")
    return uid


def t_resume():
    """断点续传：传一半停下来，查到断点后继续传完。"""
    print("\n== 3. 断点续传 ==")
    src = os.path.join(TMP_DIR, "冒烟测试-断点续传.bin")
    size = 800 * 1024
    digest = make_blob(src, size)

    status, hdrs, _ = req("POST", "/api/upload/", body=b"", headers={
        "Tus-Resumable": "1.0.0", "Upload-Length": str(size),
        "Upload-Metadata": meta_header(os.path.basename(src))})
    if not check("创建续传任务 201", status == 201, f"HTTP {status}"):
        return
    uid = hdrs["location"].rstrip("/").rsplit("/", 1)[-1]
    path = f"/api/upload/{uid}"

    half = 300 * 1024
    with open(src, "rb") as fp:
        block = fp.read(half)
    status, h2, _ = req("PATCH", path, body=block, headers={
        "Tus-Resumable": "1.0.0", "Upload-Offset": "0",
        "Content-Type": "application/offset+octet-stream"})
    check("传一半 PATCH 成功", status in (200, 204), f"HTTP {status}")

    # 中断后 HEAD 应能查到断点
    status, h2, _ = req("HEAD", path, headers={"Tus-Resumable": "1.0.0"})
    check("HEAD 查到断点偏移", int(h2.get("upload-offset", -1)) == half,
          f"offset={h2.get('upload-offset')} 期望={half}")

    _, _, data = jreq("GET", "/api/pending?include_active=1")
    hit = next((p for p in data.get("pending", []) if p["uid"] == uid), None)
    check("/api/pending 能查到未完成任务（含活跃）", bool(hit),
          f"offset={hit['offset'] if hit else None}")

    # 从断点续传剩余部分
    with open(src, "rb") as fp:
        fp.seek(half)
        block = fp.read()
    status, h2, _ = req("PATCH", path, body=block, headers={
        "Tus-Resumable": "1.0.0", "Upload-Offset": str(half),
        "Content-Type": "application/offset+octet-stream"})
    check("从断点续传完成", status in (200, 204) and
          int(h2.get("upload-offset", -1)) == size,
          f"HTTP {status}")

    # 续传结果必须是完整、正确的内容
    file_id = None
    deadline = time.time() + 30
    while time.time() < deadline:
        _, _, d = jreq("GET", "/api/files")
        hit = next((f for f in d.get("files", [])
                    if f["name"] == os.path.basename(src)), None)
        if hit:
            file_id = hit["id"]
            break
        time.sleep(0.3)
    if not check("续传后文件落库", bool(file_id)):
        return
    status, _, raw = req("GET", f"/files/{file_id}", timeout=30)
    check("续传文件内容与源文件一致",
          hashlib.sha256(raw).hexdigest() == digest, f"len={len(raw)}/{size}")
    jreq("DELETE", f"/api/record/{file_id}")
    return uid


def t_control():
    """心跳 / 暂停 / 继续 / 删除任务 接口。"""
    print("\n== 4. 任务控制接口 ==")
    src = os.path.join(TMP_DIR, "冒烟测试-控制.bin")
    size = 200 * 1024
    make_blob(src, size)

    status, hdrs, _ = req("POST", "/api/upload/", body=b"", headers={
        "Tus-Resumable": "1.0.0", "Upload-Length": str(size),
        "Upload-Metadata": meta_header(os.path.basename(src))})
    if not check("创建控制测试任务 201", status == 201, f"HTTP {status}"):
        return
    uid = hdrs["location"].rstrip("/").rsplit("/", 1)[-1]
    path = f"/api/upload/{uid}"

    with open(src, "rb") as fp:
        req("PATCH", path, body=fp.read(), headers={
            "Tus-Resumable": "1.0.0", "Upload-Offset": "0",
            "Content-Type": "application/offset+octet-stream"})
    # 任务已完成，.info 会被删除；先造一个未完成任务测心跳
    status, hdrs, _ = req("POST", "/api/upload/", body=b"", headers={
        "Tus-Resumable": "1.0.0", "Upload-Length": str(size),
        "Upload-Metadata": meta_header("冒烟测试-心跳.bin")})
    uid2 = hdrs["location"].rstrip("/").rsplit("/", 1)[-1]

    status, _, d = jreq("POST", f"/api/active/{uid2}",
                        body=json.dumps({"uploaded": 100, "speed": 1.5}).encode(),
                        headers={"Content-Type": "application/json"})
    check("心跳上报 200 且 ok=true", status == 200 and d.get("ok") is True,
          f"HTTP {status}")

    _, _, d = jreq("GET", "/api/active")
    check("任务出现在 /api/active", any(a["uid"] == uid2 for a in d.get("active", [])))

    status, _, d = jreq("POST", f"/api/pause/{uid2}")
    check("请求暂停 200", status == 200 and d.get("ok") is True, f"HTTP {status}")

    status, _, d = jreq("POST", f"/api/active/{uid2}",
                        body=json.dumps({"uploaded": 100}).encode(),
                        headers={"Content-Type": "application/json"})
    check("暂停后心跳回执 paused=true", d.get("paused") is True, str(d))

    status, _, d = jreq("POST", f"/api/resume/{uid2}")
    check("取消暂停 200", status == 200 and d.get("ok") is True, f"HTTP {status}")

    status, _, d = jreq("DELETE", f"/api/pending/{uid2}")
    check("删除未完成任务 200", status == 200 and d.get("ok") is True, f"HTTP {status}")
    check("删除后分片文件已清理",
          not os.path.isfile(os.path.join(TUS_DIR, uid2)))

    # 非法 uid 应被拒绝
    status, _, _ = jreq("POST", "/api/pause/not-a-hex-id")
    check("非法任务 ID 返回 400", status == 400, f"HTTP {status}")


def t_disk_guard():
    """磁盘空间预检：声明超大文件应返回 507。"""
    print("\n== 5. 磁盘空间预检 ==")
    free = shutil.disk_usage(UPLOAD_DIR).free
    huge = free + 100 * (1 << 30)
    status, _, _ = req("POST", "/api/upload/", body=b"", headers={
        "Tus-Resumable": "1.0.0", "Upload-Length": str(huge),
        "Upload-Metadata": meta_header("超大盘测试.bin")})
    check("空间不足时返回 507", status == 507, f"HTTP {status}（需要 {huge} 字节）")


def t_parallel_settings():
    """并行任务数设置接口：默认值、设置、读取、边界校验、持久化。"""
    print("\n== 6. 并行任务数设置 ==")
    _, _, d = jreq("GET", "/api/settings")
    check("并行上限在 1~32 之间且硬上限为 32",
          1 <= int(d.get("max_parallel", 0)) <= 32 and d.get("limit") == 32,
          str(d))
    original = int(d.get("max_parallel"))

    status, _, d = jreq("POST", "/api/settings",
                        body=json.dumps({"max_parallel": 3}).encode(),
                        headers={"Content-Type": "application/json"})
    check("设置并行上限为 3", status == 200 and d.get("max_parallel") == 3,
          f"HTTP {status} {d}")

    _, _, d = jreq("GET", "/api/settings")
    check("再次读取到刚设置的值", int(d.get("max_parallel")) == 3, str(d))

    for value in (0, 33, -1):
        status, _, _ = jreq("POST", "/api/settings",
                            body=json.dumps({"max_parallel": value}).encode(),
                            headers={"Content-Type": "application/json"})
        check(f"越界值 {value} 被拒绝（400）", status == 400, f"HTTP {status}")

    status, _, _ = jreq("POST", "/api/settings", body=b"{not json",
                        headers={"Content-Type": "application/json"})
    check("非法请求体被拒绝（400）", status == 400, f"HTTP {status}")

    # 持久化：写盘文件应记录当前值
    path = os.path.join(UPLOAD_DIR, ".settings.json")
    try:
        with open(path, encoding="utf-8") as fp:
            saved = int(json.load(fp).get("max_parallel"))
        check("设置已持久化到磁盘", saved == 3, f"saved={saved}")
    except (OSError, ValueError) as exc:
        bad("设置已持久化到磁盘", repr(exc))

    # 还原，避免影响后续用例
    jreq("POST", "/api/settings",
         body=json.dumps({"max_parallel": original}).encode(),
         headers={"Content-Type": "application/json"})


def t_pending_multi():
    """回归：多个未完成任务必须**全部**返回，不能只出现一个。

    原实现里页面只按 active 过滤，页面重载后 90 秒内的心跳残留任务会同时
    从「未完成」和「其他设备正在上传」消失；后端也一并核对条数。
    """
    print("\n== 7. 多个未完成任务（回归） ==")
    uids = []
    for i in range(3):
        name = f"冒烟测试-待续{i}.bin"
        status, hdrs, _ = req("POST", "/api/upload/", body=b"", headers={
            "Tus-Resumable": "1.0.0",
            "Upload-Length": str(2 * 1024 * 1024),
            "Upload-Metadata": meta_header(name)})
        if status != 201:
            bad(f"创建第 {i + 1} 个未完成任务", f"HTTP {status}")
            return
        uid = hdrs["location"].rstrip("/").rsplit("/", 1)[-1]
        uids.append(uid)
        req("PATCH", f"/api/upload/{uid}", body=b"y" * 2048, headers={
            "Tus-Resumable": "1.0.0", "Upload-Offset": "0",
            "Content-Type": "application/offset+octet-stream"})

    for query in ("/api/pending", "/api/pending?include_active=1"):
        _, _, d = jreq("GET", query)
        got = {p["uid"] for p in d.get("pending", [])}
        check(f"{query} 列出全部 3 个未完成任务",
              set(uids) <= got, f"实到 {len(set(uids) & got)}/3 个")

    # 删除其中 2 个，剩下的必须仍然可见
    for uid in uids[:2]:
        status, _, _ = jreq("DELETE", f"/api/pending/{uid}")
        check(f"删除未完成任务 {uid[:8]}… 成功", status == 200, f"HTTP {status}")
    _, _, d = jreq("GET", "/api/pending?include_active=1")
    left = {p["uid"] for p in d.get("pending", [])}
    check("删除 2 个后，剩余任务仍完整可见",
          uids[2] in left and not (set(uids[:2]) & left),
          f"剩余={len(left)}")

    jreq("DELETE", f"/api/pending/{uids[2]}")


def t_file_removal_sync():
    """本地磁盘文件被删除后，列表必须同步移除。

    网页端与程序端都读 /api/files（进而读 read_all_meta），所以这里验证的是
    两端的共同数据源；同时覆盖引发过"客户端列表卡死"的两个缺陷：
      - .meta 目录整个缺失时不得抛异常（否则 Tk 定时回调中断，刷新永久停摆）
      - 失效记录会被定期清理，且不会误删"所在目录整体不存在"的记录
    """
    print("\n== 9. 本地文件删除后的列表同步 ==")
    name = "冒烟测试-删除同步.bin"
    src = os.path.join(TMP_DIR, name)
    size = 128 * 1024
    make_blob(src, size)
    status, hdrs, _ = req("POST", "/api/upload/", body=b"", headers={
        "Tus-Resumable": "1.0.0", "Upload-Length": str(size),
        "Upload-Metadata": meta_header(name)})
    if not check("创建待删除的测试文件", status == 201, f"HTTP {status}"):
        return
    upath = "/api/upload/" + hdrs["location"].rstrip("/").rsplit("/", 1)[-1]
    with open(src, "rb") as fp:
        blob = fp.read()
    req("PATCH", upath, body=blob, headers={
        "Tus-Resumable": "1.0.0", "Upload-Offset": "0",
        "Content-Type": "application/offset+octet-stream"})

    found = False
    deadline = time.time() + 40
    while time.time() < deadline:
        _, _, data = jreq("GET", "/api/files")
        if any(f["name"] == name for f in data.get("files", [])):
            found = True
            break
        time.sleep(0.3)
    if not check("上传完成并出现在列表中", found):
        return

    disk = os.path.join(UPLOAD_DIR, name)
    if not check("物理文件确实存在", os.path.isfile(disk), disk):
        return
    os.remove(disk)                        # 模拟用户从资源管理器里删掉文件
    _, _, data = jreq("GET", "/api/files")
    check("磁盘文件删除后，接口立即不再返回该条目",
          not any(f["name"] == name for f in data.get("files", [])),
          f"count={data.get('count')}")

    # ---- .meta 目录整个缺失：读取与清理都必须容错 ----
    sys.path.insert(0, BASE)
    import store as store_mod
    real_meta = store_mod.META_DIR
    store_mod.META_DIR = os.path.join(TMP_DIR, "不存在的meta")
    try:
        try:
            got = store_mod.read_all_meta()
        except Exception as exc:            # noqa: BLE001
            got = repr(exc)
        check("META_DIR 不存在时 read_all_meta 返回空列表而不抛异常",
              got == [], repr(got)[:90])
        try:
            zero = store_mod.purge_missing_records()
        except Exception as exc:            # noqa: BLE001
            zero = repr(exc)
        check("META_DIR 不存在时 purge_missing_records 返回 0 而不抛异常",
              zero == 0, repr(zero)[:90])
    finally:
        store_mod.META_DIR = real_meta

    # ---- 失效记录清理，且不误删"所在目录整体不存在"的记录 ----
    tmp_meta = os.path.join(TMP_DIR, "meta")
    os.makedirs(tmp_meta, exist_ok=True)
    kept = os.path.join(TMP_DIR, "保留.bin")
    vanished = os.path.join(TMP_DIR, "已删除.bin")
    for one in (kept, vanished):
        with open(one, "wb") as fp:
            fp.write(b"x")
    store_mod.META_DIR = tmp_meta
    try:
        store_mod.write_meta("keep01", {"id": "keep01", "path": kept})
        store_mod.write_meta("gone01", {"id": "gone01", "path": vanished})
        store_mod.write_meta("nodir1", {"id": "nodir1",
                                        "path": os.path.join(TMP_DIR, "没这个目录", "x")})
        os.remove(vanished)
        removed = store_mod.purge_missing_records()
        left = set(os.listdir(tmp_meta))
        check("清理掉源文件已删除的记录",
              removed == 1 and "gone01.json" not in left,
              f"removed={removed} left={sorted(left)}")
        check("源文件仍在的记录不动", "keep01.json" in left)
        check("所在目录整体不存在时不误删记录", "nodir1.json" in left)
    finally:
        store_mod.META_DIR = real_meta


def t_paths_and_open():
    """目录信息接口 + 「在电脑上打开目录」接口。"""
    print("\n== 8. 目录信息与打开目录 ==")
    need = ("base", "upload_dir", "config_dir", "config_file", "log_dir", "log_file")
    status, _, d = jreq("GET", "/api/paths")
    check("GET /api/paths 字段完整",
          status == 200 and all(k in d for k in need),
          f"HTTP {status} {sorted(d)}")
    check("返回的目录都真实存在",
          all(os.path.isdir(d.get(k, "")) for k in
              ("base", "upload_dir", "config_dir", "log_dir")),
          str(d.get("log_dir")))
    check("日志文件就在日志目录内",
          os.path.dirname(d.get("log_file", "")) == d.get("log_dir"),
          str(d.get("log_file")))

    # 只验证「拒绝」分支：合法 key 会真的弹出资源管理器，不适合放进自动化测试
    for bad_key in ("", "etc", "../..", "C:\\Windows"):
        status, _, _ = jreq("POST", "/api/open-dir",
                            body=json.dumps({"key": bad_key}).encode(),
                            headers={"Content-Type": "application/json"})
        check(f"非法目录标识 {bad_key!r} 被拒绝（400）", status == 400,
              f"HTTP {status}")
    status, _, _ = jreq("POST", "/api/open-dir", body=b"{}",
                        headers={"Content-Type": "application/json"})
    check("缺少 key 被拒绝（400）", status == 400, f"HTTP {status}")

    # open_local_dir 本体：把 os.startfile 换成记录器，避免测试时真弹窗口
    sys.path.insert(0, BASE)
    import store as store_mod
    calls = []
    original = getattr(store_mod.os, "startfile", None)
    store_mod.os.startfile = calls.append
    try:
        target = os.path.join(TMP_DIR, "打开目录测试")
        opened = store_mod.open_local_dir(target)
        check("open_local_dir 先建目录再打开",
              opened and os.path.isdir(target) and calls == [target],
              f"opened={opened} calls={calls}")
    finally:
        if original is not None:
            store_mod.os.startfile = original


def t_page_and_apis():
    """首页与全部读接口。"""
    print("\n== 1. 页面与读接口 ==")
    status, _, raw = req("GET", "/")
    html = raw.decode("utf-8", "replace")
    check("GET / 返回 200", status == 200, f"HTTP {status}")
    check("页面含标题", "局域网文件传输" in html)
    check("tus.min.js 已内联（离线可用）",
          "tus.min.js" not in html.split("<script>")[0] and "tus" in html.lower())
    check("页面未回退到 CDN", "cdn.jsdelivr.net" not in html)

    status, _, d = jreq("GET", "/api/files")
    check("GET /api/files 结构完整",
          status == 200 and {"files", "count", "total", "free"} <= set(d),
          f"HTTP {status}")
    check("/api/files 不下发服务端绝对路径",
          all("path" not in f for f in d.get("files", [])))

    status, _, d = jreq("GET", "/api/space")
    check("GET /api/space 含磁盘与内存字段",
          status == 200 and {"total", "free", "rss", "rss_peak"} <= set(d),
          f"HTTP {status} rss={d.get('rss')}")

    status, _, d = jreq("GET", "/api/pending")
    check("GET /api/pending 正常", status == 200 and "pending" in d, f"HTTP {status}")

    status, _, d = jreq("GET", "/api/active")
    check("GET /api/active 正常", status == 200 and "active" in d, f"HTTP {status}")

    status, _, d = jreq("GET", "/api/machine")
    check("GET /api/machine 返回本机名", status == 200 and bool(d.get("name")),
          str(d)[:80])

    status, _, d = jreq("GET", "/api/device?ip=127.0.0.1")
    check("GET /api/device 正常", status == 200 and d.get("ip") == "127.0.0.1",
          str(d)[:80])

    status, _, d = jreq("GET", "/verify/" + urllib.parse.quote("不存在的编号"))
    check("不存在的文件返回 404", status == 404, f"HTTP {status}")


def t_local_uploader():
    """本机上传器（GUI 使用的 UploadTask）走通一次完整上传。"""
    print("\n== 10. 本机上传器（UploadTask）==")
    sys.path.insert(0, BASE)
    import uploader
    src = os.path.join(TMP_DIR, "冒烟测试-本机上传器.bin")
    size = 700 * 1024
    digest = make_blob(src, size)

    task = uploader.UploadTask(src, PORT, chunk_size=256 * 1024)
    task.start()
    deadline = time.time() + 60
    while time.time() < deadline and task.status not in ("done", "failed"):
        time.sleep(0.2)
    if not check("本机上传器任务完成", task.status == "done",
                 f"status={task.status} error={task.error}"):
        return

    file_id = None
    deadline = time.time() + 30
    while time.time() < deadline:
        _, _, d = jreq("GET", "/api/files")
        hit = next((f for f in d.get("files", [])
                    if f["name"] == os.path.basename(src)), None)
        if hit:
            file_id = hit["id"]
            break
        time.sleep(0.3)
    if not check("本机上传的文件落库", bool(file_id)):
        return
    status, _, raw = req("GET", f"/files/{file_id}", timeout=30)
    check("本机上传文件内容一致", hashlib.sha256(raw).hexdigest() == digest)
    jreq("DELETE", f"/api/record/{file_id}")


def t_run_py_entry():
    """验证推荐入口 run.py 也能正常启动并服务（含快速退出钩子）。"""
    print("\n== 13. run.py 入口启动 ==")
    port = free_port()
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    log_path = os.path.join(BASE, "smoke_run_entry.log")
    with open(log_path, "w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            [sys.executable, "run.py", "--no-gui", "--no-browser",
             "--host", "127.0.0.1", "--port", str(port)],
            cwd=BASE, stdout=log, stderr=subprocess.STDOUT, env=env)
    global PORT
    keep, PORT = PORT, port
    try:
        ready = False
        deadline = time.time() + 40
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                ready = True
                break
            except OSError:
                time.sleep(0.3)
        if check("run.py 启动并监听端口", ready, f"退出码={proc.poll()}"):
            status, _, _ = req("GET", "/api/files")
            check("run.py 服务可响应请求", status == 200, f"HTTP {status}")
    finally:
        PORT = keep
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
    with open(log_path, encoding="utf-8", errors="replace") as fp:
        text = fp.read()
    check("run.py 日志无未捕获异常",
          "Traceback" not in text,
          text.splitlines()[0] if "Traceback" in text else "")


def t_gui_window():
    """验证 GUI 关键路径：真实创建 Tk 窗口 + 渲染二维码 + 读取本机信息。"""
    print("\n== 14. 图形界面（tkinter 窗口）==")
    sys.path.insert(0, BASE)
    try:
        import tkinter as tk
        from tkinter import ttk

        from PIL import Image, ImageTk

        import main as entry
        from store import local_machine_info

        qr = entry.make_qrcode(f"http://127.0.0.1:{free_port()}")
        check("二维码图片生成成功", os.path.isfile(qr) and os.path.getsize(qr) > 0)

        root = tk.Tk()
        root.title("冒烟测试")
        root.geometry("320x260")
        img = ImageTk.PhotoImage(Image.open(qr).resize((160, 160), Image.LANCZOS))
        tk.Label(root, image=img).pack()          # 保持引用，避免被回收
        machine = local_machine_info()
        tk.Label(root, text=f"{machine['name']}  {machine['mac'] or '未知'}").pack()
        root.update()
        root.update_idletasks()
        check("tkinter 窗口创建并完成一次渲染",
              bool(root.winfo_exists() and machine.get("name")))

        # ---- 「已接收文件」列表的增量刷新 ----
        # 回归用例：tick() 每 2 秒刷新一次，早期实现是「删光所有行再重建」，
        # 导致用户选中后一两秒就失去高亮。这里模拟连续刷新验证选中状态不丢。
        tv = ttk.Treeview(root, columns=("a", "b", "c"), show="headings",
                          selectmode="extended")
        rows = [(f"id{i}", (f"文件{i}.bin", f"{i}00 KB", "已完成"))
                for i in range(4)]
        entry.sync_treeview(tv, rows)
        check("treeview 首次填充：行数与顺序正确",
              list(tv.get_children()) == [r[0] for r in rows])

        tv.selection_set(["id1", "id2"])
        for _ in range(5):                    # 模拟 tick() 连续刷新
            entry.sync_treeview(tv, rows)
        check("连续刷新后选中状态保持不变（原 BUG：1~2 秒后丢失）",
              sorted(tv.selection()) == ["id1", "id2"],
              f"selection={list(tv.selection())}")

        changed = [rows[0],
                   ("id1", ("文件1-改名.bin", "100 KB", "已完成")),
                   ("id9", ("新文件.bin", "9 KB", "已完成"))]
        entry.sync_treeview(tv, changed)
        check("增量刷新：移除消失行、追加新行、顺序正确",
              list(tv.get_children()) == ["id0", "id1", "id9"],
              f"children={list(tv.get_children())}")
        check("增量刷新：仍存在的行保持选中",
              sorted(tv.selection()) == ["id1"])
        check("增量刷新：数值变化已就地表更新",
              tuple(tv.item("id1", "values"))[0] == "文件1-改名.bin",
              str(tv.item("id1", "values")))
        tv.destroy()
        root.destroy()
    except Exception as exc:
        bad("GUI 路径验证", repr(exc))


def t_lan_and_proxy():
    """局域网监听 + 系统代理（Clash 非 TUN）兼容性。

    验证三件事：
      1. 不传 --host（默认 0.0.0.0）时，服务在**局域网 IP** 上可达，
         而不只是回环地址；
      2. 启动横幅里的访问地址是局域网 IP + 端口，二维码按该地址生成；
      3. 模拟 Clash 系统代理（代理指向不存在的端口）时，
         经局域网 IP 的上传 / 下载 / 本机上传器全部照常工作。
    """
    print("\n== 11. 局域网监听 + 系统代理（Clash 非 TUN）兼容性 ==")
    sys.path.insert(0, BASE)
    from netutils import get_lan_ip

    lan_ip = get_lan_ip()
    port = free_port()
    log_path = os.path.join(BASE, "smoke_lan.log")
    qr_path = os.path.join(BASE, "lan_qrcode.png")
    if os.path.isfile(qr_path):
        os.remove(qr_path)          # 先删掉，确保下面查到的确实是本轮生成的

    # 模拟 Clash 系统代理：代理地址故意指向一个不存在的端口，
    # 任何误走代理的请求都会立即失败，问题会直接暴露出来
    proxy = "http://127.0.0.1:7897"
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
               HTTP_PROXY=proxy, HTTPS_PROXY=proxy,
               ALL_PROXY="socks5://127.0.0.1:7897", NO_PROXY="")
    with open(log_path, "w", encoding="utf-8") as log:
        proc = subprocess.Popen(        # 刻意不传 --host，验证默认值
            [sys.executable, "run.py", "--no-gui", "--no-browser",
             "--port", str(port)],
            cwd=BASE, stdout=log, stderr=subprocess.STDOUT, env=env)
    try:
        ready = False
        deadline = time.time() + 40
        while time.time() < deadline and proc.poll() is None:
            try:
                socket.create_connection((lan_ip, port), timeout=1).close()
                ready = True
                break
            except OSError:
                time.sleep(0.3)
        if not check(f"默认 --host 在局域网 IP 上可达（{lan_ip}）", ready):
            return

        with open(log_path, encoding="utf-8", errors="replace") as fp:
            banner = fp.read()
        found = re.search(r"访问地址\s*:\s*http://(\d+\.\d+\.\d+\.\d+):(\d+)", banner)
        check("启动横幅的访问地址是局域网 IP + 端口",
              bool(found) and found.group(1) == lan_ip
              and found.group(2) == str(port),
              found.group(0) if found else banner[:100].replace("\n", " "))
        check("二维码按该局域网地址生成", os.path.isfile(qr_path))

        status, _, _ = req("GET", "/api/files", host=lan_ip, port=port)
        check("经局域网 IP 可访问接口", status == 200, f"HTTP {status}")

        # ---- 经局域网 IP 走一遍完整上传 + 下载（代理开启）----
        name = "冒烟测试-局域网代理.bin"
        src = os.path.join(TMP_DIR, name)
        size = 500 * 1024
        digest = make_blob(src, size)
        status, hdrs, _ = req("POST", "/api/upload/", host=lan_ip, port=port,
                              body=b"", headers={
                                  "Tus-Resumable": "1.0.0",
                                  "Upload-Length": str(size),
                                  "Upload-Metadata": meta_header(name)})
        if not check("经局域网 IP 创建上传 201", status == 201, f"HTTP {status}"):
            return
        upath = "/api/upload/" + hdrs["location"].rstrip("/").rsplit("/", 1)[-1]
        with open(src, "rb") as fp:
            blob = fp.read()
        status, _, _ = req("PATCH", upath, host=lan_ip, port=port, body=blob,
                           headers={"Tus-Resumable": "1.0.0",
                                    "Upload-Offset": "0",
                                    "Content-Type": "application/offset+octet-stream"},
                           timeout=60)
        check("经局域网 IP 分片上传成功", status in (200, 204), f"HTTP {status}")

        file_id = None
        deadline = time.time() + 40
        while time.time() < deadline:
            _, _, raw = req("GET", "/api/files", host=lan_ip, port=port)
            items = json.loads(raw.decode("utf-8")).get("files", [])
            hit = next((f for f in items if f["name"] == name), None)
            if hit:
                file_id = hit["id"]
                break
            time.sleep(0.3)
        if check("经局域网 IP 上传完成并落库", bool(file_id)):
            _, _, raw = req("GET", f"/files/{file_id}", host=lan_ip, port=port,
                            timeout=60)
            check("经局域网 IP 下载内容一致",
                  hashlib.sha256(raw).hexdigest() == digest,
                  f"len={len(raw)}/{size}")

        # ---- 系统代理开启时，本机上传器（GUI 客户端）同样不受影响 ----
        os.environ.update(HTTP_PROXY=proxy, HTTPS_PROXY=proxy,
                          ALL_PROXY="socks5://127.0.0.1:7897")
        try:
            import uploader
            src2 = os.path.join(TMP_DIR, "冒烟测试-代理上传器.bin")
            make_blob(src2, 300 * 1024)
            task = uploader.UploadTask(src2, port, chunk_size=128 * 1024)
            task.start()
            deadline = time.time() + 40
            while time.time() < deadline and task.status not in ("done", "failed"):
                time.sleep(0.2)
            check("系统代理开启时本机上传器照常工作", task.status == "done",
                  f"status={task.status} error={task.error}")
        finally:
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
                os.environ.pop(key, None)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


# ==========================================================================
# 主流程
# ==========================================================================

def main():
    global PORT
    print("=" * 66)
    print("局域网文件传输工具 —— 冒烟测试")
    print("=" * 66)

    os.makedirs(TMP_DIR, exist_ok=True)
    left = purge_artifacts()
    if left:
        print(f"已清理上一轮残留的测试文件 {left} 个（保证本轮结果可复现）")
    PORT = free_port()
    print(f"Python : {sys.executable}")
    print(f"端口   : {PORT}")

    # 刻意开启 UTF-8 模式（PYTHONUTF8=1）：这是 Python 3.15 起的默认值，
    # 也是 arp / nbtstat 输出解码最容易出问题、打印堆栈噪音的配置。
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    with open(LOG_PATH, "w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            [sys.executable, "main.py", "--no-gui", "--no-browser",
             "--host", "127.0.0.1", "--port", str(PORT)],
            cwd=BASE, stdout=log, stderr=subprocess.STDOUT, env=env,
            creationflags=flags)

    try:
        # ---- 等待服务就绪 ----
        ready = False
        deadline = time.time() + 40
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                s = socket.create_connection(("127.0.0.1", PORT), timeout=1)
                s.close()
                ready = True
                break
            except OSError:
                time.sleep(0.4)
        if not check("服务成功启动并监听端口", ready,
                     f"退出码={proc.poll()}" if not ready else ""):
            return
        time.sleep(0.6)

        t_page_and_apis()
        t_tus_upload_validated("2. TUS 完整分片上传 + 下载校验")
        t_resume()
        t_control()
        t_disk_guard()
        t_parallel_settings()
        t_pending_multi()
        t_paths_and_open()
        t_file_removal_sync()
        t_local_uploader()
        t_lan_and_proxy()

        # ---- 结束子进程服务（Windows 无控制台进程收到 Ctrl+Break 会以
        #      STATUS_CONTROL_C_EXIT 直接终止，因此退出行为单独在 7 里验证）----
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        except (OSError, ValueError):
            proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            bad("服务进程 20 秒内未结束")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

    t_graceful_shutdown()
    t_run_py_entry()
    t_gui_window()

    # ---- 检查服务端日志 ----
    print("\n== 15. 服务端日志检查 ==")
    with open(LOG_PATH, encoding="utf-8", errors="replace") as fp:
        log_text = fp.read()
    tb = re.findall(r"Traceback \(most recent call last\)", log_text)
    check("日志中无未捕获异常（Traceback）", not tb, f"发现 {len(tb)} 处")

    # 程序还会把控制台输出同步落到 logs/app.log，供无控制台时排查
    app_log = os.path.join(BASE, "logs", "app.log")
    body = (open(app_log, encoding="utf-8", errors="replace").read()
            if os.path.isfile(app_log) else "")
    check("启动输出已写入 logs/app.log",
          "程序启动" in body and "接收目录" in body,
          f"{len(body)} 字节")
    errs = [ln for ln in log_text.splitlines()
            if "[错误]" in ln or "ERROR:" in ln]
    check("日志中无 ERROR 级记录", not errs, "; ".join(errs[:3]))
    warn = [ln for ln in log_text.splitlines() if "[警告]" in ln]
    if warn:
        print("  [注意] 日志中有警告：" + "; ".join(warn[:3]))

    # ---- 清理测试残留 ----
    print("\n== 16. 清理测试产物 ==")
    removed = purge_artifacts()
    shutil.rmtree(TMP_DIR, ignore_errors=True)
    for artifact in ("lan_qrcode.png", "smoke_run_entry.log", "smoke_lan.log"):
        p = os.path.join(BASE, artifact)
        if os.path.isfile(p):
            os.remove(p)  # 启动时生成的二维码图片等运行产物，测完回收
    ok(f"清理测试文件/记录 {removed} 个、临时目录与运行产物已删除")

    # ---- 汇总 ----
    print("\n" + "=" * 66)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("-" * 66)
        for f in FAIL:
            print("  失败：", f)
        print("=" * 66)
        print(f"服务日志：{LOG_PATH}")
        return 1
    print("结果：全部通过，程序可正常运行，无错误")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(2)
