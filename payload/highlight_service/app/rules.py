from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from typing import Any


RULE_VERSION = "commerce-video-rules-2026-08-11-v3"


@dataclass(slots=True)
class Clause:
    id: str
    start: float
    end: float
    text: str
    confidence: float = 0.0
    hard_hits: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["hard_hits"] = list(self.hard_hits)
        return result


# These are deliberately conservative. A hit removes the whole spoken clause;
# the model never gets permission to keep a partial prohibited sales sentence.
HARD_PATTERNS: dict[str, tuple[str, ...]] = {
    "开播留人/憋单": (
        r"刚开播|开播福利|停留一下|别走|留下来|抓紧进来|在线的|来晚了|马上开价|憋单",
    ),
    "价格/优惠券": (
        r"(?:到手|原价|现价|直播价|粉丝价|价格)[^，。！？]{0,12}(?:元|块|钱)",
        r"(?:\d+(?:\.\d+)?|[一二三四五六七八九十百千万两]+)\s*(?:元|块|毛)(?:券)?|优惠券|领.{0,6}券|发券|红包|满减|补贴|便宜",
        r"关注.{0,10}(?:价格|价)|没关注.{0,10}(?:价格|价)",
    ),
    "链接/上架引导": (
        r"[一二三四五六七八九十\d]+号链接|上链接|上车|拍下|去下单|购物车|小黄车|链接里|库存",
        r"助播.{0,8}(?:改价|上架|放价)|改价格|改价",
    ),
    "倒计时/催单": (
        r"倒计时|最后\d+|只剩|手慢无|抢完|秒杀|马上恢复|抓紧拍|赶紧拍|现在拍",
    ),
    "尺码/身高体重": (
        r"身高|体重|斤|公斤|千克|尺码|码数|几码|[SMLX]{1,4}\s*码|报身高|报体重",
    ),
    "直播互动": (
        r"点关注|没点关注|关注主播|点点赞|点赞|评论区|扣\s*[一二三四五六七八九十\d]|公屏|欢迎.{0,8}进来",
    ),
    "绝对化/无法证明": (
        r"全网(?:最低|第一|唯一)|绝对|百分之百|100%|最(?:好|高|低|便宜|舒服|显瘦|透气)|第一名|国家级|世界级|唯一|顶级|天花板",
        r"永远|完全不会|肯定不会|零风险|包治|治愈|根治",
    ),
    "名人/公众人物": (
        r"明星|影帝|影后|顶流|同款|代言人|国家队|奥运冠军",
    ),
}

# These are ordinary product claims in live commerce. They are allowed into a
# candidate, but surfaced to the reviewer rather than treated as platform-redline
# words. The operator remains responsible for matching them to the actual item.
REVIEW_PATTERNS: dict[str, tuple[str, ...]] = {
    "商品功效需核对": (
        r"不掉色|不褪色|不缩水|不起球|不沾毛|不粘毛|不变形|不勒(?:肚子|裆)",
        r"\d+\s*(?:分钟|小时).{0,6}(?:干|速干)|一个鸡蛋.{0,4}(?:重|重量)",
    ),
}


def hard_rule_hits(text: str) -> list[str]:
    normalized = re.sub(r"\s+", "", text or "")
    hits: list[str] = []
    for category, patterns in HARD_PATTERNS.items():
        if any(re.search(pattern, normalized, re.I) for pattern in patterns):
            hits.append(category)
    return hits


def review_rule_hits(text: str) -> list[str]:
    normalized = re.sub(r"\s+", "", text or "")
    return [category for category, patterns in REVIEW_PATTERNS.items() if any(re.search(pattern, normalized, re.I) for pattern in patterns)]


def _split_text(text: str) -> list[str]:
    pieces = re.findall(r"[^，。！？；,!?;]+[，。！？；,!?;]?", text.strip())
    return [piece.strip() for piece in pieces if piece.strip()]


def _split_redline_fragments(text: str) -> list[str]:
    """Split a mixed sentence around exact local redline matches.

    This preserves useful product wording before/after a prohibited phrase. The
    resulting timestamps are assigned proportionally by ``build_clauses``;
    GPT still decides whether the surviving fragments are semantically usable.
    """
    fragments: list[str] = []
    for piece in _split_text(text) or [text]:
        intervals: list[tuple[int, int]] = []
        for patterns in HARD_PATTERNS.values():
            for pattern in patterns:
                intervals.extend((match.start(), match.end()) for match in re.finditer(pattern, piece, re.I))
        if not intervals:
            fragments.append(piece)
            continue
        merged: list[list[int]] = []
        for start, end in sorted(intervals):
            if merged and start < merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        cursor = 0
        for start, end in merged:
            if start > cursor and piece[cursor:start].strip(" ，。！？；,!?;"):
                fragments.append(piece[cursor:start].strip())
            if piece[start:end].strip():
                fragments.append(piece[start:end].strip())
            cursor = end
        if cursor < len(piece) and piece[cursor:].strip(" ，。！？；,!?;"):
            fragments.append(piece[cursor:].strip())
    return fragments


def build_clauses(spans: list[dict[str, Any]]) -> list[Clause]:
    clauses: list[Clause] = []
    sequence = 1
    for span in spans:
        start = float(span["start_time"])
        end = float(span["end_time"])
        text = str(span.get("text") or "").strip()
        if not text or end <= start:
            continue
        pieces = _split_redline_fragments(text) or [text]
        weights = [max(1, len(re.sub(r"[，。！？；,!?;\s]", "", piece))) for piece in pieces]
        total = sum(weights)
        cursor = start
        for index, (piece, weight) in enumerate(zip(pieces, weights)):
            piece_end = end if index == len(pieces) - 1 else cursor + (end - start) * weight / total
            hits = tuple(hard_rule_hits(piece))
            clauses.append(Clause(
                id=f"S{sequence:04d}", start=round(cursor, 3), end=round(piece_end, 3),
                text=piece, confidence=float(span.get("confidence") or 0), hard_hits=hits,
            ))
            sequence += 1
            cursor = piece_end
    return clauses


def clauses_json(clauses: list[Clause]) -> str:
    return json.dumps([clause.to_dict() for clause in clauses], ensure_ascii=False)
