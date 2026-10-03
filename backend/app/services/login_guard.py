"""Slows down password guessing: after too many failed sign-ins for one account, sign-in for
that account is paused for a while. Kept in memory: the app runs as one process, and a
restart clearing the counts is acceptable for a pause this short.

There is deliberately no per-computer limit. Browsers reach the API through the web app's
proxy, so every request comes from the same local address, and the proxy passes on a
client's X-Forwarded-For header unchanged (tested: a forged value arrives as sent). A limit
keyed on either would let anyone dodge it, or let one person lock out the whole office.
"""

import threading
import time
from collections import defaultdict, deque

WINDOW_SECONDS = 15 * 60
MAX_FAILURES_PER_ACCOUNT = 5

_lock = threading.Lock()
_by_account: dict[str, deque[float]] = defaultdict(deque)


def _now() -> float:
    return time.monotonic()


def _recent(times: deque[float], now: float) -> int:
    while times and now - times[0] > WINDOW_SECONDS:
        times.popleft()
    return len(times)


def blocked(account: str) -> bool:
    """Whether sign-in for this account is paused right now."""
    with _lock:
        return _recent(_by_account[account], _now()) >= MAX_FAILURES_PER_ACCOUNT


def failed(account: str) -> None:
    with _lock:
        _by_account[account].append(_now())


def succeeded(account: str) -> None:
    with _lock:
        _by_account.pop(account, None)


def reset() -> None:
    with _lock:
        _by_account.clear()
