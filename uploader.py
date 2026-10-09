"""
局域网文件传输工具 —— 电脑端上传客户端（TUS 协议）
=================================================

供 tkinter 窗口调用：在电脑本机也能把文件传到这台电脑的服务上，
行为与网页端一致：分片上传、显示进度与速度、支持暂停 / 继续 / 取消、
中断后可断点续传。

每个任务在独立线程里跑，通过回调把进度抛给界面（tkinter 只在主线程更新）。
"""
import base64
import http.client
import json
import uuid
import os
import threading
import time
import urllib.parse

# 本机上传任务的持久化文件（程序重启后可继续）
LOCAL_TASK_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "uploads", ".local_tasks.json")


class UploadTask:
    """一个本机上传任务。"""

    def __init__(self, path: str, port: int, chunk_size: int = 32 * 1024 * 1024):
        self.path = path
        self.name = os.path.basename(path)
        # 源文件绝对路径：服务端据此判重（同一路径再次上传直接失败）。
        # 必须用**绝对路径**而不是文件名——不同文件夹里的同名文件是两个不同的
        # 文件，都要能传；真正重复的只有"同一个路径"。
        self.src_path = os.path.abspath(path)
        self.size = os.path.getsize(path)
        self.port = port
        self.chunk_size = chunk_size
        self.limit_bps = 0             # 上传限速（字节/秒）；0 = 不限制
        self._rate_reset = False       # 限速值变过：发送循环据此重置时间基准
        self.status = "paused"        # 初始暂停，点"开始"才上传
        self.uploaded = 0              # 已传字节（续传时从服务端 offset 起算）
        self.speed = 0.0               # MB/s
        self.error = ""
        self.iid = uuid.uuid4().hex[:8]  # 界面用的任务编号
        self.mtime = os.path.getmtime(path) if os.path.isfile(path) else 0
        self.upload_url = ""           # TUS 资源地址
        self._offset = 0
        self._path = ""
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        self._pause = threading.Event()
        self.on_progress = None        # fn(uploaded, size, speed, status)
        self.on_done = None            # fn(task)

    def _headers(self, extra: dict) -> dict:
        head = {"Tus-Resumable": "1.0.0"}
        head.update(extra)
        return head

    def _meta_header(self) -> str:
        """Upload-Metadata：filename 与 filetype **都必须有值**。

        tuspyserver 在 HEAD（断点续传第一步）时会校验这两个字段，
        缺 filetype 会直接返回 400，导致续传退化成重新上传。

        ``local MQ==`` 是本上传器给自己的任务打的标记（MQ== 即 "1"，
        对应 store.LOCAL_UPLOADER_KEY）。服务端据此把"程序端发起的上传"
        与"本机浏览器发起的上传"区分开——两者来源 IP 都是 127.0.0.1，
        只按 IP 判断会让本上传器认领浏览器的任务，形成两个写入者
        （锁文件争用 / Error removing lock file）。

        ``srcpath`` 带上源文件绝对路径（对应 store.SRC_PATH_KEY）：
        服务端用它判重——同一个路径再次上传会被拒绝（409），
        而不同文件夹里的同名文件（路径不同）照常能传。
        """
        name_b64 = base64.b64encode(self.name.encode("utf-8")).decode()
        type_b64 = base64.b64encode(b"application/octet-stream").decode()
        path_b64 = base64.b64encode(self.src_path.encode("utf-8")).decode()
        return (f"filename {name_b64},filetype {type_b64},"
                f"local MQ==,srcpath {path_b64}")

    def _request(self, method: str, path: str, body=None, headers=None, timeout=60):
        """发一个短连接 HTTP 请求。

        注意：这里**不能**加 ``Connection: close``。实测 Windows + uvicorn
        组合下，服务端收到该头后立即关闭连接，与客户端刚发出的请求数据撞在
        一起会触发整条连接被重置（60 次请求里 16 次 WinError 10054），
        本机上传器会直接失败。保持默认 keep-alive 语义、由本端正常 close 即可。
        """
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, dict(resp.getheaders()), data
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _find_existing(self):
        """在服务端"未完成任务"中查找**本上传器自己发起**的同名同大小任务。

        这是跨进程 / 跨程序重启断点续传的关键：程序被强杀后，原来的
        UploadTask 对象已不存在，新对象靠文件名 + 大小找回服务端残留的
        分片，继续传而不是从头再来。

        两个必须点：
          1) 带 ``include_active=1``：暂停后服务端的心跳标记可能尚未过期，
             若只查"未在传输"的任务就会查不到，于是新建任务、已传字节作废，
             还会多出一个「文件名(1)」副本；
          2) 只匹配 ``local_uploader=true``（由本上传器创建的任务，
             见 ``_meta_header`` 里带的 ``local`` 标记）：浏览器发起的上传
             来源 IP 也是 127.0.0.1，若按"本机任务"认领，就会去续写**别人
             正在写的那个任务**——两个写入者同时往同一个 TUS 资源追加，
             服务端锁文件被抢（Error removing lock file / WinError 32），
             同名不同内容时还会把文件写坏。别人的任务交回给发起方续传。
        """
        try:
            # 统一走 http.client：urllib 的连接复用在部分场景会被服务端提前
            # 关闭连接，导致随机 ConnectionResetError
            status, _headers, raw = self._request(
                "GET", "/api/pending?include_active=1", timeout=15)
            if status != 200:
                return None
            data = json.loads(raw.decode("utf-8"))
            best = None
            for item in data.get("pending", []):
                if item.get("name") != self.name:
                    continue
                if int(item.get("size") or 0) != self.size:
                    continue
                if item.get("local_uploader") is not True:
                    continue          # 不是本上传器发起的：绝不续写
                if best is None or int(item.get("offset") or 0) > int(best.get("offset") or 0):
                    best = item        # 多个残留时取进度最靠前的
            return best
        except Exception:
            pass
        return None

    def _attach_existing(self, uid: str):
        """绑定到服务端已有的未完成上传。"""
        self.upload_url = f"http://127.0.0.1:{self.port}/api/upload/{uid}"
        self._path = f"/api/upload/{uid}"

    @staticmethod
    def _create_error(status: int, body: bytes) -> str:
        """把服务端拒绝的原因原样交给用户。

        服务端在创建阶段就会拒绝：409（该源路径已上传过）、507（磁盘不足）。
        早期这里固定写"磁盘空间可能不足"，用户看不到真正的原因——现在优先
        取响应体里的 detail。
        """
        try:
            detail = json.loads(body.decode("utf-8")).get("detail")
        except (ValueError, AttributeError, UnicodeDecodeError):
            detail = ""
        return str(detail) if detail else f"创建上传失败（HTTP {status}）"

    def _create_upload(self):
        """创建 TUS 上传，返回资源地址。"""
        status, headers, body = self._request(
            "POST", "/api/upload/", body=b"",
            headers=self._headers({"Upload-Length": str(self.size),
                                   "Upload-Metadata": self._meta_header()}))
        if status not in (200, 201):
            raise RuntimeError(self._create_error(status, body))
        loc = headers.get("location") or headers.get("Location") or ""
        if not loc:
            raise RuntimeError("服务端未返回上传地址")
        self.upload_url = loc
        self._path = urllib.parse.urlparse(loc).path or loc

    def _query_offset(self) -> int:
        """HEAD 查询服务端已保存字节数（断点续传关键）。"""
        status, headers, _ = self._request("HEAD", self._path,
                                           headers=self._headers({}), timeout=30)
        if status != 200:
            return 0
        return int(headers.get("Upload-Offset") or headers.get("upload-offset") or 0)

    # -------- 心跳：让其它设备（网页 / 窗口）看到本机上传进度 --------
    def _beat(self, uploaded: int, speed: float):
        """上报心跳，并**读取服务端回执**。

        回执里带着控制指令：``paused``（窗口/其它设备点了暂停）、
        ``alive``（任务是否还存在）。忽略回执会导致窗口右键"暂停上传"
        对本机上传器完全无效——后台照旧全速传输。
        """
        uid = self._uid()
        if not uid:
            return None
        try:
            body = json.dumps({"uploaded": int(uploaded), "speed": round(float(speed), 2)})
            status, _headers, raw = self._request(
                "POST", f"/api/active/{uid}", body=body.encode("utf-8"),
                headers={**self._headers({"Content-Type": "application/json"}),
                         "Content-Length": str(len(body))}, timeout=10)
            if status == 200 and raw:
                data = json.loads(raw.decode("utf-8"))
                return data if isinstance(data, dict) else None
        except Exception:
            pass
        return None

    def _unbeat(self):
        uid = self._uid()
        if uid:
            try:
                self._request("DELETE", f"/api/active/{uid}",
                              headers=self._headers({}), timeout=10)
            except Exception:
                pass

    def _uid(self) -> str:
        """当前任务的服务端 ID（从 upload_url 末尾取）。"""
        url = self.upload_url or ""
        return url.rstrip("/").rsplit("/", 1)[-1] if url else ""

    # -------- 限速 --------
    def set_limit(self, limit_bps: int) -> None:
        """更新限速并通知发送循环重置时间基准。

        改限速后必须重新计账：否则按旧基准累加，会出现两类毛病——
        调到更小（如 1 MB/s）时要先"补等"之前全速传的字节、任务长时间
        卡住像暂停；调到更大（如 10 MB/s）时又因为旧基准下的"欠账"为负，
        在追平之前一直全速跑，看起来限速失效。
        """
        if limit_bps == self.limit_bps:
            return
        self.limit_bps = limit_bps
        self._rate_reset = True

    def _chunk_bytes(self) -> int:
        """本次请求发送的字节数：限速时压到约 1 秒的数据量。

        分片变大只是让限速的颗粒度变粗（发完一大块再长时间等待），
        压到 1 秒既有平滑的限速效果，暂停/取消也能更快生效。
        """
        if not self.limit_bps:
            return self.chunk_size
        return min(self.chunk_size, max(64 * 1024, self.limit_bps))

    def _pace(self, rate_start: float, rate_base: int) -> bool:
        """按限速补足等待时间；返回 True 表示等到一半被要求停止。

        以「本次传输的起始字节 + 起始时刻」为基准，算出到目前为止
        "按目标速率最多允许多少时间"，多用的时间就在这里补回来。
        """
        while self.limit_bps:
            if self._rate_reset:
                return False           # 基准被重置：交给循环顶部用新基准重算
            used = (self._offset - rate_base) / self.limit_bps
            remain = used - (time.time() - rate_start)
            if remain <= 0 or self._pause.is_set():
                return False           # 暂停交给循环顶部的暂停点统一处理
            if self._stop.is_set():
                return True
            time.sleep(min(remain, 0.2))
        return False

    def _notify(self):
        if self.on_progress:
            try:
                self.on_progress(self.uploaded, self.size, self.speed, self.status)
            except Exception:
                pass
    # ---------------------------------------------------------- 任务控制
    def start(self):
        """开始 / 从断点继续。"""
        with self._lock:
            if self.status == "running":
                return
            self._stop.clear()
            self._pause.clear()
            self.status = "running"
            if self._thread and self._thread.is_alive():
                return  # 线程还在跑（暂停后继续）
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def pause(self):
        """暂停：当前分片传完后停下，已传数据保留在服务端。

        同时立即取消服务端"正在上传"心跳标记：任务会**马上**回到
        "未完成任务"列表，重启程序也能立刻按文件名+大小找回断点，
        不必再等 90 秒心跳过期（否则这 90 秒里任务既显示为"上传中"
        又不在未完成列表，重启后会从头重传并多出一个 (1) 副本）。
        """
        self._pause.set()
        self.status = "paused"
        self.speed = 0.0
        self._unbeat()
        self._notify()

    def resume(self):
        """继续（走断点续传，从服务端已保存的字节数接着传）。"""
        self.status = "paused"
        self.start()

    def cancel(self):
        """取消任务，并彻底清掉服务端已上传的分片。

        优先走本程序的 ``/api/pending/{uid}``：它除了删文件，还会登记
        ``DROP_UIDS`` 拦掉正在飞行中的分片，并在随后的几十秒里反复复查
        竞态残留。只发 TUS 的 DELETE 时，取消瞬间恰好有个 PATCH 在途，
        它会在删除之后把任务文件写回来——服务端就留下一个删不掉的空任务，
        网页端「未完成任务」里一直显示着。
        """
        self._stop.set()
        self._pause.clear()
        self._unbeat()
        self.status = "canceled"
        uid = self._uid()
        if uid:
            try:
                code, _headers, _body = self._request(
                    "DELETE", f"/api/pending/{uid}",
                    headers=self._headers({}), timeout=30)
                # 非本机发起的任务走不通上面那个接口（403），退回 TUS 终止扩展
                if code >= 400 and self._path:
                    self._request("DELETE", self._path,
                                  headers=self._headers({}), timeout=30)
            except Exception:
                pass
        self._notify()

    def restart(self):
        """彻底重来（清空进度，从 0 开始）。"""
        self._stop.set()
        self._pause.clear()
        self.uploaded = 0
        self._offset = 0
        if self.upload_url:
            try:
                self._request("DELETE", self._path, headers=self._headers({}), timeout=30)
            except Exception:
                pass
        self.upload_url = ""
        self._path = ""
        self.status = "paused"
        self._notify()

    # ---------------------------------------------------------- 上传主循环
    def _run(self):
        try:
            if not self.upload_url:
                # 先找服务端残留的未完成任务（断点续传），没有再新建
                found = self._find_existing()
                if found:
                    self._attach_existing(found["uid"])
                    print(f"[续传] {self.name} 已有 {found['offset']} 字节，"
                          f"从断点继续（{found['offset']/1048576:.1f} MB）", flush=True)
                else:
                    self._create_upload()
            # 断点续传：从服务端实际已保存的字节数继续
            self._offset = self._query_offset()
            self.uploaded = self._offset
            self._notify()
            if not self._pause.is_set():
                self._beat(self._offset, 0.0)

            with open(self.path, "rb") as fp:
                fp.seek(self._offset)
                last_bytes, last_time = self._offset, time.time()
                # 限速的时间基准：本次传输的起始时刻与起始字节
                rate_start, rate_base = time.time(), self._offset
                while self._offset < self.size:
                    # 暂停点：让出线程等待继续
                    paused_at = None
                    while self._pause.is_set() and not self._stop.is_set():
                        if paused_at is None:
                            paused_at = time.time()
                        time.sleep(0.2)
                    if self._stop.is_set():
                        return
                    if paused_at is not None:
                        # 暂停的这段时间不计入限速基准，否则恢复后会一次性补传
                        rate_start += time.time() - paused_at
                    if self._rate_reset:
                        # 限速值变过：从当前字节重新计时，不再按旧基准补等/超额
                        self._rate_reset = False
                        rate_start, rate_base = time.time(), self._offset
                    # 文件指针始终对齐到服务端已确认的字节
                    if fp.tell() != self._offset:
                        fp.seek(self._offset)
                    block = fp.read(min(self._chunk_bytes(),
                                        self.size - self._offset))
                    if not block:
                        break
                    status, headers, _ = self._request("PATCH", self._path, body=block,
                        headers=self._headers({
                            "Upload-Offset": str(self._offset),
                            "Content-Type": "application/offset+octet-stream",
                        }), timeout=300)
                    if status in (404, 410):
                        # 410 = 任务被主动删除（本机窗口或网页端的「删除」）；
                        # 本任务已被取消 / 停止时同理：一律就此收尾，绝不能重建。
                        # 重建会在服务端留下一个进度为 0 的空任务，网页端
                        # 「未完成任务」里会一直挂着一个怎么删都删不掉的条目。
                        if (status == 410 or self._stop.is_set()
                                or self.status == "canceled"):
                            if status == 410 and not self._stop.is_set():
                                # 被别处（网页端 / 另一个窗口）删了：本地也收尾，
                                # 界面上显示「已取消」而不是一直挂着"上传中"
                                self.cancel()
                            return
                        # 服务端这个任务不存在了（过期清理等）：重建上传后从头传，
                        # 而不是直接失败
                        if getattr(self, "_recreated", False):
                            raise RuntimeError("上传任务已在服务端被删除，请重新选择文件")
                        self._recreated = True
                        self.upload_url = ""
                        self._path = ""
                        self._offset = 0
                        self.uploaded = 0
                        self.speed = 0.0
                        fp.seek(0)
                        self._create_upload()
                        self._offset = self._query_offset()
                        last_bytes, last_time = self._offset, time.time()
                        rate_start, rate_base = time.time(), self._offset
                        self._notify()
                        continue
                    if status == 409:
                        # 偏移冲突（多任务/多窗口同时传同一个文件）：
                        # 重新查询服务端真实进度后接着传，而不是直接失败
                        self._offset = self._query_offset()
                        self.uploaded = self._offset
                        last_bytes, last_time = self._offset, time.time()
                        rate_start, rate_base = time.time(), self._offset
                        self._notify()
                        continue
                    if status not in (200, 204):
                        raise RuntimeError(f"分片上传失败（HTTP {status}）")
                    self._offset = int(headers.get("Upload-Offset")
                                       or headers.get("upload-offset")
                                       or self._offset + len(block))
                    self.uploaded = self._offset
                    # 关键：分片传完先看是不是"已经被要求暂停/停止"。
                    # 暂停时绝不补发心跳——否则刚被 pause() 清掉的活跃标记
                    # 又会被写回来，任务随后的 90 秒里既显示"上传中"、
                    # 又不在"未完成任务"列表，重启后只能从头重传。
                    if self._pause.is_set() or self._stop.is_set():
                        self.speed = 0.0
                        self._unbeat()
                        self._notify()
                        continue
                    # 速度：0.3 秒采样 + 平滑
                    now = time.time()
                    dt = now - last_time
                    if dt >= 0.3:
                        inst = (self._offset - last_bytes) / 1048576 / dt
                        self.speed = (self.speed * 0.6 + inst * 0.4) if self.speed else inst
                        last_bytes, last_time = self._offset, now
                        # 广播进度给其它设备，并接收服务端的暂停指令
                        reply = self._beat(self._offset, self.speed)
                        if reply and reply.get("paused"):
                            self.pause()
                            continue
                        if (reply and reply.get("alive") is False
                                and self._offset < self.size):
                            # 任务已被删除（本机窗口或网页端删的）：立刻收尾，
                            # 既不继续传，也绝不重建。
                            # 必须带上"还没传完"这个条件——服务端在**正常完成**时
                            # 同样会删掉 .info，此时心跳也回 alive=false，不判断
                            # 就会把刚传完的任务误标成已取消。
                            print(f"[取消] {self.name} 已被删除，停止上传", flush=True)
                            self.cancel()
                            return
                    self._notify()
                    # 限速：按目标速率补足等待，等待期间收到停止信号就直接退出
                    if self._pace(rate_start, rate_base):
                        return
            self.status = "done"
            self.speed = 0.0
            self._unbeat()
            self._notify()
            if self.on_done:
                self.on_done(self)
        except Exception as exc:
            self.status = "failed"
            self.error = str(exc)
            self.speed = 0.0
            self._unbeat()
            self._notify()

