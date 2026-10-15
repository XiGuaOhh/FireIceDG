from __future__ import annotations

import json
import os
import secrets
import sys
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

MAX_STRENGTH = 200
GAME_PORT = 19795
RELAY_PORT = 19796


def data_directory() -> Path:
    override = os.environ.get("FIREICEDG_DATA_DIR")
    path = Path(override) if override else Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "FireIceDG"
    path.mkdir(parents=True, exist_ok=True)
    return path


def resources() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "assets"
    return Path(__file__).resolve().parents[1] / "assets"


@dataclass(frozen=True)
class Settings:
    mode: str = "death"
    channel: str = "A"
    strength: int = 5
    duration_ms: int = 500
    cooldown_ms: int = 2000

    def __post_init__(self):
        if self.mode not in {"death", "mistake", "nonperfect"}:
            raise ValueError("未知触发规则")
        if self.channel not in {"A", "B"}:
            raise ValueError("请选择 A 或 B 通道")
        if type(self.strength) is not int or not 0 <= self.strength <= MAX_STRENGTH:
            raise ValueError("强度范围为 0–200")
        if any(type(v) is not int or v < 0 for v in (self.duration_ms, self.cooldown_ms)):
            raise ValueError("持续时间和冷却时间必须是非负数")


def seconds_to_ms(value: str) -> int:
    """Parse finite, nonnegative seconds without floating-point range limits."""
    try:
        seconds = Decimal(value.strip())
        if not seconds.is_finite() or seconds < 0:
            raise ValueError("时间必须是有限的非负数")
        numerator, denominator = seconds.as_integer_ratio()
        return (numerator * 1000 + denominator - 1) // denominator
    except (InvalidOperation, TypeError, AttributeError):
        raise ValueError("请输入有效的秒数") from None


def ms_to_seconds(value: int) -> str:
    whole, fraction = divmod(value, 1000)
    return f"{whole}.{fraction:03d}".rstrip("0").rstrip(".") if fraction else str(whole)


def load_preferences(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("配置格式错误")
    except (OSError, ValueError):
        value = {}
    def valid_token(token):
        return (isinstance(token, str) and len(token) == 64
                and all(char in "0123456789abcdefABCDEF" for char in token))
    if not valid_token(value.get("game_token")):
        # Keep the installed game plugin paired even if preferences are damaged.
        try:
            installed_token = (path.parent / "bridge.key").read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            installed_token = ""
        value["game_token"] = installed_token if valid_token(installed_token) else secrets.token_hex(32)
    try:
        settings = Settings(**value.get("settings", {}))
    except (ValueError, TypeError):
        settings = Settings()
    value["settings"] = asdict(settings)
    value.setdefault("game_path", "")
    value.setdefault("relay_mode", "lan")
    value.setdefault("remote_url", "wss://trex.dungeon-lab.cn/v4")
    save_preferences(path, value)
    return value


def save_preferences(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
