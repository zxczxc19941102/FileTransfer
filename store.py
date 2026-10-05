"""
局域网文件传输工具 —— 后端服务
================================

技术要点
--------
1. TUS 分片上传（tuspyserver）：前端 tus-js-client 按分片 PATCH，
   服务端 request.stream() 逐块接收并追加写盘，**任何时刻内存里只有一个分片**，
   因此 100G 级文件也不会把内存撑爆。
2. 断点续传：strict_offset_validation=True + 前端 resumeFromPreviousUpload，
   网络中断后重新上传会先 HEAD 查询已传字节数，从断点继续。
3. 不做文件大小上限：max_size 设为 2**62（4EB），实际等同"不限制"，
   真正的约束是磁盘剩余空间（上传前预检）。
4. 目录分工：
     uploads/          已完成的文件（原文件名，便于直接使用）
     uploads/.meta/    每个文件的元数据 JSON（大小、sha256、上传时间、来源 IP）
     uploads/.tus/     上传过程中的临时分片（TUS 工作目录，过期自动清理）
"""
import asyncio
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from tuspyserver import create_tus_router
from tuspyserver.router import TusRouterOptions

from netutils import get_lan_ip

# ==========================================================================
# 一、路径与全局参数
# ==========================================================================

# 数据目录：源码运行时是脚本所在目录；打包成 exe 后是 exe 所在目录
# （不能用 __file__——打包后它指向 PyInstaller 的临时解压目录，会导致
#  上传的文件存进临时目录，程序一关就丢失）
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 资源目录：打包后静态资源被解压到 sys._MEIPASS
RES_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))

UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")        # 完成的文件
META_DIR = os.path.join(UPLOAD_DIR, ".meta")          # 元数据
TUS_DIR = os.path.join(UPLOAD_DIR, ".tus")            # 上传中的临时分片
TUS_JS_PATH = os.path.join(RES_DIR, "tus.min.js")     # 内置的 tus-js-client

# 4EB —— 等同"不设上限"，真正的限制是磁盘空间
NO_SIZE_LIMIT = 1 << 62
CHUNK_READ = 4 * 1024 * 1024        # 计算哈希时的读取块大小（4MB）
EXPIRE_DAYS = 30                    # 未完成分片保留天数（大文件跨天传输常见，留足时间）
MEMORY_PEAK = {"value": 0.0}         # 进程内存峰值（MB），由后台任务更新
SERVER_IP = "127.0.0.1"             # 启动后写入真实内网 IP
SERVER_PORT = 8000                  # 启动后写入真实端口


def ensure_dirs():
    """创建全部工作目录（不存在则自动创建）。"""
    for path in (UPLOAD_DIR, META_DIR, TUS_DIR):
        os.makedirs(path, exist_ok=True)


def build_streaming_storage():
    """构造"逐块直接落盘"的存储后端。

    为什么必须自定义
    ----------------
    tuspyserver 自带的 LocalFileStorage 在 append() 里只把数据累积进
    内存 bytearray（``self._pending[uid].extend(chunk)``），直到 flush()
    才 ``bytes(pending)`` 一次性写盘——那一刻会同时存在两份完整数据，
    传输大文件时内存线性增长（实测 1.19GB 时连接被系统重置）。

    这里改成每收到一块就立刻追加写盘：内存占用恒定为单个分片大小
    （32MB），与文件总大小无关，100G 也不会涨。
    """
    from tuspyserver.storage.local import LocalFileStorage

    class StreamingLocalStorage(LocalFileStorage):
        """append 即落盘，flush 退化为空操作。"""

        async def append(self, uid: str, chunk: bytes) -> None:
            def _do() -> None:
                self._ensure_dir()
                with open(self._path(uid), "ab") as fp:
                    fp.write(chunk)

            await asyncio.to_thread(_do)

        async def flush(self, uid: str) -> None:
            """数据在 append 时已写盘，这里无需再做什么。"""
            self._pending.pop(uid, None)

        async def write_info(self, uid: str, data: dict) -> None:
            """原子写 sidecar；Windows 下 rename 不能覆盖，用 replace。"""

            def _do() -> None:
                self._ensure_dir()
                path = self._info_path(uid)
                tmp = f"{path}.tmp"
                try:
                    with open(tmp, "w", encoding="utf-8") as fp:
                        json.dump(data, fp, indent=4, default=str)
                        fp.flush()
                        os.fsync(fp.fileno())
                    os.replace(tmp, path)  # 跨平台可覆盖
                except Exception:
                    if os.path.exists(tmp):
                        try:
                            os.remove(tmp)
                        except OSError:
                            pass
                    raise

            await asyncio.to_thread(_do)

    return StreamingLocalStorage(TUS_DIR)


def patch_tus_windows_rename():
    """修复 tuspyserver 在 Windows 上无法覆盖 .info 文件的问题。

    tuspyserver 保存元数据时用 os.rename(temp, info) 做原子替换。
    Unix 上 rename 会静默覆盖已有文件，而 Windows 上会直接抛
    [WinError 183]，导致每一次 PATCH 分片都返回 500。
    这里只替换该模块自己的 rename 调用，改用 os.replace（跨平台可覆盖）。
    """
    from tuspyserver import info as tus_info

    if getattr(tus_info, "_win_rename_patched", False):
        return
    original = tus_info.os.rename

    def _safe_rename(src, dst):
        """Windows 上退化为 os.replace，其余平台保持原行为。"""
        if os.name == "nt" and os.path.exists(dst):
            return os.replace(src, dst)
        return original(src, dst)

    tus_info.os.rename = _safe_rename
    tus_info._win_rename_patched = True


# ==========================================================================
# 二、工具函数
# ==========================================================================


def human_size(num: float) -> str:
    """字节数转易读文本。"""
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(num) < 1024 or unit == "PB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.2f} {unit}"
        num /= 1024
    return f"{num:.2f} PB"


def clean_name(name: str) -> str:
    """清洗上传文件名：去掉目录分隔符与非法字符，保留中文。"""
    name = os.path.basename(name or "").strip().strip(".")
    for bad in ("/", "\\", ":", "*", "?", "<", ">", "|", "\0"):
        name = name.replace(bad, "_")
    if len(name) > 200:
        stem, ext = os.path.splitext(name)
        name = stem[: 200 - len(ext)] + ext
    return name or "unnamed"


def unique_path(folder: str, filename: str) -> str:
    """同名文件自动加序号，避免覆盖。"""
    path = os.path.join(folder, filename)
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(filename)
    index = 1
    while True:
        path = os.path.join(folder, f"{stem}({index}){ext}")
        if not os.path.exists(path):
            return path
        index += 1


def disk_free(path: str) -> int:
    """查询所在磁盘剩余字节数。"""
    return shutil.disk_usage(path).free


# ==========================================================================
# 二之二、局域网设备信息（IP / 计算机名 / MAC 地址）
#
# 说明：浏览器出于安全限制拿不到本机的 MAC 与计算机名，
# 所以由服务端根据客户端 IP 反查：
#   MAC  -> 查本机 ARP/邻居表（同局域网内有效，设备近期活动过才有记录）
#   名称 -> 先试 DNS 反查，失败再用 NetBIOS（nbtstat）查询局域网主机名
# 结果带 60 秒缓存，避免频繁调用系统命令。
# ==========================================================================