# ==========================================================================
# 本机上传任务的持久化（任务重启后仍能在界面看到并继续）
# ==========================================================================


def load_local_tasks() -> list:
    """读取上次退出时的上传任务列表。"""
    try:
        with open(LOCAL_TASK_FILE, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def save_local_tasks(items: list) -> None:
    """保存上传任务列表（原子写，避免中断损坏文件）。"""
    try:
        os.makedirs(os.path.dirname(LOCAL_TASK_FILE), exist_ok=True)
        tmp = LOCAL_TASK_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(items, fp, ensure_ascii=False, indent=1)
        os.replace(tmp, LOCAL_TASK_FILE)
    except OSError:
        pass


def verify_task_file(path: str, size: int = 0, mtime: float = 0.0):
    """校验原文件是否仍是同一个（名称/大小/修改时间）。

    返回 (是否可用, 说明)
    """
    if not os.path.isfile(path):
        return False, "原文件不存在（可能已被移动或删除）"
    st = os.stat(path)
    if size and st.st_size != size:
        return False, (f"文件大小已变化：记录 {size} 字节，"
                       f"当前 {st.st_size} 字节")
    if mtime and abs(st.st_mtime - mtime) > 1:
        return False, "文件修改时间已变化，可能不是同一个文件"
    return True, "校验通过"
