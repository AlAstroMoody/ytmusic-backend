from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Generic, TypeVar

K = TypeVar('K')
V = TypeVar('V')


class TtlLruCache(Generic[K, V]):
    """Thread-safe LRU cache with TTL and hard max size (bounded memory)."""

    def __init__(self, max_entries: int, ttl_seconds: float):
        self.max_entries = max(1, max_entries)
        self.ttl_seconds = max(1.0, ttl_seconds)
        self._data: OrderedDict[K, tuple[float, V]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: K) -> V | None:
        now = time.monotonic()
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            expires_at, value = item
            if expires_at <= now:
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return value

    def set(self, key: K, value: V) -> None:
        expires_at = time.monotonic() + self.ttl_seconds
        with self._lock:
            if key in self._data:
                del self._data[key]
            self._data[key] = (expires_at, value)
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)

    def invalidate(self, key: K) -> None:
        with self._lock:
            self._data.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)
