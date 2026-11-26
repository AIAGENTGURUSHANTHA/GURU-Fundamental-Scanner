"""
Eshwarana Fruit Forest Fetcher.

Fetches current price, 52W Low, 3-Year average price, and 5-Year average price
for NSE-listed ETFs.

Data sources:
  - NSE India (nsepython equity_history, 35-day window) — current price & trading date.
    Same source as Daily Dukaan; always up-to-date, never 3-5 day lag.
  - Yahoo Finance (5-year history) — 52W Low, 3Y Avg, 5Y Avg calculations only.
    Falls back to Yahoo Finance price/date if nsepython fails.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from typing import List, Optional, Tuple

import pandas as pd
import yfinance as yf

from src.data.eff_models import EFFData

# NSE India history window (< 40 days = 1 HTTP request per symbol in nsepython)
_NSE_HIST_DAYS = 35

# Yahoo Finance lookback windows (calendar days) — for statistical averages only
_DAYS_1Y  = 366
_DAYS_3Y  = 3 * 366
_DAYS_5Y  = 5 * 366


class EFFFetcher:
    """Calculates 52W Low, 3Y Avg, 5Y Avg and price-gap metrics for NSE symbols."""

    _name_cache: dict = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def fetch(self, symbol: str, ref_date: Optional[date] = None) -> EFFData:
        sym      = symbol.strip().upper()
        ref_date = ref_date or date.today()
        return self._fetch_one(sym, ref_date)

    # ── Core fetch ────────────────────────────────────────────────────────────

    def _fetch_one(self, sym: str, ref_date: date) -> EFFData:
        try:
            # ── Step 1: Yahoo Finance 5Y history — statistical averages only ──
            ticker    = yf.Ticker(f"{sym}.NS")
            start_5y  = ref_date - timedelta(days=_DAYS_5Y + 30)  # buffer for weekends
            end_dt    = ref_date + timedelta(days=1)

            hist = ticker.history(
                start=start_5y.strftime("%Y-%m-%d"),
                end=end_dt.strftime("%Y-%m-%d"),
                interval="1d",
                auto_adjust=True,
            )

            if hist is None or hist.empty:
                return EFFData(symbol=sym, error="No historical data from Yahoo Finance.")

            # Normalise index to date-only (tz-naive)
            hist.index = pd.to_datetime(hist.index)
            if hist.index.tz is not None:
                hist.index = hist.index.tz_convert(None)
            hist = hist[hist.index.date <= ref_date].copy()

            if hist.empty:
                return EFFData(symbol=sym, error=f"No trading data on or before {ref_date}.")

            closes = hist["Close"].dropna()
            if closes.empty:
                return EFFData(symbol=sym, error="No close price data.")

            # Yahoo Finance last-close (fallback if nsepython fails)
            yf_date   = str(closes.index[-1].date())
            yf_price  = round(float(closes.iloc[-1]), 2)

            # 52W Low — minimum close in the most recent 1-year window
            cutoff_1y  = closes.index[-1] - pd.DateOffset(days=_DAYS_1Y)
            closes_1y  = closes[closes.index >= cutoff_1y]
            week52_low = round(float(closes_1y.min()), 2) if not closes_1y.empty else None

            # 3Y Avg Price
            cutoff_3y    = closes.index[-1] - pd.DateOffset(days=_DAYS_3Y)
            closes_3y    = closes[closes.index >= cutoff_3y]
            avg_price_3y = round(float(closes_3y.mean()), 2) if len(closes_3y) >= 10 else None

            # 5Y Avg Price
            avg_price_5y = round(float(closes.mean()), 2) if len(closes) >= 10 else None

            # ── Step 2: NSE India via nsepython — accurate current price & date ──
            nse_date, nse_price = self._fetch_nsepython_price(sym, ref_date)

            # Use NSE India data if available; otherwise fall back to Yahoo Finance
            actual_date   = nse_date  if nse_date  else yf_date
            current_price = nse_price if nse_price else yf_price
            data_src      = "NSE India" if nse_date else "Yahoo Finance (NSE)"

            # Derived fields — computed from the final current_price
            diff_52w_low   = round(current_price - week52_low,   2) if week52_low   else None
            pct_vs_52w_low = round((current_price - week52_low)   / week52_low   * 100, 2) if week52_low   else None

            diff_3y   = round(current_price - avg_price_3y, 2) if avg_price_3y else None
            pct_vs_3y = round((current_price - avg_price_3y) / avg_price_3y * 100, 2) if avg_price_3y else None

            diff_5y   = round(current_price - avg_price_5y, 2) if avg_price_5y else None
            pct_vs_5y = round((current_price - avg_price_5y) / avg_price_5y * 100, 2) if avg_price_5y else None

            company_name = self._get_company_name(sym, ticker)

            return EFFData(
                symbol=sym,
                company_name=company_name,
                current_price=current_price,
                ref_date=actual_date,
                price_on_date=current_price,
                week52_low=week52_low,
                diff_52w_low=diff_52w_low,
                pct_vs_52w_low=pct_vs_52w_low,
                avg_price_3y=avg_price_3y,
                diff_3y=diff_3y,
                pct_vs_3y=pct_vs_3y,
                avg_price_5y=avg_price_5y,
                diff_5y=diff_5y,
                pct_vs_5y=pct_vs_5y,
                data_source=data_src,
                fetched_at=datetime.utcnow(),
            )

        except Exception as exc:
            return EFFData(symbol=sym, error=str(exc))

    def _fetch_nsepython_price(self, sym: str, ref_date: date) -> Tuple[Optional[str], Optional[float]]:
        """NSE India equity history (35-day window) → (actual_date, current_price).

        Mirrors Daily Dukaan's _fetch_nsepython approach for consistent, up-to-date
        trading dates. Returns (None, None) on any failure so caller falls back to
        Yahoo Finance.
        """
        try:
            from nsepython import equity_history  # type: ignore

            start     = ref_date - timedelta(days=_NSE_HIST_DAYS)
            start_str = start.strftime("%d-%m-%Y")
            end_str   = ref_date.strftime("%d-%m-%Y")

            df = equity_history(sym, "EQ", start_str, end_str)
            if df is None or not isinstance(df, pd.DataFrame) or df.empty:
                return None, None

            df = df.copy()
            df["_date"] = pd.to_datetime(df["CH_TIMESTAMP"])
            df = df.sort_values("_date")
            df["Close"] = pd.to_numeric(df["CH_CLOSING_PRICE"], errors="coerce")
            df = df[df["_date"].dt.date <= ref_date]

            closes = df["Close"].dropna()
            if closes.empty:
                return None, None

            actual_date   = str(df.iloc[-1]["_date"].date())
            current_price = round(float(closes.iloc[-1]), 2)
            return actual_date, current_price

        except Exception:
            return None, None

    # ── Company name (cached) ─────────────────────────────────────────────────

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
