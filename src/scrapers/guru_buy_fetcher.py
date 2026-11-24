"""
GURU BUY — Rainbow indicator fetcher.

Pine Script origin:
    EMA   = ta.ema(close, 2)
    EMAD1 = ta.ema(EMA,   2)   ...  EMAD6 = ta.ema(EMAD5, 2)

Color logic (green = BUY):
    EMA   : green if EMA   >= EMAD1
    EMAD1 : green if EMAD1 >= EMAD2
    EMAD2 : green if EMAD2 >= EMAD3
    EMAD3 : green if EMAD3 >= EMAD4
    EMAD4 : green if EMAD4 >= EMAD5
    EMAD5 : green if EMAD5 >= EMAD6
    EMAD6 : green if EMA   >= EMAD6  (i.e., not EMAD6 > EMA)

GURU signal:
    7/7 green → STRONG BUY
    5-6/7     → BUY
    4/7       → NEUTRAL
    2-3/7     → SELL
    0-1/7     → STRONG SELL

Data sources:
  - NSE India (nsepython equity_history, 35-day window) — current price & trading date.
  - Yahoo Finance (400-day history) — Rainbow EMA calculations. Falls back for price/date if nsepython fails.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from typing import List, Optional, Tuple

import pandas as pd
import yfinance as yf

from src.data.guru_buy_models import GURUBuyData

_NSE_HIST_DAYS = 35  # keeps nsepython to 1 HTTP request per symbol


class GURUBuyFetcher:
    _HIST_DAYS = 400  # Rainbow EMA(2) converges fast; 400 days is ample

    # ── Public API ────────────────────────────────────────────────────────────

    def fetch(self, symbol: str, ref_date: Optional[date] = None) -> GURUBuyData:
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
                return GURUBuyData(symbol=sym, error="No data from Yahoo Finance. Verify the NSE symbol.")
            company_name = self._get_company_name(t, sym)
            result = self._calc(sym, company_name, hist, ref_date)
        except Exception as exc:
            return GURUBuyData(symbol=sym, error=f"Fetch error: {exc}")

        # NSE India override: accurate current price + trading date
        # (signals are EMA-to-EMA comparisons, no recalculation needed)
        nse_date, nse_price = self._fetch_nsepython_price(sym, ref_date)
        if nse_date and nse_price and not result.error:
            result = result.model_copy(update={
                "current_price": nse_price, "ref_date": nse_date,
                "data_source": "NSE India",
            })
        return result

    def fetch_bulk(self, symbols: List[str], ref_date: Optional[date] = None,
                   max_workers: int = 5) -> List[GURUBuyData]:
        ref_date = ref_date or date.today()
        results_map: dict = {}
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(self.fetch, sym, ref_date): sym for sym in symbols}
            for future in as_completed(futures):
                sym = futures[future]
                try:
                    results_map[sym] = future.result(timeout=120)
                except Exception as exc:
                    results_map[sym] = GURUBuyData(symbol=sym.upper(), error=str(exc))
        return [results_map.get(s, GURUBuyData(symbol=s, error="Not fetched")) for s in symbols]

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
    def _calc(sym: str, company_name: str, hist: pd.DataFrame, ref_date: date) -> GURUBuyData:
        hist.index = pd.to_datetime(hist.index)
        if hist.index.tz is not None:
            hist.index = hist.index.tz_convert(None)
        hist = hist[hist.index.date <= ref_date]
        if hist.empty:
            return GURUBuyData(symbol=sym, company_name=company_name,
                               error=f"No trading data on or before {ref_date}")

        closes = hist["Close"].dropna()
        if len(closes) < 2:
            return GURUBuyData(symbol=sym, company_name=company_name,
                               error="Insufficient data for EMA(2) calculation")

        actual_date = str(closes.index[-1].date())
        current_price = round(float(closes.iloc[-1]), 2)

        # Chained EMA(2) — matches Pine Script ta.ema(source, 2)
        ema_s   = closes.ewm(span=2, adjust=False).mean()
        emad1_s = ema_s.ewm(span=2,   adjust=False).mean()
        emad2_s = emad1_s.ewm(span=2, adjust=False).mean()
        emad3_s = emad2_s.ewm(span=2, adjust=False).mean()
        emad4_s = emad3_s.ewm(span=2, adjust=False).mean()
        emad5_s = emad4_s.ewm(span=2, adjust=False).mean()
        emad6_s = emad5_s.ewm(span=2, adjust=False).mean()

        def _last(s): return round(float(s.iloc[-1]), 2)

        ema   = _last(ema_s)
        emad1 = _last(emad1_s)
        emad2 = _last(emad2_s)
        emad3 = _last(emad3_s)
        emad4 = _last(emad4_s)
        emad5 = _last(emad5_s)
        emad6 = _last(emad6_s)

        # Signals — True = green (bullish), matching Pine Script color logic
        sig_ema = ema   >= emad1
        sig_d1  = emad1 >= emad2
        sig_d2  = emad2 >= emad3
        sig_d3  = emad3 >= emad4
        sig_d4  = emad4 >= emad5
        sig_d5  = emad5 >= emad6
        sig_d6  = ema   >= emad6   # green when NOT (EMAD6 > EMA)

        buy_count = sum([sig_ema, sig_d1, sig_d2, sig_d3, sig_d4, sig_d5, sig_d6])

        if buy_count == 7:   guru_signal = "STRONG BUY"
        elif buy_count >= 5: guru_signal = "BUY"
        elif buy_count == 4: guru_signal = "NEUTRAL"
        elif buy_count >= 2: guru_signal = "SELL"
        else:                guru_signal = "STRONG SELL"

        return GURUBuyData(
            symbol=sym,
            company_name=company_name,
            ref_date=actual_date,
            current_price=current_price,
            ema=ema, emad1=emad1, emad2=emad2, emad3=emad3,
            emad4=emad4, emad5=emad5, emad6=emad6,
            sig_ema=sig_ema, sig_d1=sig_d1, sig_d2=sig_d2, sig_d3=sig_d3,
            sig_d4=sig_d4,   sig_d5=sig_d5, sig_d6=sig_d6,
            buy_count=buy_count,
            guru_signal=guru_signal,
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
