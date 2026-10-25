# -*- coding: utf-8 -*-
"""链接版边播边转: 视频流式代理秒开(像播放器), 音频边下边分段识别。

视频不落盘——本地代理直连B站流式直链(durl)边播边传;
音频仅驻内存, 分片到达即用 PyAV 按时间窗解码增量转写。
"""

import io
import re
from urllib.parse import urlparse

import av
import numpy as np

import webapp
from webapp import emit, set_phase, snapshot, st, _lock
import v2t


def _sid_safe(sid):
    return re.sub(r"[^0-9a-zA-Z]", "", str(sid))[:16] or "x"


def ranged_get(url, start, end, referer):
    """带Range的受控分片下载(SSRF校验+DNS钉扎), 返回响应对象。"""
    ip = v2t.validate_public_http_url(url)
    host = urlparse(url).hostname
    headers = {"User-Agent": v2t.UA, "Referer": referer,
               "Range": "bytes=%d-%d" % (start, end)}
    with v2t._PinnedDNS(host, ip):
        return v2t._SESSION.get(url, headers=headers, timeout=30, stream=True)


def proxy_stream_response(handler, sess):
    """把B站流式直链透明代理给播放器(Range透传, 视频不落盘)。"""
    url = sess.get("stream_url")
    referer = sess.get("stream_referer") or v2t.BILI_BASE
    rng = handler.headers.get("Range") or "bytes=0-"
    ip = v2t.validate_public_http_url(url)
    host = urlparse(url).hostname
    headers = {"User-Agent": v2t.UA, "Referer": referer, "Range": rng}
    with v2t._PinnedDNS(host, ip):
        upstream = v2t._SESSION.get(url, headers=headers, timeout=30,
                                    stream=True)
    if upstream.status_code >= 400:
        handler.send_response(502)
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return
    handler.send_response(upstream.status_code)
    for h in ("Content-Type", "Content-Length", "Content-Range",
              "Accept-Ranges"):
        if upstream.headers.get(h):
            handler.send_header(h, upstream.headers[h])
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    try:
        for chunk in upstream.iter_content(chunk_size=256 * 1024):
            handler.wfile.write(chunk)
    except OSError:
        pass


def _pcm_window(data, t0, t1, sr=16000):
    """从内存中的fMP4音频按时间窗解码出16k单声道float数组。"""
    container = av.open(io.BytesIO(bytes(data)))
    stream = container.streams.audio[0]
    target = int(t0 / float(stream.time_base))
    container.seek(target, stream=stream)
    resampler = av.AudioResampler(format="s16", layout="mono", rate=sr)
    out = bytearray()
    for packet in container.demux(stream):
        try:
            decoded = packet.decode()
        except av.error.FFmpegError:
            continue  # 截断的尾部包(分片下载中), 跳过
        for frame in decoded:
            if frame is None or frame.time is None:
                continue
            ft = float(frame.time)
            if ft > t1 + 0.2:
                container.close()
                return np.frombuffer(bytes(out), dtype=np.int16)\
                    .astype("float32") / 32768.0
            if ft >= t0 - 0.5:
                res = resampler.resample(frame)
                frames = res if isinstance(res, (list, tuple)) else [res]
                for fr in frames:
                    out.extend(fr.to_ndarray().tobytes())
    container.close()
    return (np.frombuffer(bytes(out), dtype=np.int16)
            .astype("float32") / 32768.0) if out else np.zeros(0, "float32")


def _transcribe_range(sid, buf, t_from, t_to, model, cancel):
    """识别 [t_from, t_to] 窗口并推送(全部在内存中完成)。"""
    if t_to - t_from < 0.8:
        return 0
    audio = _pcm_window(buf, t_from, t_to)
    if len(audio) < sr_len(0.5):
        return 0
    iterator, info = model.transcribe(audio, language=None, vad_filter=False,
                                      beam_size=1,
                                      condition_on_previous_text=False)
    s0 = st(sid)
    if s0 is not None and info.language:
        with _lock:
            s0["lang"] = info.language
    n = 0
    for seg in iterator:
        if cancel.is_set():
            return n
        text = seg.text.strip()
        if text and text != "♪ 音乐 ♪":
            webapp.push_seg(sid, t_from + max(0.0, seg.start),
                            t_from + seg.end, text)
            n += 1
    return n


def sr_len(sec):
    return int(sec * 16000)


def _upos_first(urls):
    for u in urls:
        if "upos-sz-mirror" in u or ".bilivideo.com/" in u:
            return u
    return urls[0] if urls else None


