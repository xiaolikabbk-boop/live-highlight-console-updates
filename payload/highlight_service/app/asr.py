from __future__ import annotations

import importlib.util
import threading
from pathlib import Path
from typing import Any

from .config import Settings


class ASRUnavailable(RuntimeError):
    pass


class WhisperTranscriber:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._model: Any = None
        self._lock = threading.Lock()
        self.runtime = "not_loaded"

    @property
    def available(self) -> bool:
        return importlib.util.find_spec("faster_whisper") is not None

    def _get_model(self) -> Any:
        if not self.available:
            raise ASRUnavailable("未安装 faster-whisper；请运行 安装依赖.ps1 后再处理录像")
        with self._lock:
            if self._model is None:
                from faster_whisper import WhisperModel
                try:
                    self._model = WhisperModel(
                        self.settings.whisper_model,
                        device=self.settings.whisper_device,
                        compute_type=self.settings.whisper_compute_type,
                    )
                    self.runtime = f"{self.settings.whisper_device}:{self.settings.whisper_compute_type}"
                except (RuntimeError, OSError) as exc:
                    message = str(exc).lower()
                    if self.settings.whisper_device != "cuda" or not any(token in message for token in ("cuda", "cudnn", "cublas")):
                        raise
                    self._model = WhisperModel(self.settings.whisper_model, device="cpu", compute_type="int8")
                    self.runtime = "cpu:int8_fallback"
            return self._model

    def transcribe(self, audio_path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        model = self._get_model()
        try:
            segments, info = self._transcribe_with_model(model, audio_path)
            materialized = list(segments)
        except (RuntimeError, OSError) as exc:
            message = str(exc).lower()
            if not self.runtime.startswith("cuda") or not any(token in message for token in ("cuda", "cudnn", "cublas")):
                raise
            from faster_whisper import WhisperModel

            with self._lock:
                self._model = WhisperModel(self.settings.whisper_model, device="cpu", compute_type="int8")
                self.runtime = "cpu:int8_fallback"
            segments, info = self._transcribe_with_model(self._model, audio_path)
            materialized = list(segments)
        rows: list[dict[str, Any]] = []
        for segment in materialized:
            words = []
            probabilities = []
            for word in segment.words or []:
                probability = float(getattr(word, "probability", 0) or 0)
                probabilities.append(probability)
                words.append({
                    "start": round(float(word.start), 3),
                    "end": round(float(word.end), 3),
                    "text": word.word.strip(),
                    "probability": round(probability, 4),
                })
            rows.append({
                "start": round(float(segment.start), 3),
                "end": round(float(segment.end), 3),
                "text": segment.text.strip(),
                "confidence": round(sum(probabilities) / len(probabilities), 4) if probabilities else 0.5,
                "words": words,
            })
        metadata = {
            "language": getattr(info, "language", "zh"),
            "language_probability": float(getattr(info, "language_probability", 0) or 0),
            "duration": float(getattr(info, "duration", 0) or 0),
        }
        return rows, metadata

    @staticmethod
    def _transcribe_with_model(model: Any, audio_path: Path) -> tuple[Any, Any]:
        return model.transcribe(
            str(audio_path),
            language="zh",
            beam_size=5,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 450},
            word_timestamps=True,
            condition_on_previous_text=True,
        )
