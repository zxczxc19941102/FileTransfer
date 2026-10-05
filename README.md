# 局域网文件传输工具（TUS 分片上传 · 支持 100G 大文件）

手机 / 电脑扫码打开网页即可上传与下载文件，**全部通信只在局域网内完成**，
不做内网穿透、不接入任何公网服务。

核心能力：**TUS 分片断点续传** + **全程流式写盘（内存占用恒定）**，
单个文件不设大小上限，100G 级文件也能稳定传输。

## 一、前置条件

- 电脑安装 **Python 3.9+**（安装时勾选 *Add Python to PATH*）
- 手机与电脑连接**同一个 WiFi / 局域网**
- **100G 文件传输的硬件前提**：
  - 磁盘剩余空间 ≥ 文件大小（4 个 100G 文件需 ≥ 450G，程序启动时会在控制台/窗口显示剩余空间）
  - 传输前请确认接收盘不是 FAT32（单文件上限 4GB），推荐 NTFS / exFAT
  - 手机端需要足够电量与稳定的 WiFi，中途断开可直接续传

## 二、安装依赖

```bash
cd 项目目录
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

| 依赖 | 作用 |
| --- | --- |
| fastapi | Web 框架（页面 / 列表 / 下载接口） |
| uvicorn | ASGI 服务器 |
| httptools | 高性能 HTTP 解析器，大文件流式传输必需 |
| tuspyserver | TUS 1.0 分片上传协议实现（断点续传 / 过期清理） |
| qrcode | 生成访问地址二维码 |
| pillow | 二维码图片渲染，供 tkinter 窗口显示 |

## 三、运行

```bash
.venv\Scripts\python.exe run.py                 # 推荐：二维码窗口
.venv\Scripts\python.exe run.py --port 8080     # 指定端口
.venv\Scripts\python.exe run.py --no-gui        # 只控制台，打印字符二维码
.venv\Scripts\python.exe run.py --no-browser    # 不自动打开浏览器
```

> **为什么用 `run.py` 而不是 `main.py`**
>
> 两者的功能、参数完全一致，区别只在**退出行为**。
>
> 上传完成后，服务端会用 `await asyncio.to_thread(sha256_of, 文件)` 计算校验值
> （见 `store.py`），几十 GB 的文件要跑几分钟。这个调用使用的是 Python 默认
> 线程池，而该线程池在解释器退出时会**无条件等待所有工作线程结束**。
>
> 结果是：如果你在"服务端正在给大文件算 SHA256"时关闭窗口，窗口虽然消失了，
> 进程却会一直挂在那里（任务管理器里还能看到，还占着 CPU），要等哈希算完才
> 真正退出。
>
> `run.py` 在启动前注册了一个退出钩子，让进程在主流程收尾（保存任务、暂停
> 上传、停止服务）完成后直接结束，不再等待线程池里的长任务。实测同一场景：
> `main.py` 要 11.3 秒才退出，`run.py` 只需 2.7 秒。
>
> 直接运行 `main.py` 也完全可用，只是可能遇到上面这个退出缓慢的问题。

启动后：

1. 自动获取局域网 IP + 随机空闲端口，窗口显示**二维码 + IP + 端口**，控制台同步打印；
2. 首次运行自动创建 `uploads` 文件夹（接收目录）；
3. 手机连同一 WiFi，用相机扫码即可打开网页。

## 四、使用方法

### 上传（手机 / 电脑均可）

1. 点击虚线框选择文件（可多选，单个文件可到 100G 级）；
2. 页面显示实时进度条（已传字节 / 总大小 / 百分比）；
3. 上传采用 **32MB 分片**逐块传输，每片独立请求，失败自动重试（0s/1s/3s/5s/10s/20s/30s/60s 递增退避）。

### 断点续传（重点）

- 网络中断、切后台、锁屏导致上传失败时，**重新选择同一个文件即可继续**，不会从头传；
- 原理：客户端上传前先 `HEAD` 查询服务端已保存的字节数（`Upload-Offset`），从断点位置续传；
- 服务端严格校验偏移量（`strict_offset_validation`），保证续传字节不会错位。

### 下载

- 页面下方列出**所有设备上传的全部文件**（每 3 秒自动刷新）；
- **点击任意一行即开始下载**，电脑本机、其它电脑、手机都能下载；
- 下载同样是流式发送，不占内存，支持大文件。

### 停止服务

关闭窗口、点击「退出」或按 `Ctrl+C`，web 服务一并关闭。

## 五、命令行参数

| 参数 | 说明 | 默认值 |
| --- | --- | --- |
| `--host` | 监听地址 | `0.0.0.0`（所有网卡） |
| `--port` | 端口，`0` 表示随机分配空闲端口 | `0` |
| `--no-gui` | 不启动 tkinter 窗口 | 关闭 |
| `--no-browser` | 启动时不自动打开浏览器 | 关闭 |

## 六、目录结构

```
main.py       入口：IP/端口、二维码、tkinter 窗口、uvicorn 启动
store.py      后端：TUS 路由、流式存储、空间预检、哈希校验、过期清理、内嵌网页
netutils.py   局域网 IP 探测与空闲端口分配
tus.min.js    内嵌的 tus-js-client（离线可用，不依赖外网 CDN）
uploads/      接收到的文件（原名保存，同名自动加序号）
uploads/.meta 每个文件的元数据（大小 / SHA256 / 接收时间）
uploads/.tus  上传中的临时分片（过期自动清理，不占用长期空间）
```
## 七、技术实现要点

| 需求 | 实现方式 |
| --- | --- |
| 100G 级单文件 | `max_size = 2**62`（4EB），代码层面不设上限；真正约束是磁盘空间 |
| 内存恒定 | 上传：`request.stream()` 逐块接收 → 每块立刻 `append` 写盘；下载：`FileResponse` 流式发送 |
| 磁盘空间预检 | TUS `pre_create` 钩子读取 `Upload-Length`，剩余不足直接返回 **507** 并提示差多少 |
| 哈希校验 | 上传完成后后台线程流式计算 SHA256（4MB 一块），存入元数据；`/verify/<id>` 可随时复算比对 |
| 过期分片清理 | 后台任务每小时执行：按 `.info` 的过期时间清理未完成上传，并清理进程被强杀留下的孤立分片 |
| 页面实时刷新 | 文件列表每 3 秒自动刷新 |
| 多设备并发 | 异步 IO + 每文件独占锁，多台设备可同时上传下载 |

**实测数据**（本机回环，Windows / Python 3.14）：

- 2GB 文件：完整上传 → SHA256 比对一致 → 完整下载比对一致
- 上传速度 ~150-220 MB/s，浏览器端 100MB 文件 74 MB/s
- **服务端内存峰值 76.7 MB**（起始 44 MB，增长量 ≈ 单个 32MB 分片，与文件大小无关）

## 八、故障排查

1. **手机扫码打不开页面**
   - 确认手机与电脑连的是**同一个** WiFi（访客网络、5G/2.4G 分离常导致不通）；
   - Windows 防火墙首次运行会弹窗，需勾选「专用网络」允许 Python 访问。误点了取消可手动放行：
     `设置 → 网络和 Internet → 防火墙 → 允许应用通过防火墙 → 勾选 Python`；
   - 先在电脑浏览器打开控制台显示的 `http://内网IP:端口`，确认服务本身正常。
