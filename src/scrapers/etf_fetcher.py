"""
NSE ETF Fetcher.

Two responsibilities:
1.  fetch_live_list()   — pulls the full NSE ETF market-data snapshot from
                          https://www.nseindia.com/api/etf  (live prices, volume,
                          52-week range, NAV, etc.)
2.  fetch_ema / fetch_ema_bulk — calculates EMA(1-200) for ETF symbols using
                          yfinance (.NS suffix), identical to the stock EMA tab.
"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from typing import List, Optional

import pandas as pd
import requests
import yfinance as yf

from src.data.ema_models import EMAData
from src.data.etf_models import ETFLiveData, ETFMarketSummary

EMA_PERIODS = [1, 2, 3, 4, 5, 6, 7, 10, 20, 22, 50, 100, 200]
_HIST_DAYS  = 800


# ── helpers ───────────────────────────────────────────────────────────────────

def _safe_float(val) -> Optional[float]:
    if val is None:
        return None
    try:
        return round(float(str(val).replace(",", "").strip()), 2)
    except (ValueError, TypeError):
        return None


def _safe_int(val) -> Optional[int]:
    if val is None:
        return None
    try:
        return int(float(str(val).replace(",", "").strip()))
    except (ValueError, TypeError):
        return None


# ── NSE session ───────────────────────────────────────────────────────────────

_NSE_HOME    = "https://www.nseindia.com"
_NSE_ETF_API = "https://www.nseindia.com/api/etf"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Referer": "https://www.nseindia.com/market-data/exchange-traded-funds-etf",
}


def _create_nse_session() -> requests.Session:
    """Return a requests.Session with NSE cookies established."""
    sess = requests.Session()
    sess.headers.update(_HEADERS)
    try:
        sess.get(_NSE_HOME, timeout=10)
        time.sleep(0.4)
        sess.get(
            "https://www.nseindia.com/market-data/exchange-traded-funds-etf",
            timeout=10,
        )
        time.sleep(0.3)
    except Exception:
        pass
    return sess


# ── ETFFetcher ────────────────────────────────────────────────────────────────

class ETFFetcher:
    """Fetches NSE ETF live data and calculates EMA values for ETF symbols."""

    # ── Live NSE data ─────────────────────────────────────────────────────────

    def fetch_live_list(self) -> tuple[list, ETFMarketSummary]:
        """
        Fetch the complete NSE ETF list with live market data.
        Returns (list_of_ETFLiveData, ETFMarketSummary).

        Actual NSE API field names (discovered from live response):
          symbol, assets, open, high, low, ltP, chn, per, qty,
          trdVal, nav, wkhi, wklo, prevClose, stockIndClosePrice,
          perChange365d, perChange30d, series
        """
        sess   = _create_nse_session()
        resp   = sess.get(_NSE_ETF_API, timeout=20)
        resp.raise_for_status()
        raw    = resp.json()

        items     = raw.get("data", [])
        timestamp = raw.get("timestamp", "")
        nav_date  = raw.get("navDate", "")
        now       = datetime.utcnow()

        summary = ETFMarketSummary(
            advances             = int(raw.get("advances", 0) or 0),
            declines             = int(raw.get("declines", 0) or 0),
            unchanged            = int(raw.get("unchanged", 0) or 0),
            total_traded_value_cr= _safe_float((raw.get("totalTradedValue") or 0) / 1e7),
            total_traded_volume  = _safe_int(raw.get("totalTradedVolume")),
            nav_date             = str(nav_date),
            nse_timestamp        = str(timestamp),
        )

        result: list = []
        for item in items:
            sym = (item.get("symbol") or "").strip().upper()
            if not sym:
                continue
            try:
                # NSE API stores most numeric values as strings
                result.append(ETFLiveData(
                    symbol            = sym,
                    assets            = str(item.get("assets") or ""),
                    series            = str(item.get("series") or ""),
                    open              = _safe_float(item.get("open")),
                    high              = _safe_float(item.get("high")),
                    low               = _safe_float(item.get("low")),
                    prev_close        = _safe_float(item.get("prevClose")),
                    ltp               = _safe_float(item.get("ltP")),
                    indicative_close  = _safe_float(item.get("stockIndClosePrice")),
                    change            = _safe_float(item.get("chn")),
                    pct_change        = _safe_float(item.get("per")),
                    volume            = _safe_int(item.get("qty")),
                    traded_value_cr   = _safe_float(
                        (float(str(item.get("trdVal") or 0).replace(",", "")) / 1e7)
                        if item.get("trdVal") else None
                    ),
                    nav               = _safe_float(item.get("nav")),
                    week52_high       = _safe_float(item.get("wkhi")),
                    week52_low        = _safe_float(item.get("wklo")),
                    pct_change_30d    = _safe_float(item.get("perChange30d")),
                    pct_change_365d   = _safe_float(item.get("perChange365d")),
                    fetched_at        = now,
                ))
            except Exception as exc:
                result.append(ETFLiveData(symbol=sym, error=str(exc), fetched_at=now))

        return result, summary

    # ── EMA analysis (mirrors EMAFetcher, same logic) ─────────────────────────

    def fetch_ema(self, symbol: str, ref_date: Optional[date] = None,
                  scheme_name: str = "") -> EMAData:
        """Calculate EMA(1-200) for one ETF symbol using yfinance."""
        sym      = symbol.strip().upper()
        ref_date = ref_date or date.today()
        t        = yf.Ticker(f"{sym}.NS")
        try:
            start = ref_date - timedelta(days=_HIST_DAYS)
            hist  = t.history(
                start=str(start),
                end=str(ref_date + timedelta(days=1)),
                auto_adjust=True,
            )
            if hist.empty:
                return EMAData(symbol=sym, company_name=scheme_name,
                               error="No data from Yahoo Finance. Verify the NSE symbol.")
            cname = scheme_name or self._get_scheme_name(t, sym)
            return self._calc_ema(sym, cname, hist, ref_date)
        except Exception as exc:
            return EMAData(symbol=sym, company_name=scheme_name,
                           error=f"Fetch error: {exc}")

    # ── Internal ──────────────────────────────────────────────────────────────

    @staticmethod
    def _get_scheme_name(ticker: yf.Ticker, fallback: str) -> str:
        try:
            fi = ticker.fast_info
            for attr in ("long_name", "longName", "short_name", "shortName"):
                name = getattr(fi, attr, None)
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
    def _calc_ema(sym: str, scheme_name: str, hist: pd.DataFrame,
                  ref_date: date) -> EMAData:
        hist.index = pd.to_datetime(hist.index)
        if hist.index.tz is not None:
            hist.index = hist.index.tz_convert(None)

        hist = hist[hist.index.date <= ref_date]
        if hist.empty:
            return EMAData(symbol=sym, company_name=scheme_name,
                           error=f"No trading data on or before {ref_date}")

        closes = hist["Close"].dropna()
        if closes.empty:
            return EMAData(symbol=sym, company_name=scheme_name,
                           error="No close price data")

        actual_date   = str(closes.index[-1].date())
        current_price = round(float(closes.iloc[-1]), 2)

        emas: dict = {}
        for p in EMA_PERIODS:
            if len(closes) >= p:
                emas[p] = round(float(closes.ewm(span=p, adjust=False).mean().iloc[-1]), 2)
            else:
                emas[p] = None

        above_count = sum(1 for v in emas.values() if v is not None and current_price > v)

        return EMAData(
            symbol=sym,
            company_name=scheme_name,
            ref_date=actual_date,
            current_price=current_price,
            ema1=emas[1],   ema2=emas[2],   ema3=emas[3],   ema4=emas[4],
            ema5=emas[5],   ema6=emas[6],   ema7=emas[7],   ema10=emas[10],
            ema20=emas[20], ema22=emas[22], ema50=emas[50],
            ema100=emas[100], ema200=emas[200],
            above_ema_count=above_count,
            data_source="Yahoo Finance (NSE)",
            fetched_at=datetime.utcnow(),
        )
