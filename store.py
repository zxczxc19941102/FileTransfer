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
import contextvars
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

from netutils import get_lan_ip, run_text

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
LOG_DIR = os.path.join(BASE_DIR, "logs")              # 运行日志
LOG_PATH = os.path.join(LOG_DIR, "app.log")
LOG_MAX_BYTES = 4 * 1024 * 1024                       # 超过即轮转为 app.log.1

# 允许「在电脑上打开」的目录白名单：接口只认这几个 key，
# 不做任意路径透传，避免变成局域网内可远程打开任意位置的后门。
OPENABLE_DIRS = {
    "upload": UPLOAD_DIR,      # 接收到的文件
    "config": UPLOAD_DIR,      # 配置文件与接收文件同目录
    "log": LOG_DIR,
    "base": BASE_DIR,
}

# 4EB —— 等同"不设上限"，真正的限制是磁盘空间
NO_SIZE_LIMIT = 1 << 62
CHUNK_READ = 4 * 1024 * 1024        # 计算哈希时的读取块大小（4MB）
EXPIRE_DAYS = 30                    # 未完成分片保留天数（大文件跨天传输常见，留足时间）
ORPHAN_KEEP_HOURS = 1               # 孤立分片（无 .info）保留小时数，超时即回收
PURGE_INTERVAL = 5                  # 已删除任务的残留分片复查间隔（秒）
MEMORY_PEAK = {"value": 0.0}         # 进程内存峰值（MB），由后台任务更新
SERVER_IP = "127.0.0.1"             # 启动后写入真实内网 IP
SERVER_PORT = 8000                  # 启动后写入真实端口

# ---- 并行任务数（同时进行的上传任务上限）----
# 程序端下拉框与网页端排队逻辑共用这一个值，网页端每 2 秒轮询 /api/settings 同步。
# 默认与硬上限都是 32；超过上限的任务进入「排队中」，有名额后自动开始。
PARALLEL_LIMIT_MAX = 32             # 硬上限，无法通过接口突破
PARALLEL_LIMIT_DEFAULT = 32         # 默认值
PARALLEL = {"value": PARALLEL_LIMIT_DEFAULT}

# ---- 上传限速（两端共用）----
# 单位 字节/秒；0 = 无限制；非 0 时最小 1 MB/s（再小会让请求碎成一片、
# 且对大文件毫无实用价值）。程序端与网页端各自在自己的发送循环里节流。
SPEED_LIMIT_MIN = 1 << 20           # 1 MB/s
SPEED_LIMIT_DEFAULT = 0             # 0 = 无限制
SPEED_LIMIT = {"value": SPEED_LIMIT_DEFAULT}

SETTINGS_PATH = os.path.join(UPLOAD_DIR, ".settings.json")

# 当前请求的客户端 IP（由 HTTP 中间件在进入业务处理前写入）。
# 0 字节文件、或长度极小在 POST 请求内就完成的 TUS 上传，其完成回调
# 早于中间件登记 CLIENT_MAP，只能靠这个上下文变量拿到真实的来源地址。
CLIENT_IP_VAR: contextvars.ContextVar = contextvars.ContextVar("client_ip", default="")

# Windows 上 asyncio proactor 的既知噪音：连接已被对端关闭后再调用
# shutdown()，会抛这些错误并打印整段堆栈，但传输结果完全正确。
_QUIET_WINERRORS = {10022, 10054, 10053, 10038}


def quiet_loop_exception_handler(loop, context: dict):
    """收敛 asyncio 事件循环里的无效报错，其余异常仍按默认方式抛出。

    注意要按 errno/winerror 数字判断：OSError 的 repr 在各语言环境下
    只显示 ``OSError(10022, '…')``，字符串里并不含 "WinError"，
    用文字匹配会漏判。
    """
    exc = context.get("exception")
    if isinstance(exc, OSError):
        code = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
        if code in _QUIET_WINERRORS:
            return  # 连接被对端关闭/重置后的收尾回调，忽略
    loop.default_exception_handler(context)


def ensure_dirs():
    """创建全部工作目录（不存在则自动创建）。"""
    for path in (UPLOAD_DIR, META_DIR, TUS_DIR, LOG_DIR):
        os.makedirs(path, exist_ok=True)


def open_local_dir(path: str) -> bool:
    """在系统文件管理器中打开目录，返回是否成功。

    目录不存在时先建出来，避免用户点「日志」时因为还没有日志而报错。
    path 只能来自 OPENABLE_DIRS 白名单，不接受外部传入的任意路径。
    """
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        return False
    try:
        if os.name == "nt":
            os.startfile(path)
        else:
            subprocess.Popen(["xdg-open", path])
        return True
    except OSError:
        return False


def load_settings():
    """读取持久化的设置（并行任务上限、上传限速），异常时保留默认值。"""
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as fp:
            data = json.load(fp)
    except (OSError, ValueError, TypeError):
        return
    if not isinstance(data, dict):
        return
    try:
        value = int(data.get("max_parallel"))
        PARALLEL["value"] = max(1, min(PARALLEL_LIMIT_MAX, value))
    except (TypeError, ValueError):
        pass
    limit = data.get("speed_limit")
    if isinstance(limit, (int, float)) and (limit == 0 or limit >= SPEED_LIMIT_MIN):
        SPEED_LIMIT["value"] = int(limit)


def save_settings():
    """原子写出设置，避免写入中断把配置弄坏。"""
    tmp = SETTINGS_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump({"max_parallel": PARALLEL["value"],
                       "speed_limit": SPEED_LIMIT["value"]}, fp)
        os.replace(tmp, SETTINGS_PATH)
    except OSError:
        pass


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
                if is_dropped(uid):
                    return  # 任务已被删除：在途分片直接丢弃，不再产生孤立文件
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
ACTIVE_UPLOADS: dict = {}    # uid -> {"ts":…, "uploaded":…, "speed":…}，区分"正在上传"与"已中断"
ACTIVE_TTL = 90              # 心跳超过该秒数未刷新，视为上传已中断（页面关闭/崩溃/断网）
PAUSED_UIDS: dict = {}        # uid -> 操作者 IP，被要求暂停的上传（等客户端心跳确认）
# uid -> 被删除的时刻。删除与"在途 PATCH"之间存在竞态：abort 之后客户端仍可能
# 发出最后一两次分片请求，把刚删掉的临时文件又写回来。写盘前查这张表即可丢弃。
DROP_UIDS: dict = {}
DROP_TTL = 600               # 删除标记保留秒数（足够覆盖任何在途请求）


def is_dropped(uid: str) -> bool:
    """该任务是否处于"已删除"状态（含 TTL 内的写盘拦截窗口）。"""
    stamp = DROP_UIDS.get(uid)
    if not stamp:
        return False
    if time.time() - stamp > DROP_TTL:
        DROP_UIDS.pop(uid, None)
        return False
    return True


