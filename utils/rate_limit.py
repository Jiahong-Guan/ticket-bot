from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Callable

MAX_EDITS = 2
# Slightly above Discord's 600 s window to absorb clock and latency skew.
WINDOW_SECONDS = 610.0


class ChannelEditLimiter:
    def __init__(
        self,
        max_edits: int = MAX_EDITS,
        window: float = WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max = max_edits
        self._window = window
        self._clock = clock
        self._stamps: dict[int, deque[float]] = defaultdict(deque)

    def _prune(self, channel_id: int) -> deque[float]:
        stamps = self._stamps[channel_id]
        cutoff = self._clock() - self._window
        while stamps and stamps[0] <= cutoff:
            stamps.popleft()
        return stamps

    def retry_after(self, channel_id: int) -> float:
        stamps = self._prune(channel_id)
        if len(stamps) < self._max:
            return 0.0
        return max(0.0, stamps[0] + self._window - self._clock())

    def acquire(self, channel_id: int) -> bool:
        stamps = self._prune(channel_id)
        if len(stamps) >= self._max:
            return False
        stamps.append(self._clock())
        return True


def humanize_seconds(seconds: float) -> str:
    total = max(1, round(seconds))
    minutes, secs = divmod(total, 60)
    return f"{minutes}m {secs}s" if minutes else f"{secs}s"
