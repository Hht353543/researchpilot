"""Double-click entrypoint: a single local server, automatic browser, and a small tray menu."""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import socket
import threading
import time
import uuid
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, cast

import httpx
import uvicorn

from researchpilot.api.app import create_app
from researchpilot.desktop.api import DesktopController
from researchpilot.desktop.settings import DesktopStore, default_data_root
from researchpilot.persistence import atomic_write_text
from researchpilot.security import safe_diagnostic
from researchpilot.utils import sha1_of


class SingleInstance:
    def __init__(self, root: Path) -> None:
        self.kernel = cast(Any, ctypes).WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        self.kernel.CreateMutexW.restype = ctypes.c_void_p
        self.kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        self.kernel.ReleaseMutex.argtypes = [ctypes.c_void_p]
        self.handle = self.kernel.CreateMutexW(
            None, True, "Local\\ResearchPilot-" + sha1_of(str(root).lower())
        )
        if not self.handle:
            raise OSError("无法准备程序启动，请重试。")
        self.primary = cast(Any, ctypes).get_last_error() != 183

    def close(self) -> None:
        if self.primary:
            self.kernel.ReleaseMutex(self.handle)
        self.kernel.CloseHandle(self.handle)


def bind_local_socket(preferred_port: int = 8000) -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if os.name == "nt":
        listener.setsockopt(socket.SOL_SOCKET, cast(Any, socket).SO_EXCLUSIVEADDRUSE, 1)
    try:
        listener.bind(("127.0.0.1", preferred_port))
    except OSError:
        listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    return listener


def instance_is_ready(port: int, instance_id: str) -> bool:
    try:
        with httpx.Client(timeout=1, trust_env=False) as client:
            response = client.get(f"http://127.0.0.1:{port}/health")
        payload = response.json()
        return response.status_code == 200 and payload.get("instance_id") == instance_id
    except (httpx.HTTPError, ValueError):
        return False


def reopen_existing(root: Path, *, open_browser: bool = True) -> None:
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            record = json.loads((root / "instance.json").read_text(encoding="utf-8"))
            port, identifier = int(record["port"]), str(record["instance_id"])
            if instance_is_ready(port, identifier):
                if open_browser:
                    webbrowser.open(f"http://127.0.0.1:{port}/")
                return
        except (OSError, ValueError, KeyError, TypeError):
            pass
        time.sleep(0.2)
    raise OSError("ResearchPilot 正在启动或暂时没有响应，请稍后重试。")


def configure_logging(store: DesktopStore) -> None:
    class PrivateFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            return safe_diagnostic(super().format(record), secrets=(store.api_key,))

    handler = RotatingFileHandler(
        store.root / "logs" / "researchpilot.log",
        maxBytes=2_000_000,
        backupCount=2,
        encoding="utf-8",
    )
    handler.setFormatter(PrivateFormatter("%(asctime)s %(levelname)s %(name)s :: %(message)s"))
    logger = logging.getLogger()
    for old_handler in logger.handlers[:]:
        logger.removeHandler(old_handler)
        old_handler.close()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def make_icon() -> Any:
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((2, 2, 62, 62), radius=16, fill="#21685c")
    draw.ellipse((13, 13, 51, 51), outline="white", width=3)
    draw.polygon(((41, 21), (36, 36), (21, 41), (27, 27)), fill="white")
    return image


def startup_error(root: Path, message: str) -> bool:
    """An error window is the only native dialog; normal use stays in the browser."""
    import tkinter as tk
    from tkinter import ttk

    retry = False
    window = tk.Tk()
    window.title("ResearchPilot 启动未完成")
    window.resizable(False, False)
    frame = ttk.Frame(window, padding=24)
    frame.pack()
    ttk.Label(frame, text=message, wraplength=420, justify="left").pack(pady=(0, 20))

    def again() -> None:
        nonlocal retry
        retry = True
        window.destroy()

    controls = ttk.Frame(frame)
    controls.pack()
    ttk.Button(controls, text="重试", command=again).pack(side="left", padx=6)
    ttk.Button(controls, text="查看诊断文件夹", command=lambda: cast(Any, os).startfile(root / "logs")).pack(
        side="left",
        padx=6,
    )
    ttk.Button(controls, text="退出", command=window.destroy).pack(side="left", padx=6)
    window.mainloop()
    return retry