def _mac_of(ip: str) -> str:
    """从邻居表（ARP/NDP）反查 MAC 地址，查不到返回空字符串。"""
    if os.name == "nt":
        try:
            out = run_text(["arp", "-a", ip], timeout=3)
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
            out = run_text(["nbtstat", "-A", ip], timeout=3)
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


def task_owner(uid: str) -> str:
    """任务所有者（首次创建上传的客户端 IP）。"""
    return CLIENT_MAP.get(uid, "")


def local_host_addresses() -> set:
    """本机所有可能的来源 IP（回环 + 各网卡地址）。

    用途：用户用 http://192.168.x.x:端口 访问本机服务时，任务的所有者是
    该局域网 IP；而程序端（GUI）通过 http://127.0.0.1:端口 操作同一个任务。
    两者其实是同一台电脑，必须都视为所有者，否则程序端右键删除/暂停会 403。
    """
    hosts = {"127.0.0.1", "::1", "localhost", "0.0.0.0"}
    try:
        from netutils import get_lan_ip, list_lan_ips
        hosts.add(get_lan_ip())
        hosts.update(list_lan_ips())
    except Exception:
        pass
    for probe in (("8.8.8.8", 80), ("114.114.114.114", 53)):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(0.2)
            sock.connect(probe)
            hosts.add(sock.getsockname()[0])
            sock.close()
        except OSError:
            pass
    return {h for h in hosts if h}


LOCAL_HOSTS = local_host_addresses()


def check_owner(uid: str, request: Request):
    """权限校验：只有任务**所有者**才能暂停 / 继续 / 删除该任务。

    CLIENT_MAP 只在创建上传（POST）时写入、后续心跳不覆盖，因此这里能准确
    判断"这个任务是谁的"。判定规则见 viewer_is_owner：同一台电脑的多个地址
    互认，但**其它设备的任务仍然只读**（本机浏览器用局域网 IP 打开也一样）。
    """
    owner = task_owner(uid)
    who = (request.client.host if request.client else "") or ""
    if not viewer_is_owner(uid, who):
        raise HTTPException(
            status_code=403,
            detail=f"只能操作自己上传的任务（该任务来自 {owner}）")


