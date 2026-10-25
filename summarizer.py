# -*- coding: utf-8 -*-
"""本地抽取式摘要: jieba分词 + TextRank 句子排序, 无网络请求。

输入转录段 [{start,end,text}], 输出:
- headline: 一句话摘要(排名最高的句子)
- keywords: 关键词列表
- points: 要点[{start,text}] 按时间排序(点击可跳转视频)
"""

import math
import re

_STOPWORDS = set("""
的 了 和 是 就 都 而 及 与 着 或 一个 没有 我们 你们 他们 它 这 那 这个 那个
什么 怎么 为什么 因为 所以 但是 而且 如果 虽然 就是 还是 不过 然后 于是 可以
这样 那样 一下 一些 比如 例如 其实 大家 觉得 知道 说到 谈到 现在 现在 可能
应该 需要 能够 对于 关于 通过 进行 成为 使得 让 给 把 被 从 到 在 对 以 向
一 二 三 四 五 六 七 八 九 十 很 非常 特别 比较 挺 更 最 太 有点 有些 一些
啊 吧 呢 吗 哦 嗯 呀 哈 呃 咳 哦哦 好的 好吧 然后 然后 然后 就是说 也就是说
""".split())

_MUSIC_MARK = re.compile(r"[♪♫♩音樂乐]{2,}|^[♪♫♩\s]+$")

# 虚词字符: 含这些字的bigram基本是跨词噪音(的声/到了/我们...)
_FUNC_CHARS = set("的了是和与就在也被把让向往对从我们你他她它这那也很最又再"
                  "不没个之其或并且但如果因为所以于以而为则等啊吧呢吗呀嗯哦"
                  "说看听想去来会在能可以要should")

# 常见繁体字归一(关键词展示用)
_TRAD = {"聲": "声", "們": "们", "來": "来", "這": "这", "後": "后", "裡": "里",
         "東": "东", "車": "车", "長": "长", "發": "发", "會": "会", "學": "学",
         "經": "经", "動": "动", "現": "现", "點": "点", "電": "电", "見": "见",
         "氣": "气", "機": "机", "樂": "乐", "師": "师", "愛": "爱", "實": "实",
         "窩": "窝", "術": "术", "體": "体", "開": "开", "聽": "听", "說": "说",
         "讀": "读", "寫": "写", "問": "问", "關": "关", "於": "于", "內": "内"}


def _norm(w):
    return "".join(_TRAD.get(c, c) for c in w)


def _is_valid_sentence(text):
    t = text.strip()
    if len(t) < 6:
        return False
    if _MUSIC_MARK.search(t):
        return False
    # 去掉标点后仍需有实际文字
    return bool(re.sub(r"[\W\s_]", "", t))


def split_sentences(segs, max_gap=1.5, max_len=80):
    """把转录碎段合并成句子单元(标点/停顿/长度切分)。"""
    sentences, buf, buf_start = [], [], None
    for s in segs:
        text = str(s.get("text", "")).strip()
        if not text:
            continue
        if buf_start is None:
            buf_start = float(s["start"])
        buf.append(text)
        joined = "".join(buf)
        if (joined.rstrip()[-1:] in "。！？；.!?;" or len(joined) >= max_len
                or float(s["end"]) - buf_start > max_len * 4):
            sentences.append({"start": buf_start, "end": float(s["end"]),
                              "text": joined})
            buf, buf_start = [], None
    if buf:
        sentences.append({"start": buf_start, "end": segs[-1]["end"],
                          "text": "".join(buf)})
    return sentences


def _ngram_tokens(text):
    """无jieba时的内置分词: 英文单词 + 中文二元组(滤跨词虚词噪音)。"""
    tokens = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9]+", text)]
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        grams = [run[i:i + 2] for i in range(len(run) - 1)]
        tokens += [g for g in grams
                   if g[0] not in _FUNC_CHARS and g[1] not in _FUNC_CHARS]
    return tokens


def _tokenize(text):
    try:
        import jieba  # 可选依赖: 装了词更准, 没装用内置bigram
        raw = jieba.lcut(text)
    except ImportError:
        raw = _ngram_tokens(text)
    return [w for w in raw
            if len(w) > 1 and w not in _STOPWORDS
            and not re.match(r"^[\W\d_]+$", w)]


def summarize(segs, top_points=6, top_keywords=8):
    """TextRank抽取式摘要。segs为空或有效句不足时返回None。"""
    sentences = [s for s in split_sentences(segs) if _is_valid_sentence(s["text"])]
    if len(sentences) < 3:
        return None

    token_lists = [_tokenize(s["text"]) for s in sentences]
    if any(not t for t in token_lists):
        # 某句分词为空会导致相似度除零, 直接剔除
        pairs = [(s, t) for s, t in zip(sentences, token_lists) if t]
        sentences = [p[0] for p in pairs]
        token_lists = [p[1] for p in pairs]
    n = len(sentences)
    if n < 3:
        return None

    # 句子相似度图 (经典TextRank: 词重叠 / log(len_i)+log(len_j))
    weights = [[0.0] * n for _ in range(n)]
    for i in range(n):
        wi = set(token_lists[i])
        for j in range(i + 1, n):
            wj = set(token_lists[j])
            overlap = len(wi & wj)
            if overlap == 0:
                continue
            sim = overlap / (math.log(len(wi) + 1) + math.log(len(wj) + 1))
            weights[i][j] = weights[j][i] = sim

    # 迭代求TextRank
    rank = [1.0 / n] * n
    out_sum = [sum(row) for row in weights]
    for _round in range(40):
        new_rank = []
        for i in range(n):
            s = 0.0
            for j in range(n):
                if weights[j][i] > 0 and out_sum[j] > 0:
                    s += rank[j] * weights[j][i] / out_sum[j]
            new_rank.append(0.15 / n + 0.85 * s)
        rank = new_rank

    # 一句话摘要: 得分最高的句子
    best = max(range(n), key=lambda i: rank[i])
    # 要点: TopK句子按原时间排序
    order = sorted(range(n), key=lambda i: rank[i], reverse=True)
    points = [sentences[i] for i in sorted(order[:top_points],
                                           key=lambda i: sentences[i]["start"])]

    # 关键词: 加权词频(句子得分加权) + 繁简归一
    kw_score = {}
    for i, tokens in enumerate(token_lists):
        for w in tokens:
            key = _norm(w)
            kw_score[key] = kw_score.get(key, 0.0) + rank[i]
    keywords = [w for w, _ in sorted(kw_score.items(),
                                     key=lambda kv: kv[1], reverse=True)]
    # 去重: 互相包含, 以及跨词bigram(首字是已选词的末字/末字是已选词的首字)
    dedup = []
    for w in keywords:
        if len(w) == 2:
            cross = any(w[0] == k[-1] or w[1] == k[0] for k in dedup)
            if cross:
                continue
        if any(w in k or k in w for k in dedup):
            continue
        dedup.append(w)
        if len(dedup) >= top_keywords:
            break

    return {
        "headline": sentences[best]["text"],
        "headline_start": round(sentences[best]["start"], 2),
        "keywords": dedup,
        "points": [{"start": round(p["start"], 2), "text": p["text"]}
                   for p in points],
        "n_sentences": n,
    }
