#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视频转文字 - 原生窗口版

用 WebView2(pywebview) 创建独立程序窗口: 自己的图标、自己的任务栏条目,
不再是 Edge 浏览器窗口。功能与网页版完全一致。
"""

import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
import v2t  # noqa: E402,F401
import webapp  # noqa: E402
import webview  # noqa: E402


class Bridge(object):
    """页面JS可调用的原生能力: 系统保存对话框(自选路径)。"""

    def save_transcript(self, arg):
        fmt, _, sid = str(arg).partition("|")
        snap = webapp.snapshot(sid or None) or {}
        segs = snap.get("segs") or []
        if not segs:
            return "EMPTY"
        body = webapp.build_export(snap, fmt)
        ext = {"srt": "srt", "md": "md", "txt-ts": "txt"}.get(fmt, "txt")

        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        name = v2t.sanitize_filename(snap.get("title") or "transcript")
        path = filedialog.asksaveasfilename(
            defaultextension="." + ext,
            initialfile=name + "." + ext,
            filetypes=[(ext.upper() + " 文件", "*." + ext), ("所有文件", "*.*")],
            parent=root)
        root.destroy()
        if not path:
            return "CANCEL"
        Path(path).write_text(body, encoding="utf-8")
        return "OK"


def main():
    sys.stdout = webapp.TeeOut()
    if sys.stderr is None:
        sys.stderr = webapp._Null()
    webapp.v2t.log = lambda msg="": webapp.emit("*", "log", str(msg))

    def _open_play_window(url):
        """边播边转: 在独立的浏览器窗口(WebView2)中打开网页播放。"""
        try:
            webview.create_window("Btchar · 播放", url,
                                  width=1150, height=800)
        except Exception:
            import webbrowser
            webbrowser.open(url)

    webapp.browse_hook = _open_play_window

    port = webapp.PORT
    httpd = None
    for p in range(webapp.PORT, webapp.PORT + 20):
        try:
            httpd = ThreadingHTTPServer((webapp.HOST, p), webapp.Handler)
            port = p
            break
        except OSError:
            continue
    if httpd is None:
        raise SystemExit("端口 %d-%d 都被占用" % (webapp.PORT, webapp.PORT + 19))
    webapp.ACTUAL_PORT = port
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    # 生命周期日志(排查窗口被谁关闭)
    import time as _t
    life_log = BASE_DIR / "Btchar_lifecycle.log"

    def _log(ev):
        try:
            with open(life_log, "a", encoding="utf-8") as f:
                f.write("%s %s\n" % (_t.strftime("%H:%M:%S"), ev))
        except OSError:
            pass

    window = webview.create_window(
        "Btchar", "http://%s:%d/" % (webapp.HOST, port),
        js_api=Bridge(), width=1360, height=920, min_size=(900, 640),
        text_select=True)
    window.events.loaded += lambda: _log("loaded")
    window.events.closing += lambda: _log("closing(用户或系统请求关闭)")
    window.events.closed += lambda: _log("closed(窗口已关闭)")
    window.events.shown += lambda: _log("shown")
    _log("start icon=" + str(BASE_DIR / "Btchar.ico"))
    webview.start(icon=str(BASE_DIR / "Btchar.ico"))
    _log("webview.start返回, 进程即将退出")


if __name__ == "__main__":
    main()
