"""
局域网 IP / 端口探测工具
=========================

被 main.py 调用，用于自动获取本机在局域网中的 IP、寻找空闲端口。
"""
import random
import socket
import subprocess
import sys

# 常见私有网段，排序时优先
PRIVATE_PREFIX = ("192.168.", "10.", "172.16.", "172.17.", "172.18.", "172.19.",
                  "172.2", "172.30.", "172.31.")


def _probe_ip() -> str:
    """UDP 套接字连接不会真正发包，但系统会按路由表选出网卡，从而得到该网卡 IP。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(0.3)
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return ""
    finally:
        sock.close()


def list_lan_ips() -> list:
    """枚举所有可用于局域网访问的 IPv4 地址（多网卡、有线 + WiFi 都能列出）。"""
    found = []

    def add(ip):
        if ip and not ip.startswith("127.") and ip not in found:
            found.append(ip)

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            add(info[4][0])
    except OSError:
        pass

    if sys.platform == "win32":
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

    found.sort(key=lambda ip: 0 if ip.startswith(PRIVATE_PREFIX) else 1)
    return found


def get_lan_ip() -> str:
    """自动获取本机局域网 IP（用户无需填写）。"""
    ip = _probe_ip()
    if ip and not ip.startswith("127."):
        return ip
    ips = list_lan_ips()
    return ips[0] if ips else "127.0.0.1"


def is_port_free(port: int) -> bool:
    """判断端口能否被本程序占用。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def find_free_port(preferred: int = 0) -> int:
    """自动分配端口：指定端口被占用则报错；未指定则随机挑选空闲端口。"""
    if preferred:
        if is_port_free(preferred):
            return preferred
        raise SystemExit("[错误] 端口已被占用，请换一个或去掉 --port 参数。")
    for _ in range(300):
        port = random.randint(20000, 60000)
        if is_port_free(port):
            return port
    sock = socket.socket()
    sock.bind(("", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port