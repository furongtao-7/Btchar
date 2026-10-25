#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v2t —— 视频转文字命令行工具

支持输入:
  - B站视频链接 / BV号 / b23.tv 短链
  - 其他主流平台链接(YouTube/抖音/西瓜/小红书等, 由 yt-dlp 支持的站点均可)
  - 本地视频/音频文件

流程: 解析输入 -> 优先抓平台CC字幕 -> 没有字幕则下载音频 -> faster-whisper 本地语音识别
输出: 纯文本(.txt) / SRT字幕(.srt) / 带时间戳的Markdown(.md)

依赖: pip install yt-dlp faster-whisper requests
"""

import argparse
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlparse, urljoin

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
BILI_BASE = "https://www.bilibili.com"
BILI_VIEW_API = "https://api.bilibili.com/x/web-interface/view"
BILI_PLAYER_API = "https://api.bilibili.com/x/player/v2"
BILI_PLAYURL_API = "https://api.bilibili.com/x/player/playurl"
LOGIN_QR_GENERATE = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
LOGIN_QR_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
LOGIN_COOKIE_FILE = Path(__file__).resolve().parent / "bili_login_cookies.txt"

_SEG = lambda s: {"start": float(s["start"]), "end": float(s["end"]),
                  "text": str(s["text"]).strip()}


# ---------------------------------------------------------------- 基础工具

def setup_console():
    """Windows 控制台切到 UTF-8，避免中文乱码。"""
    if os.name == "nt":
        try:
            subprocess.run(["chcp", "65001"], capture_output=True)
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def log(msg=""):
    print(msg, flush=True)


def sanitize_filename(name, max_len=80):
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name).strip(" .")
    return (name[:max_len] if len(name) > max_len else name) or "untitled"


def safe_output_path(base_dir, name):
    """拼出输出路径，并确保最终路径不会逃出 base_dir(防路径穿越)。"""
    base = Path(base_dir).resolve()
    target = (base / name).resolve()
    if base != target and base not in target.parents:
        raise ValueError("输出路径越界，已拒绝: %r" % name)
    return target


_DNS_CACHE = {}
_PIN_TLS = threading.local()  # 每线程独立的 host->ip 钉扎表(线程安全)
_ORIG_CREATE_CONNECTION = socket.create_connection


def _patched_create_connection(address, *args, **kwargs):
    """按当前线程的钉扎表替换目标IP(防DNS rebinding), 不影响其他线程。"""
    pins = getattr(_PIN_TLS, "pins", None)
    if pins and address and address[0] in pins:
        address = (pins[address[0]],) + tuple(address[1:])
    return _ORIG_CREATE_CONNECTION(address, *args, **kwargs)


socket.create_connection = _patched_create_connection


def resolve_public_ip(host):
    """解析域名并要求全部结果均为公网地址，返回并缓存一个钉住的IP。"""
    if host in _DNS_CACHE:
        return _DNS_CACHE[host]
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        raise ValueError("无法解析域名: %s" % host)
    pinned = None
    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if (addr.is_private or addr.is_loopback or addr.is_reserved
                or addr.is_link_local or addr.is_multicast or addr.is_unspecified):
            raise ValueError("域名 %s 解析到内网/保留地址，已拒绝" % host)
        if pinned is None:
            pinned = str(addr)
    if pinned is None:
        raise ValueError("域名 %s 无可用解析结果" % host)
    _DNS_CACHE[host] = pinned
    return pinned


def validate_public_http_url(url):
    """安全约束: 仅 http/https；解析DNS并拒绝 localhost/内网/保留地址。

    返回校验通过后钉住的IP，供请求层防 DNS rebinding。
    """
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise ValueError("仅允许 http/https 链接: %s" % url)
    host = p.hostname
    if not host:
        raise ValueError("链接缺少主机名: %s" % url)
    if host.lower() == "localhost" or host.lower().endswith(".local"):
        raise ValueError("不允许访问本地地址: %s" % host)
    return resolve_public_ip(host)


class _PinnedDNS(object):
    """请求期间把本线程的连接钉到已校验IP(防DNS rebinding, 线程局部)。

    TLS 的 SNI/证书校验仍使用原主机名，不影响 HTTPS 证书验证。
    """

    def __init__(self, host, ip):
        self._host, self._ip = host, ip

    def __enter__(self):
        if not hasattr(_PIN_TLS, "pins"):
            _PIN_TLS.pins = {}
        _PIN_TLS.pins[self._host] = self._ip

    def __exit__(self, *exc):
        _PIN_TLS.pins.pop(self._host, None)
        return False


_SESSION = requests.Session()
_SESSION.trust_env = False  # 忽略系统代理，避免经代理绕过校验


def http_get(url, params=None, cookies=None, referer=None, timeout=20):
    """带SSRF防护的GET: 限制重定向跳数，每一跳都重新校验并钉住IP。"""
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    cur, resp = url, None
    for hop in range(6):
        ip = validate_public_http_url(cur)
        target = urlparse(cur)
        with _PinnedDNS(target.hostname, ip):
            resp = _SESSION.get(cur, params=params if hop == 0 else None,
                                headers=headers, cookies=cookies,
                                timeout=timeout, allow_redirects=False)
        if 300 <= resp.status_code < 400 and resp.headers.get("Location"):
            cur = urljoin(cur, resp.headers["Location"])
            continue
        return resp
    raise ValueError("重定向次数过多或缺少Location: %s" % url)


def http_get_json(url, **kw):
    r = http_get(url, **kw)
    r.raise_for_status()
    return r.json()


def http_get_json(url, **kw):
    r = http_get(url, **kw)
    r.raise_for_status()
    return r.json()


def load_cookie_header(cookie_file, domain_suffix):
    """把 Netscape 格式 cookies.txt 转成请求头字符串(仅取指定域名的)。"""
    pairs = []
    try:
        with open(cookie_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("#HttpOnly_"):
                    line = line[len("#HttpOnly_"):]
                elif not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) >= 7 and domain_suffix in parts[0]:
                    pairs.append("%s=%s" % (parts[5], parts[6]))
    except OSError as e:
        raise SystemExit("读取 cookies 文件失败: %s" % e)
    return "; ".join(pairs)


def fmt_clock(sec, with_hour=False):
    sec = max(0, int(sec))
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return ("%d:%02d:%02d" % (h, m, s)) if (with_hour or h > 0) else ("%02d:%02d" % (m, s))


def fmt_srt_time(sec):
    ms = int(round(max(0.0, sec) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


# ---------------------------------------------------------------- B站部分

def ensure_anon_bili_cookies(cache_path):
    """访问B站首页拿匿名cookie(buvid3等)。

    B站 playurl 接口对无cookie请求返回412，未登录的匿名cookie即可绕过。
    写成 Netscape cookies.txt 供 yt-dlp 使用，返回文件路径或 None。
    """
    cache_path = Path(cache_path)
    if cache_path.exists():
        return str(cache_path)
    try:
        http_get("https://www.bilibili.com/", timeout=15)
    except (requests.RequestException, ValueError) as e:
        log("  [警告] 获取B站匿名cookie失败(%s)，继续尝试" % e)
        return None
    lines = ["# Netscape HTTP Cookie File"]
    for c in _SESSION.cookies:
        if "bilibili.com" in c.domain:
            lines.append("\t".join([c.domain, "TRUE", c.path,
                                    "TRUE" if c.secure else "FALSE",
                                    str(int(c.expires) if c.expires else 0),
                                    c.name, c.value]))
    if len(lines) > 1:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(cache_path)
    return None


def _netscape_pairs(cookie_file):
    if not cookie_file or not os.path.exists(cookie_file):
        return []
    text = load_cookie_header(cookie_file, "bilibili.com")
    pairs = []
    for kv in text.split("; "):
        if "=" in kv:
            k, v = kv.split("=", 1)
            pairs.append((k, v))
    return pairs


def build_bili_cookie_header(user_cookie_file):
    """组装B站API Cookie: 匿名cookie < 本地登录cookie < 用户指定cookie(可覆盖)。"""
    merged = {}
    for c in _SESSION.cookies:
        if "bilibili.com" in c.domain:
            merged[c.name] = c.value
    if LOGIN_COOKIE_FILE.exists():
        for k, v in _netscape_pairs(str(LOGIN_COOKIE_FILE)):
            merged[k] = v
    for k, v in _netscape_pairs(user_cookie_file):
        merged[k] = v
    return "; ".join("%s=%s" % (k, v) for k, v in merged.items())


def login_generate():
    """生成B站登录二维码(终端ASCII + PNG图片)，返回 qrcode_key。"""
    try:
        import qrcode
    except ImportError:
        raise SystemExit("缺少 qrcode 库: 请执行 .venv\\Scripts\\pip install qrcode")

    data = http_get_json(LOGIN_QR_GENERATE, referer=BILI_BASE)
    if data.get("code") != 0:
        raise SystemExit("获取登录二维码失败: %s" % data)
    qr_url, qr_key = data["data"]["url"], data["data"]["qrcode_key"]

    qr = qrcode.QRCode(border=1)
    qr.add_data(qr_url)
    qr.make(fit=True)
    log("请用【哔哩哔哩APP】扫描二维码并确认登录 (约3分钟内有效):")
    log("")
    try:
        qr.print_ascii(invert=True, tty=False)
    except Exception:
        pass
    png = LOGIN_COOKIE_FILE.with_name("login_qr.png")
    try:
        qr.make_image().save(str(png))
        log("")
        log("终端二维码显示异常的话，可打开图片扫码: %s" % png)
    except Exception:
        pass
    return qr_key


def login_wait(qr_key, wait_seconds=180):
    """轮询扫码结果，成功后把登录cookie保存到本地文件。"""
    deadline = time.time() + wait_seconds
    logged_in = False
    while time.time() < deadline:
        time.sleep(2)
        p = http_get_json(LOGIN_QR_POLL, params={"qrcode_key": qr_key},
                          referer=BILI_BASE)
        code = (p.get("data") or {}).get("code")
        if code == 86090:
            log("  已扫描，请在手机上点【确认登录】...")
            continue
        if code == 0:
            logged_in = True
            break
        if code == 86038:
            raise SystemExit("二维码已过期，请重新运行: v2t login")
        # 86101 = 等待扫描
    if not logged_in:
        raise SystemExit("等待扫码超时，请重新运行: v2t login")

    rows = []
    for c in _SESSION.cookies:
        if "bilibili.com" in c.domain:
            rows.append((c.domain, c.path, c.secure, c.expires, c.name, c.value))
    if not any(name == "SESSDATA" for *_, name, _v in rows):
        raise SystemExit("登录未完成(没拿到SESSDATA)，请重试")

    lines = ["# Netscape HTTP Cookie File"]
    for dom, path, secure, expires, name, value in rows:
        lines.append("\t".join([dom, "TRUE", path, "TRUE" if secure else "FALSE",
                                str(int(expires) if expires else 0), name, value]))
    LOGIN_COOKIE_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log("登录成功! 凭证已保存: %s" % LOGIN_COOKIE_FILE)
    log("提示: 该文件等同于账号登录态，请勿分享给他人。")


def do_login():
    """完整扫码登录流程，二维码过期自动刷新(最多5轮)。"""
    for _attempt in range(5):
        try:
            login_wait(login_generate())
            return
        except SystemExit as e:
            if "过期" in str(e):
                log("二维码过期，自动刷新...")
                continue
            raise
    raise SystemExit("二维码多次过期仍未完成登录，请重试")


def subtitle_matches_audio(segs, audio_path, model=None, window=30.0,
                           threshold=0.16):
    """校验字幕内容是否属于该视频: whisper抽查音频开头与字幕比对。

    B站字幕CDN会随机返回其他视频的字幕(脏数据), 时长校验无法完全拦截,
    用本地识别开头30秒做内容一致性抽查。
    """
    try:
        from faster_whisper.audio import decode_audio
        samples = decode_audio(audio_path, sampling_rate=16000)
        head = samples[:int(window * 16000)]
        if len(head) < 16000:
            return True  # 音频太短无从比对, 放行交给上层时长规则
        if model is None:
            model, _, _ = get_whisper_model("small", "auto", True)
        iterator, info = model.transcribe(head, language=None, vad_filter=False,
                                          beam_size=1,
                                          condition_on_previous_text=False)
        spoken = "".join(s.text for s in iterator)
        sub_text = "".join(s["text"] for s in segs if s["start"] < window)

        def _bigrams(t):
            t = re.sub(r"[\W\s_]", "", t.lower())
            return {t[i:i + 2] for i in range(len(t) - 1)} | {
                t[i:i + 3] for i in range(len(t) - 2)}

        a, b = _bigrams(spoken), _bigrams(sub_text)
        if not a or not b:
            return len(segs) > 0  # 开头无语音无法比对, 放行
        overlap = len(a & b) / min(len(a), len(b))
        return overlap >= threshold
    except Exception:
        return True  # 校验器异常不阻断主流程


def subtitle_span_ok(segs, duration):
    """字幕覆盖时长与视频时长的一致性检查(粗筛)。"""
    if not segs or not duration or duration < 60:
        return True
    span = segs[-1]["end"] - segs[0]["start"]
    if span > duration * 1.15 + 20:
        return False  # 字幕比视频还长得多: 肯定是别的视频
    if span < duration * 0.2:
        return False  # 长视频却只覆盖一丁点
    return True


def detect_bili_id(text):
    """从字符串里提取 BV号 / av号，b23.tv 短链需先展开。"""
    m = re.search(r"BV[0-9A-Za-z]{10}", text)
    if m:
        return ("bvid", m.group(0))
    m = re.search(r"(?:^|[^\w])av(\d{4,})", text)
    if m:
        return ("aid", m.group(1))
    return None


def resolve_short_link(url):
    validate_public_http_url(url)
    r = http_get(url, referer=BILI_BASE, timeout=15)
    return r.url


def fetch_bili_view(bvid=None, aid=None):
    params = {"bvid": bvid} if bvid else {"aid": aid}
    data = http_get_json(BILI_VIEW_API, params=params, referer=BILI_BASE)
    if data.get("code") != 0:
        raise ValueError("B站接口返回错误 code=%s msg=%s (视频可能不存在/下架/需付费)"
                         % (data.get("code"), data.get("message")))
    return data["data"]


def fetch_bili_cc_subtitles(bvid, cid, cookie_header):
    """抓B站 CC 字幕(UP主上传的或AI字幕)。返回 [(start,end,text)] 或 None。"""
    params = {"bvid": bvid, "cid": cid}
    cookies = None
    if cookie_header:
        cookies = {kv.split("=", 1)[0]: kv.split("=", 1)[1]
                   for kv in cookie_header.split("; ") if "=" in kv}
    try:
        r = http_get(BILI_PLAYER_API, params=params, cookies=cookies,
                     referer=BILI_BASE)
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        log("  [字幕] 字幕接口请求失败(%s)，跳过" % e.__class__.__name__)
        return None
    if data.get("code") != 0:
        return None
    subs = (data.get("data") or {}).get("subtitle") or {}
    sub_list = subs.get("subtitles") or []
    if not sub_list:
        # B站AI字幕是懒生成的: 首次查询可能还没生成好，稍等重试一次
        time.sleep(4)
        try:
            r = http_get(BILI_PLAYER_API, params=params, cookies=cookies,
                         referer=BILI_BASE)
            data = r.json()
        except (requests.RequestException, ValueError):
            return None
        if data.get("code") != 0:
            return None
        subs = (data.get("data") or {}).get("subtitle") or {}
        sub_list = subs.get("subtitles") or []
    if not sub_list:
        return None
    # 优先中文字幕(ai-zh / zh-CN 等: 语言码含zh即算)
    chosen = None
    for s in sub_list:
        if "zh" in str(s.get("lan", "")).lower():
            chosen = s
            break
    chosen = chosen or sub_list[0]
    sub_url = chosen.get("subtitle_url") or ""
    if sub_url.startswith("//"):
        sub_url = "https:" + sub_url
    if not sub_url:
        return None
    try:
        body = http_get_json(sub_url, referer=BILI_BASE)
    except (requests.RequestException, ValueError) as e:
        log("  [字幕] 字幕文件下载失败(%s)，跳过" % e.__class__.__name__)
        return None
    segs = []
    for item in body.get("body", []):
        text = str(item.get("content", "")).strip()
        if text:
            segs.append({"start": float(item["from"]), "end": float(item["to"]),
                         "text": text})
    return segs or None


def bili_audio_url(bvid, page):
    url = "%s/video/%s" % (BILI_BASE, bvid)
    if page and page > 1:
        url += "?p=%d" % page
    return url


def cookie_header_to_dict(header):
    cookies = {}
    for kv in (header or "").split("; "):
        if "=" in kv:
            k, v = kv.split("=", 1)
            cookies[k] = v
    return cookies


def download_stream(url, dest_path, referer=None, expect_size=None, label="下载音频"):
    """流式下载文件(带SSRF防护和进度显示)。"""
    headers = {"User-Agent": UA, "Accept": "*/*"}
    if referer:
        headers["Referer"] = referer
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    cur = url
    for _hop in range(6):
        ip = validate_public_http_url(cur)
        target = urlparse(cur)
        with _PinnedDNS(target.hostname, ip):
            resp = _SESSION.get(cur, headers=headers, timeout=30,
                                stream=True, allow_redirects=False)
        if 300 <= resp.status_code < 400 and resp.headers.get("Location"):
            cur = urljoin(cur, resp.headers["Location"])
            continue
        break
    else:
        raise ValueError("下载重定向次数过多: %s" % url)
    with resp:
        if resp.status_code != 200:
            raise RuntimeError("音频流HTTP %s" % resp.status_code)
        total = int(resp.headers.get("Content-Length") or 0) or expect_size or 0
        done = 0
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=256 * 1024):
                f.write(chunk)
                done += len(chunk)
                if total:
                    sys.stdout.write("\r  [%s] %d%%   " % (label,
                                                            min(99, done * 100 // total)))
                    sys.stdout.flush()
    log("  [%s] 100%% 完成" % label)
    return dest_path


def bili_fetch_playurl(bvid, cid, cookie_header):
    """直连B站 playurl 接口获取DASH音频流信息(匿名cookie即可)。"""
    params = {"bvid": bvid, "cid": cid, "qn": "64", "fnval": "16", "try_look": "1"}
    r = http_get_json(BILI_PLAYURL_API, params=params,
                      cookies=cookie_header_to_dict(cookie_header),
                      referer=bili_audio_url(bvid, 1))
    if r.get("code") != 0:
        raise RuntimeError("B站 playurl 接口返回 code=%s msg=%s"
                           % (r.get("code"), r.get("message")))
    return r["data"]


def bili_download_audio(bvid, cid, dest_dir, cookie_header, referer_url=None):
    """从B站DASH流下载音轨(无需ffmpeg合成)。返回本地音频路径。"""
    data = bili_fetch_playurl(bvid, cid, cookie_header)
    auds = (data.get("dash") or {}).get("audio") or []
    if not auds:
        raise RuntimeError("playurl 未返回音频流(视频可能需要登录/大会员)")
    # 音质档位: 30280(192k) > 30232(132k) > 30216(64k)，选可用的最高档
    best = max(auds, key=lambda a: (int(a.get("id", 0)), int(a.get("bandwidth", 0))))
    # URL优选: PCDN节点(mcdn:8082)对普通HTTP客户端会返回错误缓存,
    # 优先 upos 普通CDN镜像
    urls = []
    for label in ("baseUrl", "base_url"):
        u = best.get(label)
        if u and u not in urls:
            urls.append(u)
    for label in ("backupUrl", "backup_url"):
        for u in (best.get(label) or []):
            if u and u not in urls:
                urls.append(u)
    stream_url = next((u for u in urls
                       if "upos-sz-mirror" in u or ".bilivideo.com/" in u),
                      urls[0] if urls else None)
    if not stream_url:
        raise RuntimeError("音频流缺少下载地址")
    # ext/bvid 来自接口返回，只保留字母数字，并把最终路径限制在下载目录内
    ext = re.sub(r"[^0-9a-zA-Z]", "", (best.get("mimeType") or "audio/mp4").split("/")[-1]) \
        or "m4a"
    safe_bvid = re.sub(r"[^0-9A-Za-z_-]", "", str(bvid)) or "audio"
    dest = safe_output_path(dest_dir, "%s.%s" % (safe_bvid, ext))
    expect = data.get("timelength") and int(best.get("bandwidth", 0)) * \
        int(data["timelength"]) // 8000 or None
    return download_stream(stream_url, dest, referer=referer_url or BILI_BASE,
                           expect_size=expect)


# ---------------------------------------------------------------- yt-dlp 部分

def yt_dlp_download_audio(url, out_dir, cookie_file=None):
    """只下载音轨(B站等DASH站点无需ffmpeg合成)。返回 (音频路径, info)。"""
    import yt_dlp

    def _hook(d):
        if d.get("status") == "downloading":
            pct = d.get("_percent_str", "").strip() or "?"
            speed = d.get("_speed_str", "").strip() or "?"
            sys.stdout.write("\r  [下载音频] %s  %s   " % (pct, speed))
            sys.stdout.flush()
        elif d.get("status") == "finished":
            log("  [下载音频] 100% 完成")

    opts = {
        "format": "ba/b",
        "outtmpl": str(Path(out_dir) / "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 3,
        "noprogress": True,
        "progress_hooks": [_hook],
        "http_headers": {"User-Agent": UA, "Referer": BILI_BASE},
    }
    if cookie_file:
        opts["cookiefile"] = cookie_file
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    filepath = None
    dl = info.get("requested_downloads") or []
    if dl:
        filepath = dl[0].get("filepath")
    if not filepath:
        filepath = ydl.prepare_filename(info)
    if not filepath or not Path(filepath).exists():
        raise RuntimeError("音频下载失败，未找到输出文件")
    log("")
    return filepath, info


def yt_dlp_fetch_subtitles(url, cookie_file=None):
    """非B站站点: 用 yt-dlp 拿平台字幕(json3)，转成段落。没有则返回 None。"""
    import yt_dlp

    opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "http_headers": {"User-Agent": UA},
    }
    if cookie_file:
        opts["cookiefile"] = cookie_file
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    track_groups = [info.get("subtitles") or {}, info.get("automatic_captions") or {}]
    lang_prefs = ["zh-Hans", "zh-CN", "zh-Hant", "zh-TW", "zh", "en"]
    for tracks in track_groups:
        if not tracks:
            continue
        chosen_lang, entry = None, None
        for lang in lang_prefs:
            for key in tracks:
                if key.lower().startswith(lang.lower()) and tracks[key]:
                    chosen_lang, entry = key, tracks[key]
                    break
            if entry:
                break
        if not entry:
            continue
        fmt = None
        for f in entry:
            if f.get("ext") == "json3":
                fmt = f
                break
        fmt = fmt or entry[0]
        fmt_url = fmt.get("url")
        if not fmt_url:
            continue
        if fmt_url.startswith("//"):
            fmt_url = "https:" + fmt_url
        try:
            payload = http_get_json(fmt_url)
        except (requests.RequestException, ValueError):
            continue
        segs = []
        if fmt.get("ext") == "json3" or "events" in payload:
            for ev in payload.get("events", []):
                segs_txt = "".join(x.get("utf8", "") for x in ev.get("segs") or [])
                segs_txt = segs_txt.replace("\n", " ").strip()
                if segs_txt:
                    start = (ev.get("tStartMs") or 0) / 1000.0
                    dur = (ev.get("dDurationMs") or 0) / 1000.0
                    segs.append({"start": start, "end": start + dur, "text": segs_txt})
        if segs:
            log("  [字幕] 抓到平台字幕(%s)，跳过语音识别" % chosen_lang)
            return segs, info
    return None, info


# ---------------------------------------------------------------- Whisper 部分

def ensure_hf_endpoint():
    """huggingface.co 不可达时自动切国内镜像 hf-mirror.com。"""
    if os.environ.get("HF_ENDPOINT"):
        return
    try:
        requests.head("https://huggingface.co", timeout=4)
        return
    except requests.RequestException:
        pass
    os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
    log("  [模型] huggingface.co 不可达，已自动切换镜像 hf-mirror.com")


def _add_nvidia_dll_dirs():
    """把 pip 安装的 NVIDIA 运行库(cublas/cudnn)目录加进DLL搜索路径。

    需同时设置 PATH: ctranslate2 用普通 LoadLibrary 加载 cublas，
    只认 PATH，不认 os.add_dll_directory。
    """
    import site

    dirs = []
    for sp in site.getsitepackages() + [site.getusersitepackages()]:
        nvidia = Path(sp) / "nvidia"
        if not nvidia.is_dir():
            continue
        for dll_dir in list(nvidia.glob("*/bin")) + list(nvidia.glob("*/*/bin")):
            dirs.append(str(dll_dir))
            try:
                os.add_dll_directory(str(dll_dir))
            except (OSError, AttributeError):
                pass
    if dirs:
        os.environ["PATH"] = os.pathsep.join(dirs) + os.pathsep + os.environ.get("PATH", "")


_MODEL_CACHE = {}


def _probe_model(model, device):
    """跑一次1秒静音识别，验证CUDA运行库(cuBLAS等)真的可用。"""
    import numpy as np

    probe = np.zeros(16000, dtype="float32")
    for _seg in model.transcribe(probe, vad_filter=False, beam_size=1):
        break


def get_whisper_model(model_size, device_pref, allow_gpu):
    key = (model_size, device_pref, allow_gpu)
    if key in _MODEL_CACHE:
        return _MODEL_CACHE[key]

    ensure_hf_endpoint()
    from faster_whisper import WhisperModel  # 延迟导入，加快 --help 速度

    candidates = []
    if device_pref == "cpu":
        candidates = [("cpu", "int8")]
    elif device_pref == "cuda":
        # int8 在本机(RTX 5060)实测稳定且最快
        candidates = [("cuda", "int8")]
    else:  # auto
        try:
            import ctranslate2
            if ctranslate2.get_cuda_device_count() > 0 and allow_gpu:
                candidates = [("cuda", "int8")]
        except Exception:
            pass
        candidates.append(("cpu", "int8"))

    last_err = None
    for device, compute in candidates:
        try:
            log("  [模型] 加载 %s (%s/%s)..." % (model_size, device, compute))
            model = WhisperModel(model_size, device=device, compute_type=compute)
            if device == "cuda":
                _add_nvidia_dll_dirs()
                _probe_model(model, device)  # 缺cuBLAS/cuDNN时在这里暴露
            _MODEL_CACHE[key] = (model, device, compute)
            return _MODEL_CACHE[key]
        except Exception as e:
            last_err = e
            log("  [模型] %s/%s 不可用: %s" % (device, compute, str(e)[:140]))
            if device == "cuda":
                log("  [模型] 自动回退 CPU (提示: CUDA需要cuBLAS/cuDNN运行库)")
    raise RuntimeError("无法加载模型 %s: %s" % (model_size, last_err))


def transcribe_audio(path, model, lang):
    """跑语音识别，返回 (segments, info)。VAD滤掉全部内容时自动去掉VAD重试。"""
    def _run(use_vad):
        kw = dict(beam_size=5,
                  vad_filter=use_vad,
                  vad_parameters={"min_silence_duration_ms": 500},
                  condition_on_previous_text=False)
        kw["language"] = None if lang == "auto" else lang
        return model.transcribe(path, **kw)

    iterator, info = _run(True)
    duration = info.duration or 0

    segs, next_report = [], 0.0
    for seg in iterator:
        text = seg.text.strip()
        if text:
            segs.append({"start": seg.start, "end": seg.end, "text": text})
        if duration and seg.end >= next_report:
            sys.stdout.write("\r  [识别] %d%%" % min(99, int(seg.end / duration * 100)))
            sys.stdout.flush()
            next_report = seg.end + max(2.0, duration / 100)

    if not segs and duration > 1:
        # 纯音乐/歌声场景VAD可能把整段音频判为非语音，去掉VAD重试
        log("\r  [识别] VAD未检出人声，去掉VAD重新识别...")
        segs = []
        iterator, info = _run(False)
        for seg in iterator:
            text = seg.text.strip()
            if text:
                segs.append({"start": seg.start, "end": seg.end, "text": text})

    if duration:
        sys.stdout.write("\r  [识别] 100%\n")
    return segs, info


# ---------------------------------------------------------------- 输出

def group_paragraphs(segs, max_gap=3.0, max_chars=150):
    """把碎片段落合并成自然段: 间隔>3秒或段落超150字就分段。"""
    paras = []
    cur, cur_len, last_end = [], 0, None
    for s in segs:
        if last_end is not None and (s["start"] - last_end) > max_gap and cur:
            paras.append(cur)
            cur, cur_len = [], 0
        cur.append(s)
        cur_len += len(s["text"])
        if cur_len >= max_chars:
            paras.append(cur)
            cur, cur_len = [], 0
        last_end = s["end"]
    if cur:
        paras.append(cur)
    return paras


def join_text(texts, lang):
    if lang and lang.lower().startswith(("zh", "ja")):
        return "".join(t for t in texts)
    return " ".join(t for t in texts)


def write_outputs(base, segs, lang, args, meta):
    """按参数写出 txt/srt/md。base 为不带扩展名的路径。"""
    written = []

    def w(suffix, content):
        p = base.with_suffix(suffix)
        p.write_text(content, encoding="utf-8")
        written.append(p)

    use_ts = args.timestamps
    if args.srt:
        lines = []
        for i, s in enumerate(segs, 1):
            lines.append(str(i))
            lines.append("%s --> %s" % (fmt_srt_time(s["start"]), fmt_srt_time(s["end"])))
            lines.append(s["text"])
            lines.append("")
        w(".srt", "\n".join(lines))

    if args.md:
        md = ["# %s" % meta.get("title", base.name), ""]
        if meta.get("url"):
            md.append("- 来源: %s" % meta["url"])
        if meta.get("author"):
            md.append("- 作者: %s" % meta["author"])
        if meta.get("duration"):
            md.append("- 时长: %s" % fmt_clock(meta["duration"]))
        md.append("- 文字来源: %s" % meta.get("engine", "faster-whisper"))
        md.append("- 生成日期: %s" % date.today().isoformat())
        md += ["", "## 正文", ""]
        for para in group_paragraphs(segs):
            md.append("[%s] %s" % (fmt_clock(para[0]["start"]),
                                   join_text([s["text"] for s in para], lang)))
            md.append("")
        w(".md", "\n".join(md))

    if use_ts:
        w(".txt", "\n".join("[%s] %s" % (fmt_clock(s["start"]), s["text"]) for s in segs))
    else:
        w(".txt", join_text([s["text"] for s in segs], lang))

    if getattr(args, "summary", False) and segs:
        try:
            import summarizer
            r = summarizer.summarize(segs)
            if r:
                lines = ["# %s - 摘要" % meta.get("title", base.name), ""]
                lines.append("**关键词**: %s" % "、".join(r["keywords"]))
                lines.append("")
                lines.append("**一句话摘要**: %s" % r["headline"])
                lines.append("")
                lines.append("## 要点")
                lines.append("")
                for p in r["points"]:
                    lines.append("- [%s] %s" % (fmt_clock(p["start"]), p["text"]))
                w(".摘要.md", "\n".join(lines))
        except Exception as e:
            log("  [摘要] 生成失败: %s" % e)
    return written


# ---------------------------------------------------------------- 主流程

def process_one(src, args, out_dir):
    t0 = time.time()
    log("=" * 60)
    log("输入: %s" % src)
    written = []

    cookie_file = args.cookies

    # ---------- 本地文件 ----------
    if os.path.exists(src):
        title = Path(src).stem
        meta = {"title": title, "engine": "faster-whisper(%s)" % args.model}
        model, device, compute = get_whisper_model(args.model, args.device, not args.no_gpu)
        log("  [识别] 本地文件，直接语音识别...")
        segs, info = transcribe_audio(src, model, args.lang)
        lang = args.lang if args.lang != "auto" else (info.language or args.lang)
    else:
        # ---------- 判断输入类型 ----------
        is_url = re.match(r"^https?://", src, re.I)
        bili_id = None
        if is_url:
            real_url = resolve_short_link(src) if "b23.tv" in src else src
            bili_id = detect_bili_id(real_url)
        else:
            bili_id = detect_bili_id(src)
            real_url = None

        segs, lang, meta = None, args.lang, {"engine": "faster-whisper(%s)" % args.model}

        if bili_id:
            kind, value = bili_id
            anon_file = ensure_anon_bili_cookies(out_dir / "audio_tmp" / "bili_cookies.txt")
            cookie_header = build_bili_cookie_header(cookie_file)
            dl_cookie_file = (cookie_file
                              or (str(LOGIN_COOKIE_FILE) if LOGIN_COOKIE_FILE.exists() else None)
                              or anon_file)
            view = fetch_bili_view(bvid=value if kind == "bvid" else None,
                                   aid=value if kind == "aid" else None)
            bvid = view.get("bvid") or value
            pages = view.get("pages") or [{"page": 1, "cid": view.get("cid"),
                                           "part": view.get("title")}]
            if len(pages) > 1:
                log("  [信息] 该视频共 %d 个分P: %s" %
                    (len(pages), " | ".join("P%d %s" % (p["page"], p.get("part", ""))[:40]
                                            for p in pages[:8])))
            if args.page == 0:
                chosen = pages
            else:
                chosen = [p for p in pages if p["page"] == args.page] or pages[:1]

            audio_path = None
            for idx, page in enumerate(chosen, 1):
                page_tag = "" if len(chosen) == 1 else " P%d" % page["page"]
                title = view.get("title", bvid)
                if len(pages) > 1:
                    title = "%s P%d %s" % (title, page["page"], page.get("part", ""))
                meta = {
                    "title": title,
                    "author": (view.get("owner") or {}).get("name", ""),
                    "duration": page.get("duration") or view.get("duration"),
                    "url": bili_audio_url(bvid, page["page"]),
                    "engine": "faster-whisper(%s)" % args.model,
                }
                log("  [信息] %s (UP: %s, 时长: %s)" %
                    (title[:60], meta["author"], fmt_clock(meta["duration"] or 0)))

                # 先下音频(供播放兜底与字幕校验), 再抓字幕并验证内容可信度
                url_p = bili_audio_url(bvid, page["page"])
                try:
                    audio_path = bili_download_audio(bvid, page["cid"],
                                                     out_dir / "audio_tmp",
                                                     cookie_header, url_p)
                except Exception as e:
                    log("  [下载] B站直连下载失败(%s)，改用 yt-dlp 兜底"
                        % str(e)[:100])
                    audio_path, _info = yt_dlp_download_audio(
                        url_p, out_dir / "audio_tmp", dl_cookie_file)

                got = fetch_bili_cc_subtitles(bvid, page["cid"], cookie_header)
                if got and not subtitle_span_ok(got, meta.get("duration")):
                    log("  [字幕] 字幕时长与视频不符(疑似CDN脏数据)，弃用字幕")
                    got = None
                if got and not subtitle_matches_audio(got, audio_path):
                    log("  [字幕] 字幕内容与音频不符(疑似CDN脏数据)，"
                        "弃用字幕改用本地识别")
                    got = None
                if got:
                    log("  [字幕] 获取到B站CC字幕(共%d条)且校验通过" % len(got))
                    meta["engine"] = "B站CC字幕"
                    segs = got
                    if not args.keep_audio and audio_path:
                        try:
                            os.remove(audio_path)
                        except OSError:
                            pass
                else:
                    if not cookie_header:
                        log("  [字幕] 没有可用的CC字幕(B站AI字幕一般需要登录cookie)")
                    model, device, compute = get_whisper_model(args.model, args.device,
                                                               not args.no_gpu)
                    log("  [识别] 开始语音识别 (%s/%s)..." % (device, compute))
                    segs, winfo = transcribe_audio(audio_path, model, args.lang)
                    lang = winfo.language or lang
                    meta["engine"] = "faster-whisper(%s, %s/%s)" % (args.model, device, compute)
                    if not args.keep_audio and audio_path:
                        try:
                            os.remove(audio_path)
                        except OSError:
                            pass
                base = out_dir / sanitize_filename(title)
                files = write_outputs(base, segs, lang, args, meta)
                written.extend(files)
                log("  [完成] %.1f秒  输出: %s" %
                    (time.time() - t0, ", ".join(str(f.name) for f in files)))
            return written

        # ---------- 非B站链接 ----------
        if is_url:
            validate_public_http_url(real_url)
            log("  [信息] 通用平台链接，使用 yt-dlp 处理")
            segs_plat, info = yt_dlp_fetch_subtitles(real_url, cookie_file)
            if segs_plat:
                segs = segs_plat
                meta["title"] = info.get("title") or Path(real_url).name
                meta["url"] = real_url
                meta["duration"] = info.get("duration")
                meta["author"] = (info.get("uploader") or "")
                meta["engine"] = "平台字幕(yt-dlp)"
            else:
                audio_path, info = yt_dlp_download_audio(real_url, out_dir / "audio_tmp",
                                                         cookie_file)
                model, device, compute = get_whisper_model(args.model, args.device,
                                                           not args.no_gpu)
                log("  [识别] 开始语音识别 (%s/%s)..." % (device, compute))
                segs, winfo = transcribe_audio(audio_path, model, args.lang)
                lang = winfo.language or lang
                meta["title"] = info.get("title") or Path(audio_path).stem
                meta["url"] = real_url
                meta["duration"] = info.get("duration")
                meta["author"] = info.get("uploader") or ""
                meta["engine"] = "faster-whisper(%s, %s/%s)" % (args.model, device, compute)
                if not args.keep_audio:
                    try:
                        os.remove(audio_path)
                    except OSError:
                        pass
        else:
            raise ValueError("输入既不是链接/BV号，也不是存在的本地文件: %s" % src)

    base = out_dir / sanitize_filename(meta.get("title", "transcript"))
    files = write_outputs(base, segs, lang, args, meta)
    written.extend(files)
    log("  [完成] 耗时%.1f秒  输出: %s" %
        (time.time() - t0, ", ".join(str(f.name) for f in files)))
    return written


def main():
    setup_console()
    if len(sys.argv) >= 2 and sys.argv[1] == "login":
        do_login()
        return
    parser = argparse.ArgumentParser(
        prog="v2t",
        description="视频转文字: B站/YouTube/抖音等平台链接、BV号或本地文件 -> 文字稿",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  python v2t.py login                       (扫码登录B站, 开启AI字幕秒出)
  python v2t.py https://www.bilibili.com/video/BV1xxxx
  python v2t.py BV1xxxx --srt --md
  python v2t.py https://b23.tv/xxxxx -m medium
  python v2t.py 本地视频.mp4 --timestamps
  python v2t.py 链接1 链接2 BV号        (批量)
模型: tiny(快) < base < small(默认,推荐) < medium < large-v3(最准最慢)""")
    parser.add_argument("inputs", nargs="+", help="链接 / BV号 / 本地文件(可多个)")
    parser.add_argument("-o", "--output", default="transcripts", help="输出目录(默认 transcripts)")
    parser.add_argument("-m", "--model", default="small",
                        help="whisper模型: tiny/base/small/medium/large-v3 (默认 small)")
    parser.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"],
                        help="运行设备 (默认 auto: 有CUDA环境就用GPU, 否则CPU)")
    parser.add_argument("--no-gpu", action="store_true", help="强制不用GPU")
    parser.add_argument("--lang", default="zh", help="识别语言 zh/en/auto... (默认 zh)")
    parser.add_argument("--page", type=int, default=1,
                        help="B站多P视频选第几P (默认1, 0=全部P)")
    parser.add_argument("--srt", action="store_true", help="同时输出SRT字幕文件")
    parser.add_argument("--md", action="store_true", help="同时输出带时间戳的Markdown")
    parser.add_argument("--timestamps", action="store_true", help="txt中也带[时间戳]前缀")
    parser.add_argument("--summary", action="store_true",
                        help="额外生成摘要文件(本地TextRank, 关键词+一句话+要点)")
    parser.add_argument("--cookies", default=None,
                        help="cookies.txt路径(浏览器导出), 用于需要登录的视频/AI字幕")
    parser.add_argument("--keep-audio", action="store_true", help="保留下载的音频文件")
    args = parser.parse_args()

    out_dir = Path(args.output).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    ok, fail = 0, []
    for src in args.inputs:
        try:
            process_one(src, args, out_dir)
            ok += 1
        except KeyboardInterrupt:
            log("已中断")
            sys.exit(130)
        except Exception as e:
            fail.append((src, str(e)))
            log("  [失败] %s" % e)

    log("=" * 60)
    log("全部完成: 成功 %d 个, 失败 %d 个, 输出目录: %s" % (ok, len(fail), out_dir))
    if fail:
        for src, err in fail:
            log("  失败: %s -> %s" % (src, err))
        sys.exit(1)


if __name__ == "__main__":
    main()
