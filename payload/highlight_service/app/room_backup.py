from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from .db import Database, utc_now
from .rooms import RoomRegistry, source_key


BACKUP_KIND = "live-highlight-room-backup"
BACKUP_VERSION = 1


def export_room_backup(db: Database) -> dict[str, Any]:
    """Export room configuration only; never include recordings or processing data."""
    rows = db.all("SELECT * FROM live_rooms WHERE archived=0 ORDER BY sequence,id")
    rooms: list[dict[str, Any]] = []
    for row in rows:
        rooms.append({
            "sequence": row["sequence"],
            "name": row["name"],
            "url": row["url"],
            "enabled": bool(row["enabled"]),
            "notes": row.get("notes") or "",
            "review_mode": row.get("review_mode") or "manual",
            "recorder_prefix": row.get("recorder_prefix") or "",
            "recorder_suffix": row.get("recorder_suffix") or "",
        })
    return {
        "kind": BACKUP_KIND,
        "format_version": BACKUP_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "contains": "rooms-only",
        "rooms": rooms,
    }


def _text(item: dict[str, Any], key: str, limit: int, required: bool = False) -> str:
    value = item.get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"字段 {key} 格式不正确")
    value = value.strip()
    if required and not value:
        raise ValueError(f"字段 {key} 不能为空")
    if len(value) > limit:
        raise ValueError(f"字段 {key} 超过长度限制")
    return value


def import_room_backup(db: Database, registry: RoomRegistry, payload: dict[str, Any]) -> dict[str, int]:
    if not isinstance(payload, dict) or payload.get("kind") != BACKUP_KIND:
        raise ValueError("这不是直播间配置备份文件")
    if payload.get("format_version") != BACKUP_VERSION:
        raise ValueError("备份文件版本不受支持")
    items = payload.get("rooms")
    if not isinstance(items, list) or len(items) > 2000:
        raise ValueError("直播间列表格式不正确或数量过多")

    created = updated = 0
    now = utc_now()
    with db._lock, db.connect() as conn:  # one transaction: a bad file imports nothing
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"第 {index} 条直播间格式不正确")
            sequence = _text(item, "sequence", 20, True)
            sequence = sequence.zfill(3) if sequence.isdigit() else sequence
            name = _text(item, "name", 200, True)
            url = _text(item, "url", 1000, True)
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError(f"第 {index} 条直播间链接无效")
            key = source_key(name)
            notes = _text(item, "notes", 5000)
            review_mode = _text(item, "review_mode", 50) or "manual"
            recorder_prefix = _text(item, "recorder_prefix", 1000)
            recorder_suffix = _text(item, "recorder_suffix", 1000)
            enabled = 1 if bool(item.get("enabled", True)) else 0

            matches = conn.execute(
                "SELECT id FROM live_rooms WHERE sequence=? OR url=? OR source_key=?",
                (sequence, url, key),
            ).fetchall()
            match_ids = {int(row["id"]) for row in matches}
            if len(match_ids) > 1:
                raise ValueError(f"第 {index} 条与现有多个直播间冲突，请先整理重复的序号、名称或链接")
            if match_ids:
                room_id = next(iter(match_ids))
                conn.execute(
                    """UPDATE live_rooms SET sequence=?,name=?,url=?,source_key=?,recorder_prefix=?,
                       recorder_suffix=?,enabled=?,archived=0,review_mode=?,default_catalog_item_id=NULL,
                       notes=?,updated_at=? WHERE id=?""",
                    (sequence, name, url, key, recorder_prefix, recorder_suffix, enabled,
                     review_mode, notes, now, room_id),
                )
                updated += 1
            else:
                conn.execute(
                    """INSERT INTO live_rooms
                       (sequence,name,url,source_key,recorder_prefix,recorder_suffix,enabled,archived,
                        review_mode,notes,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (sequence, name, url, key, recorder_prefix, recorder_suffix, enabled, 0,
                     review_mode, notes, now, now),
                )
                created += 1

    registry.sync()
    return {
        "created": created,
        "updated": updated,
        "catalog_created": 0,
        "catalog_updated": 0,
    }
