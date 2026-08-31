from __future__ import annotations

import ctypes
from typing import Any


LCMAP_SIMPLIFIED_CHINESE = 0x02000000


def to_simplified(text: str) -> str:
    """Convert Traditional Chinese to Simplified Chinese with Windows NLS."""
    value = str(text or "")
    if not value or not hasattr(ctypes, "windll"):
        return value
    kernel32 = ctypes.windll.kernel32
    size = kernel32.LCMapStringEx(
        "zh-CN", LCMAP_SIMPLIFIED_CHINESE, value, len(value), None, 0, None, None, 0
    )
    if size <= 0:
        return value
    buffer = ctypes.create_unicode_buffer(size)
    result = kernel32.LCMapStringEx(
        "zh-CN", LCMAP_SIMPLIFIED_CHINESE, value, len(value), buffer, size, None, None, 0
    )
    return buffer.value if result > 0 else value


def simplify_value(value: Any) -> Any:
    if isinstance(value, str):
        return to_simplified(value)
    if isinstance(value, list):
        return [simplify_value(item) for item in value]
    if isinstance(value, dict):
        return {key: simplify_value(item) for key, item in value.items()}
    return value
