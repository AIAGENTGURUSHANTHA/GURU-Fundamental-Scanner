"""
RSI (Relative Strength Index) Fetcher.

Calculates 14-period RSI across three timeframes from a single yfinance pull:
  - Daily RSI   — from daily close prices
  - Weekly RSI  — daily series resampled to week-end (Friday last-close)
  - Monthly RSI — daily series resampled to month-end last-close

Uses Wilder's smoothing (EMA with com=13, equivalent to alpha=1/14).
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from typing import List, Optional

import pandas as pd
import yfinance as yf

from src.data.rsi_models import RSIData

_RSI_PERIOD = 14
_HIST_DAYS  = 800   # warm-up window; daily RSI needs ~28 periods, monthly needs ~15 months


class RSIFetcher:
    """Calculates Daily/Weekly/Monthly RSI for NSE stocks and ETFs."""

    _name_cache: dict = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def fetch(self, symbol: str, ref_date: Optional[date] = None) -> RSIData:
        sym      = symbol.strip().upper()
        ref_date = ref_date or date.today()
        t        = yf.Ticker(f"{sym}.NS")
        try:
            start = ref_date - timedelta(days=_HIST_DAYS)
            hist  = t.history(
                start=str(start),
                end=str(ref_date + timedelta(days=1)),
                interval="1d",
                auto_adjust=True,
            )
            if hist.empty:
                return RSIData(symbol=sym,
                               error="No data from Yahoo Finance. Verify the NSE symbol.")
            company_name = self._get_company_name(sym, t)
            return self._calc_rsi(sym, company_name, hist, ref_date)
        except Exception as exc:
            return RSIData(symbol=sym, error=f"Fetch error: {exc}")

    # ── Company name (class-level cache) ─────────────────────────────────────

    def _get_company_name(self, sym: str,
                          ticker: Optional[yf.Ticker] = None) -> str:
        if sym in self._name_cache:
            return self._name_cache[sym]
        name = ""
        try:
            t = ticker or yf.Ticker(f"{sym}.NS")
            fi = t.fast_info
            for attr in ("long_name", "longName", "short_name", "shortName"):
                val = getattr(fi, attr, None)
                if val:
                    name = str(val)
                    break
            if not name:
                info = t.info or {}
                name = info.get("longName") or info.get("shortName") or ""
        except Exception:
            pass
        name = name or sym
        self._name_cache[sym] = name
        return name

    # ── RSI calculation ───────────────────────────────────────────────────────

    @staticmethod
    def _calc_rsi_series(closes: pd.Series) -> Optional[float]:
        """Return 14-period RSI of the last bar using Wilder's EMA smoothing."""
        if len(closes) < _RSI_PERIOD + 1:
            return None
        delta    = closes.diff().dropna()
        gain     = delta.where(delta > 0, 0.0)
        loss     = (-delta).where(delta < 0, 0.0)
        avg_gain = gain.ewm(com=_RSI_PERIOD - 1, adjust=False).mean()
        avg_loss = loss.ewm(com=_RSI_PERIOD - 1, adjust=False).mean()
        last_loss = avg_loss.iloc[-1]
        if last_loss == 0:
            return 100.0
        rs  = avg_gain.iloc[-1] / last_loss
        rsi = 100.0 - (100.0 / (1.0 + rs))
        return round(float(rsi), 2)

    @classmethod
    def _calc_rsi(cls, sym: str, company_name: str,
                  hist: pd.DataFrame, ref_date: date) -> RSIData:
        hist = hist.copy()
        hist.index = pd.to_datetime(hist.index)
        if hist.index.tz is not None:
            hist.index = hist.index.tz_convert(None)

        hist = hist[hist.index.date <= ref_date]
        if hist.empty:
            return RSIData(symbol=sym, company_name=company_name,
                           error=f"No trading data on or before {ref_date}")

        closes = hist["Close"].dropna()
        if closes.empty:
            return RSIData(symbol=sym, company_name=company_name,
                           error="No close price data")

        actual_date   = str(closes.index[-1].date())
        current_price = round(float(closes.iloc[-1]), 2)

        daily_rsi = cls._calc_rsi_series(closes)

        closes_weekly = closes.resample("W").last().dropna()
        weekly_rsi = cls._calc_rsi_series(closes_weekly)

        try:
            closes_monthly = closes.resample("ME").last().dropna()
        except Exception:
            closes_monthly = closes.resample("M").last().dropna()
        monthly_rsi = cls._calc_rsi_series(closes_monthly)

        return RSIData(
            symbol=sym,
            company_name=company_name,
            ref_date=actual_date,
            current_price=current_price,
            daily_rsi=daily_rsi,
            weekly_rsi=weekly_rsi,
            monthly_rsi=monthly_rsi,
            data_source="Yahoo Finance (NSE)",
            fetched_at=datetime.utcnow(),
        )
