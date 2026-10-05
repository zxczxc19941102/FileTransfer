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
import os
import threading
import time
import urllib.parse


class UploadTask:
    """一个本机上传任务。"""

    def __init__(self, path: str, port: int, chunk_size: int = 32 * 1024 * 1024):
        self.path = path
        self.name = os.path.basename(path)
        self.size = os.path.getsize(path)
        self.port = port
        self.chunk_size = chunk_size
        self.status = "paused"        # 初始暂停，点"开始"才上传
        self.uploaded = 0              # 已传字节（续传时从服务端 offset 起算）
        self.speed = 0.0               # MB/s
        self.error = ""
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
        """
        name_b64 = base64.b64encode(self.name.encode("utf-8")).decode()
        type_b64 = base64.b64encode(b"application/octet-stream").decode()
        return f"filename {name_b64},filetype {type_b64}"

    def _request(self, method: str, path: str, body=None, headers=None, timeout=60):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, dict(resp.getheaders()), data
        finally:
            conn.close()

    def _find_existing(self):
        """在服务端"未完成任务"中查找同名同大小的任务。

        这是跨进程 / 跨程序重启断点续传的关键：程序被强杀后，原来的
        UploadTask 对象已不存在，新对象靠文件名 + 大小找回服务端残留的
        分片，继续传而不是从头再来。
        """
        try:
            import json
            # 统一走 http.client：urllib 的连接复用在部分场景会被服务端提前
            # 关闭连接，导致随机 ConnectionResetError
            status, _headers, raw = self._request("GET", "/api/pending", timeout=15)
            if status != 200:
                return None
            data = json.loads(raw.decode("utf-8"))
            for item in data.get("pending", []):
                if item.get("name") == self.name and int(item.get("size") or 0) == self.size:
                    return item
        except Exception:
            pass
        return None

    def _attach_existing(self, uid: str):
        """绑定到服务端已有的未完成上传。"""
        self.upload_url = f"http://127.0.0.1:{self.port}/api/upload/{uid}"
        self._path = f"/api/upload/{uid}"

    def _create_upload(self):
        """创建 TUS 上传，返回资源地址。"""
        status, headers, _ = self._request("POST", "/api/upload/", body=b"",
            headers=self._headers({"Upload-Length": str(self.size),
                                   "Upload-Metadata": self._meta_header()}))
        if status not in (200, 201):
            raise RuntimeError(f"创建上传失败（HTTP {status}），磁盘空间可能不足")
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
        """暂停：当前分片传完后停下，已传数据保留在服务端。"""
        self._pause.set()
        self.status = "paused"
        self._notify()

    def resume(self):
        """继续（走断点续传，从服务端已保存的字节数接着传）。"""
        self.status = "paused"
        self.start()

    def cancel(self):
        """取消任务，并删除服务端已上传的分片。"""
        self._stop.set()
        self._pause.clear()
        self.status = "canceled"
        if self.upload_url:
            try:
                self._request("DELETE", self._path, headers=self._headers({}), timeout=30)
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

            with open(self.path, "rb") as fp:
                fp.seek(self._offset)
                last_bytes, last_time = self._offset, time.time()
                while self._offset < self.size:
                    # 暂停点：让出线程等待继续
                    while self._pause.is_set() and not self._stop.is_set():
                        time.sleep(0.2)
                    if self._stop.is_set():
                        return
                    # 文件指针始终对齐到服务端已确认的字节
                    if fp.tell() != self._offset:
                        fp.seek(self._offset)
                    block = fp.read(min(self.chunk_size, self.size - self._offset))
                    if not block:
                        break
                    status, headers, _ = self._request("PATCH", self._path, body=block,
                        headers=self._headers({
                            "Upload-Offset": str(self._offset),
                            "Content-Type": "application/offset+octet-stream",
                        }), timeout=300)
                    if status == 404:
                        # 服务端这个任务已被删除（过期清理或手动删除）：
                        # 重建上传后从头传，而不是直接失败
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
                        self._notify()
                        continue
                    if status == 409:
                        # 偏移冲突（多任务/多窗口同时传同一个文件）：
                        # 重新查询服务端真实进度后接着传，而不是直接失败
                        self._offset = self._query_offset()
                        self.uploaded = self._offset
                        last_bytes, last_time = self._offset, time.time()
                        self._notify()
                        continue
                    if status not in (200, 204):
                        raise RuntimeError(f"分片上传失败（HTTP {status}）")
                    self._offset = int(headers.get("Upload-Offset")
                                       or headers.get("upload-offset")
                                       or self._offset + len(block))
                    self.uploaded = self._offset
                    # 速度：0.3 秒采样 + 平滑
                    now = time.time()
                    dt = now - last_time
                    if dt >= 0.3:
                        inst = (self._offset - last_bytes) / 1048576 / dt
                        self.speed = (self.speed * 0.6 + inst * 0.4) if self.speed else inst
                        last_bytes, last_time = self._offset, now
                    self._notify()
            self.status = "done"
            self.speed = 0.0
            self._notify()
            if self.on_done:
                self.on_done(self)
        except Exception as exc:
            self.status = "failed"
            self.error = str(exc)
            self.speed = 0.0
            self._notify()