DEVICE_CACHE: dict = {}      # ip -> {"name": str, "mac": str}
DEVICE_CACHE_TTL = 60        # 缓存秒数
CLIENT_MAP: dict = {}        # 上传 ID(uid) -> 客户端 IP，由 HTTP 中间件登记
ACTIVE_UPLOADS: dict = {}    # uid -> 最后一次心跳时间戳，用于区分"正在上传"与"已中断"
ACTIVE_TTL = 90              # 心跳超过该秒数未刷新，视为上传已中断（页面关闭/崩溃/断网）


def _mac_of(ip: str) -> str:
    """从邻居表（ARP/NDP）反查 MAC 地址，查不到返回空字符串。"""
    if os.name == "nt":
        try:
            out = subprocess.run(["arp", "-a", ip], capture_output=True,
                                 text=True, timeout=3,
                                 creationflags=0x08000000).stdout
            # 形如：  192.168.1.100           0a-bc-1d-2e-3f-4a     dynamic
            for line in out.splitlines():
                if ip in line:
                    parts = line.split()
                    for token in parts:
                        if len(token) == 17 and token.count("-") == 5:
                            return token.replace("-", ":").upper()
            return ""
        except Exception:
            return ""
    try:  # 类 Unix：读 /proc/net/arp
        with open("/proc/net/arp", "r", encoding="utf-8") as fp:
            for line in fp.readlines()[1:]:
                cols = line.split()
                if len(cols) >= 4 and cols[0] == ip and cols[3] != "00:00:00:00:00:00":
                    return cols[3].upper()
    except OSError:
        pass
    return ""


def _name_of(ip: str) -> str:
    """反查计算机名：先 DNS，失败再 NetBIOS。"""
    try:
        name = socket.gethostbyaddr(ip)[0]
        if name:
            return name
    except (OSError, IndexError):
        pass
    if os.name == "nt":
        try:
            out = subprocess.run(["nbtstat", "-A", ip], capture_output=True,
                                 text=True, timeout=3,
                                 creationflags=0x08000000).stdout
            for line in out.splitlines():
                if line.startswith(ip) or "<00>" in line:
                    cols = line.split()
                    if len(cols) >= 2 and cols[0] != ip:
                        return cols[0].strip()
        except Exception:
            pass
    return ""


def device_info(ip: str) -> dict:
    """返回 {"ip", "name", "mac"}；带 60 秒缓存。查询失败时对应字段为空。"""
    if not ip or ip.startswith("127."):
        return {"ip": ip or "", "name": "本机", "mac": _local_mac()}
    local_ip = get_lan_ip()
    if ip == local_ip:
        info = local_machine_info()
        return {"ip": ip, "name": info["name"], "mac": info["mac"]}
    now = time.time()
    cached = DEVICE_CACHE.get(ip)
    if cached and now - cached["ts"] < DEVICE_CACHE_TTL:
        return {"ip": ip, "name": cached["name"], "mac": cached["mac"]}
    name, mac = _name_of(ip), _mac_of(ip)
    DEVICE_CACHE[ip] = {"ts": now, "name": name, "mac": mac}
    return {"ip": ip, "name": name, "mac": mac}


def _local_mac() -> str:
    """本机 MAC：ARP 表里没有自己，用网卡地址直接取。"""
    node = uuid.getnode()
    # 高位为 1 时 uuid.getnode() 返回的是随机地址，不是真实网卡
    if (node >> 40) % 2:
        return ""
    return ":".join(f"{(node >> shift) & 0xFF:02X}" for shift in range(40, -1, -8))


def local_machine_info() -> dict:
    """本机信息（给 GUI 显示）。"""
    return {
        "name": socket.gethostname(),
        "mac": _mac_of(get_lan_ip()) or _local_mac(),
    }


def is_active(uid: str) -> bool:
    """判断某个上传是否仍在进行（心跳在有效期内）。

    浏览器在上传期间会定期上报心跳；一旦页面关闭、崩溃或断网，心跳停止，
    该任务就会被重新归入"未完成的任务"，从而不与"上传中"列表重复显示。
    """
    rec = ACTIVE_UPLOADS.get(uid)
    if not rec:
        return False
    stamp = rec.get("ts") if isinstance(rec, dict) else rec
    return bool(stamp) and (time.time() - stamp) < ACTIVE_TTL


def mark_active(uid: str, uploaded: int = 0, speed: float = 0.0):
    """标记为"正在上传"，并记录客户端上报的进度与速度。"""
    ACTIVE_UPLOADS[uid] = {
        "ts": time.time(),
        "uploaded": int(uploaded or 0),
        "speed": float(speed or 0.0),
    }


def mark_inactive(uid: str):
    """取消"正在上传"标记（暂停 / 完成 / 删除时调用）。"""
    ACTIVE_UPLOADS.pop(uid, None)


def list_active_uploads() -> list:
    """列出所有正在上传的任务（含进度/速度/来源），供其他设备同步显示。

    进度优先取客户端心跳上报的值；若客户端未上报（例如本机上传器
    刚创建任务），则回退读取 .info 里的 offset，保证一开始就能显示。
    """
    now = time.time()
    items = []
    for uid, info in list(ACTIVE_UPLOADS.items()):
        if now - info["ts"] >= ACTIVE_TTL:
            ACTIVE_UPLOADS.pop(uid, None)   # 心跳超时，视为已中断
            continue
        # 兜底：任务完成/删除后 .info 会消失，此时无论心跳状态如何都不该算"正在上传"
        if not os.path.isfile(os.path.join(TUS_DIR, uid + ".info")):
            ACTIVE_UPLOADS.pop(uid, None)
            continue
        meta_path_ = os.path.join(TUS_DIR, uid + ".info")
        name, total, offset, created = uid, 0, 0, ""
        try:
            with open(meta_path_, "r", encoding="utf-8") as fp:
                raw = json.load(fp)
            name = clean_name((raw.get("metadata") or {}).get("filename") or uid)
            total = int(raw.get("size") or 0)
            offset = int(raw.get("offset") or 0)
            created = raw.get("created_at") or ""
        except (OSError, ValueError):
            pass
        uploaded = max(info.get("uploaded") or 0, offset)
        ip = CLIENT_MAP.get(uid, "")
        device = device_info(ip) if ip else {"name": "", "mac": ""}
        items.append({
            "uid": uid,
            "name": name,
            "size": total,
            "uploaded": min(uploaded, total) if total else uploaded,
            "speed": round(info.get("speed") or 0.0, 2),
            "client_ip": ip,
            "client_name": device["name"],
            "client_mac": device["mac"],
            "created_at": created,
            "elapsed": int(now - info["ts"]),
        })
    items.sort(key=lambda x: x["created_at"], reverse=True)
    return items


