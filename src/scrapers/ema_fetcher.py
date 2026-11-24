"""
EMA (Exponential Moving Average) fetcher.
Fetches historical close prices from Yahoo Finance (NSE:.NS suffix)
and calculates EMA for periods: 1,2,3,4,5,6,7,10,20,22,50,100,200.

EMA formula:  EMA_t = Price_t × k  +  EMA_(t-1) × (1 − k)
              where k = 2 / (span + 1)
Implemented via pandas.Series.ewm(span=N, adjust=False).

Data sources:
  - NSE India (nsepython equity_history, 35-day window) — current price & trading date.
  - Yahoo Finance (800-day history) — EMA calculations. Falls back for price/date if nsepython fails.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from typing import List, Optional, Tuple

import pandas as pd
import yfinance as yf

from src.data.ema_models import EMAData

EMA_PERIODS = [1, 2, 3, 4, 5, 6, 7, 10, 20, 22, 50, 100, 200]

_NSE_HIST_DAYS = 35  # keeps nsepython to 1 HTTP request per symbol


class EMAFetcher:
    """Fetches NSE stock history from Yahoo Finance and calculates EMA values."""

    _HIST_DAYS = 800  # ~200 trading days + ample buffer

    # ── Public API ────────────────────────────────────────────────────────────

    def fetch_ema(self, symbol: str, ref_date: Optional[date] = None) -> EMAData:
        """Fetch single symbol with company name."""
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
                return EMAData(symbol=sym, error="No data from Yahoo Finance. Verify the NSE symbol.")
            company_name = self._get_company_name(t, sym)
            result = self._calc_ema(sym, company_name, hist, ref_date)
        except Exception as exc:
            return EMAData(symbol=sym, error=f"Fetch error: {exc}")

        # NSE India override: accurate current price + trading date
        nse_date, nse_price = self._fetch_nsepython_price(sym, ref_date)
        if nse_date and nse_price and not result.error:
            emas = [result.ema1, result.ema2, result.ema3, result.ema4,
                    result.ema5, result.ema6, result.ema7, result.ema10,
                    result.ema20, result.ema22, result.ema50, result.ema100, result.ema200]
            above_count = sum(1 for v in emas if v is not None and nse_price > v)
            result = result.model_copy(update={
                "current_price": nse_price, "ref_date": nse_date,
                "above_ema_count": above_count, "data_source": "NSE India",
            })
        return result

    def fetch_ema_bulk(self, symbols: List[str], ref_date: Optional[date] = None,
                       max_workers: int = 5) -> List[EMAData]:
        """Parallel bulk fetch. Returns results in the same order as `symbols`."""
        ref_date = ref_date or date.today()
        results_map: dict = {}

        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(self.fetch_ema, sym, ref_date): sym for sym in symbols}
            for future in as_completed(futures):
                sym = futures[future]
                try:
                    results_map[sym] = future.result(timeout=120)
                except Exception as exc:
                    results_map[sym] = EMAData(symbol=sym.upper(), error=str(exc))

        return [results_map.get(s, EMAData(symbol=s, error="Not fetched")) for s in symbols]

    # ── Internal ──────────────────────────────────────────────────────────────

    @staticmethod
    def _get_company_name(ticker: yf.Ticker, fallback: str) -> str:
        try:
            fi = ticker.fast_info
            for attr in ("long_name", "longName", "short_name", "shortName"):
                name = getattr(fi, attr, None)
                if name:
                    return str(name)
            raw = getattr(fi, "_data", None)
            if raw:
                name = raw.get("longName") or raw.get("shortName")
                if name:
                    return str(name)
        except Exception:
            pass
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
    def _calc_ema(sym: str, company_name: str, hist: pd.DataFrame, ref_date: date) -> EMAData:
        """Calculate EMA values using pandas ewm (adjust=False = standard recursive EMA)."""
        hist.index = pd.to_datetime(hist.index)
        if hist.index.tz is not None:
            hist.index = hist.index.tz_convert(None)

        hist = hist[hist.index.date <= ref_date]
        if hist.empty:
            return EMAData(symbol=sym, company_name=company_name,
                           error=f"No trading data on or before {ref_date}")

        closes = hist["Close"].dropna()
        if closes.empty:
            return EMAData(symbol=sym, company_name=company_name, error="No close price data")

        actual_date = str(closes.index[-1].date())
        current_price = round(float(closes.iloc[-1]), 2)

        # Exponential moving average: EMA_t = price × k + EMA_(t-1) × (1−k), k = 2/(span+1)
        emas: dict = {}
        for p in EMA_PERIODS:
            if len(closes) >= p:
                ema_series = closes.ewm(span=p, adjust=False).mean()
                emas[p] = round(float(ema_series.iloc[-1]), 2)
            else:
                emas[p] = None

        available = [v for v in emas.values() if v is not None]
        above_count = sum(1 for v in available if current_price > v)

        return EMAData(
            symbol=sym,
            company_name=company_name,
            ref_date=actual_date,
            current_price=current_price,
            ema1=emas[1],   ema2=emas[2],   ema3=emas[3],   ema4=emas[4],
            ema5=emas[5],   ema6=emas[6],   ema7=emas[7],   ema10=emas[10],
            ema20=emas[20], ema22=emas[22], ema50=emas[50],
            ema100=emas[100], ema200=emas[200],
            above_ema_count=above_count,
            data_source="Yahoo Finance (NSE)",
            fetched_at=datetime.now(_IST),
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
