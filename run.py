"""局域网文件传输工具 —— 推荐启动入口
==========================================

用法（与 main.py 完全一致）：

    .venv\\Scripts\\python.exe run.py                 # 二维码窗口（推荐）
    .venv\\Scripts\\python.exe run.py --port 8080     # 指定端口
    .venv\\Scripts\\python.exe run.py --no-gui        # 仅控制台

为什么不直接用 main.py
----------------------
main.py 本身功能完整，但有一个退出行为问题：**关闭窗口后进程不会马上结束**。

原因：上传完成后，服务端会用

    await asyncio.to_thread(sha256_of, 文件路径)

计算文件校验值（见 store.py 的 on_upload_complete）。这个调用跑在 Python
默认线程池里，而 concurrent.futures.thread 模块在解释器退出时会注册一个
清理钩子，**无条件等待所有工作线程结束**。

于是当你在"服务端正在给几十 GB 文件算 SHA256"时关闭窗口，主线程虽然立刻
返回了，进程却要一直等到哈希算完才真正退出 —— 表现为窗口关了、任务管理器
里进程还在（还占着 CPU），几十 GB 的文件可能挂十几分钟。

本文件的做法
------------
threading._register_atexit 注册的钩子是「后注册先执行」。因此：

1. 先导入 concurrent.futures.thread，让它那个"等待线程"的钩子先注册；
2. 再注册我们自己的 os._exit(0)，它会排在上面那个钩子之前执行，
   在主流程收尾完成后直接结束进程，跳过对长任务的等待。

主流程该做的收尾（保存本机任务、暂停进行中的上传、置 should_exit 让
uvicorn 停止监听）都在 main() 内部，位于本钩子执行之前，不会被跳过。

实测（1GB 文件反复哈希约 10 秒，在该任务运行期间结束主线程）：

    直接跑 main.py      进程 10.1 秒后才退出
    跑 run.py          进程 1.3 秒内退出
"""
import concurrent.futures.thread  # noqa: F401  必须最先导入，见上文说明
import os
import threading


def _install_fast_exit() -> None:
    """安装"快速退出"钩子：结束主线程后立即终止进程。

    pyinstaller 打包后同样有效；若运行环境缺少该私有 API，
    则退回 atexit（在较老的 Python 上可能仍会等待线程池，但不会报错）。
    """
    hook = lambda: os._exit(0)  # noqa: E731
    register = getattr(threading, "_register_atexit", None)
    if register is not None:
        register(hook)
    else:  # 兜底：Python 3.8 及更早版本没有该接口
        import atexit
        atexit.register(hook)


def main() -> None:
    """加载 main.py 并启动（延迟导入，保证钩子先装好）。"""
    _install_fast_exit()
    # 依赖自举：必须在导入 main（其内部会导入第三方库）之前完成
    import runtime
    runtime.ensure_runtime(os.path.abspath(__file__))
    import main as app_entry
    app_entry.main()


if __name__ == "__main__":
    main()
