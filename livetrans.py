# -*- coding: utf-8 -*-
"""边播边转: 捕获系统声音(WASAPI回环) + Silero VAD 分句 + 流式识别。

不下载任何视频——用户在任意播放器/网页里播放, 本地实时转文字。
"""

import threading
import time

import numpy as np

_SR_IN = 48000
_SR = 16000
_MARGIN = 0.35      # 句尾静音确认窗口(秒)
_MAX_UTT = 6.0      # 滚动切分长度: 连续语音每~6秒出一段, 保证近实时
_POLL = 0.22        # 工作循环间隔(秒)


class LiveTranscriber(object):
    def __init__(self, model_size, on_segment, on_status, lang="auto"):
        self.model_size = model_size
        self.on_segment = on_segment
        self.on_status = on_status
        self.lang = lang
        self._buf = np.zeros(0, dtype="float32")
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._last_idx = 0        # 已转写到的样本位置
        self._threads = []
        self.error = None

    # ------------------------------------------------ 采集

    def _record(self):
        import soundcard as sc
        spk = sc.default_speaker()
        mic = sc.get_microphone(spk.id, include_loopback=True)
        self.on_status("live_device:" + spk.name)
        with mic.recorder(samplerate=_SR_IN, blocksize=4800) as rec:
            while not self._stop.is_set():
                data = rec.record(numframes=4800)
                mono = data.mean(axis=1).astype("float32")
                # 48k -> 16k: 3点均值抽取(带简易低通)
                dec = mono.reshape(-1, 3).mean(axis=1).astype("float32")
                if self._paused.is_set():
                    # 暂停期间丢弃音频, 但推进静音占位保持时间轴连续
                    dec = np.zeros_like(dec)
                with self._lock:
                    self._buf = np.concatenate([self._buf, dec])

    # ------------------------------------------------ 识别循环

    def _worker(self):
        import v2t
        from faster_whisper.vad import get_speech_timestamps, VadOptions
        model, device, compute = v2t.get_whisper_model(self.model_size,
                                                       "auto", True)
        self.on_status("识别引擎: faster-whisper(%s, %s/%s)"
                       % (self.model_size, device, compute))
        vad_opt = VadOptions(min_silence_duration_ms=400,
                             speech_pad_ms=120)
        while not self._stop.is_set():
            time.sleep(_POLL)
            try:
                with self._lock:
                    total = len(self._buf)
                    tail_from = max(0, self._last_idx - int(0.3 * _SR))
                    audio = self._buf[tail_from:].copy()
                if len(audio) < _SR // 2:
                    continue
                chunks = get_speech_timestamps(audio, vad_opt)
                if not chunks:
                    continue
                base = tail_from
                limit = total - int(_MARGIN * _SR)
                for ch in chunks:
                    s, e = base + ch["start"], base + ch["end"]
                    dur = (e - s) / _SR
                    if dur < 0.45:
                        self._last_idx = max(self._last_idx, e)
                        continue
                    # 仍在进行中的语句: 短句等静音收口, 长句滚动切分保实时
                    if e > limit and dur < 3.5:
                        continue
                    if dur > _MAX_UTT:
                        e = min(e, s + int(_MAX_UTT * _SR))
                    with self._lock:
                        seg_audio = self._buf[s:e].copy()
                    iterator, info = model.transcribe(
                        seg_audio, language=None if self.lang == "auto"
                        else self.lang, vad_filter=False, beam_size=1,
                        condition_on_previous_text=False)
                    text = " ".join(x.text.strip() for x in iterator
                                    ).strip()
                    self._last_idx = max(self._last_idx, e)
                    if text:
                        self.on_segment(s / _SR, e / _SR, text)
            except Exception as ex:
                self.error = str(ex)
                self.on_status("实时识别异常: %s" % str(ex)[:80])
                time.sleep(1)

    # ------------------------------------------------ 控制

    def start(self):
        self._threads = [threading.Thread(target=self._record, daemon=True),
                         threading.Thread(target=self._worker, daemon=True)]
        for t in self._threads:
            t.start()

    def pause(self):
        self._paused.set()
        self.on_status("已暂停收音")

    def resume(self):
        self._paused.clear()
        self.on_status("继续收音中")

    def elapsed(self):
        with self._lock:
            return len(self._buf) / _SR

    def stop(self):
        self._stop.set()
        for t in self._threads:
            t.join(timeout=3)
        return self.elapsed()