def scan_pending_uploads() -> list:
    """扫描 TUS 工作目录，返回**未完成**的上传任务列表。

    程序被强制关闭后，已传分片和 .info 仍留在磁盘上，这里就能把它们
    还原成"未完成任务"（默认暂停状态），供网页端与 GUI 展示。
    """
    pending = []
    if not os.path.isdir(TUS_DIR):
        return pending
    for name in os.listdir(TUS_DIR):
        if not name.endswith(".info"):
            continue
        info_path = os.path.join(TUS_DIR, name)
        try:
            with open(info_path, "r", encoding="utf-8") as fp:
                info = json.load(fp)
        except (OSError, ValueError):
            continue
        uid = name[:-5]
        data_path = os.path.join(TUS_DIR, uid)
        if not os.path.isfile(data_path):
            continue
        total = int(info.get("size") or 0)
        offset = int(info.get("offset") or 0)
        if total > 0 and offset >= total:
            continue  # 已完成但回调未跑完（异常残留），不展示
        if is_active(uid):
            continue  # 正在上传：只应出现在"上传中"区域，避免两处重复
        meta = info.get("metadata") or {}
        client_ip = CLIENT_MAP.get(uid, "")
        pending.append({
            "uid": uid,
            "name": clean_name(meta.get("filename") or uid),
            "size": total or os.path.getsize(data_path),
            "offset": offset,
            "client_ip": client_ip,
            "uploaded_at": info.get("created_at") or "",
        })
    pending.sort(key=lambda x: x["uploaded_at"], reverse=True)
    return pending


def sha256_of(path: str) -> str:
    """分块计算文件 SHA256（4MB 一块，内存占用恒定，100G 文件也不会占用多余内存）。"""
    digest = hashlib.sha256()
    with open(path, "rb") as fp:
        while True:
            block = fp.read(CHUNK_READ)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def process_memory_mb() -> float:
    """读取本进程当前占用的物理内存（MB），用于验证"流式传输不涨内存"。

    Windows 走 psapi.dll，类 Unix 走 resource，无需额外依赖。
    """
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class _PMC(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            # 必须显式声明参数与返回类型：GetCurrentProcess 返回的是 64 位句柄，
            # 若按 ctypes 默认的 int 处理会被截断，导致取不到内存数据（返回 0）。
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            psapi.GetProcessMemoryInfo.argtypes = [
                wintypes.HANDLE, ctypes.POINTER(_PMC), wintypes.DWORD]
            psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

            counters = _PMC()
            counters.cb = ctypes.sizeof(_PMC)
            if not psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(),
                                              ctypes.byref(counters), counters.cb):
                return -1.0
            return counters.WorkingSetSize / 1048576
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    except Exception:
        return -1.0

# ==========================================================================
# 三、元数据管理（每个完成文件一条 JSON）
# ==========================================================================


def meta_path(file_id: str) -> str:
    return os.path.join(META_DIR, f"{file_id}.json")


def write_meta(file_id: str, data: dict):
    """写入元数据。"""
    with open(meta_path(file_id), "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=1)


def read_all_meta() -> list:
    """读取全部元数据，按完成时间倒序。"""
    items = []
    for name in os.listdir(META_DIR):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(META_DIR, name), "r", encoding="utf-8") as fp:
                item = json.load(fp)
            # 文件可能被用户手动删除，跳过失效记录
            if os.path.isfile(item.get("path", "")):
                items.append(item)
        except (OSError, ValueError):
            continue
    items.sort(key=lambda x: x.get("finished_at") or "", reverse=True)
    return items


# ==========================================================================
# 四、TUS 钩子：磁盘预检 / 完成后处理
# ==========================================================================

# 需要额外预留的空间：分片还在 .tus 目录，完成后要移走，
# 因此只要求"剩余空间 >= 文件大小 + 1GB 余量"即可，不必预留双倍。
RESERVE_MARGIN = 1 << 30  # 1GB 安全余量


def pre_create_hook(metadata: dict, upload_info: dict):
    """上传创建前校验磁盘空间，不足直接拒绝（返回 507）。

    tuspyserver 的 pre_create 钩子在解析完 Upload-Length 之后调用，
    upload_info["size"] 即客户端声明的文件总大小；这里抛 HTTPException
    会被 FastAPI 异常处理器捕获，浏览器收到标准的 507 响应。
    """
    size = upload_info.get("size") or 0
    if size <= 0:
        return  # 长度未知（defer-length）时不拦截
    free = disk_free(UPLOAD_DIR)
    if free < size + RESERVE_MARGIN:
        need = human_size(size + RESERVE_MARGIN)
        have = human_size(free)
        raise HTTPException(
            status_code=507,
            detail=f"磁盘空间不足：需要 {need}，仅剩 {have}。请清理磁盘后再试。",
        )


async def on_upload_complete(file_path: str, info: dict):
    """上传完成回调：重命名为原文件名、算 SHA256、落元数据。

    tuspyserver 传进来的第二个参数就是 Upload-Metadata 解析后的字典本身
    （形如 {"filename": ..., "filetype": ...}）。

    必须是 async 且把重命名 / 哈希放到线程里执行：100G 文件算 SHA256
    可能耗时数分钟，若在事件循环中同步执行，整个服务（列表页、其它设备的
    上传）都会被卡住。线程中内存占用恒定，不影响流式传输。
    """
    try:
        meta = info or {}
        uid = os.path.basename(file_path)
        original = clean_name(meta.get("filename") or uid)
        final_path = unique_path(UPLOAD_DIR, original)

        # 重命名（.tus 临时文件 → uploads 正式文件）与哈希计算都放到线程
        await asyncio.to_thread(os.replace, file_path, final_path)
        size = await asyncio.to_thread(os.path.getsize, final_path)

        # 数据已完整落盘，立刻取消"正在上传"标记：
        # 其他设备不必等服务端算完 SHA256（大文件可能耗时数十秒）才停止显示进度
        mark_inactive(uid)

        print(f"[接收] 计算 SHA256：{os.path.basename(final_path)}  {human_size(size)}", flush=True)
        digest = await asyncio.to_thread(sha256_of, final_path)

        # 客户端信息：IP 由中间件记录，机器名 / MAC 由服务端反查（见 device_info）
        client_ip = CLIENT_MAP.get(uid, "")
        device = await asyncio.to_thread(device_info, client_ip) if client_ip else \
            {"ip": "", "name": "", "mac": ""}

        file_id = uuid.uuid4().hex[:12]
        await asyncio.to_thread(write_meta, file_id, {
            "id": file_id,
            "name": os.path.basename(final_path),
            "path": final_path,
            "size": size,
            "sha256": digest,
            "uploaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "client_ip": client_ip,
            "client_name": device["name"],
            "client_mac": device["mac"],
        })
        print(f"[完成] {os.path.basename(final_path)}  {human_size(size)}  sha256={digest[:16]}…", flush=True)

        # 删除 TUS 的 .info：该上传已结束，不再参与过期清理
        old_info = os.path.splitext(file_path)[0] + ".info"
        if os.path.isfile(old_info):
            os.remove(old_info)
        mark_inactive(uid)          # 任务完成，取消"正在上传"标记
    except Exception as exc:  # 任何异常都不应影响服务本身
        print(f"[警告] 完成后处理失败：{exc}", flush=True)
    finally:
        # 无论后续处理是否出错，都不能让它一直显示为"正在上传"
        try:
            mark_inactive(os.path.basename(file_path))
        except Exception:
            pass


# ==========================================================================
# 五、过期分片清理
# ==========================================================================