2. **IP 识别错误 / 换网络后打不开**
   - 电脑有多网卡（有线 + WiFi + 虚拟机）时程序会打印「其它网卡 IP」，逐个尝试；
   - 也可手动在浏览器输入 `http://正确的内网IP:端口`。
3. **WiFi 不稳定导致分片中断**
   - 直接**重新选择同一个文件**继续上传即可，服务端已保存的字节不会丢；
   - 页面显示失败时先别刷新页面，等 WiFi 恢复后重新选文件；
   - 建议把 32MB 分片调小（`store.py` 里 HTML 中的 `chunkSize`）来降低单片失败概率，代价是请求数变多。
4. **上传被拒绝并提示磁盘空间不足（HTTP 507）**
   - 清理接收盘空间，或把 `--dir`/`UPLOAD_DIR` 指向空间更大的磁盘；
   - 程序在创建上传时就校验，不会先传完再失败。
5. **上传 4GB 以上失败**
   - 接收盘很可能是 FAT32（单文件上限 4GB），请改用 NTFS / exFAT。
6. **窗口不弹出来**
   - 当前 Python 缺少 tkinter，改用 `--no-gui`，控制台会打印字符二维码，
     同时二维码图片保存为程序目录下的 `lan_qrcode.png`。
7. **端口被占用**：换一个 `--port` 值，或省略该参数让程序随机分配。
## 九、打包成 exe（无黑框 + UPX 压缩 + 数字签名）

```bash
.venv\Scripts\python.exe build.py        # 一键打包（含 UPX 压缩与签名）
```

产物 `dist\FileTransfer.exe`：单文件、双击只显示二维码窗口（无命令提示符黑框）。

- `--noconsole`：去掉控制台窗口
- `--upx-dir`：用 UPX 压缩内部二进制，实测体积 26.8 MB → 21.6 MB
- `tus.min.js` 等静态资源通过 `--add-data` 打进 exe，运行时从解压目录读取
- **注意**：exe 的接收目录是 exe 所在文件夹下的 `uploads\`（不是临时目录）

数字签名（免费自签名方案）：

```bash
powershell -NoProfile -ExecutionPolicy Bypass -File make_cert.ps1   # 生成/复用证书
powershell -NoProfile -ExecutionPolicy Bypass -File sign.ps1        # 给 exe 签名
```

- 证书 `CN=FileTransfer Local`，RSA 3072 / SHA256，代码签名用途，有效期 3 年
- 自动附加 DigiCert 时间戳（到 2037 年），证书过期后旧签名依然有效
- 消除本机「未知发布者」提示：双击 `tools\FileTransfer-CodeSign.cer` →
  安装证书 → 当前用户 → 「受信任的根证书颁发机构」

| 签名方案 | 费用 | 本机提示 | 他人电脑 |
| --- | --- | --- | --- |
| 自签名（本方案） | 免费 | 无提示 | 仍提示未知发布者 |
| SignPath Foundation | 免费（限公开开源项目） | 无提示 | 无提示 |
| 商业 OV/EV 证书 | 每年数百美元 | 无提示 | 无提示 |

## 十、换新电脑继续开发

```bash
git clone https://github.com/zxczxc19941102/FileTransfer.git
cd FileTransfer
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
.venv\Scripts\pip install pyinstaller -i https://pypi.tuna.tsinghua.edu.cn/simple
.venv\Scripts\python.exe main.py
```

仓库内已包含 `tools\upx-5.2.1-win64\upx.exe` 与签名证书（`.pfx` + 密码），
新电脑无需任何额外准备即可打包出**同签名身份**的 exe。

> `tools\FileTransfer-CodeSign.pfx` 与 `tools\pfx-password.txt` 是私钥与密码，
> 本仓库为公开仓库，任何人拿到都能用该身份签名程序；如需收回，删除这两个文件
> 并加入 `.gitignore` 即可（已推送的历史仍需重写才能彻底清除）。