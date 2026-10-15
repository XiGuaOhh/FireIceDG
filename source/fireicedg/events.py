from __future__ import annotations

import hmac
import json
import time
from dataclasses import dataclass

MISTAKES = frozenset({"TooEarly", "VeryEarly", "VeryLate", "TooLate", "Multipress",
                      "FailMiss", "FailOverload", "OverPress"})
NONPERFECT = MISTAKES | {"EarlyPerfect", "LatePerfect"}
ALLOWED_MARGINS = NONPERFECT | {"Perfect", "Auto"}


@dataclass(frozen=True)
class GameEvent:
    kind: str
    session: str
    sequence: int
    timestamp_ms: int
    margin: str = ""
    tile: int = -1
    playing: bool = False
    ready: bool = False
    version: str = ""


class EventFilter:
    """Reject foreign, stale, reordered and replayed game packets before acting."""

    def __init__(self, token: str):
        self.token = token
        self.session = None
        self.last_sequence = -1
        self.last_heartbeat = 0.0
        self.ready = False
        self.playing = False
        self.retired = set()

    def parse(self, packet: bytes, now_ms: int | None = None) -> GameEvent | None:
        if len(packet) > 4096:
            return None
        try:
            data = json.loads(packet)
            if not isinstance(data, dict) or data.get("protocol") != "fireicedg/1":
                return None
            if not isinstance(data.get("token"), str) or not hmac.compare_digest(data["token"], self.token):
                return None
            kind = data.get("kind")
            session = data.get("session")
            sequence, timestamp = data.get("seq"), data.get("time")
            if kind not in {"heartbeat", "death", "hit", "stop"}:
                return None
            if not isinstance(session, str) or not 1 <= len(session) <= 64:
                return None
            if type(sequence) is not int or sequence < 0 or type(timestamp) is not int:
                return None
            now_ms = int(time.time() * 1000) if now_ms is None else now_ms
            if not -500 <= now_ms - timestamp <= 1500:
                return None
            if session != self.session:
                # A new run must establish its identity with a heartbeat first.
                if kind != "heartbeat" or session in self.retired:
                    return None
            if session == self.session and sequence <= self.last_sequence:
                return None
            margin = data.get("margin", "")
            if kind == "hit" and margin not in ALLOWED_MARGINS:
                return None
            tile = data.get("tile", -1)
            if type(tile) is not int:
                return None
            playing = data.get("playing") is True
            ready = data.get("ready") is True
            # Commit identity only after the entire packet has been validated.
            if session != self.session:
                if self.session:
                    self.retired.add(self.session)
                self.session = session
            self.last_sequence = sequence
            if kind == "heartbeat":
                self.last_heartbeat = time.monotonic()
                self.ready, self.playing = ready, playing
            elif kind == "stop":
                self.ready = self.playing = False
            return GameEvent(kind, session, sequence, timestamp, margin, tile, playing, ready,
                             str(data.get("version", ""))[:50])
        except (ValueError, TypeError, KeyError, UnicodeDecodeError):
            return None

    def connected(self, now: float | None = None) -> bool:
        return self.ready and (time.monotonic() if now is None else now) - self.last_heartbeat < 2.0


def qualifies(event: GameEvent, mode: str) -> bool:
    if event.kind == "death":
        return True
    if event.kind != "hit":
        return False
    return event.margin in (NONPERFECT if mode == "nonperfect" else MISTAKES) if mode != "death" else False
