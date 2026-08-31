from __future__ import annotations

import hashlib
import json
import math
import subprocess
import threading
import time
import uuid
from bisect import bisect_left, bisect_right
from pathlib import Path
from typing import Any, Callable

from PIL import Image

from .config import Settings


class MediaError(RuntimeError):
    pass


class MediaTools:
    TIMELINE_CACHE_VERSION = 1

    def __init__(self, settings: Settings):
        self.settings = settings
        self._timeline_cache: dict[str, dict[str, Any]] = {}
        self._timeline_lock = threading.Lock()
        self._timeline_path_locks: dict[str, threading.Lock] = {}
        self._timeline_path_locks_guard = threading.Lock()
        self._render_slots = threading.BoundedSemaphore(2)
        self._cpu_render_lock = threading.Lock()
        self._encoder_lock = threading.Lock()
        self._active_lock = threading.Lock()
        self._active_jobs: dict[int, dict[str, Any]] = {}
        self._gpu_available = False
        self._active_encoder = "libx264"
        self._encoder_note = "CPU 编码"
        self._detect_renderer()

    def _detect_renderer(self) -> None:
        mode = str(getattr(self.settings, "render_encoder_mode", "auto") or "auto").lower()
        if mode in {"cpu", "libx264"}:
            return
        smoke = self.settings.cache_dir / f"nvenc_probe_{uuid.uuid4().hex}.mp4"
        smoke.parent.mkdir(parents=True, exist_ok=True)
        try:
            encoders = self._run([
                str(self.settings.ffmpeg_path), "-hide_banner", "-encoders",
            ], timeout=20)
            if "h264_nvenc" not in (encoders.stdout + encoders.stderr):
                self._encoder_note = "FFmpeg 未提供 NVENC，已使用 CPU"
                return
            self._run([
                str(self.settings.ffmpeg_path), "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=black:s=256x256:r=1:d=1",
                "-frames:v", "1", "-c:v", "h264_nvenc", "-preset", "p5", "-cq", "21",
                str(smoke),
            ], timeout=25)
        except Exception as exc:  # NVIDIA-less machines must start normally.
            self._encoder_note = f"NVENC 不可用，已使用 CPU：{str(exc)[-160:]}"
            return
        finally:
            smoke.unlink(missing_ok=True)
        self._gpu_available = True
        self._active_encoder = "h264_nvenc"
        self._encoder_note = "NVIDIA NVENC 可用"

    @property
    def render_workers(self) -> int:
        with self._encoder_lock:
            return 2 if self._active_encoder == "h264_nvenc" else 1

    @property
    def active_encoder(self) -> str:
        with self._encoder_lock:
            return self._active_encoder

    def renderer_status(self) -> dict[str, Any]:
        with self._active_lock:
            jobs = [dict(value) for value in self._active_jobs.values()]
        encoder = self.active_encoder
        return {
            "encoder": encoder,
            "mode": "GPU双路" if encoder == "h264_nvenc" else "CPU单路",
            "workers": self.render_workers,
            "note": self._encoder_note,
            "jobs": jobs,
        }

    def _set_job_phase(
        self, candidate_id: int, phase: str, *, room: str = "", worker: str = ""
    ) -> None:
        with self._active_lock:
            existing = self._active_jobs.get(candidate_id, {})
            self._active_jobs[candidate_id] = {
                "candidate_id": candidate_id,
                "phase": phase,
                "room": room or existing.get("room", ""),
                "worker": worker or existing.get("worker", ""),
                "started_at": existing.get("started_at", time.time()),
                "encoder": self.active_encoder,
            }

    def _run(self, args: list[str], timeout: int = 300) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode:
            raise MediaError((result.stderr or result.stdout)[-3000:])
        return result

    def probe(self, path: Path) -> dict[str, Any]:
        result = self._run([
            str(self.settings.ffprobe_path), "-v", "error", "-show_entries",
            "format=duration,start_time:stream=index,codec_type,codec_name,width,height,r_frame_rate,sample_rate",
            "-of", "json", str(path),
        ], timeout=60)
        data = json.loads(result.stdout)
        data["duration"] = float(data.get("format", {}).get("duration") or 0)
        data["start_time"] = float(data.get("format", {}).get("start_time") or 0)
        return data

    @staticmethod
    def sha256(path: Path, chunk_size: int = 4 * 1024 * 1024) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(chunk_size):
                digest.update(chunk)
        return digest.hexdigest()

    def extract_audio(self, source: Path, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._run([
            str(self.settings.ffmpeg_path), "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(source), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
            str(destination),
        ])
        return destination

    def segments_for_range(self, db: Any, session_id: str, start: float, end: float) -> list[dict[str, Any]]:
        return db.all(
            """SELECT * FROM recording_segments
               WHERE session_id=? AND status IN
                 ('transcribed','gpt_analyzing','deepseek_analyzing','analyzed','complete','ai_waiting')
                 AND NOT(timeline_end<=? OR timeline_start>=?)
               ORDER BY timeline_start""",
            (session_id, start, end),
        )

    def build_range_source(
        self,
        db: Any,
        session_id: str,
        start: float,
        end: float,
        destination: Path,
    ) -> tuple[Path, float]:
        segments = self.segments_for_range(db, session_id, start, end)
        if not segments:
            raise MediaError("找不到覆盖候选时间段的录制分片")
        destination.parent.mkdir(parents=True, exist_ok=True)
        concat_file = destination.with_suffix(".concat.txt")
        lines = []
        for segment in segments:
            escaped = str(Path(segment["path"]).resolve()).replace("'", "'\\''")
            lines.append(f"file '{escaped}'")
        concat_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        first_start = float(segments[0]["timeline_start"])
        try:
            self._run([
                str(self.settings.ffmpeg_path), "-y", "-hide_banner", "-loglevel", "error",
                "-f", "concat", "-safe", "0", "-i", str(concat_file),
                "-c", "copy", str(destination),
            ])
        finally:
            concat_file.unlink(missing_ok=True)
        return destination, first_start

    def cleanup_transient_cache(self) -> tuple[int, int]:
        """Remove restart leftovers that are always safe to regenerate."""
        patterns = (
            "candidate_*_source.ts",
            "candidate_*_source.concat.txt",
            "candidate_*.ass",
            "segment_*.wav",
        )
        removed = 0
        removed_bytes = 0
        for pattern in patterns:
            for path in self.settings.cache_dir.glob(pattern):
                if not path.is_file():
                    continue
                try:
                    removed_bytes += path.stat().st_size
                    path.unlink()
                    removed += 1
                except FileNotFoundError:
                    continue
        return removed, removed_bytes

    def extract_keyframes(
        self,
        db: Any,
        candidate_id: int,
        session_id: str,
        start: float,
        end: float,
    ) -> list[Path]:
        # Read frames directly from the immutable recording segments. The old
        # implementation concatenated whole 20-minute files once per candidate.
        segments = self.segments_for_range(db, session_id, start, end)
        if not segments:
            raise MediaError("找不到覆盖候选时间段的录像分片")
        duration = end - start
        offsets = [1.0]
        cursor = 5.0
        while cursor < duration - 1:
            offsets.append(cursor)
            cursor += 5.0
        offsets.append(max(1.0, duration - 1.0))

        def capture(offset: float, label: str) -> Path:
            target = self.settings.keyframe_dir / f"candidate_{candidate_id}_{label}.jpg"
            absolute_time = start + offset
            segment = next((
                item for item in segments
                if float(item["timeline_start"]) <= absolute_time < float(item["timeline_end"])
            ), None)
            if segment is None:
                segment = min(
                    segments,
                    key=lambda item: min(
                        abs(absolute_time - float(item["timeline_start"])),
                        abs(absolute_time - float(item["timeline_end"])),
                    ),
                )
            source = Path(segment["path"])
            segment_offset = max(0.0, absolute_time - float(segment["timeline_start"]))
            self._run([
                str(self.settings.ffmpeg_path), "-y", "-hide_banner", "-loglevel", "error",
                "-ss", f"{segment_offset:.3f}", "-i", str(source), "-frames:v", "1",
                "-vf", "scale='min(640,iw)':-2", "-q:v", "3", str(target),
            ], timeout=90)
            return target

        entries: list[tuple[float, Path]] = []
        for index, offset in enumerate(offsets):
            entries.append((offset, capture(offset, f"base_{index}")))

        # If adjacent five-second samples change sharply, inspect that interval
        # at one frame per second. This keeps cloud traffic low in stable scenes.
        hashes = [self._dhash(path) for _, path in entries]
        dense_offsets: list[float] = []
        for index in range(len(hashes) - 1):
            distance = (hashes[index] ^ hashes[index + 1]).bit_count() / 64
            if distance > 0.45:
                left, right = offsets[index], offsets[index + 1]
                dense_offsets.extend(float(second) for second in range(math.ceil(left), math.floor(right) + 1))
        existing = {round(offset, 2) for offset in offsets}
        for index, offset in enumerate(dense_offsets):
            if round(offset, 2) not in existing and 0 < offset < duration:
                entries.append((offset, capture(offset, f"dense_{index}")))
        return [path for _, path in sorted(entries, key=lambda item: item[0])]

    @staticmethod
    def _dhash(path: Path) -> int:
        with Image.open(path) as image:
            image = image.convert("L").resize((9, 8))
            getter = getattr(image, "get_flattened_data", image.getdata)
            pixels = list(getter())
        value = 0
        for row in range(8):
            for col in range(8):
                left = pixels[row * 9 + col]
                right = pixels[row * 9 + col + 1]
                value = (value << 1) | int(left > right)
        return value

    def visual_similarity(self, frames: list[Path]) -> float:
        if len(frames) < 2:
            return 0.5
        hashes = [self._dhash(path) for path in frames]
        distances = [(hashes[i] ^ hashes[i + 1]).bit_count() / 64 for i in range(len(hashes) - 1)]
        # A change of pose or camera framing is common in live commerce; this
        # score is advisory and never rejects a clip on its own.
        return round(max(0.05, min(0.95, 1 - sum(distances) / len(distances))), 3)

    def _timeline_fingerprint(self, path: Path) -> dict[str, Any]:
        resolved = path.resolve()
        stat = resolved.stat()
        return {"path": str(resolved), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}

    def _timeline_cache_path(self, resolved_path: str) -> Path:
        key = hashlib.sha256(resolved_path.encode("utf-8")).hexdigest()
        return self.settings.cache_dir / "timelines" / f"{key}.json"

    def _timeline_path_lock(self, resolved_path: str) -> threading.Lock:
        with self._timeline_path_locks_guard:
            return self._timeline_path_locks.setdefault(resolved_path, threading.Lock())

    def cleanup_timeline_cache(self) -> tuple[int, int]:
        removed = removed_bytes = 0
        folder = self.settings.cache_dir / "timelines"
        if not folder.exists():
            return 0, 0
        for cache_file in folder.glob("*.json"):
            try:
                size = cache_file.stat().st_size
                payload = json.loads(cache_file.read_text(encoding="utf-8"))
                source = payload.get("source") or {}
                source_path = Path(str(source.get("path") or ""))
                if (payload.get("version") != self.TIMELINE_CACHE_VERSION
                        or not source_path.exists()
                        or self._timeline_fingerprint(source_path) != source):
                    cache_file.unlink(missing_ok=True)
                    removed += 1
                    removed_bytes += size
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                try:
                    size = cache_file.stat().st_size
                    cache_file.unlink(missing_ok=True)
                    removed += 1
                    removed_bytes += size
                except OSError:
                    pass
        return removed, removed_bytes

    def invalidate_timeline(self, path: Path) -> None:
        resolved = str(path.resolve())
        with self._timeline_lock:
            for key in [key for key in self._timeline_cache if key.startswith(resolved + "|")]:
                self._timeline_cache.pop(key, None)
        self._timeline_cache_path(resolved).unlink(missing_ok=True)

    def _stream_timeline(
        self, path: Path, progress: Callable[[str], None] | None = None
    ) -> dict[str, Any]:
        """Map decoded audio time back to source PTS and matching video frames."""
        fingerprint = self._timeline_fingerprint(path)
        resolved = str(fingerprint["path"])
        key = f"{resolved}|{fingerprint['size']}|{fingerprint['mtime_ns']}"
        with self._timeline_lock:
            cached = self._timeline_cache.get(key)
        if cached:
            if progress:
                progress("timeline_cached")
            return cached
        cache_path = self._timeline_cache_path(resolved)
        with self._timeline_path_lock(resolved):
            try:
                payload = json.loads(cache_path.read_text(encoding="utf-8"))
                if (payload.get("version") == self.TIMELINE_CACHE_VERSION
                        and payload.get("source") == fingerprint):
                    timeline = payload["timeline"]
                    with self._timeline_lock:
                        self._timeline_cache[key] = timeline
                    if progress:
                        progress("timeline_cached")
                    return timeline
            except (FileNotFoundError, KeyError, ValueError, TypeError, json.JSONDecodeError):
                pass
            if progress:
                progress("scanning_timeline")
            audio_result = self._run([
                str(self.settings.ffprobe_path), "-v", "error", "-select_streams", "a:0",
                "-show_packets", "-show_entries", "packet=pts_time,duration_time", "-of", "json", resolved,
            ], timeout=180)
            video_result = self._run([
                str(self.settings.ffprobe_path), "-v", "error", "-select_streams", "v:0",
                "-show_frames", "-show_entries", "frame=best_effort_timestamp_time", "-of", "json", resolved,
            ], timeout=180)
            audio_packets = json.loads(audio_result.stdout).get("packets", [])
            video_frames = json.loads(video_result.stdout).get("frames", [])
            audio_starts: list[float] = []
            audio_pts: list[float] = []
            audio_durations: list[float] = []
            elapsed = 0.0
            for packet in audio_packets:
                if packet.get("pts_time") is None:
                    continue
                duration = float(packet.get("duration_time") or (1024 / 48000))
                audio_starts.append(elapsed)
                audio_pts.append(float(packet["pts_time"]))
                audio_durations.append(duration)
                elapsed += duration
            frame_pts = [
                float(frame["best_effort_timestamp_time"])
                for frame in video_frames if frame.get("best_effort_timestamp_time") is not None
            ]
            if not audio_starts or not frame_pts:
                raise MediaError("无法建立录像音画时间轴")
            timeline = {
                "audio_starts": audio_starts, "audio_pts": audio_pts,
                "audio_durations": audio_durations, "video_pts": frame_pts,
            }
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = cache_path.with_suffix(f".{uuid.uuid4().hex}.tmp")
            try:
                temp_path.write_text(json.dumps({
                    "version": self.TIMELINE_CACHE_VERSION,
                    "source": fingerprint,
                    "timeline": timeline,
                }, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
                temp_path.replace(cache_path)
            finally:
                temp_path.unlink(missing_ok=True)
            with self._timeline_lock:
                self._timeline_cache[key] = timeline
            return timeline

    def _synced_video_frame(
        self, path: Path, decoded_audio_seconds: float,
        progress: Callable[[str], None] | None = None,
    ) -> int:
        timeline = self._stream_timeline(path, progress)
        starts = timeline["audio_starts"]
        packet_index = max(0, min(len(starts) - 1, bisect_right(starts, decoded_audio_seconds) - 1))
        within_packet = max(0.0, decoded_audio_seconds - starts[packet_index])
        source_pts = timeline["audio_pts"][packet_index] + min(
            within_packet, timeline["audio_durations"][packet_index]
        )
        frame_index = bisect_left(timeline["video_pts"], source_pts)
        return max(0, min(len(timeline["video_pts"]) - 1, frame_index))

    @staticmethod
    def _ass_time(seconds: float) -> str:
        seconds = max(0.0, seconds)
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = seconds % 60
        return f"{hours}:{minutes:02d}:{secs:05.2f}"

    @staticmethod
    def _ass_escape(text: str) -> str:
        return text.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}").replace("\n", r"\N")

    def write_ass(self, captions: list[dict[str, Any]], destination: Path, duration: float) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        keywords = ("版型", "显瘦", "面料", "材质", "透气", "弹力", "价格", "优惠", "包邮", "舒服", "百搭", "不掉色", "不起球")
        events: list[str] = []
        for caption in captions:
            start = max(0.0, min(float(caption.get("start", 0)), duration))
            end = max(start + 0.2, min(float(caption.get("end", duration)), duration))
            text = self._ass_escape(str(caption.get("text", "")).strip())
            for keyword in keywords:
                text = text.replace(keyword, rf"{{\c&H00D7FF&\b1}}{keyword}{{\c&HFFFFFF&\b0}}")
            if text:
                events.append(
                    f"Dialogue: 0,{self._ass_time(start)},{self._ass_time(end)},Default,,0,0,0,,{text}"
                )
        header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Default,Microsoft YaHei,62,&H00FFFFFF,&H000000FF,&H00101010,&H70000000,0,0,0,0,100,100,0,0,1,4,1,2,70,70,245,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
        destination.write_text(header + "\n".join(events) + "\n", encoding="utf-8-sig")
        return destination

    def render_candidate(
        self,
        db: Any,
        candidate: dict[str, Any],
        captions: list[dict[str, Any]],
        destination: Path,
        progress: Callable[[str, str], None] | None = None,
        worker: str = "",
    ) -> Path:
        candidate_id = int(candidate.get("id") or 0)
        room = str(candidate.get("room_name") or candidate.get("source_id") or "")

        def report(phase: str, encoder: str = "") -> None:
            self._set_job_phase(candidate_id, phase, room=room, worker=worker)
            if progress:
                progress(phase, encoder or self.active_encoder)

        with self._render_slots:
            report("preparing")
            try:
                return self._render_candidate(db, candidate, captions, destination, report)
            finally:
                with self._active_lock:
                    self._active_jobs.pop(candidate_id, None)

    def _render_candidate(
        self,
        db: Any,
        candidate: dict[str, Any],
        captions: list[dict[str, Any]],
        destination: Path,
        progress: Callable[[str], None] | None = None,
    ) -> Path:
        raw_ranges = candidate.get("source_ranges_json") or candidate.get("source_ranges") or []
        if isinstance(raw_ranges, str):
            try:
                raw_ranges = json.loads(raw_ranges)
            except json.JSONDecodeError:
                raw_ranges = []
        ranges = [
            {"start": float(item["start"]), "end": float(item["end"])}
            for item in raw_ranges
            if float(item.get("end", 0)) > float(item.get("start", 0))
        ]
        if not ranges:
            ranges = [{"start": float(candidate["start_time"]), "end": float(candidate["end_time"])}]
        ranges.sort(key=lambda item: item["start"])
        if any(ranges[index]["start"] < ranges[index - 1]["end"] for index in range(1, len(ranges))):
            raise MediaError("保留区间不能重叠或改变原始顺序")
        start, end = ranges[0]["start"], ranges[-1]["end"]
        duration = sum(item["end"] - item["start"] for item in ranges)
        if not (self.settings.clip_min_seconds <= duration <= self.settings.clip_max_seconds):
            raise MediaError(
                f"成片必须为 {self.settings.clip_min_seconds:.0f}–{self.settings.clip_max_seconds:.0f} 秒，当前 {duration:.2f} 秒"
            )
        pieces: list[dict[str, Any]] = []
        segments = self.segments_for_range(db, candidate["session_id"], start, end)
        for item in ranges:
            for segment in segments:
                piece_start = max(item["start"], float(segment["timeline_start"]))
                piece_end = min(item["end"], float(segment["timeline_end"]))
                if piece_end <= piece_start:
                    continue
                pieces.append({
                    "path": Path(segment["path"]),
                    # Whisper timestamps are based on sequential decoded samples,
                    # not the discontinuous MPEG timestamps stored inside TS.
                    "start": piece_start - float(segment["timeline_start"]),
                    "end": piece_end - float(segment["timeline_start"]),
                })
        if not pieces:
            raise MediaError("找不到候选保留区间对应的录像内容")
        chains: list[str] = []
        concat_inputs: list[str] = []
        input_args: list[str] = []
        for index, item in enumerate(pieces):
            probe = self.probe(item["path"])
            video = next((stream for stream in probe.get("streams", []) if stream.get("codec_type") == "video"), {})
            audio = next((stream for stream in probe.get("streams", []) if stream.get("codec_type") == "audio"), {})
            sample_rate = int(audio.get("sample_rate") or 48000)
            start_frame = self._synced_video_frame(item["path"], float(item["start"]), progress)
            end_frame = max(start_frame + 1, self._synced_video_frame(item["path"], float(item["end"]), progress))
            start_sample = max(0, round(float(item["start"]) * sample_rate))
            end_sample = max(start_sample + 1, round(float(item["end"]) * sample_rate))
            part_duration = float(item["end"]) - float(item["start"])
            fade = min(0.035, part_duration / 4)
            input_args.extend(["-i", str(item["path"])])
            chains.append(
                f"[{index}:v]trim=start_frame={start_frame}:end_frame={end_frame},settb=AVTB,setpts=PTS-STARTPTS,"
                f"scale=trunc(iw/2)*2:trunc(ih/2)*2[v{index}]"
            )
            chains.append(
                f"[{index}:a]atrim=start_sample={start_sample}:end_sample={end_sample},asetpts=PTS-STARTPTS,"
                f"afade=t=in:st=0:d={fade:.3f},afade=t=out:st={max(0, part_duration-fade):.3f}:d={fade:.3f}[a{index}]"
            )
            concat_inputs.append(f"[v{index}][a{index}]")
        if progress:
            progress("composing")
        chains.append("".join(concat_inputs) + f"concat=n={len(pieces)}:v=1:a=1[vc][ac]")
        video_output = "[vc]"
        subtitle_path: Path | None = None
        if self.settings.burn_subtitles:
            subtitle_path = self.settings.cache_dir / f"candidate_{candidate['id']}.ass"
            self.write_ass(captions, subtitle_path, duration)
            subtitle_filter_path = str(subtitle_path.resolve()).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
            chains.append(f"[vc]subtitles=filename='{subtitle_filter_path}'[vout]")
            video_output = "[vout]"
        filter_complex = ";".join(chains)
        destination.parent.mkdir(parents=True, exist_ok=True)

        def encode(encoder: str) -> None:
            encoder_args = ["-c:v", encoder]
            if encoder == "libx264":
                encoder_args += ["-preset", "medium", "-crf", "20"]
            else:
                encoder_args += ["-preset", "p5", "-cq", "21"]
            if progress:
                progress("encoding", encoder)
            self._run([
                str(self.settings.ffmpeg_path), "-y", "-hide_banner", "-loglevel", "error",
                *input_args, "-filter_complex", filter_complex,
                "-map", video_output, "-map", "[ac]", *encoder_args, "-c:a", "aac", "-b:a", "160k",
                "-movflags", "+faststart", str(destination),
            ], timeout=900)

        try:
            encoder = self.active_encoder
            if encoder == "libx264":
                with self._cpu_render_lock:
                    encode(encoder)
            else:
                try:
                    encode(encoder)
                except Exception as nvenc_exc:
                    destination.unlink(missing_ok=True)
                    with self._encoder_lock:
                        self._active_encoder = "libx264"
                        self._gpu_available = False
                        self._encoder_note = f"NVENC 运行失败，已自动回退 CPU：{str(nvenc_exc)[-160:]}"
                    if progress:
                        progress("gpu_fallback", "libx264")
                    with self._cpu_render_lock:
                        encode("libx264")
            if progress:
                progress("complete", self.active_encoder)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        finally:
            if subtitle_path is not None:
                subtitle_path.unlink(missing_ok=True)
        return destination


def evenly_timed_captions(text: str, duration: float) -> list[dict[str, Any]]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines and text.strip():
        lines = [text.strip()]
    if not lines:
        return []
    step = duration / len(lines)
    return [
        {"start": round(index * step, 3), "end": round(min(duration, (index + 1) * step), 3), "text": line}
        for index, line in enumerate(lines)
    ]
