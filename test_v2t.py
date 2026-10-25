# -*- coding: utf-8 -*-
"""mock测试: B站CC字幕解析 + 三种输出格式的正确性。"""
import argparse
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import v2t


class FakeResp(object):
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p

    def raise_for_status(self):
        pass


body_payload = {"body": [
    {"from": 1.0, "to": 3.5, "content": "大家好，今天我们来讲一个很有意思的话题。"},
    {"from": 3.6, "to": 6.2, "content": "首先我们要理解基本概念。"},
    {"from": 6.3, "to": 9.0, "content": "然后一步步推导出结论。"},
]}


def run_with(subtitles):
    """mock字幕列表请求，记录实际抓取的字幕文件URL。"""
    fetched = []

    def fake_get(url, **kw):
        fetched.append(url)
        if "player" in url:
            return FakeResp({"code": 0, "data": {"subtitle": {"subtitles": subtitles}}})
        return FakeResp(body_payload)

    v2t.http_get = fake_get
    return fetched


# 1. 正常解析
run_with([{"lan": "zh-CN", "lan_doc": "中文", "subtitle_url": "//aisubtitle.hdslb.com/zh.json"}])
segs = v2t.fetch_bili_cc_subtitles("BV1TESTTEST", 12345, "SESSDATA=fake")
assert segs and len(segs) == 3, "字幕解析失败: %s" % segs
assert segs[0]["text"] == "大家好，今天我们来讲一个很有意思的话题。"
assert abs(segs[2]["end"] - 9.0) < 0.01
print("PASS fetch_bili_cc_subtitles: 3段字幕解析正确")

# 2. 多语言时应优先中文字幕(含 ai-zh 这种非zh开头的语言码)
fetched = run_with([
    {"lan": "ai-en", "lan_doc": "English", "subtitle_url": "//x/en.json"},
    {"lan": "ai-zh", "lan_doc": "中文", "subtitle_url": "//x/zh.json"},
])
segs2 = v2t.fetch_bili_cc_subtitles("BV1TESTTEST", 12345, "")
assert any("zh.json" in u for u in fetched), "未优先选择中文字幕, 抓取的URL: %s" % fetched
print("PASS 语言优先级: 中文字幕优先选择(含ai-zh)")

# 3. 无字幕时返回 None
run_with([])
assert v2t.fetch_bili_cc_subtitles("BV1TESTTEST", 12345, "") is None
print("PASS 无字幕场景: 正确返回None")

# 4. 输出格式
v2t.http_get = lambda url, **kw: FakeResp({"code": 0, "data": {"subtitle": {"subtitles": []}}})
args = argparse.Namespace(srt=True, md=True, timestamps=False)
base = Path(r"D:\文案摘要\video2text\_mock_out\测试视频")
base.parent.mkdir(parents=True, exist_ok=True)
meta = {"title": "测试视频", "url": "https://www.bilibili.com/video/BV1TESTTEST",
        "author": "测试UP", "duration": 9, "engine": "B站CC字幕"}
files = v2t.write_outputs(base, segs, "zh", args, meta)
txt = base.with_suffix(".txt").read_text(encoding="utf-8")
srt = base.with_suffix(".srt").read_text(encoding="utf-8")
md = base.with_suffix(".md").read_text(encoding="utf-8")
assert "大家好" in txt and "推导出结论" in txt
assert "\n" not in txt, "中文纯文本应连排不换行"
assert "00:00:01,000 --> 00:00:03,500" in srt
assert "# 测试视频" in md and "- 来源:" in md and "[00:01]" in md
print("PASS write_outputs: txt/srt/md 三种格式内容正确")

# 5. 路径穿越防护
out_base = Path(r"D:\文案摘要\video2text\_mock_out")
p_ok = v2t.safe_output_path(out_base, "BV1xx.m4a")
assert str(p_ok).startswith(str(out_base.resolve()))
try:
    v2t.safe_output_path(out_base, "..\\..\\evil.txt")
    raise AssertionError("路径穿越未被拦截")
except ValueError:
    pass
print("PASS safe_output_path: 路径穿越被正确拦截")

# 6. 本地摘要(TextRank)
import summarizer

demo = [{"start": i * 4.0, "end": i * 4.0 + 3.5,
         "text": t} for i, t in enumerate([
    "大家好今天我们来聊聊人工智能在教育领域的应用前景",
    "人工智能可以个性化地分析每个学生的学习情况",
    "通过大数据分析系统能够发现学生的知识薄弱环节",
    "个性化学习路径是人工智能教育最重要的价值",
    "当然人工智能教育也面临着隐私和公平性的挑战",
    "未来人工智能和老师的配合会越来越紧密",
    "老师可以把重复性工作交给机器自己专注育人",
    "总之人工智能教育的核心是因材施教",
    "希望大家理性看待技术的价值与边界",
    "谢谢大家观看我们下期再见",
])]
r = summarizer.summarize(demo)
assert r, "摘要生成失败"
assert r["headline"] and isinstance(r["keywords"], list) and r["keywords"]
assert len(r["points"]) >= 3
starts = [p["start"] for p in r["points"]]
assert starts == sorted(starts), "要点未按时间排序"
print("PASS summarize: 一句摘要/关键词/时间排序要点正常, 关键词: %s"
      % "、".join(r["keywords"][:5]))

music = [{"start": i * 3.0, "end": i * 3.0 + 2.5, "text": "♪ 音乐 ♪"}
         for i in range(20)]
assert summarizer.summarize(music) is None, "纯音乐不应生成摘要"
print("PASS summarize: 纯音乐内容正确返回None")

print("\n全部PASS")
