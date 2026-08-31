from __future__ import annotations

import configparser
import re
import threading
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import Settings
from .db import Database, utc_now


_ROOM_STATUS_PATTERNS = (
    re.compile(r'\\"room\\":\{.{0,12000}?\\"status\\":(\d+)', re.DOTALL),
    re.compile(r'"room"\s*:\s*\{.{0,12000}?"status"\s*:\s*(\d+)', re.DOTALL),
    re.compile(r'"roomInfo"\s*:\s*\{.{0,12000}?"status"\s*:\s*(\d+)', re.DOTALL),
)


@dataclass(frozen=True, slots=True)
class LiveProbeResult:
    status: str
    detail: str = ""


def parse_douyin_live_status(html: str) -> LiveProbeResult:
    """Extract the room status used by Douyin's web/reflow pages.

    Douyin currently uses status=2 for a live room and status=4 for an
    ended/offline room. Unknown values are deliberately not guessed.
    """
    for pattern in _ROOM_STATUS_PATTERNS:
        match = pattern.search(html)
        if not match:
            continue
        raw_status = int(match.group(1))
        if raw_status == 2:
            return LiveProbeResult("live")
        if raw_status == 4:
            return LiveProbeResult("offline")
        return LiveProbeResult("unknown", f"未识别的直播状态码：{raw_status}")
    return LiveProbeResult("unknown", "直播页没有返回可识别的状态")


class DouyinLiveProbe:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _cookie(self) -> str:
        config_path = self.settings.recorder_root / "config" / "config.ini"
        if not config_path.exists():
            return ""
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(config_path, encoding="utf-8-sig")
            return parser.get("Cookie", "抖音cookie", fallback="").strip()
        except (configparser.Error, OSError):
            return ""

    def check(self, url: str) -> LiveProbeResult:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://live.douyin.com/",
        }
        cookie = self._cookie()
        if cookie:
            headers["Cookie"] = cookie
        try:
            with httpx.Client(
                follow_redirects=True,
                timeout=self.settings.live_check_timeout_seconds,
                headers=headers,
            ) as client:
                response = client.get(url)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            return LiveProbeResult("unknown", f"状态查询失败：{type(exc).__name__}")
        return parse_douyin_live_status(response.text)


class LiveStatusMonitor:
    """Poll room presence independently from the recorder's enabled switch."""

    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self.probe = DouyinLiveProbe(settings)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.settings.live_status_check_enabled or self._thread:
            return
        self._thread = threading.Thread(target=self._loop, name="live-status-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def request_refresh(self) -> None:
        self._wake.set()

    def check_room(self, room: dict) -> LiveProbeResult:
        result = self.probe.check(str(room["url"]))
        now = utc_now()
        self.db.execute(
            """UPDATE live_rooms
               SET live_status=?,live_checked_at=?,live_check_error=?,last_detected_at=?,updated_at=?
               WHERE id=?""",
            (result.status, now, result.detail, now, now, room["id"]),
        )
        return result

    def check_all(self) -> None:
        rooms = self.db.all("SELECT id,url FROM live_rooms WHERE archived=0 ORDER BY sequence,id")
        for room in rooms:
            if self._stop.is_set():
                return
            self.check_room(room)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.check_all()
            except Exception as exc:  # noqa: BLE001
                # Never let a platform/network change stop recording or clipping.
                self.db.event("warning", "live_status", f"静默直播状态巡检失败：{type(exc).__name__}")
            self._wake.clear()
            self._wake.wait(self.settings.live_check_interval_seconds)

