# Btchar

**Video-to-text desktop app** — watch the video on the left, watch the transcript
appear in real time on the right.

Supports Bilibili / YouTube / Douyin and any other site supported by
[yt-dlp](https://github.com/yt-dlp/yt-dlp), plus local video files.
Transcription runs **fully locally** — your audio never leaves your machine.

**Btchar** = **B**ili**b**ili **t**o **char**acters.

> 📖 中文说明见 [README.md](README.md)

## Features

- 🎬 **Split view** — video player on the left, live transcript on the right;
  click any line to seek; the current line auto-highlights during playback
- ⚡ **Subtitles first** — grabs Bilibili AI/CC subtitles when available
  (after QR login) for instant, most accurate results
- 🧠 **Local speech recognition fallback** —
  [faster-whisper](https://github.com/SYSTRAN/faster-whisper) with automatic
  language detection and GPU acceleration (int8, ~60× real-time on an RTX 5060);
  model selector from tiny to large-v3
- ⏸ **Pause & resume** — pause mid-transcription, keep partial results, resume
  from the break point (cached video, no re-download)
- 📁 **Four export formats** — plain TXT / timestamped TXT / SRT / MD,
  saved via the system save dialog to any path you choose
- 🔤 Timestamp display toggle for the transcript
- ✨ **One-click AI summary** — local TextRank extracts keywords, a one-line
  summary and key-point timeline (click to seek); zero network requests
- 🎙 **Live listen-and-transcribe** — skip downloading entirely: press the
  🎙 button and play anything (web videos, local players, livestreams);
  system audio is captured via WASAPI loopback, VAD-segmented and
  transcribed in real time; pause/stop with auto-save
- 🛡 **Subtitle integrity check** — Bilibili's subtitle CDN occasionally
  serves another video's subtitles; a duration + audio-content probe
  rejects dirty subtitles and falls back to local recognition
- 🍎 Apple-style UI in a native window (WebView2 via pywebview — not a browser)

## Install (Windows, Python 3.9+)

**📦 Quick download** (no git needed):
[Download latest ZIP](https://github.com/furongtao-7/Btchar/archive/refs/heads/main.zip),
extract anywhere, then continue below.

```bat
git clone https://github.com/furongtao-7/Btchar.git
cd Btchar
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

Notes:

- The speech model (small ≈ 460 MB) downloads automatically on first use;
  falls back to the hf-mirror.com mirror when huggingface.co is unreachable
- Optional GPU acceleration:
  `.venv\Scripts\pip install nvidia-cublas-cu12 nvidia-cudnn-cu12`
- No system ffmpeg needed (bundled via imageio-ffmpeg)

## Usage

Desktop app:

```bat
.venv\Scripts\pythonw app_native.py
```

Bilibili QR login (enables instant AI subtitles): click the login button in the
app, or run `v2t.bat login`.

CLI (batch / advanced):

```bat
v2t.bat https://www.bilibili.com/video/BV1GJ411x7h7 --srt --md
v2t.bat link1 link2 BV1GJ411x7h7
v2t.bat local_video.mp4 --timestamps
```

## Security design

- Local-only server bound to `127.0.0.1`
- SSRF protection: protocol/host/resolved-IP validation (private, loopback and
  link-local ranges rejected), per-hop redirect re-validation, thread-local
  DNS pinning against rebinding
- Command whitelist: only venv-bundled binaries (ffmpeg), list-form args, no shell
- Path-traversal protection on all externally influenced file paths
- Origin/Host check on write endpoints (CSRF)
- Bilibili credentials stay in a local git-ignored file

## Tests

```bat
.venv\Scripts\python test_v2t.py
```

## License

[CC BY-NC 4.0](LICENSE) — **non-commercial use only**.
Commercial use requires permission from the repository owner.
