"""
DMA (Daily Moving Average) fetcher.
Fetches historical close prices from Yahoo Finance (NSE:.NS suffix)
and calculates SMAs for periods: 1,2,3,4,5,6,7,10,20,22,50,100,200.

Data sources:
  - NSE India (nsepython equity_history, 35-day window) — current price & trading date.
  - Yahoo Finance (800-day history) — DMA calculations. Falls back for price/date if nsepython fails.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from typing import List, Optional, Tuple

import pandas as pd
import yfinance as yf

from src.data.dma_models import DMAData

DMA_PERIODS = [1, 2, 3, 4, 5, 6, 7, 10, 20, 22, 50, 100, 200]

_NSE_HIST_DAYS = 35  # keeps nsepython to 1 HTTP request per symbol


class DMAFetcher:
    """Fetches NSE stock history from Yahoo Finance and calculates DMA values."""

    _HIST_DAYS = 800  # ~200 trading days + ample buffer

    # ── Public API ────────────────────────────────────────────────────────────

    def fetch_dma(self, symbol: str, ref_date: Optional[date] = None) -> DMAData:
        """Fetch single symbol with company name (used for single-stock card view)."""
        sym = symbol.strip().upper()
        ref_date = ref_date or date.today()
        t = yf.Ticker(f"{sym}.NS")
        try:
            start = ref_date - timedelta(days=self._HIST_DAYS)
            hist = t.history(
                start=str(start),
                end=str(ref_date + timedelta(days=1)),
                auto_adjust=True,
            )
            if hist.empty:
                return DMAData(symbol=sym, error="No data from Yahoo Finance. Verify the NSE symbol.")
            company_name = self._get_company_name(t, sym)
            result = self._calc_dma(sym, company_name, hist, ref_date)
        except Exception as exc:
            return DMAData(symbol=sym, error=f"Fetch error: {exc}")

        # NSE India override: accurate current price + trading date
        nse_date, nse_price = self._fetch_nsepython_price(sym, ref_date)
        if nse_date and nse_price and not result.error:
            dmas = [result.dma1, result.dma2, result.dma3, result.dma4,
                    result.dma5, result.dma6, result.dma7, result.dma10,
                    result.dma20, result.dma22, result.dma50, result.dma100, result.dma200]
            above_count = sum(1 for v in dmas if v is not None and nse_price > v)
            result = result.model_copy(update={
                "current_price": nse_price, "ref_date": nse_date,
                "above_dma_count": above_count, "data_source": "NSE India",
            })
        return result

    # ── Internal ──────────────────────────────────────────────────────────────

    @staticmethod
    def _get_company_name(ticker: yf.Ticker, fallback: str) -> str:
        """Try multiple yfinance attributes to get the human-readable company name."""
        # 1. fast_info standard properties (available in some yfinance versions)
        try:
            fi = ticker.fast_info
            for attr in ("long_name", "longName", "short_name", "shortName"):
                name = getattr(fi, attr, None)
                if name:
                    return str(name)
            # 2. fast_info raw response dict (yfinance 0.2.x internal)
            raw = getattr(fi, "_data", None)
            if raw:
                name = raw.get("longName") or raw.get("shortName")
                if name:
                    return str(name)
        except Exception:
            pass
        # 3. Full info dict (slower but always has the name)
        try:
            info = ticker.info
            if info:
                name = info.get("longName") or info.get("shortName")
                if name:
                    return str(name)
        except Exception:
            pass
        return fallback

    @staticmethod
    def _calc_dma(sym: str, company_name: str, hist: pd.DataFrame, ref_date: date) -> DMAData:
        """Core calculation: derive DMA values from an OHLCV DataFrame."""
        hist.index = pd.to_datetime(hist.index)
        if hist.index.tz is not None:
            hist.index = hist.index.tz_convert(None)

        hist = hist[hist.index.date <= ref_date]
        if hist.empty:
            return DMAData(symbol=sym, company_name=company_name,
                           error=f"No trading data on or before {ref_date}")

        closes = hist["Close"].dropna()
        if closes.empty:
            return DMAData(symbol=sym, company_name=company_name, error="No close price data")

        actual_date = str(closes.index[-1].date())
        current_price = round(float(closes.iloc[-1]), 2)

        dmas: dict = {}
        for p in DMA_PERIODS:
            dmas[p] = round(float(closes.tail(p).mean()), 2) if len(closes) >= p else None

        available = [v for v in dmas.values() if v is not None]
        above_count = sum(1 for v in available if current_price > v)

        return DMAData(
            symbol=sym,
            company_name=company_name,
            ref_date=actual_date,
            current_price=current_price,
            dma1=dmas[1],   dma2=dmas[2],   dma3=dmas[3],   dma4=dmas[4],
            dma5=dmas[5],   dma6=dmas[6],   dma7=dmas[7],   dma10=dmas[10],
            dma20=dmas[20], dma22=dmas[22], dma50=dmas[50],
            dma100=dmas[100], dma200=dmas[200],
            above_dma_count=above_count,
            data_source="Yahoo Finance (NSE)",
            fetched_at=datetime.utcnow(),
        )

    def _fetch_nsepython_price(self, sym: str, ref_date: date) -> Tuple[Optional[str], Optional[float]]:
        """NSE India equity history (35-day window) → (actual_date, current_price).
        Returns (None, None) on any failure so caller falls back to Yahoo Finance.
        """
        try:
            from nsepython import equity_history  # type: ignore
            start = ref_date - timedelta(days=_NSE_HIST_DAYS)
            df = equity_history(sym, "EQ", start.strftime("%d-%m-%Y"), ref_date.strftime("%d-%m-%Y"))
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

