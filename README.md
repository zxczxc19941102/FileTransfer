# 局域网文件互传工具（电脑端服务程序）

手机扫码 → 电脑端接收文件。全部通信只在局域网内完成，**不做公网穿透、不使用任何第三方服务器**。

## 一、前置条件

- 手机与电脑连接**同一个 WiFi / 局域网**
- tkinter 用于显示二维码窗口；无 tkinter 时自动降级为控制台字符二维码

## 二、安装依赖

```bash
cd 项目目录
pip install -r requirements.txt
```

| 依赖 | 作用 |
| --- | --- |
| fastapi | HTTP 服务框架（上传 / 列表 / 下载接口） |
| uvicorn | ASGI 服务器，运行 FastAPI |
| python-multipart | 解析网页文件上传表单（FastAPI 必需） |
| qrcode | 生成二维码 |
| pillow | 输出二维码 PNG 图片，供窗口显示 |

## 三、运行

```bash
python main.py                 # 自动 IP + 随机端口 + 二维码窗口（推荐）
python main.py --port 8080     # 指定端口
python main.py --no-gui        # 不弹窗口，控制台打印字符二维码
python main.py --max-size 512  # 单文件上限改为 512 MB（默认 2048）
python main.py --no-browser    # 启动时不自动打开浏览器
```

启动后：窗口显示二维码（内容 `http://内网IP:端口`），控制台同步打印地址；首次运行会自动创建 `uploads` 文件夹，所有上传文件保存在这里；手机连同一 WiFi，相机扫码即可打开网页。
## 四、使用方法

- **手机上传**：点击虚线框选择文件（可多选），页面显示上传进度；完成后返回局域网下载链接 `http://内网IP:端口/files/xxxx`。
- **查看 / 下载**：页面下方列出**所有设备上传的全部文件**（每 3 秒自动刷新），**点击任意一行即开始下载**，电脑本机与手机都能下载。
- **电脑端窗口**：二维码下方即文件列表，**双击某行**在本机下载该文件；另有「打开网页」「打开文件夹」按钮。
- **停止服务**：关闭窗口、点击「退出」或按 `Ctrl+C`，web 服务会一并关闭。

## 五、命令行参数

| 参数 | 说明 | 默认值 |
| --- | --- | --- |
| `--host` | 监听地址 | `0.0.0.0`（所有网卡） |
| `--port` | 服务端口，`0` 表示随机分配空闲端口 | `0` |
| `--max-size` | 单文件大小上限（MB） | `2048` |
| `--no-gui` | 不启动 tkinter 窗口 | 关闭 |
| `--no-browser` | 启动时不自动打开浏览器 | 关闭 |

## 六、故障排查

1. **手机扫码打不开页面**
   - 确认手机与电脑连的是**同一个** WiFi（访客网络 / 5G 与 2.4G 分离常常不通）；
   - 防火墙拦截：Windows 首次运行会弹窗，需勾选「专用网络」允许 Python 访问。若误点取消，可手动放行：`设置 → 网络和 Internet → 防火墙 → 允许应用通过防火墙 → 勾选 Python`；
   - 在电脑浏览器先打开控制台显示的 `http://内网IP:端口`，确认服务本身正常。
2. **IP 识别错误 / 换网络后打不开**
   - 电脑有多张网卡（有线 + WiFi + 虚拟机）时，程序会打印「其它网卡 IP」，逐个尝试；
   - 也可在浏览器手动输入 `http://正确的内网IP:端口` 访问。
3. **提示端口被占用**：换一个 `--port` 值，或去掉该参数让程序随机分配。
4. **上传大文件失败**：默认上限 2048 MB，用 `--max-size` 调大；超限返回 413 并自动删除未完成的文件。
5. **窗口不弹出来**：当前 Python 缺少 tkinter 时会降级，改用 `python main.py --no-gui`，控制台会打印字符二维码，同时二维码图片保存为程序目录下的 `lan_qrcode.png`。
6. **手机之间互相打不开**：路由器开启「AP 隔离 / 客户端隔离」时需在路由器中关闭。
<<<<<<< HEAD
## 七、打包成 exe（无黑框窗口 + UPX 压缩 + 数字签名）

