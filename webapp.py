#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视频转文字 - 本地网页版App

左边播放视频, 右边实时显示转录文字。
启动后自动用 Edge 应用窗口打开 http://127.0.0.1:8765

流程: 输入链接 -> 下载视频+音频并合成MP4 -> (B站优先AI字幕 / 否则本地Whisper流式识别)
事件通过 SSE 推到页面, 转录逐段实时显示, 点字幕跳转视频进度。
"""

import argparse
import json
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

if getattr(sys, "frozen", False):
    # PyInstaller 打包: 静态资源在 _MEIPASS(_internal), 用户数据写到 exe 旁边
    BASE_DIR = Path(sys.executable).resolve().parent
    STATIC = Path(getattr(sys, "_MEIPASS", BASE_DIR)) / "static"
else:
    BASE_DIR = Path(__file__).resolve().parent
    STATIC = BASE_DIR / "static"
sys.path.insert(0, str(BASE_DIR))
import v2t  # noqa: E402

HOST = "127.0.0.1"
PORT = 8917
ACTUAL_PORT = PORT  # main() 启动后更新为真实端口
CACHE = BASE_DIR / "cache"
CACHE.mkdir(parents=True, exist_ok=True)

# 允许调用的外部程序白名单(安装在本venv内的固定二进制)
EDGE_PATHS = (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")


# ---------------------------------------------------------------- 安全执行

def _checked_run(argv, timeout=300):
    """仅执行白名单内的参数列表命令, 禁止shell。"""
    if not argv or not all(isinstance(a, str) and a for a in argv):
        raise RuntimeError("非法命令参数")
    exe = Path(argv[0]).resolve()
    if not exe.is_file():
        raise RuntimeError("可执行文件不存在: %s" % exe)
    if getattr(sys, "frozen", False) or str(exe).startswith(
            str(BASE_DIR / ".venv")):
        return subprocess.run([str(exe)] + argv[1:], shell=False,
                              capture_output=True, text=True, timeout=timeout)
    raise RuntimeError("程序不在白名单内: %s" % exe)


def get_ffmpeg_exe_safe():
    from imageio_ffmpeg import get_ffmpeg_exe
    if getattr(sys, "frozen", False):
        # 打包环境: ffmpeg 随exe分发, 整体受信
        return str(Path(get_ffmpeg_exe()).resolve())
    exe = str(Path(get_ffmpeg_exe()).resolve())
    if not exe.startswith(str(BASE_DIR / ".venv")):
        raise RuntimeError("ffmpeg 不在venv白名单内")
    return exe


def _cache_path(name):
    """生成锁死在缓存目录内的路径(防路径穿越)。"""
    return v2t.safe_output_path(CACHE, name)


# ---------------------------------------------------------------- stdout 桥接

class _Null(object):
    def write(self, *a):
        return 0

    def flush(self):
        pass

    def reconfigure(self, **kw):
        pass


class TeeOut(object):
    """把 v2t 的 stdout 输出转成页面事件: \\r进度行->status, 普通行->log。"""

    def __init__(self):
        self._buf = ""

    def write(self, s):
        try:
            if "\r" in s:
                tail = s.rsplit("\r", 1)[-1].strip()
                if tail:
                    emit(sid, "status", tail)
                return len(s)
            self._buf += s
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                line = line.strip()
                if line:
                    emit(sid, "log", line)
            return len(s)
        except Exception:
            return len(s)

    def flush(self):
        pass

    def reconfigure(self, **kw):
        pass


# ---------------------------------------------------------------- 全局状态

def _blank_session(sid):
    return {
        "sid": sid,
        "phase": "idle",   # idle|working|paused|done|error
        "step": "就绪",
        "title": "",
        "cover": "",
        "segs": [],        # [{start,end,text}]
        "video": None,     # 服务端内部路径, 不暴露给客户端
        "lang": "",
        "error": "",
        "src": "",
        "model": "small",
        "resume_at": 0.0,
        "live": False,
        "cancel": threading.Event(),
        "busy": False,
        "stream": False,     # 链接版边播边转(视频代理不落盘)
        "stream_url": None,
        "stream_referer": "",
    }


_sessions = {}            # sid -> session dict
MODELS = ("tiny", "base", "small", "medium", "large-v3")
_live = None               # LiveTranscriber 实例(全局单例, 含 sid)


def new_session():
    sid = secrets.token_hex(4)
    with _lock:
        _sessions[sid] = _blank_session(sid)
    return sid


def st(sid):
    return _sessions.get(sid)


def snapshot(sid=None):
    with _lock:
        if sid is None:
            return [dict(s, cancel=None) for s in _sessions.values()]
        s = _sessions.get(sid)
        if s is None:
            return None
        out = dict(s)
        out.pop("cancel", None)
        return out


def set_phase(sid, step):
    s = st(sid)
    if s is None:
        return
    with _lock:
        s["step"] = step
    emit(sid, "step", step)


def push_seg(sid, start, end, text):
    seg = {"start": round(float(start), 2), "end": round(float(end), 2),
           "text": str(text).strip()}
    s = st(sid)
    if s is None:
        return
    with _lock:
        s["segs"].append(seg)
    emit(sid, "seg", seg)
_lock = threading.Lock()
_subs = []             # SSE 订阅者队列


def emit(sid, kind, payload):
    """向所有SSE订阅者广播带会话ID的事件。"""
    data = json.dumps({"sid": sid, "type": kind, "data": payload},
                      ensure_ascii=False)
    with _lock:
        dead = []
        for q in _subs:
            try:
                q.put_nowait(data)
            except queue.Full:
                dead.append(q)
        for q in dead:
            try:
                _subs.remove(q)
            except ValueError:
                pass


def _live_on_segment(s, e, text):
    sid = _live.sid if _live else None
    if sid is None:
        return
    push_seg(sid, s, e, text)
    el = _live.elapsed()
    emit(sid, "status", "已录%d分%02d秒 · 已出%d段"
         % (el // 60, el % 60, len((snapshot(sid) or {}).get("segs", []))))


def _live_on_status(text):
    sid = _live.sid if _live else None
    if sid is None:
        return
    if text.startswith("live_device:"):
        emit(sid, "log", "收音设备: " + text[len("live_device:"):])
    else:
        emit(sid, "status", text)


# ---------------------------------------------------------------- 转换任务

def _page_from_url(url):
    """从B站链接解析分P号(缺省1): ...video/BVxxx?p=3。"""
    m = re.search(r"[?&]p=(\d+)", url or "")
    return int(m.group(1)) if m else 1


def _pick_page(url, view):
    """按链接里的p参数选定分P(无参数或越界则P1)。"""
    pages = (view.get("pages")
             or [{"page": 1, "cid": view.get("cid"),
                  "part": view.get("title"),
                  "duration": view.get("duration")}])
    pn = _page_from_url(url)
    page = next((x for x in pages if x.get("page") == pn), pages[0])
    title = view.get("title", view.get("bvid") or "")
    if len(pages) > 1:
        title = "%s P%d %s" % (title, page.get("page", 1),
                               page.get("part") or "")
    return page, title


def _pick_bili_video(dash):
    """选浏览器能直接播放的H.264流(1080p以内最高清)。"""
    vids = dash.get("video") or []
    ok = [v for v in vids if "avc1" in (v.get("codecs") or "")
          and int(v.get("height") or 0) <= 1080]
    if not ok:
        ok = [v for v in vids if int(v.get("height") or 9999) <= 1080] or vids
    return max(ok, key=lambda v: int(v.get("height") or 0))


def _stream_candidates(stream):
    """流的全部URL(主+备), 优先普通CDN(upos镜像)。

    mcdn.bilivideo.cn:8082 和第三方 edge 节点是PCDN, 对普通HTTP客户端
    会返回错误的缓存内容, 只作最后手段。
    """
    urls = []
    for label in ("baseUrl", "base_url"):
        u = stream.get(label)
        if u and u not in urls:
            urls.append(u)
    for label in ("backupUrl", "backup_url"):
        for u in (stream.get(label) or []):
            if u and u not in urls:
                urls.append(u)

    def reliable(u):
        return ("upos-sz-mirror" in u) or (".bilivideo.com/" in u)

    return (sorted([u for u in urls if reliable(u)])
            + sorted([u for u in urls if not reliable(u)]))


def _media_duration_sec(path):
    """ffprobe 读取媒体时长(秒), 失败返回None。"""
    p = _checked_run([get_ffmpeg_exe_safe(), "-hide_banner", "-i", str(path)],
                     timeout=60)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+)", p.stderr or "")
    if not m:
        return None
    h, mi, s = map(int, m.groups())
    return h * 3600 + mi * 60 + s


def _download_stream_validated(stream, dest, referer, expect_sec):
    """按候选顺序下载, 校验时长防PCDN错误内容; 全部失败抛最后错误。"""
    last_err = RuntimeError("无可用下载地址")
    for url in _stream_candidates(stream):
        try:
            v2t.download_stream(url, dest, referer=referer)
            if expect_sec:
                got = _media_duration_sec(dest)
                if got and abs(got - expect_sec) > 3:
                    last_err = RuntimeError(
                        "下载内容时长%.0fs与预期%.0fs不符(疑似PCDN脏数据), 换源重试"
                        % (got, expect_sec))
                    continue
            return dest
        except Exception as e:
            last_err = e
    raise last_err


def _ffmpeg_mux(video_name, audio_name, out_name):
    """把缓存目录里的视频/音频流无损封装成MP4(参数均为缓存目录内文件名)。"""
    from imageio_ffmpeg import get_ffmpeg_exe
    _checked_run([get_ffmpeg_exe(), "-y", "-loglevel", "error",
                  "-i", str(_cache_path(video_name)),
                  "-i", str(_cache_path(audio_name)),
                  "-c", "copy", "-movflags", "+faststart",
                  str(_cache_path(out_name))])


def _stream_whisper(sid, audio_path, lang, offset=0.0, model_size="small",
                    cancel=None):
    """faster-whisper 流式识别: 每识别出一段立刻推送(支持续传偏移)。"""
    _never = threading.Event()
    cancel = cancel if cancel is not None else _never
    set_phase(sid, "本地语音识别中…" if not offset
              else ("从 %s 继续识别…" % v2t.fmt_clock(offset)))
    model, device, compute = v2t.get_whisper_model(model_size, "auto", True)
    emit(sid, "log", "识别引擎: faster-whisper(%s, %s/%s)" % (model_size, device,
                                                         compute))
    kw = dict(beam_size=5, vad_filter=True,
              vad_parameters={"min_silence_duration_ms": 500},
              condition_on_previous_text=False,
              language=None if lang == "auto" else lang)
    iterator, info = model.transcribe(str(audio_path), **kw)
    s0 = st(sid)
    if s0 is not None:
        with _lock:
            s0["lang"] = info.language or ""
    duration = (info.duration or 0) + offset
    for seg in iterator:
        if cancel.is_set():
            return
        text = seg.text.strip()
        if text:
            push_seg(sid, seg.start + offset, seg.end + offset, text)
            if duration:
                emit(sid, "status", "识别中 %d%% (已出%d段)"
                     % (min(99, int((seg.end + offset) / duration * 100)),
                        len((snapshot(sid) or {}).get("segs", []))))
    if not (snapshot(sid) or {}).get("segs") and duration > 1 and not offset:
        emit(sid, "log", "VAD未检出人声，去掉VAD重新识别…")
        kw["vad_filter"] = False
        iterator, info = model.transcribe(str(audio_path), **kw)
        for seg in iterator:
            if cancel.is_set():
                return
            text = seg.text.strip()
            if text:
                push_seg(sid, seg.start + offset, seg.end + offset, text)


def _extract_audio(video_path, start=0.0, name="audio.m4a"):
    """从视频提取音轨到缓存目录; start>0时从该位置截取(用于续转)。"""
    out = _cache_path(name)
    ss = ["-ss", "%.2f" % start] if start > 0 else []
    try:
        _checked_run([get_ffmpeg_exe_safe(), "-y", "-loglevel", "error"]
                     + ss + ["-i", str(video_path), "-vn", "-c", "copy",
                             str(out)], timeout=120)
        if out.exists() and out.stat().st_size > 1024:
            return out
    except Exception:
        pass
    _checked_run([get_ffmpeg_exe_safe(), "-y", "-loglevel", "error"]
                 + ss + ["-i", str(video_path), "-vn",
                         "-c:a", "aac", "-b:a", "128k", str(out)], timeout=600)
    if not out.exists() or out.stat().st_size <= 1024:
        raise RuntimeError("音轨提取失败(视频可能无音轨)")
    return out


def _ytdlp_video(url, sid="x"):
    """非B站链接: yt-dlp 直接下成品视频(720p内, 自动合并音视频)。"""
    set_phase(sid, "下载视频 (yt-dlp)…")
    import yt_dlp
    from imageio_ffmpeg import get_ffmpeg_exe

    def hook(d):
        if d.get("status") == "downloading":
            pct = (d.get("_percent_str") or "").strip()
            if pct:
                emit(sid, "status", "下载视频 %s" % pct)

    opts = {
        "format": "bv*[height<=720]+ba/b[height<=720]/b",
        "merge_output_format": "mp4",
        "ffmpeg_location": get_ffmpeg_exe(),
        "outtmpl": str(CACHE / ("yt_%s.%(ext)s" % sid)),
        "quiet": True, "no_warnings": True, "socket_timeout": 30, "retries": 3,
        "progress_hooks": [hook],
        "http_headers": {"User-Agent": v2t.UA},
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    dl = info.get("requested_downloads") or []
    path = (dl[0].get("filepath") if dl else None) or ydl.prepare_filename(info)
    p = Path(path).resolve()
    if CACHE.resolve() not in p.parents:
        raise RuntimeError("下载文件落在缓存目录外, 已拒绝")
    if not p.exists():
        raise RuntimeError("yt-dlp 未产出视频文件")
    return p, info.get("title") or p.stem, ""


def run_job(sid, src, resume_at=0.0, model="small"):
    """完整转换流水线, 在后台线程运行。resume_at>0 时从暂停点续转。"""
    sess = st(sid)
    if sess is None:
        return
    cancel = sess["cancel"]
    with _lock:
        sess["busy"] = True
    t0 = time.time()
    if model not in MODELS:
        model = "small"
    try:
        if resume_at > 0:
            # 续转: 不清空已转录内容, 直接用缓存视频
            snap = snapshot(sid)
            cached = snap.get("video")
            if snap.get("src") != src or not cached or not os.path.exists(cached):
                resume_at = 0.0  # 缓存失效则从头来
            else:
                with _lock:
                    sess.update(phase="working", step="",
                                error="", resume_at=resume_at)
        if resume_at <= 0:
            with _lock:
                sess.update(phase="working", step="", title="", cover="",
                            segs=[], video=None, error="", src=src, lang="",
                            model=model, resume_at=0.0, live=False)
                sess["cancel"] = threading.Event()
                cancel = sess["cancel"]
            emit(sid, "reset", True)
        emit(sid, "phase", "working")

        segs = None
        title, cover = Path(src).stem, ""
        video_path = None
        audio_path = None

        if resume_at > 0:
            snap = snapshot(sid)
            title, cover = snap.get("title") or title, snap.get("cover") or ""
            video_path = Path(snap["video"])
            with _lock:
                sess["video"] = str(video_path)
            segs = []  # 续转不走字幕, 从音轨续识别
        # ---------- 本地文件 ----------
        elif os.path.exists(src):
            set_phase(sid, "处理本地文件…")
            video_path = Path(src).resolve()

        # ---------- 在线链接 ----------
        else:
            url = src
            if "b23.tv" in url:
                set_phase(sid, "解析短链接…")
                url = v2t.resolve_short_link(url)
            bili_id = v2t.detect_bili_id(url)
            if bili_id:
                kind, value = bili_id
                set_phase(sid, "获取视频信息…")
                view = v2t.fetch_bili_view(bvid=value if kind == "bvid" else None,
                                           aid=value if kind == "aid" else None)
                bvid = view.get("bvid") or value
                page, title = _pick_page(url, view)
                title = title or bvid
                cover = view.get("pic") or ""
                emit(sid, "meta", {"title": title, "cover": cover,
                                   "duration": page.get("duration")
                                   or view.get("duration") or 0})
                cookie_header = v2t.build_bili_cookie_header(None)
                # 字幕优先(登录后AI字幕秒出)
                segs = v2t.fetch_bili_cc_subtitles(bvid, page["cid"], cookie_header)
                if segs:
                    set_phase(sid, "获取到B站字幕…")
                else:
                    emit(sid, "log", "无可用字幕，稍后本地语音识别")

                set_phase(sid, "下载视频流…")
                play = v2t.bili_fetch_playurl(bvid, page["cid"], cookie_header)
                dash = play.get("dash") or {}
                v = _pick_bili_video(dash)
                a = max(dash.get("audio") or [],
                        key=lambda x: int(x.get("bandwidth", 0)), default=None)
                if not v or not a:
                    raise RuntimeError("拿不到可播放的视频流(可能需要大会员)")
                expect_sec = (play.get("timelength") or 0) / 1000.0
                referer = v2t.bili_audio_url(bvid, page.get("page", 1))
                v_file = _download_stream_validated(
                    v, _cache_path("v_%s.m4s" % sid), referer, expect_sec)
                a_file = _download_stream_validated(
                    a, _cache_path("a_%s.m4s" % sid), referer, expect_sec)
                set_phase(sid, "合成MP4…")
                _ffmpeg_mux("v_%s.m4s" % sid, "a_%s.m4s" % sid,
                            "video_%s.mp4" % sid)
                video_path = _cache_path("video_%s.mp4" % sid)
                audio_path = a_file  # 识别必须用纯音轨(合成视频会出乱码)
                try:
                    os.remove(_cache_path("v_%s.m4s" % sid))
                except OSError:
                    pass

                # 字幕可信度校验: B站字幕CDN会随机返回其他视频的字幕
                if segs and not v2t.subtitle_span_ok(segs, view.get("duration")):
                    emit(sid, "log", "字幕时长与视频不符(疑似CDN脏数据)，改用本地识别")
                    segs = None
                if segs and not v2t.subtitle_matches_audio(segs, audio_path):
                    emit(sid, "log", "字幕内容与音频开头不符(疑似CDN脏数据)，改用本地识别")
                    segs = None
            else:
                # ---------- 其他平台 (先做SSRF校验再交给yt-dlp) ----------
                v2t.validate_public_http_url(url)
                video_path, title, cover = _ytdlp_video(url, sid)
                emit(sid, "meta", {"title": title, "cover": cover, "duration": 0})

        # ---------- 发布视频 ----------
        with _lock:
            sess["title"] = title
            sess["cover"] = cover
            sess["video"] = str(video_path) if video_path else None
        if video_path and video_path.exists():
            emit(sid, "video_ready", {"title": title})

        # ---------- 转写 ----------
        if resume_at > 0:
            set_phase(sid, "提取剩余音轨…")
            audio_path = _extract_audio(video_path, start=resume_at,
                                        name="audio_r_%s.m4a" % sid)
            _stream_whisper(sid, audio_path, "auto", offset=resume_at,
                            model_size=sess.get("model") or "small",
                            cancel=cancel)
        elif segs:
            for s in segs:
                if cancel.is_set():
                    break
                push_seg(sid, s["start"], s["end"], s["text"])
            with _lock:
                sess["lang"] = "zh"
        elif video_path:
            # 识别用纯音轨: 合成视频/本地视频先提取音轨
            if audio_path is None:
                set_phase(sid, "提取音轨…")
                audio_path = _extract_audio(video_path,
                                            name="audio_%s.m4a" % sid)
            _stream_whisper(sid, audio_path, "auto",
                            model_size=sess.get("model") or "small",
                            cancel=cancel)

        if cancel.is_set():
            # 暂停: 保留已转录内容, 记录续转起点
            last_end = max((s["end"] for s in snapshot(sid)["segs"]),
                           default=0.0)
            with _lock:
                sess["phase"] = "paused"
                sess["resume_at"] = last_end
            set_phase(sid, "已暂停 — 已转录%d段, 可继续或直接导出"
                      % len(snapshot(sid)["segs"]))
            emit(sid, "phase", "paused")
        else:
            try:
                out_dir = BASE_DIR / "transcripts"
                out_dir.mkdir(exist_ok=True)
                snap = snapshot(sid)
                args = argparse.Namespace(srt=True, md=True, timestamps=False)
                base = out_dir / v2t.sanitize_filename(title or "transcript")
                files = v2t.write_outputs(base, snap["segs"],
                                          snap.get("lang") or "zh", args,
                                          {"title": title, "url": src,
                                           "engine": "v2t-webapp"})
                emit(sid, "log", "已保存: " + ", ".join(f.name for f in files))
            except Exception as e:
                emit(sid, "log", "保存文件失败: %s" % e)
            with _lock:
                sess["phase"] = "done"
                sess["resume_at"] = 0.0
            set_phase(sid, "完成! 用时%.0f秒" % (time.time() - t0))
    except Exception as e:
        with _lock:
            sess["phase"] = "error"
            sess["error"] = str(e)
        emit(sid, "error", str(e))
    finally:
        with _lock:
            sess["busy"] = False
        for name in ("v_%s.m4s" % sid, "a_%s.m4s" % sid,
                     "audio_%s.m4a" % sid, "audio_r_%s.m4a" % sid):
            try:
                os.remove(_cache_path(name))
            except OSError:
                pass
        emit(sid, "phase", snapshot(sid)["phase"] if snapshot(sid) else "done")


def build_export(snap, fmt):
    """按格式生成导出文本(供 /api/export 与原生保存对话框共用)。"""
    segs = snap.get("segs") or []
    lang = snap.get("lang") or "zh"
    if fmt == "srt":
        lines = []
        for i, s in enumerate(segs, 1):
            lines += [str(i), "%s --> %s" % (v2t.fmt_srt_time(s["start"]),
                                             v2t.fmt_srt_time(s["end"])),
                      s["text"], ""]
        return "\n".join(lines)
    if fmt == "md":
        body = "# %s\n\n- 来源: %s\n- 段数: %d\n\n## 正文\n\n" % (
            snap.get("title") or "转录", snap.get("src") or "", len(segs))
        for para in v2t.group_paragraphs(segs):
            body += "[%s] %s\n\n" % (v2t.fmt_clock(para[0]["start"]),
                                     v2t.join_text([s["text"] for s in para], lang))
        return body
    if fmt == "txt-ts":
        return "\n".join("[%s] %s" % (v2t.fmt_clock(s["start"]), s["text"])
                         for s in segs)
    return v2t.join_text([s["text"] for s in segs], lang)


# ---------------------------------------------------------------- HTTP 服务

INDEX = STATIC / "index.html"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # ---------- 响应工具 ----------
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _origin_ok(self):
        """本机防CSRF: Origin/Host 必须指向本服务真实端口。"""
        origin = self.headers.get("Origin")
        if origin and origin not in ("http://%s:%d" % (HOST, ACTUAL_PORT),
                                     "null"):
            return False
        host = self.headers.get("Host", "")
        return host.startswith("%s:" % HOST) or host == HOST

    # ---------- GET ----------
    def _qsid(self):
        """从查询串取会话id。"""
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        for kv in query.split("&"):
            if kv.startswith("sid="):
                return kv[4:]
        return None

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            body = INDEX.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/state":
            self._json({"sessions": snapshot()})
        elif path == "/api/summary":
            import summarizer
            sid = self._qsid()
            snap = snapshot(sid)
            segs = (snap or {}).get("segs") or []
            if not segs:
                self._json({"error": "还没有可摘要的转录内容"}, 404)
                return
            try:
                result = summarizer.summarize(segs)
            except Exception as e:
                self._json({"error": "摘要生成失败: %s" % e}, 500)
                return
            if not result:
                self._json({"error": "有效内容不足, 无法生成摘要"}, 404)
                return
            self._json(result)
        elif path == "/api/events":
            self._sse()
        elif path == "/video":
            self._video()
        elif path == "/api/export":
            self._export()
        elif path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path == "/Btchar.ico":
            ico = BASE_DIR / "Btchar.ico"
            body = ico.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/x-icon")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(body)
        else:
            self._json({"error": "not found"}, 404)

    def _sse(self):
        q = queue.Queue(maxsize=500)
        with _lock:
            _subs.append(q)
        try:
            self.send_response(200)
            self.send_header("Content-Type",
                             "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            hello = {"type": "snapshot", "data": snapshot(),
                     "sid": "*", "broadcast": True}
            self.wfile.write(("data: %s\n\n" % json.dumps(
                hello, ensure_ascii=False)).encode("utf-8"))
            self.wfile.flush()
            while True:
                try:
                    data = q.get(timeout=15)
                    self.wfile.write(("data: %s\n\n" % data).encode("utf-8"))
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except (ConnectionAbortedError, ConnectionResetError, OSError):
            pass
        finally:
            with _lock:
                try:
                    _subs.remove(q)
                except ValueError:
                    pass

    def _video(self):
        """会话视频: stream模式代理B站直链(不落盘), 否则服务本地文件。"""
        snap = snapshot(self._qsid()) or {}
        if snap.get("stream") and snap.get("stream_url"):
            import btstream
            sess = st(self._qsid())
            if sess is not None:
                btstream.proxy_stream_response(self, sess)
                return
        video = snap.get("video")
        if not video or not os.path.exists(video):
            self._json({"error": "no video"}, 404)
            return
        f = Path(video)
        size = f.stat().st_size
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        if rng and rng.startswith("bytes="):
            m = re.match(r"bytes=(\d*)-(\d*)$", rng)
            if m:
                start = int(m.group(1) or 0)
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
        length = max(0, end - start + 1)
        self.send_response(206 if (rng or start > 0) else 200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with open(f, "rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(1024 * 512, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except OSError:
                    return
                remaining -= len(chunk)

    def _export(self):
        snap = snapshot(self._qsid()) or {}
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        fmt = "txt"
        for kv in query.split("&"):
            if kv.startswith("fmt="):
                fmt = kv[4:]
        body = build_export(snap, fmt)
        data = body.encode("utf-8")
        name = v2t.sanitize_filename(snap.get("title") or "transcript")
        from urllib.parse import quote
        ext = {"srt": "srt", "md": "md", "txt-ts": "txt"}.get(fmt, "txt")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Disposition",
                         "attachment; filename*=UTF-8''%s.%s"
                         % (quote(name), ext))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---------- POST ----------
    def do_POST(self):
        global _live
        if not self._origin_ok():
            self._json({"error": "forbidden"}, 403)
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._json({"error": "bad json"}, 400)
            return
        path = self.path.split("?")[0]
        sid = str(body.get("sid") or "")
        sess = st(sid) if sid else None
        if path == "/api/start":
            src = str(body.get("src") or "").strip().strip('"')
            if not src:
                self._json({"error": "empty"}, 400)
                return
            model = str(body.get("model") or "small")
            if model not in MODELS:
                self._json({"error": "bad model"}, 400)
                return
            new_sid = new_session()
            if str(body.get("mode") or "") == "stream":
                import btstream
                threading.Thread(
                    target=btstream.run_stream_job,
                    args=(new_sid, src, model), daemon=True).start()
            else:
                threading.Thread(target=run_job,
                                 args=(new_sid, src, 0.0, model),
                                 daemon=True).start()
            self._json({"ok": True, "sid": new_sid})
        elif path == "/api/resume":
            if sess is None:
                self._json({"error": "no session"}, 404)
                return
            if sess.get("busy"):
                self._json({"error": "busy"}, 409)
                return
            if sess.get("phase") != "paused" or not sess.get("src"):
                self._json({"error": "not paused"}, 409)
                return
            resume_at = float(sess.get("resume_at") or 0)
            if resume_at <= 0 and not sess.get("stream"):
                self._json({"error": "nothing to resume"}, 409)
                return
            if sess.get("stream"):
                import btstream
                with _lock:
                    sess.update(phase="working", busy=True, error="")
                threading.Thread(
                    target=btstream.run_stream_job,
                    args=(sid, sess["src"], sess.get("model") or "small"),
                    daemon=True).start()
                self._json({"ok": True})
                return
            threading.Thread(target=run_job,
                             args=(sid, sess["src"], resume_at,
                                   sess.get("model") or "small"),
                             daemon=True).start()
            self._json({"ok": True})
        elif path == "/api/stop":
            if sess is None:
                self._json({"error": "no session"}, 404)
                return
            sess["cancel"].set()
            self._json({"ok": True})
        elif path == "/api/session/close":
            if sess is None:
                self._json({"error": "no session"}, 404)
                return
            sess["cancel"].set()
            if _live is not None and _live.sid == sid:
                _live.stop()
                _live = None
            with _lock:
                _sessions.pop(sid, None)
            emit(sid, "closed", True)
            self._json({"ok": True})
        elif path == "/api/live/start":
            if _live is not None:
                self._json({"error": "live exists", "sid": _live.sid}, 409)
                return
            model = str(body.get("model") or "small")
            if model not in MODELS:
                model = "small"
            live_sid = new_session()
            with _lock:
                _sessions[live_sid].update(
                    phase="working", step="", title="边播边转 · 实时转录",
                    cover="", segs=[], video=None, error="",
                    src="(系统声音实时收音)", lang="", model=model,
                    resume_at=0.0, live=True, busy=True)
            emit(live_sid, "reset", True)
            emit(live_sid, "live", True)
            emit(live_sid, "phase", "working")
            emit(live_sid, "log", "边播边转已开始: 播放任何视频/音频即可实时转文字")
            try:
                import livetrans
                _live = livetrans.LiveTranscriber(
                    model, _live_on_segment, _live_on_status)
                _live.sid = live_sid
                _live.start()
            except Exception as e:
                s = st(live_sid)
                if s is not None:
                    with _lock:
                        s.update(phase="error", live=False, busy=False,
                                 error="启动实时收音失败: %s" % e)
                emit(live_sid, "error", str(e))
                _live = None
                self._json({"error": str(e)}, 500)
                return
            self._json({"ok": True, "sid": live_sid})
        elif path == "/api/live/pause":
            if _live is None:
                self._json({"error": "not live"}, 409)
                return
            if body.get("resume"):
                _live.resume()
            else:
                _live.pause()
            self._json({"ok": True})
        elif path == "/api/live/stop":
            if _live is None:
                self._json({"error": "not live"}, 409)
                return
            live_sid = _live.sid
            elapsed = _live.stop()
            err = _live.error
            _live = None
            snap = snapshot(live_sid) or {}
            sess_live = st(live_sid)
            if sess_live is not None:
                try:
                    out_dir = BASE_DIR / "transcripts"
                    out_dir.mkdir(exist_ok=True)
                    args = argparse.Namespace(srt=True, md=True,
                                              timestamps=False)
                    base = out_dir / v2t.sanitize_filename(
                        "边播边转_%s" % time.strftime("%H%M%S"))
                    files = v2t.write_outputs(base, snap["segs"],
                                              snap.get("lang") or "zh", args,
                                              {"title": "边播边转",
                                               "url": "(实时收音)",
                                               "engine": "live-faster-whisper"})
                    emit(live_sid, "log",
                         "已保存: " + ", ".join(f.name for f in files))
                except Exception as e:
                    emit(live_sid, "log", "保存失败: %s" % e)
                with _lock:
                    sess_live.update(phase="done", live=False, busy=False,
                                     resume_at=0.0, error=err or "")
                emit(live_sid, "live", False)
                set_phase(live_sid, "直播结束 · 共录制%.0f分%02.0f秒, %d段"
                          % (elapsed // 60, elapsed % 60,
                             len(snap.get("segs") or [])))
                emit(live_sid, "phase", "done")
            self._json({"ok": True, "elapsed": elapsed})
        elif path == "/api/browse":
            url = str(body.get("url") or "").strip()
            try:
                v2t.validate_public_http_url(url)
            except ValueError as e:
                self._json({"error": str(e)}, 400)
                return
            open_playback_window(url)
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, 404)


browse_hook = None    # app_native 注入: 用pywebview开内置浏览器窗口


def open_playback_window(url):
    """在外部播放窗口中打开URL: 优先pywebview窗口(原生App), 否则Edge应用窗口。"""
    if browse_hook is not None:
        threading.Thread(target=browse_hook, args=(url,), daemon=True).start()
        return True
    for exe in EDGE_PATHS:
        if os.path.exists(exe):
            subprocess.Popen([exe, "--app=" + url,
                              "--window-size=1150,800"], shell=False)
            return True
    webbrowser.open(url)
    return True


def open_browser(port):
    url = "http://%s:%d" % (HOST, port)
    for exe in EDGE_PATHS:
        if os.path.exists(exe):
            subprocess.Popen([exe, "--app=" + url, "--window-size=1360,920"],
                             shell=False)
            return
    webbrowser.open(url)


def main():
    global ACTUAL_PORT
    # 以脚本方式运行时注册模块别名, 保证 btstream 等 import webapp 拿到同一实例
    sys.modules.setdefault("webapp", sys.modules[__name__])
    sys.stdout = TeeOut()
    if sys.stderr is None:
        sys.stderr = _Null()
    v2t.log = lambda msg="": emit("*", "log", str(msg))

    port = PORT
    for p in range(PORT, PORT + 20):
        try:
            httpd = ThreadingHTTPServer((HOST, p), Handler)
            port = p
            break
        except OSError:
            continue
    else:
        raise SystemExit("端口 %d-%d 都被占用" % (PORT, PORT + 19))
    ACTUAL_PORT = port

    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, lambda: open_browser(port)).start()
    emit("*", "log", "服务已启动: http://%s:%d" % (HOST, port))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
