from __future__ import annotations

import json
import queue
import re
import subprocess
import threading
import time
import ctypes
from ctypes import wintypes
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from .ai import AIUnavailable, CandidateAnalyzer
from .asr import ASRUnavailable, WhisperTranscriber
from .config import Settings
from .db import Database, utc_now
from .media import MediaError, MediaTools
from .live_status import LiveStatusMonitor
from .rooms import RoomRegistry
from .text_normalize import simplify_value, to_simplified


SUPPORTED_EXTENSIONS = {".ts", ".mp4", ".mkv", ".flv"}
RENDER_TIMELINE_VERSION = "audio-master-pts-v3"


class RecorderSupervisor:
    """Keeps the bundled recorder running without modifying its executable."""

    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return bool(self._recorder_process_ids())

    def has_enabled_room(self) -> bool:
        """Only start the recorder after at least one active URL is configured.

        A fresh portable package intentionally ships with an empty URL_config.ini.
        Starting DouyinLiveRecorder in that state makes it enter its interactive
        "please input a URL" prompt, and it will not notice rooms saved later by
        the web console.  Deferring startup avoids that first-run dead end.
        """
        url_config = self.settings.recorder_root / "config" / "URL_config.ini"
        if not url_config.is_file():
            return False
        try:
            for raw in url_config.read_text(encoding="utf-8-sig").splitlines():
                line = raw.strip()
                if line and not line.startswith("#") and re.search(r"https?://", line):
                    return True
        except OSError as exc:
            self.db.event("warning", "recorder", f"读取录制器直播间配置失败：{exc}")
        return False

    @staticmethod
    def _recorder_process_ids() -> list[int]:
        """Enumerate by image name without WMI/tasklist permissions."""
        if not hasattr(ctypes, "windll"):
            return []

        class ProcessEntry32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
            ]

        kernel32 = ctypes.windll.kernel32
        snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
        invalid_handle = ctypes.c_void_p(-1).value
        if snapshot == invalid_handle:
            return []
        entry = ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(entry)
        process_ids: list[int] = []
        try:
            success = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while success:
                if entry.szExeFile.casefold() == "douyinliverecorder.exe":
                    process_ids.append(int(entry.th32ProcessID))
                success = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snapshot)
        return process_ids

    def ensure_running(self) -> bool:
        if not self.settings.recorder_auto_start or self.running:
            return self.running
        if not self.has_enabled_room():
            return False
        if not self.settings.recorder_exe_path.exists():
            self.db.event("error", "recorder", "没有找到 DouyinLiveRecorder.exe")
            return False
        creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        subprocess.Popen(
            [str(self.settings.recorder_exe_path)],
            cwd=str(self.settings.recorder_root),
            creationflags=creationflags,
        )
        self.db.event("info", "recorder", "已自动启动直播录制器")
        return True

    def start(self) -> None:
        if not self.settings.recorder_auto_start:
            return
        self.ensure_running()
        self._thread = threading.Thread(target=self._loop, name="recorder-supervisor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.wait(self.settings.recorder_check_seconds):
            try:
                self.ensure_running()
            except Exception as exc:  # noqa: BLE001
                self.db.event("warning", "recorder", f"检查录制器失败：{exc}")


def safe_id(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "_", value).strip("_")
    return cleaned[:80] or "unknown"


class SegmentWatcher:
    """Polls for completed files so it also catches segments created while offline."""

    def __init__(self, settings: Settings, db: Database, on_ready: Any):
        self.settings = settings
        self.db = db
        self.on_ready = on_ready
        self._states: dict[Path, tuple[int, float, float]] = {}
        self._queued: set[Path] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.settings.input_dir.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._loop, name="segment-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def scan_once(self) -> None:
        now = time.monotonic()
        files = sorted(
            (path for path in self.settings.input_dir.rglob("*") if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS),
            key=lambda path: path.stat().st_mtime,
        )
        for path in files:
            resolved = path.resolve()
            existing = self.db.one("SELECT id,status,size_bytes FROM recording_segments WHERE path=?", (str(resolved),))
            if resolved in self._queued:
                # Older versions could queue a file while it was still marked
                # awaiting_finalization. Once a successor exists the file is
                # definitely closed, so release it to the ASR stage instead of
                # leaving it stuck in the in-memory queued set forever.
                if existing and existing["status"] == "awaiting_finalization" and any(
                    item.parent == resolved.parent and item.name > resolved.name for item in files
                ):
                    self.db.update_segment_status(int(existing["id"]), "discovered", "")
                continue
            if existing and existing["status"] in {"complete", "analyzed"}:
                # A temporary network pause used to make an actively written TS
                # look closed. Do not permanently trust that record if the same
                # file has subsequently grown.
                if int(existing.get("size_bytes") or 0) == resolved.stat().st_size:
                    self._queued.add(resolved)
                    continue
            try:
                stat = resolved.stat()
            except FileNotFoundError:
                continue
            previous = self._states.get(resolved)
            if previous is None or previous[0] != stat.st_size or previous[1] != stat.st_mtime:
                self._states[resolved] = (stat.st_size, stat.st_mtime, now)
                continue
            unchanged_since = previous[2]
            if (stat.st_size > 0 and now - unchanged_since >= self.settings.stable_seconds
                    and self._safe_to_process(resolved, files, stat, now, unchanged_since)
                    and self._is_closed(resolved)):
                self._queued.add(resolved)
                self.on_ready(resolved)

    def _safe_to_process(
        self, path: Path, files: list[Path], stat: Any, now: float, unchanged_since: float
    ) -> bool:
        """Never treat the currently recording room's newest file as finalized."""
        siblings = [item for item in files if item.parent == path.parent]
        if any(item.name > path.name for item in siblings):
            return True

        room = self.db.one(
            "SELECT enabled,live_status,live_checked_at FROM live_rooms WHERE name=? AND archived=0",
            (path.parent.name,),
        )
        stable_for = now - unchanged_since
        if room and not int(room.get("enabled") or 0):
            return stable_for >= self.settings.stopped_segment_stable_seconds
        if room and room.get("live_status") == "offline":
            try:
                checked_at = datetime.fromisoformat(str(room.get("live_checked_at") or "")).timestamp()
            except ValueError:
                checked_at = 0.0
            # Ignore an offline result left over from before this recording or
            # from service startup. It must be a fresh check after the file's
            # latest write before it can finalize the live room's newest file.
            if checked_at >= stat.st_mtime:
                return stable_for >= self.settings.stopped_segment_stable_seconds

        # While monitoring a live room, never infer finalization from elapsed
        # time alone: a stalled stream may resume writing the same TS much
        # later. A successor file, confirmed offline state, or manual pause is
        # required before the newest file can enter ASR/model processing.
        return False

    @staticmethod
    def _is_closed(path: Path) -> bool:
        """Require exclusive read access on Windows before processing a segment."""
        if not hasattr(ctypes, "windll"):
            try:
                with path.open("rb"):
                    return True
            except OSError:
                return False
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateFileW(
            str(path), 0x80000000, 0, None, 3, 0x80, None,
        )
        invalid = ctypes.c_void_p(-1).value
        if handle == invalid:
            return False
        kernel32.CloseHandle(handle)
        return True

    def retry(self, path: Path) -> None:
        resolved = path.resolve()
        self._queued.discard(resolved)
        self._states.pop(resolved, None)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception as exc:  # noqa: BLE001
                self.db.event("error", "watcher", f"扫描录像目录失败：{exc}")
            self._stop.wait(self.settings.scan_interval_seconds)


class HighlightPipeline:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self.media = MediaTools(settings)
        self.rooms = RoomRegistry(settings, db)
        self.transcriber = WhisperTranscriber(settings)
        primary_settings = [settings]
        if settings.ai_secondary_api_key and settings.ai_secondary_model:
            primary_settings.append(replace(
                settings,
                ai_api_key=settings.ai_secondary_api_key,
                ai_model=settings.ai_secondary_model,
                ai_vision_enabled=False,
            ))
        self.primary_analyzers = [
            CandidateAnalyzer(item, analysis_label=item.ai_model)
            for item in primary_settings if item.ai_api_key and item.ai_model
        ]
        # Keep the historical attribute for health checks, vision and scripts.
        self.analyzer = self.primary_analyzers[0] if self.primary_analyzers else CandidateAnalyzer(settings)
        self.fallback_analyzer: CandidateAnalyzer | None = None
        if settings.ai_fallback_api_key and settings.ai_fallback_model:
            fallback_settings = replace(
                settings,
                ai_api_key=settings.ai_fallback_api_key,
                ai_model=settings.ai_fallback_model,
                ai_vision_enabled=False,
            )
            self.fallback_analyzer = CandidateAnalyzer(
                fallback_settings, analysis_label=settings.ai_fallback_model
            )
        self.supplement_analyzer: CandidateAnalyzer | None = None
        if settings.deepseek_supplement_enabled and settings.deepseek_api_key:
            deepseek_settings = replace(
                settings,
                ai_base_url=settings.deepseek_base_url,
                ai_api_key=settings.deepseek_api_key,
                ai_model=settings.deepseek_model,
                ai_protocol="chat",
                ai_thinking_mode="disabled",
                ai_max_output_tokens=8000,
                ai_vision_enabled=False,
            )
            self.supplement_analyzer = CandidateAnalyzer(
                deepseek_settings, analysis_label=settings.deepseek_model
            )
        self.recorder = RecorderSupervisor(settings, db)
        self.live_monitor = LiveStatusMonitor(settings, db)
        self.queue: queue.Queue[Path | None] = queue.Queue()
        self.watcher = SegmentWatcher(settings, db, self.enqueue)
        self._stage_stop = threading.Event()
        self._ai_claim_lock = threading.Lock()
        self._ai_model_locks = {
            analyzer.analysis_version: threading.Lock()
            for analyzer in [
                *self.primary_analyzers,
                *([self.fallback_analyzer] if self.fallback_analyzer else []),
                *([self.supplement_analyzer] if self.supplement_analyzer else []),
            ]
        }
        self._render_claim_lock = threading.Lock()
        self._last_room: dict[str, int | None] = {"asr": None, "ai": None, "render": None}
        self._worker = threading.Thread(target=self._work, name="segment-discovery", daemon=True)
        self._asr_worker = threading.Thread(target=self._asr_loop, name="asr-worker-1", daemon=True)
        ai_worker_count = min(
            max(1, int(settings.ai_worker_count)), max(1, len(self.primary_analyzers))
        )
        self._ai_workers = [
            threading.Thread(target=self._ai_loop, args=(index,), name=f"ai-worker-{index + 1}", daemon=True)
            for index in range(ai_worker_count)
        ]
        self._render_workers = [
            threading.Thread(target=self._render_loop, args=(index,), name=f"render-worker-{index}", daemon=True)
            for index in (1, 2)
        ]
        self._started = False

    def ai_route_status(self) -> dict[str, Any]:
        primary = [item.settings.ai_model for item in self.primary_analyzers]
        fallback = self.fallback_analyzer.settings.ai_model if self.fallback_analyzer else ""
        return {
            "primary_models": primary,
            "fallback_model": fallback,
            "worker_count": len(self._ai_workers),
            "label": " + ".join(primary) if primary else "未配置",
        }

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        imported = self.rooms.import_existing()
        if imported:
            self.db.event("info", "rooms", f"已从录制器配置导入 {imported} 个直播间")
        # 数据库是主配置源；启动时也统一回写一次，确保外部手改或异常退出后配置一致。
        self.rooms.sync()
        removed_files, removed_bytes = self.media.cleanup_transient_cache()
        timeline_files, timeline_bytes = self.media.cleanup_timeline_cache()
        removed_files += timeline_files
        removed_bytes += timeline_bytes
        if removed_files:
            self.db.event(
                "info", "cache_cleanup",
                f"已清理 {removed_files} 个中断遗留临时文件",
                {"files": removed_files, "bytes": removed_bytes},
            )
        self._recover_interrupted_segments()
        self.live_monitor.start()
        self.recorder.start()
        self._worker.start()
        self._asr_worker.start()
        for worker in self._ai_workers:
            worker.start()
        for worker in self._render_workers:
            worker.start()
        self.watcher.start()
        renderer = self.media.renderer_status()
        self.db.event(
            "info", "service",
            f"分阶段公平调度服务已启动（转写单路，模型{len(self._ai_workers)}路，渲染{renderer['mode']} · {renderer['encoder']}）",
        )

    def _recover_interrupted_segments(self) -> None:
        """Make process phases left behind by a service restart runnable again."""
        interrupted = self.db.all(
            """SELECT * FROM recording_segments
               WHERE status IN ('transcribing','gpt_analyzing','deepseek_analyzing','rendering')"""
        )
        recovered = 0
        for segment in interrupted:
            path = Path(segment["path"])
            if not path.exists():
                self.db.update_segment_status(int(segment["id"]), "error", "源录像文件不存在，无法恢复任务")
                continue
            has_transcript = bool(self.db.one(
                "SELECT id FROM transcript_spans WHERE segment_id=? LIMIT 1", (segment["id"],)
            ))
            next_status = "transcribed" if has_transcript else "discovered"
            self.db.update_segment_status(int(segment["id"]), next_status)
            recovered += 1
        awaiting = self.db.all(
            "SELECT * FROM recording_segments WHERE status='awaiting_finalization' ORDER BY id"
        )
        for segment in awaiting:
            successor = self.db.one(
                """SELECT id,path FROM recording_segments
                   WHERE session_id=? AND id>? ORDER BY id LIMIT 1""",
                (segment["session_id"], segment["id"]),
            )
            if not successor:
                continue
            source_path = Path(segment["path"])
            successor_path = Path(successor["path"])
            if source_path.exists() and successor_path.exists():
                self.db.update_segment_status(int(segment["id"]), "discovered", "")
                self.db.event(
                    "info", "recovery",
                    f"等待封口的分片已发现后续分片，恢复处理：{source_path.name}",
                    {"segment_id": segment["id"], "successor_segment_id": successor["id"]},
                )
                recovered += 1
        self.db.execute(
            """UPDATE highlight_candidates SET status='visual_review',render_phase='',render_started_at='',
               render_worker='',updated_at=? WHERE status='rendering'""",
            (utc_now(),),
        )
        if recovered:
            self.db.event("info", "recovery", f"已恢复 {recovered} 个服务重启遗留任务")

    def stop(self) -> None:
        if not self._started:
            return
        self.watcher.stop()
        self.live_monitor.stop()
        self.recorder.stop()
        self._stage_stop.set()
        self.queue.put(None)
        self._worker.join(timeout=10)
        for worker in (self._asr_worker, *self._ai_workers, *self._render_workers):
            worker.join(timeout=10)
        self._started = False

    def enqueue(self, path: Path) -> None:
        self.queue.put(path)

    def retry_segment(self, segment_id: int) -> None:
        segment = self.db.one("SELECT * FROM recording_segments WHERE id=?", (segment_id,))
        if not segment:
            raise KeyError(segment_id)
        self.db.update_segment_status(segment_id, "discovered", "")
        self._stage_stop.wait(0.01)

    def _work(self) -> None:
        while True:
            path = self.queue.get()
            if path is None:
                return
            try:
                self.discover_file(path)
            except Exception as exc:  # noqa: BLE001
                self.db.event("error", "pipeline", f"处理分片失败：{exc}", {"file": path.name})
            finally:
                self.queue.task_done()

    def _source_and_session(self, path: Path) -> tuple[str, str]:
        relative = path.relative_to(self.settings.input_dir)
        source = safe_id(path.parent.name if len(relative.parts) > 1 else path.stem)
        modified = datetime.fromtimestamp(path.stat().st_mtime)
        previous = self.db.one(
            "SELECT session_id,path FROM recording_segments WHERE source_id=? ORDER BY id DESC LIMIT 1",
            (source,),
        )
        session = f"{source}_{modified:%Y%m%d_%H%M%S}"
        if previous:
            try:
                previous_mtime = Path(previous["path"]).stat().st_mtime
                if 0 <= path.stat().st_mtime - previous_mtime <= self.settings.session_join_gap_seconds:
                    session = previous["session_id"]
            except FileNotFoundError:
                pass
        return source, session

    def discover_file(self, path: Path) -> None:
        path = path.resolve()
        source_id, session_id = self._source_and_session(path)
        room = self.rooms.find_for_source(source_id)
        file_hash = self.media.sha256(path)
        probe = self.media.probe(path)
        duration = float(probe["duration"])
        if duration <= 0:
            raise MediaError("无法读取录像分片时长")
        size_bytes = path.stat().st_size
        existing = self.db.one("SELECT * FROM recording_segments WHERE path=?", (str(path),))
        if existing and (
            int(existing.get("size_bytes") or 0) != size_bytes
            or existing.get("file_hash") != file_hash
        ):
            segment = self.db.refresh_grown_segment(
                int(existing["id"]), file_hash=file_hash,
                size_bytes=size_bytes, duration=duration,
            )
            self.db.event(
                "warning", "segment_refinalized",
                f"录像分片曾短暂停写，现已按完整文件重新处理：{path.name}",
                {"segment_id": existing["id"], "old_size": existing.get("size_bytes"), "new_size": size_bytes},
            )
        else:
            segment = self.db.register_segment(
                source_id=source_id,
                session_id=session_id,
                path=path,
                file_hash=file_hash,
                size_bytes=size_bytes,
                duration=duration,
                room_id=int(room["id"]) if room else None,
                catalog_item_id=None,
            )
        if segment.get("status") == "awaiting_finalization":
            self.db.update_segment_status(int(segment["id"]), "discovered", "")
            segment = self.db.one("SELECT * FROM recording_segments WHERE id=?", (segment["id"],)) or segment
        if room and not segment.get("room_id"):
            self.db.execute(
                "UPDATE recording_segments SET room_id=?,catalog_item_id=NULL WHERE id=?",
                (room["id"], segment["id"]),
            )
            segment = self.db.one("SELECT * FROM recording_segments WHERE id=?", (segment["id"],)) or segment
        if room:
            self.db.execute(
                "UPDATE live_rooms SET last_detected_at=?,last_recording_at=?,updated_at=? WHERE id=?",
                (utc_now(), utc_now(), utc_now(), room["id"]),
            )
        self.db.event("info", "queued", f"录像分片已进入处理队列：{path.name}", {"segment_id": segment["id"]})

    def process_file(self, path: Path) -> None:
        """Synchronous compatibility path used by diagnostics and tests."""
        self.discover_file(path)
        segment = self.db.one("SELECT * FROM recording_segments WHERE path=?", (str(path.resolve()),))
        if not segment:
            return
        self._transcribe_segment(segment)
        segment = self.db.one("SELECT * FROM recording_segments WHERE id=?", (segment["id"],)) or segment
        if segment.get("status") == "transcribed":
            try:
                self.analyze_segment_windows(segment)
                self.db.update_segment_status(int(segment["id"]), "complete")
            except AIUnavailable as exc:
                self.db.update_segment_status(int(segment["id"]), "ai_waiting", str(exc))

    def _next_fair_segment(self, stage: str, statuses: tuple[str, ...]) -> dict[str, Any] | None:
        placeholders = ",".join("?" for _ in statuses)
        rows = self.db.all(
            f"""SELECT s.*,COALESCE(r.sequence,'999999') AS room_sequence
                FROM recording_segments s LEFT JOIN live_rooms r ON r.id=s.room_id
                WHERE s.status IN ({placeholders})
                ORDER BY room_sequence,s.id""",
            statuses,
        )
        if not rows:
            return None
        first_by_room: dict[int, dict[str, Any]] = {}
        for row in rows:
            first_by_room.setdefault(int(row.get("room_id") or 0), row)
        room_ids = list(first_by_room)
        last = self._last_room.get(stage)
        if last in room_ids:
            index = (room_ids.index(last) + 1) % len(room_ids)
        else:
            index = 0
        chosen_room = room_ids[index]
        self._last_room[stage] = chosen_room
        return first_by_room[chosen_room]

    def _asr_loop(self) -> None:
        while not self._stage_stop.is_set():
            segment = self._next_fair_segment("asr", ("discovered",))
            if not segment:
                self._stage_stop.wait(2)
                continue
            self._transcribe_segment(segment)

    def _transcribe_segment(self, segment: dict[str, Any]) -> None:
        segment_id = int(segment["id"])
        path = Path(segment["path"])
        audio_path = self.settings.cache_dir / f"segment_{segment_id}.wav"
        try:
            self.db.update_segment_status(segment_id, "transcribing")
            self.media.extract_audio(path, audio_path)
            transcript, metadata = self.transcriber.transcribe(audio_path)
            timeline_start = float(segment["timeline_start"])
            rows = []
            for span in transcript:
                words = [
                    {**word, "start": round(float(word["start"]) + timeline_start, 3), "end": round(float(word["end"]) + timeline_start, 3)}
                    for word in span["words"]
                ]
                rows.append((
                    segment_id, segment["session_id"],
                    float(span["start"]) + timeline_start,
                    float(span["end"]) + timeline_start,
                    to_simplified(span["text"]), float(span["confidence"]),
                    json.dumps(words, ensure_ascii=False), utc_now(),
                ))
            self.db.execute("DELETE FROM transcript_spans WHERE segment_id=?", (segment_id,))
            self.db.execute_many(
                """INSERT INTO transcript_spans
                   (segment_id,session_id,start_time,end_time,text,confidence,words_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                rows,
            )
            self.db.update_segment_status(segment_id, "transcribed")
            self.db.mark_segment_stage(segment_id, "transcribed")
            self.db.event("info", "transcribed", f"已转写分片 {path.name}", {"segment_id": segment_id, **metadata})
        except ASRUnavailable as exc:
            self.db.update_segment_status(segment_id, "asr_unavailable", str(exc))
            self.db.event("warning", "asr", str(exc), {"segment_id": segment_id})
        except Exception as exc:
            self.db.update_segment_status(segment_id, "error", str(exc))
            if segment.get("room_id"):
                self.db.execute(
                    "UPDATE live_rooms SET last_error=?,updated_at=? WHERE id=?",
                    (str(exc)[:1000], utc_now(), segment["room_id"]),
                )
            self.db.event("error", "asr", f"转写分片失败：{exc}", {"segment_id": segment_id})

        finally:
            audio_path.unlink(missing_ok=True)

    def _primary_lane_for_segment(self, segment_id: int) -> int:
        if len(self.primary_analyzers) <= 1:
            return 0
        # Stable 60/40 split: GPT-5.5 receives three out of every five
        # segments, GPT-5.4 receives the other two. A retry or restart keeps
        # the same preferred model for the segment.
        return 0 if segment_id % 5 in {0, 1, 2} else 1

    def _next_fair_ai_segment(self, lane_index: int) -> dict[str, Any] | None:
        with self._ai_claim_lock:
            rows = self.db.all(
                """SELECT s.*,COALESCE(r.sequence,'999999') AS room_sequence
                   FROM recording_segments s LEFT JOIN live_rooms r ON r.id=s.room_id
                   WHERE s.status IN ('transcribed','ai_waiting')
                   ORDER BY room_sequence,s.id"""
            )
            rows = [row for row in rows if self._primary_lane_for_segment(int(row["id"])) == lane_index]
            if not rows:
                return None
            first_by_room: dict[int, dict[str, Any]] = {}
            for row in rows:
                first_by_room.setdefault(int(row.get("room_id") or 0), row)
            room_ids = list(first_by_room)
            stage_key = f"ai:{lane_index}"
            last = self._last_room.get(stage_key)
            index = (room_ids.index(last) + 1) % len(room_ids) if last in room_ids else 0
            chosen_room = room_ids[index]
            segment = first_by_room[chosen_room]
            claimed = self.db.execute_changes(
                """UPDATE recording_segments SET status='gpt_analyzing',error='',updated_at=?
                   WHERE id=? AND status IN ('transcribed','ai_waiting')""",
                (utc_now(), segment["id"]),
            )
            if claimed != 1:
                return None
            self._last_room[stage_key] = chosen_room
            segment["status"] = "gpt_analyzing"
            return segment

    def _ai_loop(self, lane_index: int = 0) -> None:
        while not self._stage_stop.is_set():
            segment = self._next_fair_ai_segment(lane_index)
            if not segment:
                self._stage_stop.wait(2)
                continue
            segment_id = int(segment["id"])
            try:
                self.analyze_segment_windows(segment, lane_index=lane_index)
                self.db.update_segment_status(segment_id, "complete")
                if segment.get("room_id"):
                    self.db.execute(
                        "UPDATE live_rooms SET last_processed_at=?,last_error='',updated_at=? WHERE id=?",
                        (utc_now(), utc_now(), segment["room_id"]),
                    )
            except AIUnavailable as exc:
                self.db.update_segment_status(segment_id, "ai_waiting", str(exc))
                self.db.event("warning", "ai_waiting", str(exc), {"segment_id": segment_id})
                self._stage_stop.wait(self.settings.ai_retry_seconds)
            except Exception as exc:  # noqa: BLE001
                self.db.update_segment_status(segment_id, "error", str(exc))
                self.db.event("error", "ai", f"模型分析失败：{exc}", {"segment_id": segment_id})

    def _next_fair_candidate(self, worker_name: str = "render-worker-1") -> dict[str, Any] | None:
        with self._render_claim_lock:
            rows = self.db.all(
                """SELECT h.*,COALESCE(r.sequence,'999999') AS room_sequence,
                          COALESCE(r.name,h.source_id) AS room_name
                   FROM highlight_candidates h LEFT JOIN live_rooms r ON r.id=h.room_id
                   WHERE h.status='visual_review'
                     AND EXISTS (
                       SELECT 1 FROM recording_segments s
                       WHERE s.session_id=h.session_id
                         AND NOT(s.timeline_end<=h.start_time OR s.timeline_start>=h.end_time)
                     )
                     AND NOT EXISTS (
                       SELECT 1 FROM recording_segments s
                       WHERE s.session_id=h.session_id
                         AND NOT(s.timeline_end<=h.start_time OR s.timeline_start>=h.end_time)
                         AND s.status NOT IN ('complete','analyzed')
                     )
                   ORDER BY room_sequence,h.id""",
            )
            if not rows:
                return None
            first_by_room: dict[int, dict[str, Any]] = {}
            for row in rows:
                first_by_room.setdefault(int(row.get("room_id") or 0), row)
            room_ids = list(first_by_room)
            last = self._last_room.get("render")
            index = (room_ids.index(last) + 1) % len(room_ids) if last in room_ids else 0
            chosen_room = room_ids[index]
            candidate = first_by_room[chosen_room]
            now = utc_now()
            claimed = self.db.execute_changes(
                """UPDATE highlight_candidates SET status='rendering',render_phase='preparing',
                   render_started_at=?,render_worker=?,render_encoder=?,updated_at=?
                   WHERE id=? AND status='visual_review'""",
                (now, worker_name, self.media.active_encoder, now, candidate["id"]),
            )
            if claimed != 1:
                return None
            self._last_room["render"] = chosen_room
            candidate.update(
                status="rendering", render_phase="preparing", render_started_at=now,
                render_worker=worker_name, render_encoder=self.media.active_encoder,
            )
            return candidate

    def _render_loop(self, worker_index: int) -> None:
        worker_name = f"render-worker-{worker_index}"
        while not self._stage_stop.is_set():
            if worker_index > self.media.render_workers:
                self._stage_stop.wait(2)
                continue
            candidate = self._next_fair_candidate(worker_name)
            if not candidate:
                self._stage_stop.wait(2)
                continue
            self._visual_review_and_render(int(candidate["id"]), worker_name)

    def analyze_segment_windows(self, segment: dict[str, Any], lane_index: int | None = None) -> None:
        """Analyze each window once and persist progress after every paid request."""
        windows = self._analysis_windows(segment)
        segment_id = int(segment["id"])
        current = self.db.one("SELECT * FROM recording_segments WHERE id=?", (segment_id,)) or segment
        gpt_done = self._completed_window_keys(current, "gpt_windows_done_json")
        preferred_lane = self._primary_lane_for_segment(segment_id) if lane_index is None else lane_index
        for since, until in windows:
            window_key = self._window_key(since, until)
            if window_key in gpt_done:
                continue
            self.db.update_segment_status(segment_id, "gpt_analyzing")
            self._analyze_window_with_failover(segment, since, until, preferred_lane)
            self._mark_window_complete(segment_id, "gpt_windows_done_json", window_key)
            gpt_done.add(window_key)

        if self.supplement_analyzer:
            current = self.db.one("SELECT * FROM recording_segments WHERE id=?", (segment_id,)) or segment
            deepseek_done = self._completed_window_keys(current, "deepseek_windows_done_json")
            excluded_ranges = self._candidate_ranges(segment["session_id"], primary_only=True)
            try:
                for since, until in windows:
                    window_key = self._window_key(since, until)
                    if window_key in deepseek_done:
                        continue
                    self.db.update_segment_status(segment_id, "deepseek_analyzing")
                    with self._ai_model_locks[self.supplement_analyzer.analysis_version]:
                        self._analyze_window(
                            segment, since, until, self.supplement_analyzer, excluded_ranges
                        )
                    self._mark_window_complete(
                        segment_id, "deepseek_windows_done_json", window_key
                    )
                    deepseek_done.add(window_key)
            except Exception as exc:  # supplemental model must not discard primary results
                self.db.event(
                    "warning", "deepseek_supplement",
                    f"DeepSeek 补漏暂时失败，GPT 主选已保留：{exc}",
                    {"segment_id": segment_id},
                )
                raise AIUnavailable(str(exc)) from exc
        self.db.update_segment_status(segment_id, "analyzed")
        self.db.mark_segment_stage(segment_id, "analyzed")

    @staticmethod
    def _window_key(since: float, until: float) -> str:
        return f"{since:.3f}:{until:.3f}"

    @staticmethod
    def _completed_window_keys(segment: dict[str, Any], column: str) -> set[str]:
        try:
            values = json.loads(segment.get(column) or "[]")
        except (TypeError, json.JSONDecodeError):
            values = []
        return {str(value) for value in values}

    def _mark_window_complete(self, segment_id: int, column: str, window_key: str) -> None:
        if column not in {"gpt_windows_done_json", "deepseek_windows_done_json"}:
            raise ValueError("invalid model progress column")
        with self.db._lock, self.db.connect() as conn:
            row = conn.execute(
                f"SELECT {column} FROM recording_segments WHERE id=?", (segment_id,)
            ).fetchone()
            try:
                values = json.loads(row[column] or "[]") if row else []
            except (TypeError, json.JSONDecodeError):
                values = []
            if window_key not in values:
                values.append(window_key)
            conn.execute(
                f"UPDATE recording_segments SET {column}=?,updated_at=? WHERE id=?",
                (json.dumps(values, ensure_ascii=False), utc_now(), segment_id),
            )

    def _analysis_windows(self, segment: dict[str, Any]) -> list[tuple[float, float]]:
        duration = float(segment["timeline_end"]) - float(segment["timeline_start"])
        windows: list[tuple[float, float]] = []
        if duration <= self.settings.rolling_window_seconds:
            windows.append((
                max(0.0, float(segment["timeline_end"]) - self.settings.rolling_window_seconds),
                float(segment["timeline_end"]),
            ))
        else:
            window_size = float(self.settings.rolling_window_seconds)
            stride = float(self.settings.analysis_stride_seconds)
            cursor = float(segment["timeline_start"])
            segment_end = float(segment["timeline_end"])
            while cursor < segment_end:
                window_end = min(segment_end, cursor + window_size)
                windows.append((cursor, window_end))
                if window_end >= segment_end:
                    break
                cursor += stride
        return windows

    def _analyze_window(
        self, segment: dict[str, Any], since: float, until: float,
        analyzer: CandidateAnalyzer, excluded_ranges: list[dict[str, float]] | None = None,
        primary_request: bool = False,
    ) -> None:
        spans = self.db.spans_for_session(segment["session_id"], since)
        spans = [span for span in spans if float(span["start_time"]) < until]
        on_submit = None
        if primary_request:
            on_submit = lambda: self.db.mark_segment_stage(int(segment["id"]), "model_submitted")
        proposals = analyzer.analyze(spans, excluded_ranges=excluded_ranges, on_submit=on_submit)
        for proposal in proposals:
            start = float(proposal["start"])
            stop = float(proposal["end"])
            if self._is_duplicate_candidate(
                segment["session_id"], proposal["source_ranges"], proposal["analysis_version"]
            ):
                continue
            candidate_id = self._create_candidate(segment, proposal, spans)

    def _analyze_window_with_failover(
        self, segment: dict[str, Any], since: float, until: float, preferred_lane: int,
    ) -> CandidateAnalyzer:
        available = self.primary_analyzers or [self.analyzer]
        ordered = [available[preferred_lane % len(available)]]
        ordered.extend(item for item in available if item is not ordered[0])
        if self.fallback_analyzer:
            ordered.append(self.fallback_analyzer)
        errors: list[str] = []
        for index, analyzer in enumerate(ordered):
            model_name = getattr(getattr(analyzer, "settings", None), "ai_model", "GPT 主模型")
            try:
                lock = self._ai_model_locks.setdefault(analyzer.analysis_version, threading.Lock())
                with lock:
                    self._analyze_window(
                        segment, since, until, analyzer, primary_request=True
                    )
                if index:
                    self.db.event(
                        "warning", "ai_failover",
                        f"主线路暂时不可用，已由 {model_name} 接续完成",
                        {"segment_id": segment["id"], "model": model_name},
                    )
                return analyzer
            except AIUnavailable as exc:
                errors.append(f"{model_name}: {exc}")
                self.db.event(
                    "warning", "ai_route_failed",
                    f"{model_name} 暂时失败，准备尝试下一条模型线路：{exc}",
                    {"segment_id": segment["id"], "model": model_name},
                )
        raise AIUnavailable("；".join(errors))

    @staticmethod
    def _is_supplement_analysis(analysis_version: str) -> bool:
        return "deepseek" in str(analysis_version).lower()

    def _candidate_ranges(
        self, session_id: str, analysis_version: str = "", primary_only: bool = False,
    ) -> list[dict[str, float]]:
        ranges: list[dict[str, float]] = []
        for candidate in self.db.active_candidates_for_session(session_id):
            candidate_version = str(candidate.get("analysis_version") or "")
            if primary_only and self._is_supplement_analysis(candidate_version):
                continue
            if not primary_only and analysis_version and candidate_version != analysis_version:
                continue
            try:
                ranges.extend(json.loads(candidate.get("source_ranges_json") or "[]"))
            except (TypeError, json.JSONDecodeError):
                ranges.append({
                    "start": float(candidate["start_time"]),
                    "end": float(candidate["end_time"]),
                })
        return ranges

    def _is_duplicate_candidate(
        self, session_id: str, source_ranges: list[dict[str, Any]], analysis_version: str
    ) -> bool:
        """Deduplicate retained material, not the empty gaps inside its outer envelope."""
        new_ranges = [(float(item["start"]), float(item["end"])) for item in source_ranges]
        new_duration = sum(max(0.0, end - start) for start, end in new_ranges)
        if new_duration <= 0:
            return True
        for existing in self.db.active_candidates_for_session(session_id):
            existing_version = str(existing.get("analysis_version") or "")
            # All GPT routes are one primary selection family. This prevents a
            # fallback model from recreating material already selected by a
            # different GPT route while keeping DeepSeek supplementation
            # independent.
            same_family = (
                self._is_supplement_analysis(existing_version)
                == self._is_supplement_analysis(analysis_version)
            )
            if not same_family:
                continue
            try:
                old_ranges = [
                    (float(item["start"]), float(item["end"]))
                    for item in json.loads(existing.get("source_ranges_json") or "[]")
                ]
            except (TypeError, ValueError, json.JSONDecodeError):
                old_ranges = [(float(existing["start_time"]), float(existing["end_time"]))]
            old_duration = sum(max(0.0, end - start) for start, end in old_ranges)
            overlap_ratio = self._retained_overlap_ratio(new_ranges, old_ranges)
            if overlap_ratio >= self.settings.duplicate_overlap_ratio:
                return True
        return False

    @staticmethod
    def _retained_overlap_ratio(
        first: list[tuple[float, float]], second: list[tuple[float, float]]
    ) -> float:
        first_duration = sum(max(0.0, end - start) for start, end in first)
        second_duration = sum(max(0.0, end - start) for start, end in second)
        shortest = min(first_duration, second_duration)
        if shortest <= 0:
            return 0.0
        intersection = sum(
            max(0.0, min(first_end, second_end) - max(first_start, second_start))
            for first_start, first_end in first
            for second_start, second_end in second
        )
        return min(1.0, intersection / shortest)

    def _create_candidate(self, segment: dict[str, Any], proposal: dict[str, Any], spans: list[dict[str, Any]]) -> int:
        ranges = proposal["source_ranges"]
        start = float(ranges[0]["start"])
        end = float(ranges[-1]["end"])
        captions: list[dict[str, Any]] = []
        output_cursor = 0.0
        kept_by_id = {item["id"]: item for item in proposal.get("kept_clauses") or []}
        for source_range in ranges:
            range_start = float(source_range["start"])
            for clause_id in source_range.get("clause_ids") or []:
                clause = kept_by_id.get(clause_id)
                if clause:
                    captions.append({
                        "start": round(output_cursor + float(clause["start"]) - range_start, 3),
                        "end": round(output_cursor + float(clause["end"]) - range_start, 3),
                        "text": clause["caption_text"], "source_clause_id": clause_id,
                    })
            output_cursor += float(source_range["end"]) - range_start
        now = utc_now()
        return self.db.execute(
            """INSERT INTO highlight_candidates
               (source_id,session_id,room_id,start_time,end_time,source_ranges_json,captions_json,
                sales_score,coherence_score,confidence,risk_json,reason,kept_clauses_json,
                removed_clauses_json,compliance_hits_json,analysis_version,prompt_version,rule_version,
                status,catalog_item_id,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                segment["source_id"], segment["session_id"], segment.get("room_id"), start, end,
                json.dumps(ranges, ensure_ascii=False),
                json.dumps(captions, ensure_ascii=False),
                float(proposal["sales_score"]), float(proposal["coherence_score"]), float(proposal["confidence"]),
                json.dumps(simplify_value(proposal.get("risks") or []), ensure_ascii=False), to_simplified(str(proposal.get("reason", ""))),
                json.dumps(simplify_value(proposal.get("kept_clauses") or []), ensure_ascii=False),
                json.dumps(simplify_value(proposal.get("removed_clauses") or []), ensure_ascii=False),
                json.dumps(proposal.get("compliance_hits") or [], ensure_ascii=False),
                proposal["analysis_version"], proposal["prompt_version"], proposal["rule_version"],
                "visual_review", None, now, now,
            ),
        )

    def _visual_review_and_render(self, candidate_id: int, worker_name: str = "render-worker-1") -> None:
        candidate = self.db.one("SELECT * FROM highlight_candidates WHERE id=?", (candidate_id,))
        if not candidate:
            return
        try:
            frames = self.media.extract_keyframes(
                self.db, candidate_id, candidate["session_id"], float(candidate["start_time"]), float(candidate["end_time"])
            )
            local_score = self.media.visual_similarity(frames)
            product_score = local_score
            visual_reason = "本地关键帧相似度检查"
            risks = json.loads(candidate["risk_json"] or "[]")
            if self.analyzer.cloud.vision_enabled:
                visual = self.analyzer.cloud.analyze_frames(frames)
                product_score = float(visual.get("confidence", local_score)) if visual.get("same_product", True) else min(0.35, float(visual.get("confidence", 0.35)))
                visual_reason = str(visual.get("reason", visual_reason))
                if not visual.get("same_product", True):
                    risks.append("片段内可能发生商品切换")
            elif local_score < 0.35:
                risks.append("关键帧变化较大，请人工确认是否换款")
            confidence = round(
                float(candidate["sales_score"]) * 0.45
                + float(candidate["coherence_score"]) * 0.35
                + product_score * 0.20,
                3,
            )
            base_reason = str(candidate.get("reason") or "").split("；视觉检查：", 1)[0]
            reason = base_reason + "；视觉检查：" + visual_reason
            self.db.execute(
                """UPDATE highlight_candidates SET product_score=?,confidence=?,risk_json=?,
                   keyframes_json=?,reason=?,updated_at=? WHERE id=?""",
                (product_score, confidence, json.dumps(risks, ensure_ascii=False),
                 json.dumps([str(path) for path in frames], ensure_ascii=False),
                 reason, utc_now(), candidate_id),
            )
            candidate = self.db.one("SELECT * FROM highlight_candidates WHERE id=?", (candidate_id,)) or candidate
            output = self.settings.output_dir / "previews" / f"{safe_id(candidate['source_id'])}_{candidate_id}_v{candidate['version']}.mp4"
            def render_progress(phase: str, encoder: str) -> None:
                self.db.execute(
                    """UPDATE highlight_candidates SET render_phase=?,render_encoder=?,updated_at=?
                       WHERE id=? AND status='rendering'""",
                    (phase, encoder, utc_now(), candidate_id),
                )

            self.media.render_candidate(
                self.db, candidate, json.loads(candidate["captions_json"]), output,
                progress=render_progress, worker=worker_name,
            )
            clean_reason = str(candidate.get("reason") or "").split("；渲染失败：", 1)[0]
            self.db.execute(
                  """UPDATE highlight_candidates
                     SET preview_path=?,status='pending_review',reason=?,render_timeline_version=?,
                         render_phase='complete',updated_at=? WHERE id=?""",
                  (str(output), clean_reason, RENDER_TIMELINE_VERSION, utc_now(), candidate_id),
              )
        except Exception as exc:  # keep candidate reviewable even if preview fails
            self.db.execute(
                """UPDATE highlight_candidates SET status='render_error',render_phase='failed',
                   reason=reason||?,updated_at=? WHERE id=?""",
                (f"；渲染失败：{str(exc)[:500]}", utc_now(), candidate_id),
            )
