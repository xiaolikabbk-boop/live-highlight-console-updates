from __future__ import annotations

import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Database, utc_now


def source_key(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "_", value).strip("_")
    return cleaned[:80] or "unknown"


class RoomRegistry:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self.url_config = settings.recorder_root / "config" / "URL_config.ini"
        self.backup_dir = settings.recorder_root / "backup_config"

    @staticmethod
    def parse_line(raw: str) -> dict[str, Any] | None:
        line = raw.strip()
        if not line:
            return None
        enabled = not line.startswith("#")
        body = line[1:].lstrip() if not enabled else line
        # The recorder may append scheduling text immediately after a Douyin
        # short link. Match that link's canonical token first so strings such
        # as "/0@0.com :3pm" never become part of the saved URL.
        match = re.search(r"https?://v\.douyin\.com/[0-9A-Za-z_-]+/?", body, re.IGNORECASE)
        if not match:
            match = re.search(r"https?://[^\s,，]+", body)
        if not match:
            return None
        url = match.group(0).rstrip("/.,，") + ("/" if match.group(0).endswith("/") else "")
        prefix = body[:match.start()]
        suffix = body[match.end():]
        name_match = re.search(r"主播\s*[:：]\s*(.+?)\s*$", suffix)
        name = name_match.group(1).strip() if name_match else source_key(url)
        if name_match:
            suffix = suffix[:name_match.start()].rstrip(" ,，")
        return {
            "url": url, "name": name, "enabled": enabled,
            "recorder_prefix": prefix, "recorder_suffix": suffix,
        }

    def import_existing(self) -> int:
        if not self.url_config.exists():
            return 0
        imported = 0
        next_number = 1
        for raw in self.url_config.read_text(encoding="utf-8-sig").splitlines():
            item = self.parse_line(raw)
            if not item:
                continue
            key = source_key(item["name"])
            existing = self.db.one(
                "SELECT id,url FROM live_rooms WHERE url=? OR source_key=? ORDER BY id LIMIT 1",
                (item["url"], key),
            )
            if existing:
                # Older imports could keep recorder scheduling parameters inside
                # the URL field. Normalize them without creating a duplicate room.
                self.db.execute(
                    """UPDATE live_rooms SET url=?,recorder_prefix=?,recorder_suffix=?,updated_at=?
                       WHERE id=?""",
                    (item["url"], item["recorder_prefix"], item["recorder_suffix"], utc_now(), existing["id"]),
                )
                continue
            while self.db.one("SELECT id FROM live_rooms WHERE sequence=?", (f"{next_number:03d}",)):
                next_number += 1
            now = utc_now()
            room_id = self.db.execute(
                """INSERT INTO live_rooms
                   (sequence,name,url,source_key,recorder_prefix,recorder_suffix,enabled,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (f"{next_number:03d}", item["name"], item["url"], source_key(item["name"]),
                 item["recorder_prefix"], item["recorder_suffix"], int(item["enabled"]), now, now),
            )
            self.db.execute("UPDATE recording_segments SET room_id=? WHERE source_id=? AND room_id IS NULL", (room_id, key))
            self.db.execute("UPDATE highlight_candidates SET room_id=? WHERE source_id=? AND room_id IS NULL", (room_id, key))
            imported += 1
            next_number += 1
        return imported

    def sync(self) -> None:
        rooms = self.db.all("SELECT * FROM live_rooms WHERE archived=0 ORDER BY sequence,id")
        self.url_config.parent.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        if self.url_config.exists():
            stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
            shutil.copy2(self.url_config, self.backup_dir / f"URL_config.ini_control_{stamp}")
        lines: list[str] = []
        for room in rooms:
            disabled = "" if room["enabled"] else "#"
            prefix = room.get("recorder_prefix") or ""
            suffix = room.get("recorder_suffix") or ""
            if suffix and not suffix.startswith((" ", ",", "，")):
                suffix = " " + suffix
            name_part = f",主播: {room['name']}"
            lines.append(f"{disabled}{prefix}{room['url']}{suffix}{name_part}")
        temp_path = self.url_config.with_suffix(".ini.tmp")
        temp_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        os.replace(temp_path, self.url_config)

    def find_for_source(self, key: str) -> dict[str, Any] | None:
        return self.db.one(
            "SELECT * FROM live_rooms WHERE source_key=? OR name=? ORDER BY archived,id LIMIT 1",
            (key, key),
        )
