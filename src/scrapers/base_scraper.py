import time
from typing import Optional

import requests

from config import config


class BaseScraper:
    """Base class for all HTTP-based scrapers."""

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "max-age=0",
    }

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(self.HEADERS)

    def _make_request(
        self, url: str, retries: int = 3
    ) -> Optional[requests.Response]:
        """GET with retry + exponential back-off. Returns None on permanent failure."""
        for attempt in range(retries):
            try:
                if attempt > 0:
                    time.sleep(config.REQUEST_DELAY * (2 ** (attempt - 1)))

                resp = self.session.get(url, timeout=config.REQUEST_TIMEOUT)

                if resp.status_code == 200:
                    return resp
                if resp.status_code == 404:
                    self._log_error(f"404 Not Found: {url}")
                    return None
                if resp.status_code == 429:
                    self._log_error(f"Rate limited ({url}), waiting 10 s…")
                    time.sleep(10)
                    continue
                if resp.status_code in (500, 502, 503):
                    self._log_error(
                        f"Server error {resp.status_code} for {url} (attempt {attempt + 1})"
                    )
            except requests.exceptions.Timeout:
                self._log_error(f"Timeout on {url} (attempt {attempt + 1})")
            except requests.exceptions.ConnectionError as exc:
                self._log_error(f"Connection error on {url}: {exc}")
            except Exception as exc:
                self._log_error(f"Unexpected error on {url}: {exc}")
                return None

        return None

    def _log_error(self, message: str) -> None:
        print(f"[{self.__class__.__name__}] ERROR: {message}")
