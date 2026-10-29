from __future__ import annotations

import pytest

from utils.rate_limit import ChannelEditLimiter, humanize_seconds


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_allows_two_edits_then_blocks() -> None:
    limiter = ChannelEditLimiter(clock=Clock())
    assert limiter.acquire(1) is True
    assert limiter.acquire(1) is True
    assert limiter.acquire(1) is False


def test_budget_is_per_channel() -> None:
    limiter = ChannelEditLimiter(clock=Clock())
    limiter.acquire(1)
    limiter.acquire(1)
    assert limiter.acquire(2) is True


def test_retry_after_counts_down_and_budget_recovers() -> None:
    clock = Clock()
    limiter = ChannelEditLimiter(max_edits=2, window=600, clock=clock)
    limiter.acquire(1)
    clock.now += 100
    limiter.acquire(1)
    assert limiter.retry_after(1) == pytest.approx(500)
    clock.now += 499
    assert limiter.acquire(1) is False
    clock.now += 2
    assert limiter.retry_after(1) == 0
    assert limiter.acquire(1) is True  # oldest edit aged out


def test_rejected_attempts_do_not_extend_the_penalty() -> None:
    clock = Clock()
    limiter = ChannelEditLimiter(max_edits=1, window=600, clock=clock)
    limiter.acquire(1)
    for _ in range(50):
        clock.now += 10
        limiter.acquire(1)
    clock.now += 101  # 601s after the only real edit
    assert limiter.acquire(1) is True


def test_retry_after_is_zero_when_unused() -> None:
    assert ChannelEditLimiter(clock=Clock()).retry_after(9) == 0


@pytest.mark.parametrize(
    ("seconds", "text"), [(0, "1s"), (23, "23s"), (60, "1m 0s"), (383, "6m 23s")]
)
def test_humanize(seconds: float, text: str) -> None:
    assert humanize_seconds(seconds) == text
