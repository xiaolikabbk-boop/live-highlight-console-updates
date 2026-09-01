from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


SERVICE_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = SERVICE_ROOT.parent
RECORDER_ROOT = WORKSPACE_ROOT / "DouyinLiveRecorder_v4.0.7"


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(SERVICE_ROOT / ".env")


@dataclass(slots=True)
class Settings:
    service_root: Path = SERVICE_ROOT
    recorder_root: Path = RECORDER_ROOT
    input_dir: Path = RECORDER_ROOT / "downloads"
    data_dir: Path = SERVICE_ROOT / "data"
    cache_dir: Path = SERVICE_ROOT / "data" / "cache"
    output_dir: Path = SERVICE_ROOT / "data" / "outputs"
    keyframe_dir: Path = SERVICE_ROOT / "data" / "keyframes"
    db_path: Path = SERVICE_ROOT / "data" / "highlight.db"
    ffmpeg_path: Path = RECORDER_ROOT / "ffmpeg" / "ffmpeg.exe"
    ffprobe_path: Path = RECORDER_ROOT / "ffmpeg" / "ffprobe.exe"
    recorder_exe_path: Path = RECORDER_ROOT / "DouyinLiveRecorder.exe"
    recorder_auto_start: bool = True
    recorder_check_seconds: int = 30
    live_status_check_enabled: bool = True
    live_check_interval_seconds: int = 180
    live_check_timeout_seconds: int = 15
    session_join_gap_seconds: int = 1800
    host: str = "127.0.0.1"
    port: int = 8876
    stable_seconds: int = 8
    recorder_segment_seconds: int = 600
    stopped_segment_stable_seconds: int = 30
    scan_interval_seconds: int = 3
    rolling_window_seconds: int = 300
    analysis_stride_seconds: int = 240
    max_candidates_per_window: int = 10
    clip_min_seconds: float = 15.0
    clip_max_seconds: float = 20.0
    default_clip_seconds: float = 18.0
    candidate_threshold: float = 0.58
    whisper_model: str = "small"
    whisper_device: str = "cuda"
    whisper_compute_type: str = "int8_float16"
    ai_base_url: str = ""
    ai_api_key: str = ""
    ai_model: str = "gpt-5.5"
    ai_secondary_api_key: str = ""
    ai_secondary_model: str = "gpt-5.4"
    ai_fallback_api_key: str = ""
    ai_fallback_model: str = "gpt-5.4-mini"
    ai_worker_count: int = 2
    ai_protocol: str = "auto"
    ai_timeout_seconds: int = 120
    ai_max_attempts: int = 3
    ai_retry_seconds: int = 60
    ai_thinking_mode: str = ""
    ai_max_output_tokens: int = 8000
    ai_vision_enabled: bool = False
    ai_vision_model: str = ""
    guardian_ai_base_url: str = ""
    guardian_ai_api_key: str = ""
    guardian_ai_model: str = "gpt-5.5"
    guardian_vision_api_key: str = ""
    guardian_vision_model: str = "gpt-5.5"
    guardian_ai_protocol: str = "responses"
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-v4-pro"
    deepseek_supplement_enabled: bool = True
    max_source_span_seconds: float = 300.0
    duplicate_overlap_ratio: float = 0.70
    max_source_ranges: int = 5
    range_merge_gap_seconds: float = 0.45
    # "auto" probes the bundled FFmpeg and the installed NVIDIA driver.  Old
    # installations may still carry video_encoder=libx264 in settings.json;
    # render_encoder_mode deliberately takes precedence so the upgrade can
    # enable acceleration without rewriting a user's configuration file.
    video_encoder: str = "libx264"
    render_encoder_mode: str = "auto"
    burn_subtitles: bool = False
    candidate_media_retention_days: int = 7

    @classmethod
    def load(cls) -> "Settings":
        cfg = cls()
        config_path = SERVICE_ROOT / "config" / "settings.json"
        if config_path.exists():
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            for key, value in raw.items():
                if not hasattr(cfg, key):
                    continue
                current = getattr(cfg, key)
                setattr(cfg, key, Path(value) if isinstance(current, Path) else value)
        env_map = {
            "HIGHLIGHT_AI_BASE_URL": "ai_base_url",
            "HIGHLIGHT_AI_API_KEY": "ai_api_key",
            "HIGHLIGHT_AI_MODEL": "ai_model",
            "HIGHLIGHT_AI_TEXT_MODEL": "ai_model",
            "HIGHLIGHT_AI_SECONDARY_API_KEY": "ai_secondary_api_key",
            "HIGHLIGHT_AI_SECONDARY_MODEL": "ai_secondary_model",
            "HIGHLIGHT_AI_FALLBACK_API_KEY": "ai_fallback_api_key",
            "HIGHLIGHT_AI_FALLBACK_MODEL": "ai_fallback_model",
            "HIGHLIGHT_AI_WORKER_COUNT": "ai_worker_count",
            "HIGHLIGHT_AI_PROTOCOL": "ai_protocol",
            "HIGHLIGHT_AI_VISION_ENABLED": "ai_vision_enabled",
            "HIGHLIGHT_AI_VISION_MODEL": "ai_vision_model",
            "HIGHLIGHT_GUARDIAN_AI_BASE_URL": "guardian_ai_base_url",
            "HIGHLIGHT_GUARDIAN_AI_API_KEY": "guardian_ai_api_key",
            "HIGHLIGHT_GUARDIAN_AI_MODEL": "guardian_ai_model",
            "HIGHLIGHT_GUARDIAN_VISION_API_KEY": "guardian_vision_api_key",
            "HIGHLIGHT_GUARDIAN_VISION_MODEL": "guardian_vision_model",
            "HIGHLIGHT_GUARDIAN_AI_PROTOCOL": "guardian_ai_protocol",
            "HIGHLIGHT_DEEPSEEK_BASE_URL": "deepseek_base_url",
            "HIGHLIGHT_DEEPSEEK_API_KEY": "deepseek_api_key",
            "HIGHLIGHT_DEEPSEEK_MODEL": "deepseek_model",
            "HIGHLIGHT_DEEPSEEK_SUPPLEMENT_ENABLED": "deepseek_supplement_enabled",
            "HIGHLIGHT_WHISPER_MODEL": "whisper_model",
            "HIGHLIGHT_WHISPER_DEVICE": "whisper_device",
            "HIGHLIGHT_WHISPER_COMPUTE_TYPE": "whisper_compute_type",
            "HIGHLIGHT_HOST": "host",
            "HIGHLIGHT_PORT": "port",
            "HIGHLIGHT_LIVE_STATUS_CHECK_ENABLED": "live_status_check_enabled",
            "HIGHLIGHT_LIVE_CHECK_INTERVAL_SECONDS": "live_check_interval_seconds",
            "HIGHLIGHT_CANDIDATE_MEDIA_RETENTION_DAYS": "candidate_media_retention_days",
        }
        for env_key, attr in env_map.items():
            if env_key in os.environ:
                value: object = os.environ[env_key]
                current = getattr(cfg, attr)
                if isinstance(current, bool):
                    value = str(value).strip().lower() in {"1", "true", "yes", "on"}
                elif isinstance(current, int):
                    value = int(str(value))
                setattr(cfg, attr, value)
        for directory in (cfg.data_dir, cfg.cache_dir, cfg.output_dir, cfg.keyframe_dir):
            directory.mkdir(parents=True, exist_ok=True)
        return cfg

    def public_dict(self) -> dict:
        data = asdict(self)
        data["ai_api_key"] = "configured" if self.ai_api_key else ""
        data["ai_secondary_api_key"] = "configured" if self.ai_secondary_api_key else ""
        data["ai_fallback_api_key"] = "configured" if self.ai_fallback_api_key else ""
        data["guardian_ai_api_key"] = "configured" if self.guardian_ai_api_key else ""
        data["guardian_vision_api_key"] = "configured" if self.guardian_vision_api_key else ""
        data["deepseek_api_key"] = "configured" if self.deepseek_api_key else ""
        return {key: str(value) if isinstance(value, Path) else value for key, value in data.items()}


settings = Settings.load()