def run_stream_job(sid, src, model="small"):
    """链接版边播边转主流程(视频代理秒开 + 音频内存增量识别)。"""
    sess = st(sid)
    if sess is None:
        return
    cancel = sess["cancel"]
    with _lock:
        sess.update(phase="working", step="", title="", cover="",
                    segs=[], error="", src=src, lang="", model=model,
                    resume_at=0.0, live=False, busy=True,
                    stream=True, stream_url=None)
    emit(sid, "reset", True)
    emit(sid, "phase", "working")
    buf = bytearray()
    try:
        set_phase(sid, "获取视频信息…")
        url = src
        if "b23.tv" in url:
            url = v2t.resolve_short_link(url)
        bili_id = v2t.detect_bili_id(url)
        if not bili_id:
            raise RuntimeError("边播边转(链接版)目前仅支持B站链接")
        kind, value = bili_id
        view = v2t.fetch_bili_view(bvid=value if kind == "bvid" else None,
                                   aid=value if kind == "aid" else None)
        bvid = view.get("bvid") or value
        title = view.get("title", bvid)
        cover = view.get("pic") or ""
        page = (view.get("pages") or [{"page": 1,
                                       "cid": view.get("cid")}])[0]
        emit(sid, "meta", {"title": title, "cover": cover,
                           "duration": view.get("duration") or 0})
        cookie_header = v2t.build_bili_cookie_header(None)
        referer = v2t.bili_audio_url(bvid, page.get("page", 1))

        # 1) 播放直链(durl单文件流式MP4, 像播放器秒开, 不下载)
        set_phase(sid, "获取播放直链…")
        play_d = v2t.http_get_json(
            v2t.BILI_PLAYURL_API,
            params={"bvid": bvid, "cid": page["cid"], "qn": "64",
                    "fnval": "1", "platform": "pc"},
            cookies=v2t.cookie_header_to_dict(cookie_header),
            referer=referer)
        durls = ((play_d.get("data") or {}).get("durl") or [])
        if not durls:
            raise RuntimeError("该视频不提供流式直链, 请用普通模式转换")
        play_url = durls[0].get("url")
        if not play_url:
            raise RuntimeError("流式直链为空")
        with _lock:
            sess["title"] = title
            sess["cover"] = cover
            sess["stream_url"] = play_url
            sess["stream_referer"] = referer
        emit(sid, "video_ready", {"title": title, "stream": True})

        # 2) 音频流地址(仅用于识别)
        set_phase(sid, "边播边转(音频增量下载)…")
        play_a = v2t.bili_fetch_playurl(bvid, page["cid"], cookie_header)
        auds = (play_a.get("dash") or {}).get("audio") or []
        if not auds:
            raise RuntimeError("拿不到音频流")
        best = max(auds, key=lambda x: int(x.get("bandwidth", 0)))
        a_urls = []
        for label in ("baseUrl", "base_url"):
            u = best.get(label)
            if u:
                a_urls.append(u)
        for label in ("backupUrl", "backup_url"):
            a_urls.extend(best.get(label) or [])
        a_url = _upos_first(a_urls)
        if not a_url:
            raise RuntimeError("音频流缺少下载地址")
        total_dur = (play_a.get("timelength") or 0) / 1000.0

        # 3) 分片下载音频到内存, 按"字节比例估时"触发增量识别
        piece = 2 * 1024 * 1024
        pos, done_t, total_bytes = 0, 0.0, 0
        m, dev, comp = v2t.get_whisper_model(model, "auto", True)
        emit(sid, "log", "识别引擎: faster-whisper(%s, %s/%s) · 流式分片"
             % (model, dev, comp))
        while not cancel.is_set():
            try:
                r = ranged_get(a_url, pos, pos + piece - 1, referer)
            except Exception as e:
                raise RuntimeError("音频分片下载失败: %s" % str(e)[:80])
            if r.status_code not in (200, 206):
                raise RuntimeError("音频下载HTTP %d" % r.status_code)
            chunk = r.content
            if not chunk:
                break
            buf.extend(chunk)
            pos += len(chunk)
            cr = r.headers.get("Content-Range") or ""
            mm = re.match(r"bytes \d+-\d+/(\d+)", cr)
            if mm:
                total_bytes = int(mm.group(1))
            if total_bytes and total_dur:
                est_t = total_dur * pos / float(total_bytes)
            else:
                est_t = done_t
            if est_t >= done_t + 12:
                set_phase(sid, "识别中… 已到 %s" % v2t.fmt_clock(est_t))
                _transcribe_range(sid, buf, max(0.0, done_t - 0.3),
                                  max(0.0, est_t - 0.5), m, cancel)
                done_t = est_t
            if len(chunk) < piece:
                break
        if not cancel.is_set() and total_dur > done_t:
            _transcribe_range(sid, buf, max(0.0, done_t - 0.3),
                              total_dur, m, cancel)
        if cancel.is_set():
            with _lock:
                sess["phase"] = "paused"
            set_phase(sid, "已暂停")
        else:
            with _lock:
                sess["phase"] = "done"
            set_phase(sid, "边播边转完成! 共%d段" % len(sess["segs"]))
            try:
                import argparse
                out_dir = webapp.BASE_DIR / "transcripts"
                out_dir.mkdir(exist_ok=True)
                snap = snapshot(sid)
                args = argparse.Namespace(srt=True, md=True,
                                          timestamps=False)
                base = out_dir / v2t.sanitize_filename(title or "transcript")
                files = v2t.write_outputs(base, snap["segs"],
                                          snap.get("lang") or "zh", args,
                                          {"title": title, "url": src,
                                           "engine": "stream-whisper"})
                emit(sid, "log",
                     "已保存: " + ", ".join(x.name for x in files))
            except Exception as e:
                emit(sid, "log", "保存失败: %s" % e)
    except Exception as e:
        with _lock:
            sess["phase"] = "error"
            sess["error"] = str(e)
        emit(sid, "error", str(e))
    finally:
        with _lock:
            sess["busy"] = False
        buf.clear()
        s = snapshot(sid)
        emit(sid, "phase", s["phase"] if s else "done")
