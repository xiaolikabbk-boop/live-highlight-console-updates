from __future__ import annotations

import base64
import difflib
import json
import re
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from .config import Settings
from .rules import Clause, RULE_VERSION, build_clauses, review_rule_hits
from .text_normalize import simplify_value, to_simplified


ANALYSIS_VERSION = "clause-select-v3-5min"
PROMPT_VERSION = "live-to-short-video-2026-08-11-v4-5min"

SYSTEM_PROMPT = """你是直播录屏转商品短视频的内容剪辑导演。你的任务不是寻找一段连续的直播话术，而是逐句判断哪些原话适合独立发布为商品短视频。所有返回文字必须使用简体中文，禁止输出繁体字。

只保留：产品身份、设计/版型、面料/材质、工艺、功能、穿着感受、适合人群、使用场景，以及脱离直播间后仍成立的具体产品卖点。

必须删除：开播留人和憋单；价格、券、补贴；链接、购物车、上架和助播改价；倒计时和库存催单；身高体重及尺码推荐；点赞关注评论等互动；明星或公众人物姓名；“全网最低、第一、唯一、国家级、世界级、百分之百、绝对、永远、完全不会”等平台高风险绝对词；医疗治疗功效。输入里标为 hard_removed 的短语绝不可选择，但要结合它前后的干净片段判断是否仍有可用产品内容。

可以保留行业常见的商品卖点表达，例如显瘦、凉快、速干、不掉色、不起球、不沾毛、不勒、一个鸡蛋重量等。不要仅因它略带宣传性就删除；这类内容交给人工审核核对商品真实性，除非句中同时出现上述平台红线词。

品牌历史、门店数量、工艺设备、成本和竞品比较不由本地规则预先删除。你要从“能否独立成为商品短视频内容、是否具体可信、是否过度跑题”的角度判断；有产品价值可以保留，只有纯背书吹嘘、贬低竞品或无法形成产品表达时才删除。

按句子 ID 选择，不要估计秒数。输入最多覆盖 5 分钟，其中可能切换商品。每条候选只能取自同一商品，在该输入中删掉无用句，再按原顺序拼成 15–20 秒；不得改序，不得跨商品，不得为了凑时长留下违规或空话。某个主题内容不足 15 秒就不输出。

不要只选“最好的几条”。请完整寻找输入中所有互不重复、可独立发布的有效主题；只要质量合格就输出，数量可以是 0 条，也可以有多条。同一卖点换一种近似说法不重复输出，但不同卖点、不同适合人群或不同商品应分别输出。

严格质量要求：不要保留重复卖点；不要保留“对、是的、要不要、是不是、而且给你做什么”等依赖直播上下文的残句或问句；不要保留“天气越来越热”等没有产品信息的句子；不要保留明显转写错误、语义不通、主谓宾缺失的句子。若纠错后才能理解，可在 caption_corrections 中只修正明显同音字/识别错误，不能改写主播含义或编造录音中没有的话。无法可靠纠正就删除。

剪辑限制：所选句子必须自然聚合成 1–5 个连续保留区间，相邻保留句时间间隔不超过 0.45 秒才算同一区间；超过即算新的一刀。严禁超过 5 刀。优先选择连续、信息密集的完整表达，不要用短碎句补时长。

只返回 JSON：
{"candidates":[{"theme":"主题","keep_clause_ids":["S0001"],"caption_corrections":{"S0001":"纠正后的原意文字"},"sales_score":0.0,"coherence_score":0.0,"confidence":0.0,"reason":"选择理由","risks":[]}]}
最多 10 条作为接口安全上限；没有合格内容返回 {"candidates":[]}。"""

VISION_PROMPT = """这些图片按成片的源时间顺序排列。只判断是否仍是同一类商品；换颜色、姿势或镜头远近不算换商品。只返回 JSON：{"same_product":true,"confidence":0.0,"reason":"理由"}。"""


class AIUnavailable(RuntimeError):
    """Provider is not configured or temporarily unavailable; caller should retry later."""


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            raise
        return json.loads(match.group(0))


class OpenAICompatibleClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def text_enabled(self) -> bool:
        return bool(self.settings.ai_base_url and self.settings.ai_api_key and self.settings.ai_model)

    @property
    def vision_enabled(self) -> bool:
        return bool(self.text_enabled and self.settings.ai_vision_enabled)

    @staticmethod
    def _response_text(data: dict[str, Any]) -> str:
        if isinstance(data.get("output_text"), str):
            return data["output_text"]
        for output in data.get("output") or []:
            for content in output.get("content") or []:
                if isinstance(content.get("text"), str):
                    return content["text"]
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("模型响应中没有可读取的文本") from exc

    def _post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = self.settings.ai_base_url.rstrip("/") + endpoint
        headers = {"Authorization": f"Bearer {self.settings.ai_api_key}", "Content-Type": "application/json"}
        retryable = {408, 409, 429, 500, 502, 503, 504}
        last_error: Exception | None = None
        for attempt in range(self.settings.ai_max_attempts):
            try:
                with httpx.Client(timeout=self.settings.ai_timeout_seconds) as client:
                    response = client.post(url, headers=headers, json=payload)
                if response.status_code in retryable:
                    raise AIUnavailable(f"中转站暂时不可用（HTTP {response.status_code}）")
                response.raise_for_status()
                return response.json()
            # RequestError also covers RemoteProtocolError (the upstream server
            # closed the connection without a response), TLS failures and the
            # other transport errors that used to become permanent segment
            # failures after a single attempt.
            except (httpx.RequestError, json.JSONDecodeError, AIUnavailable) as exc:
                last_error = exc
                if attempt + 1 < self.settings.ai_max_attempts:
                    time.sleep(min(8, 2 ** attempt))
                    continue
                raise AIUnavailable(str(last_error)) from last_error
            except httpx.HTTPStatusError as exc:
                raise AIUnavailable(f"中转站请求失败（HTTP {exc.response.status_code}）：{exc.response.text[:300]}") from exc
        raise AIUnavailable(str(last_error or "中转站请求失败"))

    def _json_request(self, messages: list[dict[str, Any]], model: str | None = None) -> dict[str, Any]:
        model = model or self.settings.ai_model
        protocol = self.settings.ai_protocol.lower()
        errors: list[str] = []
        if protocol in {"auto", "responses"}:
            try:
                return self._parsed_post(
                    "/responses", {"model": model, "input": messages, "temperature": 0.1}
                )
            except AIUnavailable as exc:
                errors.append(str(exc))
                if protocol == "responses" or not any(code in str(exc) for code in ("400", "404", "405", "422")):
                    raise
        if protocol in {"auto", "chat"}:
            payload: dict[str, Any] = {
                "model": model, "messages": messages,
                "response_format": {"type": "json_object"},
                "max_tokens": self.settings.ai_max_output_tokens,
            }
            if self.settings.ai_thinking_mode:
                payload["thinking"] = {"type": self.settings.ai_thinking_mode}
            if self.settings.ai_thinking_mode != "enabled":
                payload["temperature"] = 0.1
            return self._parsed_post("/chat/completions", payload)
        raise AIUnavailable("不支持的 AI 协议配置：" + protocol + "; ".join(errors))

    def _parsed_post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Retry empty, truncated and non-JSON model responses as transient failures."""
        last_error: Exception | None = None
        for attempt in range(self.settings.ai_max_attempts):
            try:
                data = self._post(endpoint, payload)
                return _extract_json(self._response_text(data))
            except (json.JSONDecodeError, ValueError, TypeError, KeyError, IndexError) as exc:
                last_error = exc
                if attempt + 1 < self.settings.ai_max_attempts:
                    time.sleep(min(8, 2 ** attempt))
                    continue
        raise AIUnavailable(
            "模型连续返回空内容、截断内容或无效 JSON，任务已保留等待重试："
            + str(last_error or "无法解析模型响应")
        ) from last_error

    def analyze_clauses(self, clauses: list[Clause], task_instruction: str = "") -> list[dict[str, Any]]:
        if not self.text_enabled:
            raise AIUnavailable("尚未配置中转站 Base URL、API Key 和模型，任务已保留等待")
        payload = [{
            "id": c.id, "start": c.start, "end": c.end, "duration": round(c.end - c.start, 3),
            "text": c.text, "hard_removed": bool(c.hard_hits), "hard_hits": list(c.hard_hits),
        } for c in clauses]
        user_text = "逐句筛选以下转写：\n" + json.dumps(payload, ensure_ascii=False)
        if task_instruction:
            user_text = task_instruction + "\n\n" + user_text
        result = simplify_value(self._json_request([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ]))
        return list(result.get("candidates") or [])

    def analyze_frames(self, frames: list[Path]) -> dict[str, Any]:
        if not self.vision_enabled:
            raise AIUnavailable("线上视觉未启用")
        content: list[dict[str, Any]] = [{"type": "text", "text": VISION_PROMPT}]
        for path in frames:
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}", "detail": "low"}})
        return self._json_request([{"role": "user", "content": content}], self.settings.ai_vision_model or self.settings.ai_model)


class CandidateAnalyzer:
    def __init__(self, settings: Settings, analysis_label: str = ""):
        self.settings = settings
        self.cloud = OpenAICompatibleClient(settings)
        suffix = f"-{analysis_label}" if analysis_label else ""
        self.analysis_version = ANALYSIS_VERSION + suffix
        self.prompt_version = PROMPT_VERSION

    def analyze(
        self, spans: list[dict[str, Any]], excluded_ranges: list[dict[str, float]] | None = None,
        on_submit: Callable[[], None] | None = None,
    ) -> list[dict[str, Any]]:
        clauses = build_clauses(spans)
        if not clauses:
            return []
        excluded_ranges = excluded_ranges or []
        if on_submit:
            on_submit()
        if excluded_ranges:
            clauses = [
                Clause(
                    id=clause.id, start=clause.start, end=clause.end, text=clause.text,
                    confidence=clause.confidence,
                    hard_hits=clause.hard_hits + (("GPT主选已采用",) if any(
                        min(clause.end, float(item["end"])) - max(clause.start, float(item["start"])) > 0.05
                        for item in excluded_ranges
                    ) else ()),
                )
                for clause in clauses
            ]
            raw = self.cloud.analyze_clauses(
                clauses,
                "这是补漏任务。标为 GPT主选已采用 的句子已经生成过候选，绝不可再次选择。"
                "只从其余句子中寻找新的可用片段；没有新增内容就返回 0 条。",
            )
        else:
            raw = self.cloud.analyze_clauses(clauses)
        return self._sanitize(raw, clauses)

    @staticmethod
    def _unit(value: Any) -> float:
        try:
            return round(max(0.0, min(1.0, float(value))), 3)
        except (TypeError, ValueError):
            return 0.0

    def _sanitize(self, candidates: list[dict[str, Any]], clauses: list[Clause]) -> list[dict[str, Any]]:
        by_id = {clause.id: clause for clause in clauses}
        clean: list[dict[str, Any]] = []
        for item in candidates[:self.settings.max_candidates_per_window]:
            ids = [str(value) for value in item.get("keep_clause_ids") or []]
            selected = [by_id[value] for value in ids if value in by_id and not by_id[value].hard_hits]
            selected = sorted({c.id: c for c in selected}.values(), key=lambda c: c.start)
            if not selected or selected[-1].end - selected[0].start > self.settings.max_source_span_seconds:
                continue
            ranges: list[dict[str, Any]] = []
            for clause in selected:
                if ranges and clause.start - ranges[-1]["end"] <= self.settings.range_merge_gap_seconds:
                    ranges[-1]["end"] = clause.end
                    ranges[-1]["clause_ids"].append(clause.id)
                else:
                    ranges.append({"start": clause.start, "end": clause.end, "clause_ids": [clause.id]})
            if len(ranges) > self.settings.max_source_ranges:
                continue
            duration = sum(float(r["end"]) - float(r["start"]) for r in ranges)
            if not self.settings.clip_min_seconds <= duration <= self.settings.clip_max_seconds:
                continue
            corrections = item.get("caption_corrections") or {}
            correction_risks: list[str] = []
            safe_corrections: dict[str, str] = {}
            for clause in selected:
                proposed = str(corrections.get(clause.id) or "").strip()
                if not proposed:
                    continue
                original = re.sub(r"\s+", "", clause.text)
                corrected = re.sub(r"\s+", "", proposed)
                ratio = difflib.SequenceMatcher(None, original, corrected).ratio()
                length_ratio = len(corrected) / max(1, len(original))
                if ratio >= 0.55 and 0.55 <= length_ratio <= 1.5:
                    safe_corrections[clause.id] = proposed
                else:
                    correction_risks.append(f"已拒绝 {clause.id} 的大幅字幕改写")
            kept = [{
                **clause.to_dict(),
                "caption_text": safe_corrections.get(clause.id, clause.text),
            } for clause in selected]
            selected_ids = {c.id for c in selected}
            removed = [c.to_dict() for c in clauses if c.id not in selected_ids and selected[0].start <= c.start <= selected[-1].end]
            hits = sorted({
                hit for clause in removed for hit in clause.get("hard_hits", [])
                if hit != "GPT主选已采用"
            })
            review_hits = sorted({hit for clause in selected for hit in review_rule_hits(clause.text)})
            clean.append({
                "start": ranges[0]["start"], "end": ranges[-1]["end"], "source_ranges": ranges,
                "kept_clauses": kept, "removed_clauses": removed, "compliance_hits": hits,
                "sales_score": self._unit(item.get("sales_score")),
                "coherence_score": self._unit(item.get("coherence_score")),
                "confidence": self._unit(item.get("confidence")),
                "reason": to_simplified(str(item.get("reason") or "模型选择了可独立发布的产品讲解")),
                "risks": [to_simplified(str(value)) for value in item.get("risks") or []] + correction_risks + review_hits,
                "analysis_version": self.analysis_version, "prompt_version": self.prompt_version, "rule_version": RULE_VERSION,
            })
        return clean