def cleanup_expired(days: int = EXPIRE_DAYS) -> int:
    """清理超过保留期的未完成分片，返回清理的文件数。

    两部分：
      1) 带 .info 且已过期的上传（按 TUS 规范 expires 为 HTTP 日期格式解析）；
      2) 没有任何 .info 关联的孤立分片文件（进程被强杀等异常残留）。
    """
    removed = 0
    now = datetime.now()
    deadline = now - timedelta(days=days)
    from email.utils import parsedate_to_datetime

    for name in os.listdir(TUS_DIR):
        path = os.path.join(TUS_DIR, name)
        if not os.path.isfile(path):
            continue
        if name.endswith(".info"):
            try:
                with open(path, "r", encoding="utf-8") as fp:
                    info = json.load(fp)
                expires = info.get("expires")
                if not expires:
                    continue
                expire_time = parsedate_to_datetime(expires)
                if expire_time.tzinfo is not None:
                    expire_time = expire_time.astimezone().replace(tzinfo=None)
                if expire_time >= now:
                    continue
                uid = name[:-5]
            except (OSError, ValueError):
                continue
        else:
            uid = name
            try:
                if datetime.fromtimestamp(os.path.getmtime(path)) >= deadline:
                    continue
            except OSError:
                continue
        for target in (os.path.join(TUS_DIR, uid), path):
            if os.path.isfile(target):
                try:
                    os.remove(target)
                    removed += 1
                except OSError:
                    pass

    if removed:
        print(f"[清理] 移除 {removed} 个过期分片文件", flush=True)
    return removed

# ==========================================================================
# 六、内嵌网页（手机 / 电脑同一页面，不依赖任何前端工程）
# ==========================================================================

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>局域网文件传输</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>&#128193;</text></svg>">
<style>
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;
     background:#0f1220;color:#eef1ff}
