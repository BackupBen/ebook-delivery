"""Einfache Rate Limits im Arbeitsspeicher (gleitendes Zeitfenster).

Die App läuft als ein einzelner Prozess; ein gemeinsamer Speicher ist deshalb nicht nötig.
Nach einem Neustart beginnen die Zähler von vorn.
"""

from __future__ import annotations

import threading
import time
from collections import deque


class RateLimiter:
    def __init__(self, max_keys: int = 50_000) -> None:
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._max_keys = max_keys
        self._last_sweep = time.monotonic()

    def _prune(self, key: str, window: float, now: float) -> deque[float]:
        events = self._events.get(key)
        if events is None:
            events = deque()
            self._events[key] = events
        cutoff = now - window
        while events and events[0] <= cutoff:
            events.popleft()
        return events

    def retry_after(self, key: str, limit: int, window: float) -> int:
        """Sekunden bis zur nächsten erlaubten Anfrage; 0, wenn das Limit nicht erreicht ist."""
        now = time.monotonic()
        with self._lock:
            events = self._prune(key, window, now)
            if len(events) < limit:
                if not events:
                    self._events.pop(key, None)
                return 0
            return max(1, int(events[0] + window - now) + 1)

    def hit(self, key: str, limit: int, window: float) -> int:
        """Zählt ein Ereignis. Liefert 0 bei Erfolg, sonst die Wartezeit in Sekunden."""
        now = time.monotonic()
        with self._lock:
            self._sweep(now)
            events = self._prune(key, window, now)
            if len(events) >= limit:
                return max(1, int(events[0] + window - now) + 1)
            events.append(now)
            return 0

    def record(self, key: str, window: float) -> None:
        """Zählt ein Ereignis ohne Limitprüfung (z. B. fehlgeschlagene Anmeldung)."""
        now = time.monotonic()
        with self._lock:
            self._sweep(now)
            self._prune(key, window, now).append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._events.clear()

    def _sweep(self, now: float) -> None:
        # Leere oder sehr alte Einträge entfernen, damit der Speicher begrenzt bleibt.
        if now - self._last_sweep < 60 and len(self._events) < self._max_keys:
            return
        self._last_sweep = now
        horizon = now - 24 * 3600
        for key in [k for k, v in self._events.items() if not v or v[-1] < horizon]:
            del self._events[key]
        if len(self._events) >= self._max_keys:
            # Notbremse gegen Speicherüberlauf: älteste Hälfte verwerfen.
            ordered = sorted(self._events.items(), key=lambda item: item[1][-1])
            for key, _ in ordered[: len(ordered) // 2]:
                del self._events[key]
