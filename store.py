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
import shutil
import sys
import uuid
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from tuspyserver import create_tus_router
from tuspyserver.router import TusRouterOptions

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
EXPIRE_DAYS = 1                     # 未完成分片保留天数，超过自动清理
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
        original = clean_name(meta.get("filename") or os.path.basename(file_path))
        final_path = unique_path(UPLOAD_DIR, original)

        # 重命名（.tus 临时文件 → uploads 正式文件）与哈希计算都放到线程
        await asyncio.to_thread(os.replace, file_path, final_path)
        size = await asyncio.to_thread(os.path.getsize, final_path)
        print(f"[接收] 计算 SHA256：{os.path.basename(final_path)}  {human_size(size)}", flush=True)
        digest = await asyncio.to_thread(sha256_of, final_path)

        file_id = uuid.uuid4().hex[:12]
        await asyncio.to_thread(write_meta, file_id, {
            "id": file_id,
            "name": os.path.basename(final_path),
            "path": final_path,
            "size": size,
            "sha256": digest,
            "uploaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        print(f"[完成] {os.path.basename(final_path)}  {human_size(size)}  sha256={digest[:16]}…", flush=True)

        # 删除 TUS 的 .info：该上传已结束，不再参与过期清理
        old_info = os.path.splitext(file_path)[0] + ".info"
        if os.path.isfile(old_info):
            os.remove(old_info)
    except Exception as exc:  # 任何异常都不应影响服务本身
        print(f"[警告] 完成后处理失败：{exc}", flush=True)


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
</style>
</head>
<body>
<div class="wrap">
  <h1>局域网文件传输</h1>
  <p class="sub" id="info">正在连接…</p>

  <div class="card">
    <div class="box" id="box">
      <div style="font-size:32px">&#128196;</div>
      <div style="margin-top:8px">点击选择文件（可多选，支持 100G 级大文件）</div>
      <div class="sz">分片上传 · 支持断点续传 · 中断后重新选择同一文件即可继续</div>
    </div>
    <input type="file" id="pick" multiple hidden>
    <div id="up"></div>
  </div>

  <div class="card">
    <b>全部文件（点击任意一行下载）</b>
    <table>
      <thead><tr><th>文件名</th><th>大小</th><th>接收时间</th><th>SHA256</th></tr></thead>
      <tbody id="rows"></tbody>
    </table>
    <div class="empty" id="empty">暂无文件</div>
  </div>
</div>

<script>
/*__TUS_JS__*/

const pick = document.getElementById('pick'), box = document.getElementById('box');
pick.onchange = () => { [...pick.files].forEach(start); pick.value = ''; };
box.onclick = () => pick.click();

function fmt(n){
  const u = ['B','KB','MB','GB','TB','PB'];
  let i = 0;
  while(n >= 1024 && i < u.length-1){ n /= 1024; i++; }
  return (i ? n.toFixed(2) : n) + ' ' + u[i];
}
function esc(s){
  return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

/* 单文件上传：TUS 分片 + 断点续传 */
function start(file){
  const el = document.createElement('div');
  el.className = 'row';
  el.innerHTML = '<div class="nm">' + esc(file.name) + '</div>'
    + '<div class="sz">' + fmt(file.size) + '</div>'
    + '<div class="bar"><i></i></div>'
    + '<div class="st">准备中…</div>';
  document.getElementById('up').appendChild(el);
  const bar = el.querySelector('.bar > i'), st = el.querySelector('.st');

  const upload = new tus.Upload(file, {
    endpoint: '/api/upload/',
    chunkSize: 32 * 1024 * 1024,                 // 32MB 一片
    retryDelays: [0, 1000, 3000, 5000, 10000, 20000, 30000, 60000],
    resumeFromPreviousUpload: true,              // 断点续传：先 HEAD 查已传字节
    removeFingerprintOnSuccess: true,
    metadata: { filename: file.name, filetype: file.type || '' },
    onError(err){
      st.className = 'st err';
      st.textContent = '失败：' + (err && err.message ? err.message : '未知错误');
    },
    onProgress(uploaded, total){
      const pct = total ? uploaded / total * 100 : 0;
      bar.style.width = pct.toFixed(1) + '%';
      st.textContent = pct.toFixed(1) + '%  (' + fmt(uploaded) + ' / ' + fmt(total) + ')';
    },
    onSuccess(){
      st.className = 'st ok';
      st.textContent = '上传完成';
      refresh();
    }
  });
  upload.start();
}

/* 刷新文件列表 */
async function refresh(){
  let d;
  try {
    d = await (await fetch('/api/files')).json();
  } catch (e) { return; }
  document.getElementById('rows').innerHTML = d.files.map(f =>
    '<tr onclick="location.href=\'/files/' + f.id + '\'">'
    + '<td>' + esc(f.name) + '</td>'
    + '<td>' + fmt(f.size) + '</td>'
    + '<td>' + f.uploaded_at + '</td>'
    + '<td><span class="tag" title="' + f.sha256 + '">' + f.sha256.slice(0,12) + '…</span></td>'
    + '</tr>').join('');
  document.getElementById('empty').style.display = d.files.length ? 'none' : 'block';
  document.getElementById('empty').textContent = d.files.length ? '' : '暂无文件';
  document.getElementById('info').textContent =
    '共 ' + d.count + ' 个文件 / ' + fmt(d.total) + '　剩余磁盘 ' + fmt(d.free)
    + '　地址 ' + location.origin;
}
refresh();
setInterval(refresh, 3000);
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