def launch(root: Path, *, open_browser: bool = True, tray_enabled: bool = True) -> int:
    guard = SingleInstance(root)
    if not guard.primary:
        try:
            reopen_existing(root, open_browser=open_browser)
            return 0
        finally:
            guard.close()
    store = DesktopStore(root)
    server: uvicorn.Server | None = None
    tray: Any = None
    worker: threading.Thread | None = None
    listener: socket.socket | None = None
    app: Any = None
    instance_path = root / "instance.json"
    identifier = uuid.uuid4().hex
    stopped = threading.Event()
    try:
        store.prepare()
        configure_logging(store)
        identifier = uuid.uuid4().hex

        def shutdown() -> None:
            stopped.set()
            if server:
                app.state.container.pipeline.request_shutdown()
                server.should_exit = True

        controller = DesktopController(store, instance_id=identifier, on_exit=shutdown)
        app = create_app(store.runtime_settings(api_key=store.api_key), desktop=controller)
        listener = bind_local_socket()
        port = listener.getsockname()[1]
        url = f"http://127.0.0.1:{port}/"
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=port,
                log_config=None,
                access_log=False,
                timeout_graceful_shutdown=10,
            )
        )

        if tray_enabled:
            import pystray

            def exit_from_tray(icon: Any, item: Any) -> None:
                if app.state.container.pipeline.background_task_count:
                    answer = cast(Any, ctypes).windll.user32.MessageBoxW(
                        None,
                        "有研究正在进行。退出将取消这些任务，确定退出吗？",
                        "ResearchPilot",
                        0x24,
                    )
                    if answer != 6:
                        return
                shutdown()

            tray = pystray.Icon(
                "ResearchPilot",
                make_icon(),
                "ResearchPilot",
                pystray.Menu(
                    pystray.MenuItem(
                        "打开 ResearchPilot", lambda icon, item: webbrowser.open(url), default=True
                    ),
                    pystray.MenuItem("退出", exit_from_tray),
                ),
            )

        def serve() -> None:
            try:
                assert server is not None
                server.run(sockets=[listener])
            except Exception:
                logging.getLogger(__name__).exception("Local service failed")
            finally:
                stopped.set()
                if tray:
                    tray.stop()

        worker = threading.Thread(target=serve, name="researchpilot-local-server", daemon=True)
        worker.start()
        atomic_write_text(
            instance_path, json.dumps({"port": port, "instance_id": identifier, "pid": os.getpid()})
        )
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline and worker.is_alive():
            if server.started and instance_is_ready(port, identifier):
                break
            stopped.wait(0.2)
        else:
            raise OSError("本机服务未能启动。请重试或查看诊断日志。")
        logging.getLogger(__name__).info("Desktop ready on port %s", port)
        if open_browser and not webbrowser.open(url):
            raise OSError("默认浏览器未能打开，请设置默认浏览器后重试。")
        if tray:

            def tray_ready(icon: Any) -> None:
                # pystray ignores stop() before its message loop is ready.
                if stopped.is_set():
                    icon.stop()
                else:
                    icon.visible = True

            tray.run(setup=tray_ready)
        else:
            while worker.is_alive():
                worker.join(timeout=0.5)
        return 0
    finally:
        if server:
            server.should_exit = True
        if worker:
            worker.join(timeout=15)
        if app:
            app.state.container.close()
        if tray:
            tray.stop()
        if listener:
            listener.close()
        if instance_path.exists():
            try:
                record = json.loads(instance_path.read_text(encoding="utf-8"))
                if record.get("instance_id") == identifier:
                    instance_path.unlink()
            except (OSError, ValueError):
                pass
        guard.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ResearchPilot desktop")
    parser.add_argument("--data-dir", type=Path, help="developer smoke-test data directory")
    parser.add_argument("--no-browser", action="store_true", help="developer smoke test")
    parser.add_argument("--no-tray", action="store_true", help="developer smoke test")
    args = parser.parse_args(argv)
    root = (args.data_dir or default_data_root()).resolve()
    while True:
        try:
            return launch(root, open_browser=not args.no_browser, tray_enabled=not args.no_tray)
        except Exception as exc:
            logging.getLogger(__name__).exception("Desktop startup failed")
            if args.no_browser and args.no_tray:
                return 1
            message = "启动未完成。请点击重试，或打开诊断文件夹查看日志。\n" + safe_diagnostic(str(exc))
            if not startup_error(root, message):
                return 1


if __name__ == "__main__":
    raise SystemExit(main())
