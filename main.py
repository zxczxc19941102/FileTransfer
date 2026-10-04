"""文件传输助手 —— 电脑端启动入口。

用法::

    python main.py                 # 自动选 IP / 端口
    python main.py --port 9000
    python main.py --dir D:\\接收   # 指定接收目录
    python main.py --pin 1234      # 开启访问口令
"""
import argparse
import os
import sys
import threading
import time
import webbrowser

BASE_DIR = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
RES_DIR = getattr(sys, "_MEIPASS", BASE_DIR)

sys.path.insert(0, RES_DIR)
sys.path.insert(0, BASE_DIR)

from app import create_app  # noqa: E402
from netutils import get_all_lan_ips, get_lan_ip, is_port_free, pick_port  # noqa: E402

BANNER = r"""
  ____  ___    ___  ___   ___  ___  ___  ___
 / __|/ _ \  / _ \/ __|/ _ \/ _ \/ __|/ __|
| (__| (_) || (_) \__ \| (_) \ (_) \__ \ (__|
 \___|\___/ \___/|___/\___/\___/|___/__\_\
"""


def build_qr(url: str, png_path: str, show_terminal: bool = True) -> None:
    """生成二维码 PNG，并在终端打印可扫描的字符二维码。"""
    try:
        import qrcode
    except ImportError:
        print("[提示] 未安装 qrcode，跳过二维码生成：pip install qrcode pillow")
        return
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M,
                       box_size=8, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    try:
        img = qr.make_image(fill_color="black", back_color="white")
        os.makedirs(os.path.dirname(png_path), exist_ok=True)
        img.save(png_path)
        print(f"[二维码图片] {png_path}")
    except Exception as exc:  # pragma: no cover
        print(f"[提示] 二维码图片保存失败：{exc}")

    if not show_terminal:
        return
    try:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
        qr.print_ascii(tty=False, invert=True)
    except Exception:
        print("[提示] 终端无法显示字符二维码，请查看生成的二维码图片。")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="局域网文件传输助手")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址，默认 0.0.0.0（所有网卡）")
    parser.add_argument("--port", type=int, default=0, help="端口，默认自动选择 8000 起")
    parser.add_argument("--dir", default=os.path.join(BASE_DIR, "received"), help="文件接收目录")
    parser.add_argument("--pin", default="", help="访问口令（留空表示不启用）")
    parser.add_argument("--no-browser", action="store_true", help="启动时不自动打开管理页")
    parser.add_argument("--no-qr", action="store_true", help="不在终端显示二维码")
    args = parser.parse_args()

    receive_dir = os.path.abspath(args.dir)
    data_dir = os.path.join(BASE_DIR, "data")
    os.makedirs(receive_dir, exist_ok=True)
    os.makedirs(data_dir, exist_ok=True)

    port = args.port or pick_port(8000)
    if args.port and not is_port_free(args.port):
        print(f"[错误] 端口 {args.port} 已被占用，请换一个端口。")
        return 1

    app = create_app(receive_dir, data_dir, pin=args.pin.strip())

    print(BANNER)
    print("  局域网文件互传助手 v1.0")
    print(f"[接收目录] {receive_dir}")
    print(f"[数据文件] {os.path.join(data_dir, 'records.json')}")
    if args.pin.strip():
        print(f"[访问口令] 已启用")
    print("-" * 62)

    lan_ips = get_all_lan_ips()
    main_ip = get_lan_ip()
    base_url = f"http://{main_ip}:{port}"
    print(f"  电脑管理台: {base_url}/admin")
    print(f"  手机上传页: {base_url}/upload")
    if len(lan_ips) > 1:
        print(f"  其他网卡地址: {', '.join(f'http://{ip}:{port}' for ip in lan_ips if ip != main_ip)}")
    print(f"  下载直链格式: {base_url}/d/<文件ID>")
    print("-" * 62)
    print("  用手机连同一个 WiFi，扫下面二维码即可发送文件：")
    print()
    build_qr(f"{base_url}/upload", os.path.join(data_dir, "upload_qrcode.png"),
             show_terminal=not args.no_qr)
    print()
    print("  提示：手机与电脑必须处于同一局域网；如无法访问请关闭电脑防火墙或允许 Python 通过防火墙。")
    print("  按 Ctrl+C 停止服务。")
    print("-" * 62)

    admin_url = f"{base_url}/admin"
    if not args.no_browser:
        threading.Thread(target=lambda: (time.sleep(1.0), webbrowser.open(admin_url)),
                         daemon=True).start()

    try:
        from waitress import serve
    except ImportError:
        print("[提示] 未安装 waitress，回退到 Flask 开发服务器。")
        app.run(host=args.host, port=port, threaded=True, debug=False)
        return 0

    print(f"[服务已启动] 监听 {args.host}:{port}  |  访问 {admin_url}")
    try:
        serve(app, host=args.host, port=port, threads=24,
              max_request_body_size=64 * 1024 * 1024, channel_timeout=600,
              connection_limit=500, clear_untrusted_proxy_headers=True)
    except KeyboardInterrupt:
        print("\n[服务已停止] 感谢使用。")
    except OSError as exc:
        print(f"[错误] 服务启动失败：{exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
