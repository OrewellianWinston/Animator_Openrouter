"""Run the desktop GUI: python -m animator.gui [--no-window]"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import webbrowser

import uvicorn

from .server import create_app


def quiet_streams() -> None:
    """pythonw has no stdout/stderr; give it something harmless to write to."""
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w"))


def serve_in_background() -> tuple:
    """Start uvicorn on a free 127.0.0.1 port. Returns (server, app, url)."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    app = create_app(port)
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False))
    threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    return server, app, f"http://127.0.0.1:{port}/"


def main() -> None:
    quiet_streams()
    parser = argparse.ArgumentParser(prog="python -m animator.gui")
    parser.add_argument("--no-window", action="store_true", help="only serve; print the URL")
    args = parser.parse_args()

    server, app, url = serve_in_background()
    print(f"Animator GUI at {url}", flush=True)
    try:
        if args.no_window:
            wait_forever()
            return
        try:
            import webview
        except ImportError:
            webbrowser.open(url)
            wait_forever()
            return
        webview.create_window("Animator", url, width=1280, height=820, min_size=(900, 600))
        webview.start()
    finally:
        app.state.manager.cancel()
        server.should_exit = True


def wait_forever() -> None:
    """Keep serving until Ctrl+C."""
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


main()
