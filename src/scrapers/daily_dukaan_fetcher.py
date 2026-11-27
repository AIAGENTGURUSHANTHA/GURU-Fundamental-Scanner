"""
Daily Dukaan Fetcher.

Fetches current price and 20-day SMA for NSE-listed stocks and ETFs.
Primary data source: NSE India via nsepython (equity series).
Fallback for ETFs: Yahoo Finance (.NS suffix mirrors NSE India data).
Company names are fetched from yfinance ticker info and cached.

Speed notes:
  - _HIST_DAYS kept < 40 so nsepython makes exactly 1 HTTP request per symbol
    (the library chunks date ranges in 40-day windows).
  - Company names are fetched once from yfinance ticker.info and stored in a
    class-level cache so repeated scans skip the ~1.5 s per-symbol lookup.
  - nse_eq() is intentionally NOT used — it takes ~7 s per symbol and returns
    empty names for ETFs.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from typing import List, Optional

import pandas as pd
import yfinance as yf

from src.data.daily_dukaan_models import DailyDukaanData

# Keep < 40 so nsepython makes exactly 1 API call per symbol
_HIST_DAYS = 35


class DailyDukaanFetcher:
    """Calculates 20-day SMA and price-vs-DMA metrics for NSE symbols."""

    # Class-level cache: persists across scans in the same Streamlit session
    _name_cache: dict = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def fetch(self, symbol: str, ref_date: Optional[date] = None) -> DailyDukaanData:
        sym      = symbol.strip().upper()
        ref_date = ref_date or date.today()
        return self._fetch_one(sym, ref_date)

    # ── Fetch strategies ──────────────────────────────────────────────────────

    def _fetch_one(self, sym: str, ref_date: date) -> DailyDukaanData:
        """Try NSE India (equity series); fall back to Yahoo Finance for ETFs."""
        result = self._fetch_nsepython(sym, ref_date)
        if result and not result.error:
            return result
        result = self._fetch_yfinance(sym, ref_date)
        if result and not result.error:
            return result
        return DailyDukaanData(
            symbol=sym,
            error="No data from NSE India. Verify the NSE symbol is correct.",
        )

    def _fetch_nsepython(self, sym: str, ref_date: date) -> Optional[DailyDukaanData]:
        """Single API call via nsepython (< 40-day window = 1 HTTP request)."""
        try:
            from nsepython import equity_history  # type: ignore

            start     = ref_date - timedelta(days=_HIST_DAYS)
            start_str = start.strftime("%d-%m-%Y")
            end_str   = ref_date.strftime("%d-%m-%Y")

            df = equity_history(sym, "EQ", start_str, end_str)
            if df is None or not isinstance(df, pd.DataFrame) or df.empty:
                return None

            df = df.copy()
            df["_date"] = pd.to_datetime(df["CH_TIMESTAMP"])
            df = df.set_index("_date").sort_index()
            df["Close"] = pd.to_numeric(df["CH_CLOSING_PRICE"], errors="coerce")

            company_name = self._get_company_name(sym)
            return self._calc(sym, company_name, df, ref_date, source="NSE India")
        except Exception:
            return None

    def _fetch_yfinance(self, sym: str, ref_date: date) -> Optional[DailyDukaanData]:
        """Fallback for ETFs: Yahoo Finance .NS mirrors NSE India data."""
        try:
            t    = yf.Ticker(f"{sym}.NS")
            hist = t.history(period="35d", interval="1d", auto_adjust=True)
            if hist is None or hist.empty:
                return None
            if hist.index.tz is not None:
                hist.index = hist.index.tz_convert(None)
            company_name = self._get_company_name(sym, ticker=t)
            return self._calc(sym, company_name, hist, ref_date, source="NSE India")
        except Exception:
            return None

    # ── Company name (cached) ─────────────────────────────────────────────────

    def _get_company_name(self, sym: str,
                          ticker: Optional[yf.Ticker] = None) -> str:
        """Return company name from cache; fetch from yfinance if not cached."""
        if sym in self._name_cache:
            return self._name_cache[sym]

        name = ""
        try:
            t = ticker or yf.Ticker(f"{sym}.NS")
            # fast_info first (no network if already fetched)
            fi = t.fast_info
            for attr in ("long_name", "longName", "short_name", "shortName"):
                val = getattr(fi, attr, None)
                if val:
                    name = str(val)
                    break
            # fall back to ticker.info (slower, ~1.5 s, but reliable)
            if not name:
                info = t.info or {}
                name = (info.get("longName") or info.get("shortName") or "")
        except Exception:
            pass

        name = name or sym
        self._name_cache[sym] = name
        return name

    # ── Shared calculation ────────────────────────────────────────────────────

    @staticmethod
    def _calc(sym: str, company_name: str, hist: pd.DataFrame,
              ref_date: date, source: str = "NSE India") -> DailyDukaanData:
        hist.index = pd.to_datetime(hist.index)
        if hist.index.tz is not None:
            hist.index = hist.index.tz_convert(None)

        hist = hist[hist.index.date <= ref_date]
        if hist.empty:
            return DailyDukaanData(
                symbol=sym, company_name=company_name,
                error=f"No trading data on or before {ref_date}",
            )

        closes = hist["Close"].dropna()
        if closes.empty:
            return DailyDukaanData(
                symbol=sym, company_name=company_name,
                error="No close price data",
            )

        actual_date   = str(closes.index[-1].date())
        current_price = round(float(closes.iloc[-1]), 2)

        dma20 = price_vs_dma20 = pct_vs_dma20 = None
        if len(closes) >= 20:
            dma20          = round(float(closes.rolling(20).mean().iloc[-1]), 2)
            price_vs_dma20 = round(current_price - dma20, 2)
            pct_vs_dma20   = round((current_price - dma20) / dma20 * 100, 2)

        return DailyDukaanData(
            symbol=sym,
            company_name=company_name,
            ref_date=actual_date,
            current_price=current_price,
            dma20=dma20,
            price_vs_dma20=price_vs_dma20,
            pct_vs_dma20=pct_vs_dma20,
            data_source=source,
            fetched_at=datetime.utcnow(),
        )
