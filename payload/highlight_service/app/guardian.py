from __future__ import annotations

import base64
import json
import platform
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from .config import Settings
from .db import Database, utc_now


GUARDIAN_SYSTEM_PROMPT = """你是“直播录制剪辑中控台”的运维管家。你只根据提供的本地状态快照和用户问题回答，不能编造未提供的信息。回答必须使用简体中文，先给结论，再给可执行建议。不要展示或索取API密钥、Cookie。涉及删除、舍弃、重试、暂停等操作时只能提出建议，真正操作由本地程序的确认按钮完成。只返回JSON：{"answer":"回答","severity":"ok|warning|error"}。"""


def _safe_json(value: str, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


class Guardian:
    def __init__(self, settings: Settings, db: Database, pipeline: Any):
        self.settings = settings
        self.db = db
        self.pipeline = pipeline

    def snapshot(self) -> dict[str, Any]:
        status_rows = self.db.all(
            "SELECT status,COUNT(*) AS count FROM recording_segments GROUP BY status ORDER BY status"
        )
        candidate_rows = self.db.all(
            "SELECT status,COUNT(*) AS count FROM highlight_candidates GROUP BY status ORDER BY status"
        )
        delayed = self.db.all(
            """SELECT s.id,s.room_id,s.status,s.ai_retry_count,s.ai_next_retry_at,
                      s.ai_last_failed_stage,s.ai_last_failed_model,s.error,s.updated_at,
                      COALESCE(r.sequence,'') AS room_sequence,COALESCE(r.name,s.source_id) AS room_name
               FROM recording_segments s LEFT JOIN live_rooms r ON r.id=s.room_id
               WHERE s.status IN ('ai_waiting','ai_retry_paused','ai_abandoned')
               ORDER BY s.ai_retry_count DESC,s.id LIMIT 100"""
        )
        recent_errors = self.db.all(
            """SELECT id,level,event_type,message,details_json,created_at FROM service_events
               WHERE level IN ('warning','error') ORDER BY id DESC LIMIT 80"""
        )
        render_failures = self.db.all(
            """SELECT c.id,c.room_id,c.source_id,c.session_id,c.status,c.render_phase,
                      c.render_started_at,c.render_worker,c.render_encoder,c.updated_at,
                      COALESCE(r.sequence,'') AS room_sequence,COALESCE(r.name,'') AS room_name
               FROM highlight_candidates c LEFT JOIN live_rooms r ON r.id=c.room_id
               WHERE c.status='render_error' ORDER BY c.updated_at DESC LIMIT 100"""
        )
        route = self.pipeline.ai_route_status()
        return {
            "generated_at": utc_now(),
            "version": self._version(),
            "recorder_running": bool(self.pipeline.recorder.running),
            "segment_status": {row["status"]: int(row["count"]) for row in status_rows},
            "candidate_status": {row["status"]: int(row["count"]) for row in candidate_rows},
            "ai_route": route,
            "problem_segments": delayed,
            "recent_errors": recent_errors,
            "render_failures": render_failures,
        }

    def _version(self) -> str:
        path = self.settings.service_root.parent / "VERSION"
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return "unknown"

    @staticmethod
    def _status_count(snapshot: dict[str, Any], *statuses: str) -> int:
        counts = snapshot["segment_status"]
        return sum(int(counts.get(status, 0)) for status in statuses)

    def local_answer(self, message: str, snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
        snapshot = snapshot or self.snapshot()
        text = message.strip()
        routes = snapshot["ai_route"]
        waiting_asr = self._status_count(snapshot, "discovered", "transcribing")
        waiting_ai = self._status_count(snapshot, "transcribed", "ai_waiting", "gpt_analyzing", "deepseek_analyzing")
        delayed = self._status_count(snapshot, "ai_waiting")
        paused = self._status_count(snapshot, "ai_retry_paused")
        abandoned = self._status_count(snapshot, "ai_abandoned")
        waiting_render = int(snapshot["candidate_status"].get("visual_review", 0))
        render_errors = int(snapshot["candidate_status"].get("render_error", 0))
        pending = int(snapshot["candidate_status"].get("pending_review", 0))
        if any(word in text for word in ("异常", "故障", "卡住", "为什么", "诊断")):
            problems: list[str] = []
            if routes.get("circuit_open"):
                problems.append(f"三条主力线路均已进入保护暂停，约 {max(1, int(routes['circuit_remaining_seconds']) // 60)} 分钟后恢复")
            else:
                protected = [item for item in routes.get("routes", []) if item.get("open")]
                if protected:
                    problems.append("部分线路暂时保护，其他主力仍在接续：" + "、".join(
                        str(item.get("label") or item.get("model")) for item in protected
                    ))
            if delayed:
                problems.append(f"{delayed} 个模型任务正在延迟重试")
            if paused:
                problems.append(f"{paused} 个模型任务因连续失败已暂停")
            if render_errors:
                problems.append(f"{render_errors} 个候选渲染异常")
            if not problems:
                problems.append("当前未发现熔断、暂停重试或渲染异常")
            return {
                "answer": "；".join(problems) + "。可以点击“导出诊断包”交给维护人员进一步检查。",
                "severity": "warning" if delayed or paused or render_errors or routes.get("circuit_open") else "ok",
            }
        if any(word in text for word in ("状态", "任务", "进度", "播报", "还有多少")):
            return {
                "answer": (
                    f"当前待转写 {waiting_asr} 个，待模型处理 {waiting_ai} 个，其中延迟重试 {delayed} 个、"
                    f"暂停 {paused} 个、已舍弃但可恢复 {abandoned} 个；待渲染 {waiting_render} 个，"
                    f"待人工审核 {pending} 个，渲染异常 {render_errors} 个。"
                ),
                "severity": "warning" if delayed or paused or render_errors else "ok",
            }
        return {
            "answer": "我可以查询当前状态、诊断异常、导出脱敏诊断包，并协助你确认继续重试或舍弃异常任务。",
            "severity": "ok",
        }

    def _remote_request(self, message: str, snapshot: dict[str, Any], image: tuple[bytes, str] | None = None) -> dict[str, Any]:
        base_url = (self.settings.guardian_ai_base_url or self.settings.ai_base_url).rstrip("/")
        api_key = self.settings.guardian_ai_api_key or self.settings.ai_api_key
        model = self.settings.guardian_ai_model or "gpt-5.5"
        if image:
            api_key = self.settings.guardian_vision_api_key or api_key
            model = self.settings.guardian_vision_model or model
        if not (base_url and api_key and model):
            raise RuntimeError("管家中转站模型尚未配置")
        # Keep remote context deliberately compact: the local diagnostic ZIP
        # contains the detailed history, while routine manager questions only
        # need counts and a few recent symptoms. This reduces cost and avoids
        # sending transcript/media details to the relay.
        compact_snapshot = {
            "generated_at": snapshot["generated_at"],
            "version": snapshot["version"],
            "recorder_running": snapshot["recorder_running"],
            "segment_status": snapshot["segment_status"],
            "candidate_status": snapshot["candidate_status"],
            "ai_route": snapshot["ai_route"],
            "problem_segments": [
                {
                    "id": row.get("id"), "room_sequence": row.get("room_sequence"),
                    "room_name": row.get("room_name"), "status": row.get("status"),
                    "retry_count": row.get("ai_retry_count"),
                    "next_retry_at": row.get("ai_next_retry_at"),
                    "failed_stage": row.get("ai_last_failed_stage"),
                    "failed_model": row.get("ai_last_failed_model"),
                }
                for row in snapshot["problem_segments"][:30]
            ],
            "recent_errors": [
                {
                    "type": row.get("event_type"),
                    "message": str(row.get("message") or "")[:300],
                    "created_at": row.get("created_at"),
                }
                for row in snapshot["recent_errors"][:12]
            ],
            "render_failures": [
                {key: row.get(key) for key in ("id", "room_sequence", "room_name", "render_phase", "render_worker", "render_encoder", "updated_at")}
                for row in snapshot["render_failures"][:20]
            ],
        }
        snapshot_text = json.dumps(compact_snapshot, ensure_ascii=False, separators=(",", ":"))
        user_text = f"用户问题：{message}\n本地状态快照：{snapshot_text}"
        content: Any = user_text
        if image:
            raw, mime = image
            encoded = base64.b64encode(raw).decode("ascii")
            content = [
                {"type": "text", "text": user_text + "\n请结合截图判断，但以数据库状态为准。"},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}", "detail": "low"}},
            ]
        protocol = str(self.settings.guardian_ai_protocol or "responses").lower()
        if protocol not in {"responses", "chat", "auto"}:
            raise RuntimeError("不支持的管家模型协议：" + protocol)
        if protocol in {"responses", "auto"}:
            response_content: Any = user_text
            if image:
                raw, mime = image
                encoded = base64.b64encode(raw).decode("ascii")
                response_content = [
                    {"type": "input_text", "text": user_text + "\n请结合截图判断，但以数据库状态为准。"},
                    {"type": "input_image", "image_url": f"data:{mime};base64,{encoded}", "detail": "low"},
                ]
            endpoint = "/responses"
            payload = {
                "model": model,
                "input": [
                    {"role": "system", "content": GUARDIAN_SYSTEM_PROMPT},
                    {"role": "user", "content": response_content},
                ],
                "temperature": 0.1,
                "max_output_tokens": 1200,
            }
        else:
            endpoint = "/chat/completions"
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": GUARDIAN_SYSTEM_PROMPT},
                    {"role": "user", "content": content},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.1,
                "max_tokens": 1200,
            }
        response = httpx.post(
            base_url + endpoint,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload, timeout=120,
        )
        response.raise_for_status()
        data = response.json()
        raw_text = data.get("output_text")
        if not isinstance(raw_text, str):
            for output in data.get("output") or []:
                for item in output.get("content") or []:
                    if isinstance(item.get("text"), str):
                        raw_text = item["text"]
                        break
                if isinstance(raw_text, str):
                    break
        if not isinstance(raw_text, str):
            raw_text = data["choices"][0]["message"]["content"]
        match = re.search(r"\{.*\}", raw_text, flags=re.S)
        result = json.loads(match.group(0) if match else raw_text)
        return {
            "answer": str(result.get("answer") or "管家没有返回可读结论"),
            "severity": str(result.get("severity") or "warning"),
            "model": model,
        }

    def answer(self, message: str, image: tuple[bytes, str] | None = None) -> dict[str, Any]:
        snapshot = self.snapshot()
        try:
            result = self._remote_request(message, snapshot, image=image)
            result["source"] = "vision" if image else "gpt"
            return result
        except Exception as exc:  # noqa: BLE001
            result = self.local_answer(message, snapshot)
            result.update({"source": "local", "fallback_reason": str(exc)[:500]})
            return result

    def export_diagnostics(self) -> Path:
        snapshot = self.snapshot()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        directory = self.settings.data_dir / "diagnostics"
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / f"AI管家诊断包_{stamp}_{uuid4().hex[:6]}.zip"
        segments = self.db.all(
            """SELECT id,room_id,source_id,session_id,size_bytes,duration,timeline_start,timeline_end,
                      status,error,transcribed_at,model_submitted_at,analyzed_at,gpt_windows_done_json,
                      deepseek_windows_done_json,ai_retry_count,ai_next_retry_at,ai_last_failed_stage,
                      ai_last_failed_model,ai_abandoned_at,created_at,updated_at
               FROM recording_segments ORDER BY id DESC LIMIT 500"""
        )
        events = self.db.all(
            "SELECT id,level,event_type,message,details_json,created_at FROM service_events ORDER BY id DESC LIMIT 1000"
        )
        render_failures = self.db.all(
            """SELECT c.id,c.room_id,c.source_id,c.session_id,c.status,c.render_phase,
                      c.render_started_at,c.render_worker,c.render_encoder,c.updated_at,
                      COALESCE(r.sequence,'') AS room_sequence,COALESCE(r.name,'') AS room_name
               FROM highlight_candidates c LEFT JOIN live_rooms r ON r.id=c.room_id
               WHERE c.status='render_error' ORDER BY c.updated_at DESC LIMIT 500"""
        )
        rooms = self.db.all(
            """SELECT id,sequence,name,enabled,archived,live_status,live_checked_at,
                      last_recording_at,last_processed_at,last_error FROM live_rooms ORDER BY sequence"""
        )
        public_settings = self.settings.public_dict()
        manifest = {
            "generated_at": utc_now(), "version": self._version(),
            "privacy": "不包含API密钥、Cookie、直播链接、转写正文、录像、成片或完整数据库",
            "system": {"platform": platform.platform(), "python": platform.python_version()},
            "snapshot": snapshot,
        }
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            archive.writestr("segments.json", json.dumps(segments, ensure_ascii=False, indent=2))
            archive.writestr("events.json", json.dumps(events, ensure_ascii=False, indent=2))
            archive.writestr("render-failures.json", json.dumps(render_failures, ensure_ascii=False, indent=2))
            archive.writestr("rooms.json", json.dumps(rooms, ensure_ascii=False, indent=2))
            archive.writestr("settings-masked.json", json.dumps(public_settings, ensure_ascii=False, indent=2))
        self.db.event("info", "guardian_diagnostics", f"AI管家已生成脱敏诊断包：{destination.name}")
        return destination
