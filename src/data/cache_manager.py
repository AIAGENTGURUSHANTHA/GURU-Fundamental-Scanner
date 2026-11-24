import threading
from datetime import datetime, timedelta
from typing import Any, Dict, Optional


class CacheManager:
    """Thread-safe in-memory TTL cache for stock data."""

    def __init__(self, ttl_seconds: int = 300) -> None:
        self._store: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._ttl = ttl_seconds

    def get(self, symbol: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            entry = self._store.get(symbol.upper())
            if not entry:
                return None
            if datetime.utcnow() > entry["expires_at"]:
                del self._store[symbol.upper()]
                return None
            return entry["data"]

    def set(self, symbol: str, data: Dict[str, Any]) -> None:
        with self._lock:
            self._store[symbol.upper()] = {
                "data": data,
                "expires_at": datetime.utcnow() + timedelta(seconds=self._ttl),
            }

    def invalidate(self, symbol: str) -> None:
        with self._lock:
            self._store.pop(symbol.upper(), None)

    def clear_all(self) -> None:
        with self._lock:
            self._store.clear()

    def size(self) -> int:
        with self._lock:
            # Purge expired first
            now = datetime.utcnow()
            expired = [k for k, v in self._store.items() if now > v["expires_at"]]
            for k in expired:
                del self._store[k]
            return len(self._store)


# Singleton shared instance
from config import config  # noqa: E402

cache_manager = CacheManager(ttl_seconds=config.CACHE_TTL)