def list_active_uploads(viewer_ip: str = "") -> list:
    """列出所有正在上传的任务（含进度/速度/来源），供其他设备同步显示。

    进度优先取客户端心跳上报的值；若客户端未上报（例如本机上传器
    刚创建任务），则回退读取 .info 里的 offset，保证一开始就能显示。

    ``viewer_ip`` 为请求方地址，用于给每项打上 ``mine``：判断该任务是否由
    **请求方这台电脑**发起。程序端（GUI）发起的上传，浏览器里没有任何记录，
    仅凭前端自己记的 uid 无法识别，会把它误显示成"其他设备正在上传"，
    所以归属必须由服务端判定后下发。
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
            # 是否属于请求方这台电脑：本机上传器创建的任务所有者为 127.0.0.1，
            # 浏览器从本机打开时两者都算本机，不应显示成"其他设备"；
            # 手机访问时它确实属于另一台设备，仍然照常显示。
            "mine": viewer_is_owner(uid, viewer_ip),
        })
    items.sort(key=lambda x: x["created_at"], reverse=True)
    return items


def task_is_local(uid: str) -> bool:
    """任务是否由**本机**（回环或本机任一网卡地址）创建。

    这是**断点续传匹配**用的严格判定：别的设备（手机）传了一半的同名同
    大小任务，绝不能被本机当成自己的断点继续写，否则两台设备的数据会
    互相覆盖。与 check_owner 相比少了"未知所有者也放行"的宽松处理。
    """
    owner = CLIENT_MAP.get(uid, "")
    return not owner or owner in LOCAL_HOSTS


def viewer_is_owner(uid: str, viewer_ip: str) -> bool:
    """以某个客户端视角看，该任务是否归它管。

    同一台电脑可能用 127.0.0.1 或局域网 IP 访问（两个地址都是本机网卡），
    必须互认为所有者；但**其它设备的任务仍然只读**，哪怕请求来自本机。
    """
    owner = CLIENT_MAP.get(uid, "")
    if not owner or not viewer_ip:
        return True                     # 所有者未知（老任务/服务重启）→ 放行
    return owner == viewer_ip or (owner in LOCAL_HOSTS and viewer_ip in LOCAL_HOSTS)


def scan_pending_uploads(include_active: bool = False, viewer_ip: str = "") -> list:
    """扫描 TUS 工作目录，返回**未完成**的上传任务列表。

    程序被强制关闭后，已传分片和 .info 仍留在磁盘上，这里就能把它们
    还原成"未完成任务"（默认暂停状态），供网页端与 GUI 展示。

    ``include_active=True`` 时连"仍带心跳标记"的任务一起返回（多一个
    ``active`` 字段）：客户端的断点续传检索必须用它，否则暂停/崩溃后
    90 秒内查不到自己的断点，会新建任务从头重传。
    """
    pending = []
    if not os.path.isdir(TUS_DIR):
        return pending
    now = time.time()
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
        active = is_active(uid)
        if active and not include_active:
            continue  # 正在上传：只应出现在"上传中"区域，避免两处重复
        meta = info.get("metadata") or {}
        client_ip = CLIENT_MAP.get(uid, "")
        device = device_info(client_ip) if client_ip else {"name": "", "mac": ""}
        pending.append({
            "uid": uid,
            "name": clean_name(meta.get("filename") or uid),
            "size": total or os.path.getsize(data_path),
            "offset": offset,
            "client_ip": client_ip,
            "client_name": device["name"],
            "client_mac": device["mac"],
            "active": active,
            "active_elapsed": int(now - ACTIVE_UPLOADS[uid]["ts"]) if active else 0,
            "local_task": task_is_local(uid),
            "mine": viewer_is_owner(uid, viewer_ip),
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
    """读取全部元数据，按接收时间倒序；源文件已被删除的记录自动跳过。

    ``.meta`` 目录本身也可能不存在（用户把 uploads 下的内容删掉了），
    这里必须容错返回空列表：早期直接 ``os.listdir`` 会抛 FileNotFoundError，
    而该异常发生在 Tk 定时回调里，会让界面自动刷新**永久停摆**——
    表现出来就是"文件删了，客户端列表也不跟着更新"。
    """
    items = []
    if not os.path.isdir(META_DIR):
        return items
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
    # 按接收时间倒序。write_meta 写入的字段是 uploaded_at，
    # 原先这里取 finished_at（从未写入过）导致排序恒定失效、列表顺序随机。
    items.sort(key=lambda x: x.get("uploaded_at") or "", reverse=True)
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

        # 客户端信息：IP 由中间件记录，机器名 / MAC 由服务端反查（见 device_info）。
        # 0 字节文件会在 POST 请求内就触发完成回调，此时中间件还没登记
        # CLIENT_MAP，必须回退到请求上下文里的来源地址，否则来源信息全空。
        client_ip = CLIENT_MAP.get(uid, "") or CLIENT_IP_VAR.get("")
        if client_ip and not CLIENT_MAP.get(uid):
            CLIENT_MAP[uid] = client_ip
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


# 已删除任务的 uid -> 复查截止时间戳。
# 客户端 abort() 与服务端删除分片之间存在竞态：abort 之后 tus 仍可能发出
# 最后一次 PATCH，重新创建出一个没有 .info 的孤立分片。这里在删除后的一小段
# 时间内反复复查，确保残留被清干净（否则要等过期清理才回收）。
PURGE_QUEUE: dict = {}


def queue_purge(uid: str, seconds: int = 30) -> None:
    """登记一个需要在随后若干秒内反复清理的任务 ID。"""
    PURGE_QUEUE[uid] = time.time() + seconds


def purge_once() -> int:
    """清理 PURGE_QUEUE 中登记的残留分片，返回删除的文件数。"""
    removed = 0
    now = time.time()
    for uid, until in list(PURGE_QUEUE.items()):
        for suffix in ("", ".info"):
            path = os.path.join(TUS_DIR, uid + suffix)
            if os.path.isfile(path):
                try:
                    os.remove(path)
                    removed += 1
                except OSError:
                    pass
        if now >= until:
            PURGE_QUEUE.pop(uid, None)
    # 顺手回收过期的"已删除"标记，避免这张表无限增长
    for uid, stamp in list(DROP_UIDS.items()):
        if now - stamp > DROP_TTL:
            DROP_UIDS.pop(uid, None)
    return removed


def purge_missing_records() -> int:
    """清理「源文件已不存在」的元数据记录，返回清理条数。

    只在该记录所在目录**仍然存在**时才判定为"文件被删了"；如果整个目录都
    不见了（磁盘未挂载、目录被整体移走），一律不动，避免误删记录。
    """
    if not os.path.isdir(META_DIR):
        return 0
    removed = 0
    for name in os.listdir(META_DIR):
        if not name.endswith(".json"):
            continue
        record = os.path.join(META_DIR, name)
        try:
            with open(record, "r", encoding="utf-8") as fp:
                target = json.load(fp).get("path") or ""
        except (OSError, ValueError):
            continue                      # 读不出来的坏记录留给人工处理
        folder = os.path.dirname(target)
        if not target or not os.path.isdir(folder):
            continue                      # 连目录都不在：不判定为删除
        try:
            if not os.path.isfile(target):
                os.remove(record)
                removed += 1
        except OSError:
            pass
    if removed:
        print(f"[清理] 移除 {removed} 条源文件已被删除的记录", flush=True)
    return removed


def cleanup_expired(days: int = EXPIRE_DAYS) -> int:
    """清理超过保留期的未完成分片，返回清理的文件数。

    两部分：
      1) 带 .info 且已过期的上传（按 TUS 规范 expires 为 HTTP 日期格式解析）；
      2) 没有任何 .info 关联的孤立分片文件（进程被强杀等异常残留）。
    """
    if not os.path.isdir(TUS_DIR):
        return 0
    removed = 0
    now = datetime.now()
    deadline = now - timedelta(days=days)
    orphan_deadline = now - timedelta(hours=ORPHAN_KEEP_HOURS)
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
            # 孤立分片（没有 .info）：正常上传一定伴随 .info，出现孤立分片
            # 说明上传已被终止，多半是客户端 abort 与服务端删除之间的竞态残留，
            # 用更短的阈值回收即可，不必等满整个保留期。
            try:
                if datetime.fromtimestamp(os.path.getmtime(path)) >= orphan_deadline:
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
/* 折叠角标：任务超过 2 个时出现，点击折叠/展开该任务卡片 */
.tg{flex:none;cursor:pointer;color:#98a1c4;font-size:12px;line-height:1;
  padding:2px 7px;border-radius:6px;background:#262c4a;border:1px solid #363d61;
  user-select:none}
.tg:hover{background:#313a5c;color:#eef1ff}
.row.collapsed .sz,.row.collapsed .bar,.row.collapsed .st,.row.collapsed .ops{display:none}
.row.collapsed .nm{font-size:13px}
</style>
</head>
<body>
<div class="wrap">
  <h1>局域网文件传输</h1>
  <p class="sub" id="info">正在连接…</p>
  <p class="sz" id="paths" style="margin:-8px 0 12px"></p>
  <p class="sz" id="speedBox" style="margin:-8px 0 12px"></p>

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
    <b id="pendingTitle">未完成的任务</b>
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

/* 统一的请求包装：把 HTTP 错误（尤其 403 权限不足）变成可提示的结果，
   避免"点了删除却没反应"的静默失败。*/
async function apiFetch(url, opts){
  try {
    const resp = await fetch(url, opts);
    let data = {};
    try { data = await resp.json(); } catch (e) { data = {}; }
    return {ok: resp.ok, status: resp.status, data: data,
            detail: (data && data.detail) ? data.detail : ''};
  } catch (e) {
    return {ok: false, status: 0, data: {},
            detail: '网络错误：' + (e && e.message ? e.message : e)};
  }
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

/* ---------------- 并行任务上限（与程序端下拉框同步） ----------------
   程序端改数字 -> POST /api/settings -> 本页轮询到新值后重排名额：
   列表靠前的任务保持传输，多出来的转「排队中」，腾出名额再自动续传。 */
const WAIT_TEXT = '排队中（等待空闲名额）';
let maxParallel = 32;        // 当前并行上限
let parallelLimitMax = 32;   // 服务端硬上限
let maxParallelReady = false;

/* 上传限速（字节/秒，0 = 无限制），与程序端下拉框同步；非 0 时最小 1 MB/s。
   实现方式：每个分片请求前在 onBeforeRequest 里按「已传字节 / 目标速率」
   算出应到的时刻，没到就延后——请求数与内存占用都不变，只是发送被节流。 */
let speedLimit = 0;
let speedMin = 1048576;
const SPEED_CHOICES = [0, 1, 2, 5, 10, 20, 50, 100];   // MB/s，0 = 无限制

function runningCount(){
  return TASKS.filter(t => t.status === STATE.UPLOADING).length;
}
function queuedCount(){
  return TASKS.filter(t => t.status === STATE.WAITING).length;
}

/* 调度器：按列表顺序补足名额，先到先传 */
function pump(){
  for (const t of TASKS) {
    if (runningCount() >= maxParallel) break;
    if (t.status === STATE.WAITING) startTask(t);
  }
  syncCards();
}

/* 让出一个名额：中止当前分片但保留服务端进度，状态转为「排队中」 */
function queueTask(t){
  try { if (t.upload) t.upload.abort(); } catch (e) {}
  stopBeat(t);
  t.status = STATE.WAITING;
  if (t.st) t.st.textContent = WAIT_TEXT;
  renderTask(t);
}

/* 上限变小：靠前的 maxParallel 个保持传输，其余转「排队中」 */
function applyParallelLimit(){
  let kept = 0;
  for (const t of TASKS) {
    if (t.status !== STATE.UPLOADING && t.status !== STATE.WAITING) continue;
    kept++;
    if (kept > maxParallel && t.status === STATE.UPLOADING) queueTask(t);
  }
  pump();
}

async function syncSettings(){
  let d;
  try { d = await (await fetch('/api/settings')).json(); } catch (e) { return; }
  const value = Number(d && d.max_parallel) || 32;
  const speed = Number(d && d.speed_limit) || 0;
  parallelLimitMax = Number(d && d.limit) || 32;
  speedMin = Number(d && d.speed_min) || 1048576;

  const first = !maxParallelReady;   // 首次同步不算「被程序端改动」，不弹提示
  const notes = [];
  if (value !== maxParallel) {
    maxParallel = value;
    if (!first) { applyParallelLimit(); notes.push('并行任务数 ' + value); }
  }
  if (speed !== speedLimit) {
    speedLimit = speed;
    applySpeedSelect();
    if (!first) notes.push(speed ? '限速 ' + (speed / 1048576) + ' MB/s' : '取消限速');
  }
  maxParallelReady = true;
  syncCards();
  if (notes.length) toast('已与电脑端同步：' + notes.join('、'));
}

/* 记录本次传输的限速基准：从当前已传字节重新计时（续传/暂停恢复后要重置） */
function markRateStart(t){
  t.rateBase = t.uploaded || 0;
  t.rateStart = Date.now();
}

/* 记住"本标签页发起过的上传 ID"。
   刷新页面后 TASKS 会清空，若只靠它判断，自己正在传的任务会被
   「其他设备正在上传」误报成别人的。用 sessionStorage 持久化，
   刷新后仍能认出；标签页关闭后自动失效。*/
const MY_UIDS_KEY = 'lan_transfer_my_uids';
function myUids(){
  try { return new Set(JSON.parse(sessionStorage.getItem(MY_UIDS_KEY) || '[]')); }
  catch (e) { return new Set(); }
}
function rememberUid(uid){
  if (!uid) return;
  try {
    const s = myUids();
    if (!s.has(uid)) { s.add(uid); sessionStorage.setItem(MY_UIDS_KEY, JSON.stringify([...s])); }
  } catch (e) {}
}
function forgetUid(uid){
  if (!uid) return;
  try {
    const s = myUids();
    s.delete(uid);
    sessionStorage.setItem(MY_UIDS_KEY, JSON.stringify([...s]));
  } catch (e) {}
}

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
  }).then(r => r.json()).then(d => {
    if (!d || d.ok === false) return;
    // 服务端回执：被本机窗口或其它设备请求暂停
    if (d.paused) applyRemotePause(uid);
    // 服务端已删除该任务（.info 不存在）—— 停止重试，标记为已取消
    if (d.alive === false) applyRemoteDelete(uid);
  }).catch(() => {});
}

/* 服务端要求暂停：停止发送分片，进度保留 */
function applyRemotePause(uid){
  const t = TASKS.find(x => taskUid(x) === uid);
  if (!t || t.status !== STATE.UPLOADING) return;
  try { t.upload.abort(); } catch (e) {}
  t.status = STATE.PAUSED;
  stopBeat(t);
  t.st.textContent = '已被暂停（进度已保留）';
  renderTask(t);
  toast('已在电脑端暂停该上传');
  refreshPending();
  pump();                        // 名额空出来，让排队的任务顶上
}

/* 服务端已删除该任务：停止一切重试（不再自动重建） */
function applyRemoteDelete(uid){
  const t = TASKS.find(x => taskUid(x) === uid);
  if (!t || t.status === STATE.COMPLETED) return;   // 已完成的任务不回退成失败
  stopBeat(t);
  t.killed = true;                 // 阻止 404 后的自动重建
  t.status = STATE.ERROR;
  t.st.textContent = '任务已被删除';
  renderTask(t);
  toast('该上传任务已被删除');
  pump();                        // 名额空出来，让排队的任务顶上
}
/* 每个任务各自维护心跳（支持多任务并行上传）；
   uid 在 POST 创建成功后才由 tus-js-client 写入 url，因此这里每轮重新解析。*/
function startBeat(t){
  stopBeat(t);
  const tick = () => {
    if (t._beatStopped) return;   // 已停止：不再补发，避免删除后记录被重新写活
    const u = taskUid(t);
    if (u) { rememberUid(u); beatOnce(u, t.uploaded || 0, t.speed || 0); }
  };
  t._beatStopped = false;
  t._beatTimer = setInterval(tick, 1500);
  // 这两次"立即补发"也要能取消，否则删除后仍会再发一次心跳
  t._beatOnce = [setTimeout(tick, 300), setTimeout(tick, 1200)];
}
function stopBeat(t){
  t._beatStopped = true;
  if (t._beatTimer) { clearInterval(t._beatTimer); t._beatTimer = null; }
  if (t._beatOnce) { t._beatOnce.forEach(clearTimeout); t._beatOnce = null; }
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
    + '<span class="pct">0.00%</span>'
    + '<span class="tg" hidden>&#9662;</span></div>'
    + '<div class="sz">' + fmt(t.file.size) + '</div>'
    + '<div class="bar"><i></i></div>'
    + '<div class="st">' + WAIT_TEXT + '</div>'
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
  // 折叠角标：任务较多时可把卡片收成一行，避免撑爆屏幕
  el.querySelector('.tg').addEventListener('click', ev => {
    ev.target.textContent = el.classList.toggle('collapsed') ? '\u25b8' : '\u25be';
  });
  return el;
}

/* 折叠角标：任务超过 2 个时才出现；回到 2 个以内则全部展开 */
function syncCards(){
  const cards = TASKS.map(t => t.el).filter(Boolean);
  const many = cards.length > 2;
  cards.forEach(el => {
    const tg = el.querySelector('.tg');
    if (!tg) return;
    tg.hidden = !many;
    if (!many && el.classList.contains('collapsed')) {
      el.classList.remove('collapsed');
      tg.textContent = '\u25be';
    }
  });
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
  if (t.upload) {                     // 已创建过上传对象：直接续传
    t.status = STATE.UPLOADING;
    renderTask(t);
    markRateStart(t);
    t.upload.start();
    startBeat(t);
    refreshPending();      // 立即刷新：恢复上传后该任务不应再出现在"未完成"
    return;
  }
  if (t.creating) return;             // 正在创建中：避免调度器重复触发
  t.creating = true;
  t.status = STATE.UPLOADING;         // 先占住名额，pump 只挑 WAITING 的任务
  renderTask(t);
  // 速度计算：每 0.3 秒采样一次并平滑，避免数字跳动
  t.lastTick = Date.now(); t.lastBytes = 0; t.speed = 0; t.uploaded = t.uploaded || 0;
  // 先向服务端查有没有同名同大小的未完成任务：
  // tus-js-client 自带的指纹存在浏览器里，清缓存/换浏览器就会失效，
  // 这里按"文件名 + 大小"兜底，保证任何情况下都能从断点续传。
  // 必须带 include_active=1：暂停后心跳可能还没过期，只查"未在传输"
  // 会查不到自己的断点，于是从头重传并多出一个副本。
  if (!t.resumeUrl) {
    try {
      const d = await (await fetch('/api/pending?include_active=1')).json();
      // mine !== false：只认自己设备发起的未完成任务，避免续写到别人的任务里
      const hit = (d.pending || []).find(p => p.mine !== false &&
        p.name === t.file.name && p.size === t.file.size);
      if (hit) {
        t.resumeUrl = '/api/upload/' + hit.uid;
        t.uploaded = hit.offset || 0;
        t.st.textContent = '检测到未完成任务，从断点续传…';
      }
    } catch (e) {}
  }
  // 查断点期间可能已被调度器转为「排队中」、或被删除，这时不能再启动
  if (t.killed || t.status !== STATE.UPLOADING) { t.creating = false; return; }
  const opts = {
    endpoint: '/api/upload/',
    uploadUrl: t.resumeUrl || undefined,   // 指定则直接续传该任务
    // 限速时把每个分片压到约 1 秒的数据量：发送更平滑，暂停/取消响应更快
    chunkSize: speedLimit
      ? Math.max(64 * 1024, Math.min(32 * 1024 * 1024, speedLimit))
      : 32 * 1024 * 1024,
    // 限速节流：tus-js-client 会 await 本回调，返回 Promise 即可延后本次请求
    onBeforeRequest: () => new Promise((resolve) => {
      if (!speedLimit) { resolve(); return; }
      if (!t.rateStart) markRateStart(t);
      const sent = Math.max(0, (t.uploaded || 0) - (t.rateBase || 0));
      const waitMs = sent / speedLimit * 1000 - (Date.now() - t.rateStart);
      if (waitMs <= 0) { resolve(); return; }
      // 单次最多等 2 秒：不破坏长期平均速率，也不至于让暂停/取消迟迟不响应
      setTimeout(resolve, Math.min(waitMs, 2000));
    }),
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
      if (code === 404 && !t.retried && !t.killed) {
        // 服务端这个任务已不存在（过期清理 / 被删除）：
        // 若已被本机窗口主动删除则不再重建，否则换一个任务地址重建续传
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
      pump();                        // 名额空出来，让排队的任务顶上
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
      pump();                        // 名额空出来，让排队的任务顶上
      // 短暂停留显示"完成"，随后从上传区移除（文件已进入「全部文件」）
      setTimeout(() => {
        t.el.remove();
        const i = TASKS.indexOf(t);
        if (i >= 0) TASKS.splice(i, 1);
        syncCards();                 // 卡片数变化，重算折叠角标
      }, 1500);
    }
  };
  t.upload = new tus.Upload(t.file, opts);
  t.creating = false;
  renderTask(t);
  markRateStart(t);
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
  pump();                           // 名额空出来，让排队的任务顶上
}

async function deleteTask(t){
  if (!confirm('删除该上传任务？已上传的分片数据也会被清除。')) return;
  t.killed = true;                      // 阻止 404 后自动重建
  stopBeat(t);
  // 关键：真正中止上传。只把卡片从界面移除的话，tus 仍会在后台继续传
  try { if (t.upload) t.upload.abort(); } catch (e) {}
  // 若服务端已有分片，调用终止接口彻底清理（含 .info 与分片文件）
  const gone = taskUid(t);
  if (gone) {
    forgetUid(gone);
    const r = await apiFetch('/api/pending/' + gone, {method: 'DELETE'});
    if (!r.ok) toast(r.detail || '服务端分片未清除，可稍后在「未完成任务」里删除');
  }
  t.el.remove();
  const i = TASKS.indexOf(t);
  if (i >= 0) TASKS.splice(i, 1);
  refreshPending();
  refreshRemote();
  pump();                        // 名额空出来，让排队的任务顶上
}

function addFiles(files){
  [...files].sort((a, b) => a.size - b.size).forEach(f => {
    // 先入队：有空闲名额时下面的 pump() 会立刻启动，否则显示「排队中」
    const t = { file: f, upload: null, status: STATE.WAITING };
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
    renderTask(t);
  });
  pump();                              // 多选的文件按顺序占用名额，超出的排队
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
  // 自己的任务 = 当前页面上传中的 + 本标签页历史上传过的（刷新后仍在 sessionStorage）
  //          + 服务端判定「属于本机」的（含程序端 GUI 发起的上传，
  //            它们在浏览器里没有任何记录，只能靠服务端的 mine 字段识别）
  const mine = new Set([...TASKS.map(taskUid).filter(Boolean), ...myUids()]);
  const items = (d.active || []).filter(a => !mine.has(a.uid) && a.mine !== true);
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
  let d;
  try {
    // include_active=1：连"心跳还没过期"的任务一起取回，再在下面精确过滤。
    // 只取"未在传输"的任务会出现黑洞——页面重载/崩溃后心跳残留的 90 秒内，
    // 这些任务既不在「未完成」（被 active 过滤），也不在「其他设备正在上传」
    // （被 sessionStorage 里的本机 uid 过滤），看起来就像任务丢了。
    d = await (await fetch('/api/pending?include_active=1')).json();
  } catch (e) {
    return;      // 服务端暂时不可达（重启/网络抖动）：跳过本轮，不抛未捕获异常
  }
  const box = document.getElementById('pending');
  // 只排除两类：本页自己正在传的（已有任务卡片）、正被其它设备传的
  // （在「其他设备正在上传」区域展示）。其余一律列出，保证一个都不丢。
  const live = new Set(TASKS
    .filter(t => t.status === STATE.UPLOADING || t.status === STATE.WAITING)
    .map(taskUid).filter(Boolean));
  const items = (d.pending || []).filter(p =>
    !live.has(p.uid) && !(p.active && p.mine === false));
  document.getElementById('pendingCard').hidden = items.length === 0;
  document.getElementById('pendingTitle').textContent =
    '未完成的任务（' + items.length + '）';
  box.innerHTML = items.map(p => {
    const pct = p.size ? Math.min(100, p.offset / p.size * 100) : 0;
    const pctTxt = pct.toFixed(2);
    // 别人的任务只能只读展示（服务端同样会 403），按钮置灰并给出原因
    const mine = p.mine !== false;
    const who = [p.client_name, p.client_ip].filter(Boolean).join(' / ');
    return '<div class="row">'
      + '<div class="hd"><span class="nm">' + esc(p.name) + '</span>'
      + '<span class="pct">' + pctTxt + '%</span></div>'
      + '<div class="sz">已传 ' + fmt(p.offset) + ' / ' + fmt(p.size)
      + (who ? '　来自 ' + esc(who) : '') + '</div>'
      + '<div class="bar"><i style="width:' + pctTxt + '%"></i></div>'
      + '<div class="st err">' + (mine ? '未完成 · 已暂停' : '其它设备的任务 · 只读') + '</div>'
      + '<div class="ops">'
      + '<button class="mini" data-a="resume" data-u="' + p.uid + '"'
      + ' data-n="' + esc(p.name) + '" data-s="' + p.size + '"'
      + (mine ? '' : ' disabled') + '>继续</button>'
      + '<button class="mini" data-a="drop" data-u="' + p.uid + '"'
      + (mine ? '' : ' disabled') + '>删除</button>'
      + '</div>'
      + '</div>';
  }).join('');
  // 按钮一律用事件委托（只在启动时绑定一次）：列表每 3 秒重建也能稳定命中，
  // 不会出现"恰好刷新的瞬间点击落空"
  const pendBox = document.getElementById('pending');
  if (!pendBox._bound) {
    pendBox._bound = true;
    pendBox.addEventListener('click', async (ev) => {
      const btn = ev.target.closest('button[data-a]');
      if (!btn) return;
      if (btn.dataset.a === 'drop') {
        if (!confirm('删除该未完成任务？已上传的分片将被清除。')) return;
        const r = await apiFetch('/api/pending/' + btn.dataset.u, {method: 'DELETE'});
        if (!r.ok) {
          toast(r.detail || ('删除失败（HTTP ' + r.status + '）'));
          return;
        }
        toast('已删除该未完成任务');
        refreshPending();
      } else if (btn.dataset.a === 'resume') {
        if (btn.disabled) { toast('只能续传自己设备发起的任务'); return; }
        resumePendingTask(btn.dataset.u, btn.dataset.n, Number(btn.dataset.s));
      }
    });
  }
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
            renderTask(t);
            toast('已自动读取原文件，从断点继续');
            pump();                  // 交给调度器：有空闲名额就立刻开始
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

/* ---------------- 电脑端目录：显示路径，点击在电脑上打开 ---------------- */
async function refreshPaths(){
  const box = document.getElementById('paths');
  if (!box) return;
  let d;
  try { d = await (await fetch('/api/paths')).json(); } catch (e) { return; }
  const item = (key, label, path, title) =>
    '<button class="mini" data-dir="' + key + '" title="' + esc(title) + '">'
    + label + '</button> <span class="tag">' + esc(path) + '</span>　';
  box.innerHTML = '电脑端目录（点击在电脑上打开）：'
    + item('config', '接收/配置', d.config_dir, '接收到的文件与配置文件所在目录')
    + item('log', '日志', d.log_dir, '运行日志 app.log 所在目录');
  box.querySelectorAll('button[data-dir]').forEach(b => {
    b.onclick = async () => {
      const r = await apiFetch('/api/open-dir', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({key: b.dataset.dir})
      });
      toast(r.ok ? '已在电脑上打开该目录' : (r.detail || '打开失败'));
    };
  });
}

/* ---------------- 上传限速下拉框（与程序端同步） ---------------- */
function buildSpeedBox(){
  const box = document.getElementById('speedBox');
  if (!box || box._built) return;
  box._built = true;
  const options = SPEED_CHOICES.map(m =>
    '<option value="' + (m * 1048576) + '">'
    + (m ? m + ' MB/s' : '无限制') + '</option>').join('');
  box.innerHTML = '上传限速：<select id="speedSel" style="background:#262c4a;'
    + 'border:1px solid #363d61;color:#cfd6f5;border-radius:6px;'
    + 'padding:2px 6px;font-size:12.5px">' + options + '</select>'
    + '<span style="margin-left:8px">（与电脑端同步，最小 1 MB/s）</span>';
  document.getElementById('speedSel').onchange = async (ev) => {
    const value = Number(ev.target.value) || 0;
    const r = await apiFetch('/api/settings', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({speed_limit: value})
    });
    if (!r.ok) { toast(r.detail || '限速设置失败'); applySpeedSelect(); return; }
    speedLimit = value;
    toast(value ? '上传限速已设为 ' + (value / 1048576) + ' MB/s' : '已取消上传限速');
  };
  applySpeedSelect();
}

function applySpeedSelect(){
  const sel = document.getElementById('speedSel');
  if (sel) sel.value = String(speedLimit);
}

/* 刷新文件列表 */
let lastFileSig = '';
function fileSig(files){
  return files.map(f => f.id + '|' + f.name + '|' + f.size).join(',');
}
async function refresh(){
  let d;
  try {
    d = await (await fetch('/api/files')).json();
  } catch (e) { return; }
  const files = d.files || [];
  const sig = fileSig(files);
  // 数据没变就不重建表格：每 3 秒重刷 innerHTML 会让行元素被替换，
  // 用户恰好在刷新瞬间点击会点空（"点了没反应"）。
  if (sig !== lastFileSig) {
    lastFileSig = sig;
    document.getElementById('rows').innerHTML = files.map(f =>
      '<tr class="file-row" data-id="' + esc(f.id) + '">'
      + '<td>' + esc(f.name) + '</td>'
      + '<td>' + fmt(f.size) + '</td>'
      + '<td>' + f.uploaded_at + '</td>'
      + '<td>' + esc(f.client_ip || '-') + '</td>'
      + '<td>' + esc(f.client_name || '-') + '</td>'
      + '<td><span class="tag">' + esc(f.client_mac || '-') + '</span></td>'
      + '<td><button class="mini" data-del="' + esc(f.id) + '">删记录</button></td>'
      + '</tr>').join('');
    // 下载绑定改用事件委托：内联 onclick 里写 location.href=/files/xxx 会被
    // JS 解析成正则字面量而报语法错误，导致点击无反应
    document.querySelectorAll('#rows tr.file-row').forEach(tr => {
      tr.onclick = () => {
        location.href = '/files/' + encodeURIComponent(tr.dataset.id);
      };
    });
    document.querySelectorAll('button[data-del]').forEach(b => {
      b.onclick = async (ev) => {
        ev.stopPropagation();
        if (!confirm('仅删除该任务记录？uploads 里的实际文件会保留。')) return;
        const r = await apiFetch('/api/record/' + b.dataset.del, {method: 'DELETE'});
        if (!r.ok) toast(r.detail || '删除记录失败');
        lastFileSig = '';       // 强制下一轮重建
        refresh();
      };
    });
  }
  document.getElementById('empty').style.display = files.length ? 'none' : 'block';
  document.getElementById('empty').textContent = files.length ? '' : '暂无文件';
  const queued = queuedCount();
  document.getElementById('info').textContent =
    '共 ' + (d.count || files.length) + ' 个文件 / ' + fmt(d.total || 0) + '　剩余磁盘 ' + fmt(d.free || 0)
    + '　并行上限 ' + maxParallel + (queued ? '（排队 ' + queued + '）' : '')
    + '　地址 ' + location.origin;
}
refresh();
refreshPending();
refreshRemote();
syncSettings();
refreshPaths();
buildSpeedBox();
renderTips();
setInterval(refresh, 3000);
setInterval(refreshPending, 3000);
setInterval(refreshRemote, 1500);
setInterval(syncSettings, 2000);   // 持续跟随程序端的并行任务数

/* 页面刷新 / 关闭时立即释放「上传中」标记。
   否则服务端要等心跳超时（约 90 秒）才把任务归入「未完成」，这段时间里
   任务既不在上传中、也不在未完成处，容易让人以为文件丢了。
   上传本身会随页面卸载而中断，这里只负责让它尽快回到「可继续」列表。*/
window.addEventListener('beforeunload', () => {
  TASKS.forEach(t => {
    const u = taskUid(t);
    if (!u) return;
    try { fetch('/api/active/' + u, {method: 'DELETE', keepalive: true}); } catch (e) {}
  });
});
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
    load_settings()             # 恢复上次的并行任务上限

    app = FastAPI(title="局域网文件传输工具", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def _record_client(request: Request, call_next):
        """登记"上传 ID → 客户端 IP"，并拦截跨设备续写别人的上传任务。

        三件事：
          1) 把来源 IP 放进上下文变量 —— 0 字节文件在 POST 请求内就完成，
             完成回调必须能拿到它（见 on_upload_complete）；
          2) PATCH / HEAD / DELETE 落在别人的上传任务上时直接 403，
             杜绝两台设备互相续写同一个任务导致文件内容混合；
          3) 响应返回后按 Location 里的 uid 登记所有者（仅 POST 创建时写入，
             后续心跳不覆盖，保证权限判定稳定）。
        """
        who = request.client.host if request.client else ""
        token = CLIENT_IP_VAR.set(who)
        try:
            match = re.fullmatch(r"/api/upload/([0-9a-fA-F]{8,64})/?",
                                 request.url.path or "")
            if match and request.method in ("PATCH", "HEAD", "DELETE"):
                uid = match.group(1)
                if not viewer_is_owner(uid, who):
                    return JSONResponse(
                        {"detail": "只能续传自己上传的任务"
                                   f"（该任务来自 {CLIENT_MAP.get(uid, '')}）"},
                        status_code=403)
            response = await call_next(request)
            if request.method == "POST" and "/api/upload" in request.url.path:
                location = response.headers.get("location") or ""
                if "/api/upload/" in location:
                    uid = location.rstrip("/").rsplit("/", 1)[-1]
                    if who:
                        CLIENT_MAP[uid] = who
            return response
        finally:
            CLIENT_IP_VAR.reset(token)

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
        # 连接被对端关闭后的收尾回调会抛 WinError 10022/10054，
        # 属于 Windows asyncio 的已知噪音，这里统一收敛，避免刷屏误导用户
        loop.set_exception_handler(quiet_loop_exception_handler)
        loop.create_task(_gc_loop())
        loop.create_task(_memory_watch())

    @app.get("/", response_class=HTMLResponse)
    async def index():
        """首页：上传 + 文件列表。"""
        return render_page()

    @app.get("/api/files")
    async def api_files():
        """全部已上传文件列表（所有设备上传的都在一起）。

        注意：只返回展示需要的字段，**不下发服务端绝对路径**（path 字段
        属于服务端内部信息，泄露给局域网任意客户端没有必要）。
        """
        items = [{k: v for k, v in m.items() if k != "path"} for m in read_all_meta()]
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

    @app.get("/api/paths")
    async def api_paths():
        """本机目录信息，供程序端与网页端展示并可点击打开。

        配置文件（.settings.json / .local_tasks.json）与接收到的文件同在
        uploads 目录，所以 config_dir 与 upload_dir 是同一个路径。
        """
        return {
            "base": BASE_DIR,
            "upload_dir": UPLOAD_DIR,
            "config_dir": UPLOAD_DIR,
            "config_file": SETTINGS_PATH,
            "log_dir": LOG_DIR,
            "log_file": LOG_PATH,
        }

    @app.post("/api/open-dir")
    async def api_open_dir(request: Request):
        """在**服务端这台电脑**上打开目录（网页端点击目录时调用）。

        只接受 OPENABLE_DIRS 里的固定 key，不接受任意路径。
        """
        try:
            payload = await request.json()
            key = str(payload.get("key") or "")
        except (ValueError, TypeError, AttributeError):
            key = ""
        target = OPENABLE_DIRS.get(key)
        if not target:
            raise HTTPException(status_code=400, detail="目录标识非法")
        if not await asyncio.to_thread(open_local_dir, target):
            raise HTTPException(status_code=500, detail="无法打开该目录")
        return {"ok": True, "path": target}

    @app.get("/api/settings")
    async def api_settings():
        """当前并行任务上限与上传限速（程序端与网页端共用）。

        网页端每 2 秒轮询本接口：并行数变小就把超出名额的任务转成「排队中」；
        限速改变则立即用于后续分片请求。
        """
        return {
            "max_parallel": PARALLEL["value"],
            "limit": PARALLEL_LIMIT_MAX,
            "speed_limit": SPEED_LIMIT["value"],   # 字节/秒，0 = 无限制
            "speed_min": SPEED_LIMIT_MIN,          # 非 0 时的最小限速
        }

    @app.post("/api/settings")
    async def api_set_settings(request: Request):
        """修改并行任务上限 / 上传限速，只处理请求里出现的字段，立即持久化。"""
        try:
            payload = await request.json()
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")

        updated = []
        if "max_parallel" in payload:
            try:
                value = int(payload["max_parallel"])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="max_parallel 必须是整数")
            if not 1 <= value <= PARALLEL_LIMIT_MAX:
                raise HTTPException(
                    status_code=400,
                    detail=f"max_parallel 需在 1~{PARALLEL_LIMIT_MAX} 之间")
            PARALLEL["value"] = value
            updated.append("max_parallel")

        if "speed_limit" in payload:
            try:
                limit = int(payload["speed_limit"])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="speed_limit 必须是整数")
            # 0 表示无限制；非 0 时不得低于下限，也不接受负数
            if limit < 0 or (limit and limit < SPEED_LIMIT_MIN):
                raise HTTPException(
                    status_code=400,
                    detail=f"speed_limit 需为 0（无限制）或不小于 "
                           f"{SPEED_LIMIT_MIN} 字节/秒")
            SPEED_LIMIT["value"] = limit
            updated.append("speed_limit")

        if not updated:
            raise HTTPException(
                status_code=400,
                detail="请求里没有可更新的字段（max_parallel / speed_limit）")

        await asyncio.to_thread(save_settings)
        return {"ok": True, "updated": updated,
                "max_parallel": PARALLEL["value"],
                "speed_limit": SPEED_LIMIT["value"]}

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
    async def api_pending(request: Request):
        """未完成的上传任务（已暂停 / 中断 / 程序异常关闭后残留的分片）。

        正在正常上传的任务有心跳，默认不出现在这里——它只应在"上传中"区域
        出现，两处不会重复。带 ``?include_active=1`` 时连有心跳的一起返回，
        供客户端做断点续传检索（暂停后心跳可能还没过期，只查"未在传输"
        会查不到自己的断点）。

        每个条目附带 ``mine``（以请求方视角是否有权操作）与 ``local_task``
        （是否由本机创建），前端据此只读展示别人的任务。
        """
        include_active = request.query_params.get("include_active") in ("1", "true", "yes")
        viewer = request.client.host if request.client else ""
        items = await asyncio.to_thread(scan_pending_uploads, include_active, viewer)
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
        alive = os.path.isfile(os.path.join(TUS_DIR, uid + ".info"))
        # 已被要求暂停 / 已被删除的任务：心跳不得把它重新标记成"正在上传"。
        # 否则暂停后的 90 秒里，任务会同时"显示上传中"且"不在未完成列表"，
        # 用户重启程序时找不到断点，只能从头重传。
        if uid in PAUSED_UIDS or is_dropped(uid) or not alive:
            mark_inactive(uid)
            return {"ok": True, "ttl": ACTIVE_TTL,
                    "paused": uid in PAUSED_UIDS, "alive": alive}
        mark_active(uid,
                    uploaded=int(payload.get("uploaded") or 0),
                    speed=float(payload.get("speed") or 0.0))
        # 注意：这里**不能**覆盖 CLIENT_MAP —— 所有者以创建上传时的 IP 为准，
        # 否则任何设备刷新心跳都会被当成所有者，从而绕过权限校验。
        if request.client and not task_owner(uid):
            CLIENT_MAP[uid] = request.client.host
        # 回传控制指令：被其它设备暂停 / 任务已被删除
        return {"ok": True, "ttl": ACTIVE_TTL,
                "paused": uid in PAUSED_UIDS, "alive": alive}

    @app.post("/api/pause/{uid}")
    async def api_pause(uid: str, request: Request):
        """请求暂停某个上传（仅限任务所有者）。

        TUS 协议没有"服务端强制暂停"的能力，这里采用心跳回执方式：
        标记后客户端下一次心跳会收到 paused=true 并主动停止发送分片。
        """
        if not re.fullmatch(r"[0-9a-f]{8,64}", uid or ""):
            raise HTTPException(status_code=400, detail="任务 ID 非法")
        check_owner(uid, request)
        if not os.path.isfile(os.path.join(TUS_DIR, uid + ".info")):
            raise HTTPException(status_code=404, detail="任务不存在")
        PAUSED_UIDS[uid] = request.client.host if request.client else ""
        mark_inactive(uid)          # 立刻从"正在上传"列表中消失
        return {"ok": True, "requested": True}

    @app.post("/api/resume/{uid}")
    async def api_resume(uid: str, request: Request):
        """取消暂停标记（仅限任务所有者）。"""
        if not re.fullmatch(r"[0-9a-f]{8,64}", uid or ""):
            raise HTTPException(status_code=400, detail="任务 ID 非法")
        check_owner(uid, request)
        PAUSED_UIDS.pop(uid, None)
        return {"ok": True}

    @app.get("/api/active")
    async def api_active_list(request: Request):
        """当前所有正在上传的任务（含进度与速度），供其它设备同步显示。

        每项带 ``mine``：以请求方视角看，该任务是否属于同一台电脑。
        网页端据此过滤，避免把程序端本机上传的任务误报成"其他设备"。
        """
        viewer = request.client.host if request.client else ""
        items = await asyncio.to_thread(list_active_uploads, viewer)
        exclude = request.query_params.get("exclude") or ""
        if exclude:
            items = [i for i in items if i["uid"] != exclude]
        return {"active": items, "count": len(items)}

    @app.delete("/api/active/{uid}")
    async def api_inactive(uid: str):
        """取消"正在上传"标记（暂停 / 完成 / 删除时调用）。"""
        mark_inactive(uid)
        PAUSED_UIDS.pop(uid, None)
        return {"ok": True}

    @app.delete("/api/pending/{uid}")
    async def api_drop_pending(uid: str, request: Request):
        """删除某个未完成任务的临时分片（终止上传，不动已完成的文件）。

        仅限任务所有者操作，避免误删他人正在上传的任务。
        """
        if not re.fullmatch(r"[0-9a-f]{8,64}", uid or ""):
            raise HTTPException(status_code=400, detail="任务 ID 非法")
        check_owner(uid, request)
        # 先登记"已删除"：在途 PATCH 到达时会被 Storage.append 直接丢弃，
        # 否则刚删掉的分片文件又会被写回来，留下没有 .info 的孤立分片
        DROP_UIDS[uid] = time.time()
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
        PAUSED_UIDS.pop(uid, None)
        queue_purge(uid)                    # 客户端 abort 有竞态，随后几秒反复复查
        await asyncio.to_thread(purge_once)  # 立刻清一次，不必等下一轮定时任务
        # 幂等：任务可能刚完成或已被删过，这种情况同样视为删除成功
        return {"ok": True, "removed": removed}

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
    """后台任务：定期回收磁盘。

    - 每 PURGE_INTERVAL 秒复查一次"已删除任务"的竞态残留分片（轻量）；
    - 每小时做一次全量过期清理（含跨天未完成的大文件分片）。
    """
    rounds = 0
    per_hour = max(1, 3600 // PURGE_INTERVAL)
    while True:
        try:
            await asyncio.to_thread(purge_once)
        except Exception:
            pass
        if rounds % per_hour == 0:
            try:
                await asyncio.to_thread(cleanup_expired)
            except Exception:
                pass
            # 源文件被手动删除后，把对应的元数据记录也清掉，列表不会长期残留
            try:
                await asyncio.to_thread(purge_missing_records)
            except Exception:
                pass
        rounds += 1
        await asyncio.sleep(PURGE_INTERVAL)


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