### 1. 一次性准备

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
.venv\Scripts\pip install pyinstaller -i https://pypi.tuna.tsinghua.edu.cn/simple
```

UPX 压缩工具已放在 `tools\upx-5.2.1-win64\`（若缺失会自动跳过压缩，不影响打包）。

### 2. 一键打包

```bash
.venv\Scripts\python.exe build.py
```

产物：`dist\FileTransfer.exe`，**双击不会出现命令提示符黑框**，只显示二维码窗口。

- `--noconsole`：去掉控制台边框
- `--upx-dir`：用 UPX 压缩内部的 python314.dll 与各 `.pyd`，实测体积从 **26.8 MB 降到 21.6 MB（-19.5%）**
- 上传文件保存在 **exe 同目录的 `uploads\`** 文件夹（打包后路径已修正，不会存到临时目录）

### 3. 免费数字签名

签名用的证书由脚本自动生成（自签名，无需付费申请）：

```bash
powershell -NoProfile -ExecutionPolicy Bypass -File make_cert.ps1   # 生成/复用证书
powershell -NoProfile -ExecutionPolicy Bypass -File sign.ps1       # 给 exe 签名
```

- 证书：`CN=FileTransfer Local`，RSA 3072 / SHA256，用途为**代码签名**，有效期 3 年
- 已自动加 **DigiCert 时间戳**（到 2037 年），证书过期后签名依然有效
- 产物：`tools\FileTransfer-CodeSign.cer` / `.pfx`（密码见 `tools\pfx-password.txt`）

**消除本机"未知发布者"提示**（只需做一次）：

1. 双击 `tools\FileTransfer-CodeSign.cer`
2. 选择「安装证书」→「当前用户」
3. 选择「将所有的证书放入下列存储」→「受信任的根证书颁发机构」→ 完成
4. 再双击 `dist\FileTransfer.exe`，签名状态变为 `Valid`

### 签名方案说明（重要）

| 方案 | 费用 | 本机提示 | 他人电脑提示 |
| --- | --- | --- | --- |
| 自签名（本方案） | 免费 | 无提示 | 仍提示未知发布者 |
| SignPath Foundation | 免费（限公开开源项目，需审核） | 无提示 | 无提示 |
| 商业 OV/EV 证书 | 每年数百美元 | 无提示 | 无提示 |

也就是说：**免费方案只能做到"自己电脑不提示"**。要让任何人都看到可信发布者，需走 SignPath Foundation 申请或购买商业证书。
## 八、换新电脑继续开发

```bash
git clone https://github.com/zxczxc19941102/FileTransfer.git
cd FileTransfer
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
.venv\Scripts\pip install pyinstaller -i https://pypi.tuna.tsinghua.edu.cn/simple
.venv\Scripts\python.exe main.py
```

仓库内已包含下列文件，**无需额外准备**：

| 文件 | 用途 |
| --- | --- |
| `tools\upx-5.2.1-win64\upx.exe` | UPX 压缩器，打包时自动调用 |
| `tools\FileTransfer-CodeSign.pfx` | 代码签名证书（密码见 `tools\pfx-password.txt`） |
| `tools\FileTransfer-CodeSign.cer` | 公钥证书，装到本机受信任根后 exe 显示为已签名 |
| `tools\cert-thumbprint.txt` | 证书指纹，`sign.ps1` 用它定位证书 |

因此新电脑上执行 `build.py` 即可直接产出**同签名身份**的 exe。

**注意**：`tools\FileTransfer-CodeSign.pfx` 与 `tools\pfx-password.txt` 是私钥与密码，本仓库为公开仓库，任何人拿到后都能用该身份签署程序。若在意，可将这两个文件从仓库移除并在本地单独备份，换电脑时手动拷贝。
=======
>>>>>>> 3167bf58de6e988757747d13cc89af96d9bb6636