.wrap{max-width:960px;margin:0 auto;padding:16px 14px 60px}
h1{font-size:20px;margin:0 0 4px}
.sub{color:#98a1c4;font-size:13px;margin:0 0 14px}
.card{background:#181c30;border:1px solid #2b3152;border-radius:12px;padding:14px;margin-bottom:14px}
.box{border:2px dashed #3a4372;border-radius:12px;padding:24px 12px;text-align:center;cursor:pointer}
.box:hover{background:#1d2338}
.row{padding:10px;border-radius:10px;background:#1e2338;margin-top:9px}
.nm{font-size:14px;font-weight:600;word-break:break-all}
.sz{color:#98a1c4;font-size:12px;margin-top:3px;word-break:break-all}
.bar{height:7px;background:#2b3152;border-radius:6px;overflow:hidden;margin-top:7px}
.bar>i{display:block;height:100%;width:0;background:#3f7cff;transition:width .25s}
.st{font-size:12px;margin-top:5px;color:#98a1c4}
.ok{color:#22d3a6}.err{color:#ff5c72}
table{width:100%;border-collapse:collapse}
th,td{padding:9px 6px;border-bottom:1px solid #2b3152;font-size:13.5px;text-align:left}
th{color:#98a1c4;font-size:12px;font-weight:600}
tr{cursor:pointer}tr:hover{background:#1e2338}
.empty{color:#98a1c4;text-align:center;padding:22px 0;font-size:13px}
a{color:#8fb0ff}
.tag{display:inline-block;padding:1px 7px;border-radius:8px;background:#1e2338;font-size:11.5px;color:#98a1c4}
.hd{display:flex;justify-content:space-between;align-items:center;gap:10px}
.pct{font-size:12.5px;color:#8fb0ff;flex:none}
.ops{display:flex;gap:6px;margin-top:8px}
button.mini{background:#262c4a;border:1px solid #363d61;color:#cfd6f5;border-radius:7px;
  padding:3px 12px;font-size:12.5px;cursor:pointer}
button.mini:hover{background:#313a5c}
button.mini[data-a="del"]{color:#ffb3bd;border-color:#5a3440}
</style>
</head>
<body>
<div class="wrap">
  <h1>局域网文件传输</h1>
  <p class="sub" id="info">正在连接…</p>

  <div class="card" id="remoteCard" hidden>
    <b>其他设备正在上传</b>
    <div class="sz" style="margin:4px 0 8px">实时同步显示局域网内其它设备的上传进度（只读）</div>
    <div id="remote"></div>
  </div>

  <div class="card">
    <div class="box" id="box">
      <div style="font-size:32px">&#128196;</div>
      <div style="margin-top:8px">点击选择文件（可多选，支持 100G 级大文件）</div>
      <div class="sz" id="pickTip">分片上传 · 支持断点续传 · 可随时暂停继续</div>
    </div>
    <input type="file" id="pick" multiple hidden>
    <div id="up"></div>
  </div>

  <div class="card" id="pendingCard" hidden>
    <b>未完成的任务（程序异常关闭后保留，默认暂停）</b>
    <div class="sz" style="margin:4px 0 8px" id="pendingTip"></div>
    <div id="pending"></div>
  </div>

  <div class="card">
    <b>全部文件（点击任意一行下载）</b>
    <table>
      <thead><tr><th>文件名</th><th>大小</th><th>接收时间</th><th>来源IP</th><th>计算机名</th><th>MAC地址</th></tr></thead>
      <tbody id="rows"></tbody>
    </table>
    <div class="empty" id="empty">暂无文件</div>
  </div>
</div>

<script>
/*__TUS_JS__*/

function fmt(n){
  const u = ['B','KB','MB','GB','TB','PB'];
  let i = 0;
  while(n >= 1024 && i < u.length-1){ n /= 1024; i++; }
  return (i ? n.toFixed(2) : n) + ' ' + u[i];
}
function toast(msg){
  let el = document.getElementById('toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'toast';
    el.style.cssText = 'position:fixed;left:50%;bottom:26px;transform:translateX(-50%);'
      + 'background:#262c4a;border:1px solid #363d61;color:#eef1ff;padding:9px 16px;'
      + 'border-radius:10px;font-size:13px;z-index:99;opacity:0;transition:.25s';
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.style.opacity = '1';
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.style.opacity = '0'; }, 2600);
}
function esc(s){
  return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

/* ---------------- 上传任务管理（支持暂停 / 继续 / 删除） ---------------- */
/* 任务状态枚举：页面渲染与持久化一律以此为准 */
const STATE = {
  WAITING:   'waiting',    // 排队中
  UPLOADING: 'uploading',  // 上传中（唯一允许出现在「上传中」区域的状态）
  PAUSED:    'paused',     // 用户主动暂停
  ERROR:     'error',      // 失败且重试无果
  COMPLETED: 'completed',  // 已完成
};
const STATE_TEXT = {
  waiting: '排队中', uploading: '上传中', paused: '已暂停',
  error: '失败', completed: '已完成',
};
const TASKS = [];   // {file, upload, el, status, uid}
let pendingResume = null;   // 点「继续」后待绑定的未完成任务

/* 心跳：声明"该任务正在上传"，让服务端不要把它算作未完成任务。
   页面关闭 / 崩溃 / 断网后心跳停止，任务自动回到「未完成」列表。*/
let heartbeatTimer = null;
let heartTask = null;
function beatOnce(uid, uploaded, speed){
  if (!uid) return;
  fetch('/api/active/' + uid, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({uploaded: uploaded || 0, speed: speed || 0})
  }).catch(() => {});
}
/* 每个任务各自维护心跳（支持多任务并行上传）；
   uid 在 POST 创建成功后才由 tus-js-client 写入 url，因此这里每轮重新解析。*/
function startBeat(t){
  stopBeat(t);
  const tick = () => {
    const u = taskUid(t);
    if (u) beatOnce(u, t.uploaded || 0, t.speed || 0);
  };
  t._beatTimer = setInterval(tick, 1500);
  setTimeout(tick, 300);
  setTimeout(tick, 1200);
}
function stopBeat(t){
  if (t._beatTimer) { clearInterval(t._beatTimer); t._beatTimer = null; }
  const u = taskUid(t);
  if (u) fetch('/api/active/' + u, {method: 'DELETE'}).catch(() => {});
}
function taskUid(t){
  let url = '';
  if (t.upload && t.upload.url) url = String(t.upload.url);
  if (!url && t.resumeUrl) url = String(t.resumeUrl);
  const m = url.match(/([0-9a-f]{8,64})(?:\\?|$)/);
  return m ? m[1] : '';
}

function taskRow(t){
  const el = document.createElement('div');
  el.className = 'row';
  el.innerHTML =
    '<div class="hd"><span class="nm">' + esc(t.file.name) + '</span>'
    + '<span class="pct">0.00%</span></div>'
    + '<div class="sz">' + fmt(t.file.size) + '</div>'
    + '<div class="bar"><i></i></div>'
    + '<div class="st">排队中…</div>'
    + '<div class="ops">'
    + '<button class="mini" data-a="pause">暂停</button>'
    + '<button class="mini" data-a="resume" hidden>继续</button>'
    + '<button class="mini" data-a="del">删除</button>'
    + '</div>';
  t.el = el;
  t.bar = el.querySelector('.bar > i');
  t.st = el.querySelector('.st');
  t.pct = el.querySelector('.pct');
  el.querySelector('.ops').addEventListener('click', ev => {
    const act = ev.target.dataset.a;
    if (act === 'pause') pauseTask(t);
    else if (act === 'resume') startTask(t);
    else if (act === 'del') deleteTask(t);
  });
  return el;
}

/* 依据状态统一渲染：只有 uploading/waiting 允许显示暂停按钮 */
function renderTask(t){
  const running = t.status === STATE.UPLOADING || t.status === STATE.WAITING;
  t.el.querySelector('[data-a="pause"]').hidden = !running;
  t.el.querySelector('[data-a="resume"]').hidden = running;
  const st = t.el.querySelector('.st');
  st.className = 'st' + (t.status === STATE.ERROR ? ' err' : (t.status === STATE.COMPLETED ? ' ok' : ''));
}

async function startTask(t){
  if (t.upload) {
    t.status = STATE.UPLOADING;
    t.upload.start();
    renderTask(t);
    startBeat(t);
    refreshPending();      // 立即刷新：恢复上传后该任务不应再出现在"未完成"
    return;
  }
  t.status = STATE.WAITING;
  renderTask(t);
  // 速度计算：每 0.3 秒采样一次并平滑，避免数字跳动
  t.lastTick = Date.now(); t.lastBytes = 0; t.speed = 0; t.uploaded = t.uploaded || 0;
  // 先向服务端查有没有同名同大小的未完成任务：
  // tus-js-client 自带的指纹存在浏览器里，清缓存/换浏览器就会失效，
  // 这里按"文件名 + 大小"兜底，保证任何情况下都能从断点续传。
  if (!t.resumeUrl) {
    try {
      const d = await (await fetch('/api/pending')).json();
      const hit = (d.pending || []).find(p =>
        p.name === t.file.name && p.size === t.file.size);
      if (hit) {
        t.resumeUrl = '/api/upload/' + hit.uid;
        t.uploaded = hit.offset || 0;
        t.st.textContent = '检测到未完成任务，从断点续传…';
      }
    } catch (e) {}
  }
  const opts = {
    endpoint: '/api/upload/',
    uploadUrl: t.resumeUrl || undefined,   // 指定则直接续传该任务
    chunkSize: 32 * 1024 * 1024,
    retryDelays: [0, 1000, 3000, 5000, 10000, 20000, 30000, 60000],
    // 不用浏览器指纹（localStorage）：它指向的旧任务可能已被删除，
    // 会导致 PATCH 404；续传统一以服务端 /api/pending 的结果为准
    resumeFromPreviousUpload: false,
    removeFingerprintOnSuccess: true,
    // filetype 必须有值：tuspyserver 在 HEAD（续传第一步）会校验，
    // 空字符串会导致 400，续传退化成重新上传
    metadata: { filename: t.file.name,
                filetype: t.file.type || 'application/octet-stream' },
    onError(err){
      const code = err && err.originalResponse ? err.originalResponse.getStatus() : 0;
      if (code === 404 && !t.retried) {
        // 服务端的这个任务已被删除（过期清理/手动删除）：
        // 丢掉旧地址，重新建一个上传接着传
        t.retried = true;
        t.upload = null;
        t.resumeUrl = null;
        startTask(t);
        return;
      }
      t.status = STATE.ERROR;
      stopBeat(t);
      t.st.textContent = '失败：' + (err && err.message ? err.message : '未知错误');
      renderTask(t);
      refreshPending();
    },
    onProgress(uploaded, total){
      const now = Date.now(), dt = (now - t.lastTick) / 1000;
      if (dt >= 0.3) {                       // 速度按 0.3 秒采样并平滑
        const inst = (uploaded - t.lastBytes) / 1048576 / dt;
        t.speed = t.speed ? t.speed * 0.6 + inst * 0.4 : inst;
        t.lastBytes = uploaded; t.lastTick = now;
      }
      t.uploaded = uploaded;
      // 双精度计算，保留两位小数；进度条与百分比始终同步
      const p = total ? Math.min(100, uploaded / total * 100) : 0;
      t.bar.style.width = p.toFixed(2) + '%';
      t.pct.textContent = p.toFixed(2) + '%';
      t.st.textContent = p.toFixed(2) + '%  (' + fmt(uploaded) + ' / ' + fmt(total) + ')'
        + '  速度 ' + t.speed.toFixed(1) + ' MB/s';
      if (t.status !== STATE.UPLOADING) { t.status = STATE.UPLOADING; renderTask(t); }
      // 实时上报进度，供其他设备（程序端窗口 / 其它浏览器）同步显示
      if (t._beatTimer) { const u = taskUid(t); if (u) beatOnce(u, uploaded, t.speed); }
      scheduleRemoteRefresh();
    },
    onSuccess(){
      t.status = STATE.COMPLETED;
      stopBeat(t);
      t.bar.style.width = '100.00%';
      t.pct.textContent = '100.00%';
      t.st.textContent = '上传完成';
      renderTask(t);
      t.el.querySelector('[data-a="del"]').textContent = '清除';
      refresh(); refreshPending();
      // 短暂停留显示"完成"，随后从上传区移除（文件已进入「全部文件」）
      setTimeout(() => {
        t.el.remove();
        const i = TASKS.indexOf(t);
        if (i >= 0) TASKS.splice(i, 1);
      }, 1500);
    }
  };
  t.upload = new tus.Upload(t.file, opts);
  t.status = STATE.UPLOADING;
  renderTask(t);
  t.upload.start();
  startBeat(t);
  setTimeout(refreshPending, 1200);
}

function pauseTask(t){
  if (t.upload) { t.upload.abort(); }
  t.status = STATE.PAUSED;          // 主动暂停：此时才允许进入"未完成任务"
  stopBeat(t);
  const p = t.file.size ? Math.min(100, (t.uploaded || 0) / t.file.size * 100) : 0;
  t.st.textContent = '已暂停 · ' + p.toFixed(2) + '%（进度已保留）';
  renderTask(t);
  refreshPending();
}

async function deleteTask(t){
  if (!confirm('删除该上传任务？已上传的分片数据也会被清除。')) return;
  stopBeat(t);
  // 若服务端已有分片，调用终止接口彻底清理（含 .info 与分片文件）
  const gone = taskUid(t);
  if (gone) { try { await fetch('/api/pending/' + gone, {method: 'DELETE'}); } catch (e) {} }
  t.el.remove();
  const i = TASKS.indexOf(t);
  if (i >= 0) TASKS.splice(i, 1);
  refreshPending();
}

function addFiles(files){
  [...files].sort((a, b) => a.size - b.size).forEach(f => {
    const t = { file: f, upload: null, status: 'new' };
    // 若刚从某个未完成任务的「继续」进来，且文件匹配，就绑定到那个任务
    if (pendingResume) {
      if (f.name === pendingResume.name && f.size === pendingResume.size) {
        t.resumeUrl = '/api/upload/' + pendingResume.uid;
      } else {
        toast('所选文件与待续传任务不一致，将作为新任务上传');
      }
      pendingResume = null;
    }
    TASKS.push(t);
    document.getElementById('up').appendChild(taskRow(t));
    startTask(t);
  });
}

/* ---------------- 其他设备正在上传（跨设备进度同步，只读） ---------------- */
let remoteTimer = null;
function scheduleRemoteRefresh(){
  if (remoteTimer) return;
  remoteTimer = setTimeout(() => { remoteTimer = null; refreshRemote(); }, 1000);
}

async function refreshRemote(){
  const box = document.getElementById('remote');
  if (!box) return;
  let d;
  try {
    d = await (await fetch('/api/active')).json();
  } catch (e) { return; }
  const mine = new Set(TASKS.map(taskUid).filter(Boolean));
  const items = (d.active || []).filter(a => !mine.has(a.uid));
  document.getElementById('remoteCard').hidden = items.length === 0;
  box.innerHTML = items.map(a => {
    const pct = a.size ? Math.min(100, a.uploaded / a.size * 100) : 0;
    const pctTxt = pct.toFixed(2);
    const who = [a.client_name, a.client_ip].filter(Boolean).join(' / ') || '未知设备';
    return '<div class="row">'
      + '<div class="hd"><span class="nm">' + esc(a.name) + '</span>'
      + '<span class="pct">' + pctTxt + '%</span></div>'
      + '<div class="sz">来自 ' + esc(who)
      + '　已传 ' + fmt(a.uploaded) + ' / ' + fmt(a.size)
      + (a.speed > 0 ? '　速度 ' + a.speed.toFixed(1) + ' MB/s' : '')
      + '</div>'
      + '<div class="bar"><i style="width:' + pctTxt + '%"></i></div>'
      + '<div class="st">对方上传中 · 进度已同步</div>'
      + '</div>';
  }).join('');
}

/* ---------------- 未完成任务（程序异常关闭后残留） ---------------- */
async function refreshPending(){
  const d = await (await fetch('/api/pending')).json();
  const box = document.getElementById('pending');
  // 前端兜底：把本页面正在正常上传（waiting/uploading）的任务过滤掉，
  // 避免心跳延迟时同一个任务在两处重复显示
  const live = new Set(TASKS
    .filter(t => t.status === STATE.UPLOADING || t.status === STATE.WAITING)
    .map(taskUid).filter(Boolean));
  const items = (d.pending || []).filter(p => !live.has(p.uid));
  document.getElementById('pendingCard').hidden = items.length === 0;
  box.innerHTML = items.map(p => {
    const pct = p.size ? Math.min(100, p.offset / p.size * 100) : 0;
    const pctTxt = pct.toFixed(2);
    return '<div class="row">'
      + '<div class="hd"><span class="nm">' + esc(p.name) + '</span>'
      + '<span class="pct">' + pctTxt + '%</span></div>'
      + '<div class="sz">已传 ' + fmt(p.offset) + ' / ' + fmt(p.size)
      + (p.client_ip ? '　来自 ' + esc(p.client_ip) : '') + '</div>'
      + '<div class="bar"><i style="width:' + pctTxt + '%"></i></div>'
      + '<div class="st err">未完成 · 已暂停</div>'
      + '<div class="ops">'
      + '<button class="mini" data-a="resume" data-u="' + p.uid + '"'
      + ' data-n="' + esc(p.name) + '" data-s="' + p.size + '">继续</button>'
      + '<button class="mini" data-a="drop" data-u="' + p.uid + '">删除</button>'
      + '</div>'
      + '</div>';
  }).join('');
  box.querySelectorAll('button[data-a="drop"]').forEach(b => {
    b.onclick = async () => {
      if (!confirm('删除该未完成任务？已上传的分片将被清除。')) return;
      await fetch('/api/pending/' + b.dataset.u, {method: 'DELETE'});
      refreshPending();
    };
  });
  // 「继续」：支持 File System Access API 时自动读取原文件，免弹框；
  // 否则降级为手动选择同一个文件
  box.querySelectorAll('button[data-a="resume"]').forEach(b => {
    b.onclick = () => resumePendingTask(b.dataset.u, b.dataset.n, Number(b.dataset.s));
  });
}


function renderTips(){
  const el = document.getElementById('pickTip');
  if (el) el.textContent = FSA_OK
    ? '分片上传 · 支持断点续传 · 选择后本机可免重选直接续传'
    : '分片上传 · 支持断点续传 · 可随时暂停继续（多选）';
  const pt = document.getElementById('pendingTip');
  if (pt) pt.innerHTML = FSA_OK
    ? '点「继续」将<b>自动读取原文件</b>并从断点接着传（浏览器授权后生效）'
    : '点「继续」后需选中<b>同一个文件</b>以续传；手机通过局域网 IP 访问时浏览器禁止网页自动读取本地文件（安全限制）';
}

const pick = document.getElementById('pick'), box2 = document.getElementById('box');
pick.onchange = () => { addFiles(pick.files); pick.value = ''; };
box2.onclick = () => { FSA_OK ? pickByFSA() : pick.click(); };

/* ==========================================================================
   File System Access API：让「继续」无需重新选择文件
   --------------------------------------------------------------------------
   浏览器限制（重要）：
     File System Access API 只在「安全上下文」可用，即 https:// 或 http://localhost。
     通过局域网 IP（http://192.168.1.x:端口）访问时 isSecureContext === false，
     浏览器会禁用该 API —— 这是浏览器的硬性限制，任何网站都无法绕过。
     因此：
       电脑本机用 http://127.0.0.1:端口 打开 → 可自动读取原文件续传
       手机 / 局域网 IP 打开              → 只能降级为手动重新选择
   ========================================================================== */
const FSA_OK = (typeof window.showOpenFilePicker === 'function') && window.isSecureContext;

function handleKey(name, size) { return name + '|' + size; }

function openHandleDB() {
  return new Promise((resolve, reject) => {
    if (typeof indexedDB === 'undefined') { reject(new Error('无 IndexedDB')); return; }
    const rq = indexedDB.open('lan-transfer-handles', 1);
    rq.onupgradeneeded = () => rq.result.createObjectStore('handles');
    rq.onsuccess = () => resolve(rq.result);
    rq.onerror = () => reject(rq.error);
  });
}

async function saveHandle(key, handle) {
  try {
    const db = await openHandleDB();
    db.transaction('handles', 'readwrite').objectStore('handles').put(handle, key);
  } catch (e) { /* 存储失败不影响上传 */ }
}

async function loadHandle(key) {
  try {
    const db = await openHandleDB();
    return await new Promise((resolve) => {
      const rq = db.transaction('handles', 'objectStore').objectStore('handles').get(key);
      rq.onsuccess = () => resolve(rq.result || null);
      rq.onerror = () => resolve(null);
    });
  } catch (e) { return null; }
}

/* 选择文件：支持时用 File System Access API（可记住句柄，免重选续传） */
async function pickByFSA() {
  try {
    const [handle] = await window.showOpenFilePicker({ multiple: false });
    const file = await handle.getFile();
    await saveHandle(handleKey(file.name, file.size), handle);
    addFiles([file]);
  } catch (e) {
    if (e && e.name !== 'AbortError') toast('选择文件失败：' + e.message);
  }
}

/* 「继续」：优先用记住的文件句柄直接读原文件；不可用或校验失败才回退到选择框 */
async function resumePendingTask(uid, name, size) {
  if (FSA_OK) {
    const handle = await loadHandle(handleKey(name, size));
    if (handle) {
      try {
        let perm = await handle.queryPermission({ mode: 'read' });
        if (perm !== 'granted') perm = await handle.requestPermission({ mode: 'read' });
        if (perm === 'granted') {
          const file = await handle.getFile();
          if (file.name === name && file.size === size) {
            const t = { file: file, upload: null, status: STATE.WAITING,
                        resumeUrl: '/api/upload/' + uid };
            TASKS.push(t);
            document.getElementById('up').appendChild(taskRow(t));
            toast('已自动读取原文件，从断点继续');
            startTask(t);
            return;
          }
          toast('原文件已变化（大小 ' + fmt(file.size) + '），请重新选择');
          pendingResume = { uid: uid, name: name, size: size };
          pick.click();
          return;
        }
        toast('未获得文件访问授权，请重新选择');
      } catch (e) { /* 落到下面降级 */ }
    } else {
      toast('未找到该文件的记录，请重新选择一次');
    }
    pendingResume = { uid: uid, name: name, size: size };
    pick.click();
    return;
  }
  // 运行环境不支持 FSA（如手机通过局域网 IP 访问）：降级为手动选择
  pendingResume = { uid: uid, name: name, size: size };
  toast('当前环境无法自动读取本地文件，请选中同一个文件以续传');
  pick.click();
}

/* 刷新文件列表 */
async function refresh(){
  let d;
  try {
    d = await (await fetch('/api/files')).json();
  } catch (e) { return; }
  document.getElementById('rows').innerHTML = d.files.map(f =>
    '<tr onclick="location.href=/files/' + encodeURIComponent(f.id) + '">'
    + '<td>' + esc(f.name) + '</td>'
    + '<td>' + fmt(f.size) + '</td>'
    + '<td>' + f.uploaded_at + '</td>'
    + '<td>' + esc(f.client_ip || '-') + '</td>'
    + '<td>' + esc(f.client_name || '-') + '</td>'
    + '<td><span class="tag">' + esc(f.client_mac || '-') + '</span></td>'
    + '<td><button class="mini" data-del="' + f.id + '">删记录</button></td>'
    + '</tr>').join('');
  document.querySelectorAll('button[data-del]').forEach(b => {
    b.onclick = async (ev) => {
      ev.stopPropagation();
      if (!confirm('仅删除该任务记录？uploads 里的实际文件会保留。')) return;
      await fetch('/api/record/' + b.dataset.del, {method: 'DELETE'});
      refresh();
    };
  });
  document.getElementById('empty').style.display = d.files.length ? 'none' : 'block';
  document.getElementById('empty').textContent = d.files.length ? '' : '暂无文件';
  document.getElementById('info').textContent =
    '共 ' + d.count + ' 个文件 / ' + fmt(d.total) + '　剩余磁盘 ' + fmt(d.free)
    + '　地址 ' + location.origin;
}
refresh();
refreshPending();
refreshRemote();
renderTips();
setInterval(refresh, 3000);
setInterval(refreshPending, 3000);
setInterval(refreshRemote, 1500);
</script>
</body>
</html>
"""


def render_page() -> str:
    """把本地 tus.min.js 内联进页面，保证无外网也能正常上传。"""
    try:
        with open(TUS_JS_PATH, "r", encoding="utf-8") as fp:
            lib = fp.read()
    except OSError:
        lib = ""  # 缺失时退化为 CDN
    if not lib:
        lib = '<script src="https://cdn.jsdelivr.net/npm/tus-js-client@4/dist/tus.min.js"></script>'
    return HTML_PAGE.replace("/*__TUS_JS__*/", lib)

# ==========================================================================
# 七、FastAPI 应用装配
# ==========================================================================

# 供 cleanup_expired 使用的 TUS 配置（create_tus_router 生成的同一份）
TUS_OPTIONS = None


async def _no_auth():
    """局域网内不做任何鉴权（需求明确：无登录密码）。"""
    return None


def create_app() -> FastAPI:
    """组装 FastAPI 应用：TUS 上传路由 + 页面 + 列表 / 下载 / 校验接口。"""
    global TUS_OPTIONS
    patch_tus_windows_rename()  # Windows 覆盖 .info 的兼容修复
    ensure_dirs()

    app = FastAPI(title="局域网文件传输工具", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def _record_client(request: Request, call_next):
        """登记"上传 ID → 客户端 IP"。

        TUS 的完成回调拿不到请求信息，所以在这里从 POST 创建上传的响应
        Location 里取出上传 ID，配上发起方的 IP 存起来，完成时就能显示
        是哪台设备传的。
        """
        response = await call_next(request)
        if request.method == "POST" and "/api/upload" in request.url.path:
            location = response.headers.get("location") or ""
            if "/api/upload/" in location:
                uid = location.rstrip("/").rsplit("/", 1)[-1]
                if request.client:
                    CLIENT_MAP[uid] = request.client.host
        return response

    # ---- TUS 分片上传路由 ----
    # max_size 设为 4EB，等同不设上限；auth=None 表示局域网内无需登录
    # strict_offset_validation=True 保证断点续传时严格按 offset 续写
    tus_router = create_tus_router(
        prefix="api/upload",
        files_dir=TUS_DIR,
        max_size=NO_SIZE_LIMIT,
        auth=_no_auth,
        days_to_keep=EXPIRE_DAYS,
        pre_create_hook=pre_create_hook,
        on_upload_complete=on_upload_complete,
        strict_offset_validation=True,
        storage=build_streaming_storage(),  # 逐块直接落盘，内存占用恒定
    )
    # 清理任务需要一份 TUS 配置；这里直接构造一份最小可用的（只用到 files_dir）
    TUS_OPTIONS = TusRouterOptions(
        prefix="api/upload",
        files_dir=TUS_DIR,
        max_size=NO_SIZE_LIMIT,
        auth=None,
        days_to_keep=EXPIRE_DAYS,
        on_upload_complete=on_upload_complete,
        upload_complete_dep=None,
        pre_create_hook=pre_create_hook,
        pre_create_dep=None,
        file_dep=None,
        tags=None,
        tus_version="1.0.0",
        tus_extension="creation,expiration,termination",
        strict_offset_validation=True,
        storage=None,
    )
    app.include_router(tus_router)

    @app.on_event("startup")
    async def _startup():
        """启动后开启过期分片清理定时任务（每小时一次）。"""
        loop = asyncio.get_running_loop()
        loop.create_task(_gc_loop())
        loop.create_task(_memory_watch())

    @app.get("/", response_class=HTMLResponse)
    async def index():
        """首页：上传 + 文件列表。"""
        return render_page()

    @app.get("/api/files")
    async def api_files():
        """全部已上传文件列表（所有设备上传的都在一起）。"""
        items = read_all_meta()
        return {
            "files": items,
            "count": len(items),
            "total": sum(i["size"] for i in items),
            "free": disk_free(UPLOAD_DIR),
        }

    @app.get("/files/{file_id}")
    async def download(file_id: str):
        """流式下载：FileResponse 边读边发，不会把整个文件读进内存。"""
        try:
            with open(meta_path(file_id), "r", encoding="utf-8") as fp:
                item = json.load(fp)
        except (OSError, ValueError):
            raise HTTPException(status_code=404, detail="文件不存在")
        path = item.get("path", "")
        if not os.path.isfile(path):
            raise HTTPException(status_code=404, detail="文件已被删除")
        return FileResponse(path, filename=item["name"],
                            media_type="application/octet-stream")

    @app.get("/verify/{file_id}")
    async def verify(file_id: str):
        """重新计算 SHA256 并与记录比对，用于确认文件未损坏。"""
        try:
            with open(meta_path(file_id), "r", encoding="utf-8") as fp:
                item = json.load(fp)
        except (OSError, ValueError):
            raise HTTPException(status_code=404, detail="文件不存在")
        path = item.get("path", "")
        if not os.path.isfile(path):
            raise HTTPException(status_code=404, detail="文件已被删除")
        digest = await asyncio.to_thread(sha256_of, path)
        ok = digest == item.get("sha256")
        return {"ok": ok, "sha256": digest, "recorded": item.get("sha256")}

    @app.get("/api/space")
    async def api_space():
        """磁盘空间 + 进程内存占用（前端展示 / 验证流式传输）。"""
        usage = shutil.disk_usage(UPLOAD_DIR)
        return {
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
            "rss": await asyncio.to_thread(process_memory_mb),
            "rss_peak": MEMORY_PEAK["value"],
        }

    @app.get("/api/pending")
    async def api_pending():
        """未完成的上传任务（已暂停 / 中断 / 程序异常关闭后残留的分片）。

        正在正常上传的任务有心跳，不会出现在这里——它只应在"上传中"区域出现，
        两处不会重复。用户重新选择同一文件即可断点续传。
        """
        items = await asyncio.to_thread(scan_pending_uploads)
        return {"pending": items, "count": len(items)}

    @app.post("/api/active/{uid}")
    async def api_active(uid: str, request: Request):
        """客户端心跳：声明"这个任务正在上传"，并上报当前进度与速度。

        页面 / 程序端在上传期间定期调用；页面关闭、崩溃或断网后心跳停止，
        服务端就把该任务重新归入"未完成"，从而不会与"上传中"列表重复。

        同时这些进度数据会通过 /api/active 广播给**其他设备**，
        使程序端窗口与其它浏览器都能看到同一个上传任务的实时进度。
        """
        if not re.fullmatch(r"[0-9a-f]{8,64}", uid or ""):
            raise HTTPException(status_code=400, detail="任务 ID 非法")
        payload = {}
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        mark_active(uid,
                    uploaded=int(payload.get("uploaded") or 0),
                    speed=float(payload.get("speed") or 0.0))
        if request.client:
            CLIENT_MAP[uid] = request.client.host
        return {"ok": True, "ttl": ACTIVE_TTL}

    @app.get("/api/active")
    async def api_active_list(request: Request):
        """当前所有正在上传的任务（含进度与速度），供其它设备同步显示。"""
        items = await asyncio.to_thread(list_active_uploads)
        mine = request.query_params.get("exclude") or ""
        if mine:
            items = [i for i in items if i["uid"] != mine]
        return {"active": items, "count": len(items)}

    @app.delete("/api/active/{uid}")
    async def api_inactive(uid: str):
        """取消"正在上传"标记（暂停 / 完成 / 删除时调用）。"""
        mark_inactive(uid)
        return {"ok": True}

    @app.delete("/api/pending/{uid}")
    async def api_drop_pending(uid: str):
        """删除某个未完成任务的临时分片（终止上传，不动已完成的文件）。"""
        if not re.fullmatch(r"[0-9a-f]{8,64}", uid or ""):
            raise HTTPException(status_code=400, detail="任务 ID 非法")
        removed = False
        for suffix in ("", ".info"):
            path = os.path.join(TUS_DIR, uid + suffix)
            if os.path.isfile(path):
                try:
                    os.remove(path)
                    removed = True
                except OSError as exc:
                    raise HTTPException(status_code=500, detail=f"删除失败：{exc}") from exc
        CLIENT_MAP.pop(uid, None)
        mark_inactive(uid)
        if not removed:
            raise HTTPException(status_code=404, detail="任务不存在")
        return {"ok": True}

    @app.delete("/api/record/{file_id}")
    async def api_delete_record(file_id: str):
        """只删除任务记录（.meta），**不删除** uploads 里的实际文件。"""
        path = meta_path(file_id)
        if not os.path.isfile(path):
            raise HTTPException(status_code=404, detail="记录不存在")
        try:
            os.remove(path)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"删除失败：{exc}") from exc
        return {"ok": True, "kept_file": True}

    @app.get("/api/device")
    async def api_device(request: Request):
        """查询某台局域网设备的计算机名 / MAC（供 GUI 与页面显示）。"""
        ip = request.query_params.get("ip") or (request.client.host if request.client else "")
        return await asyncio.to_thread(device_info, ip)

    @app.get("/api/machine")
    async def api_machine():
        """本机计算机名与 MAC。"""
        return await asyncio.to_thread(local_machine_info)

    @app.exception_handler(Exception)
    async def on_unhandled(request: Request, exc: Exception):
        """未捕获异常：打印完整堆栈，便于排查 TUS 上传过程中的问题。"""
        import traceback
        print(f"[错误] {request.method} {request.url.path} -> {exc}", flush=True)
        traceback.print_exc()
        return JSONResponse({"detail": str(exc)}, status_code=500)

    return app


async def _gc_loop():
    """后台任务：每小时清理一次过期分片。"""
    while True:
        try:
            await asyncio.to_thread(cleanup_expired)
        except Exception:
            pass
        await asyncio.sleep(3600)


async def _memory_watch():
    """后台任务：每 2 秒采样一次进程内存并记录峰值。

    用于验证"流式写盘，内存占用恒定"：传输 100G 时峰值也应保持在
    几十 MB 量级；若峰值随文件大小线性增长，说明某处把数据读进了内存。
    """
    while True:
        try:
            value = await asyncio.to_thread(process_memory_mb)
            if value > 0:
                MEMORY_PEAK["value"] = round(max(MEMORY_PEAK["value"], value), 1)
        except Exception:
            pass
        await asyncio.sleep(2)
