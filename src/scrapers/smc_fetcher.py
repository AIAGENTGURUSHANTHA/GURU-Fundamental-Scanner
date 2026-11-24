"""
Smart Money Concepts — Premium / Discount Zone Fetcher.

Replicates the LuxAlgo "Smart Money Concepts" PineScript indicator logic for
the Premium & Discount Zones section.

Algorithm (mirrors the PineScript exactly):
1.  Swing detection (right-side confirmation only):
      Swing HIGH at bar j  →  high[j] > max(high[j+1 : j+size+1])
      Swing LOW  at bar j  →  low[j]  < min(low[j+1  : j+size+1])
    Default size = 50 (matches swingsLengthInput default in the script).

2.  Trailing extremes (matches updateTrailingExtremes()):
      trailing_top    = max(all highs from last_swing_high_idx → current bar)
      trailing_bottom = min(all lows  from last_swing_low_idx  → current bar)

3.  Zone boundaries (matches drawPremiumDiscountZones()):
      Premium zone:    [0.95·top + 0.05·bot, top]
      Near Premium:    [0.525·top + 0.475·bot, 0.95·top + 0.05·bot)
      Equilibrium:     [0.525·bot + 0.475·top, 0.525·top + 0.475·bot]
      Near Discount:   (0.95·bot + 0.05·top, 0.525·bot + 0.475·top)
      Discount zone:   [bot, 0.95·bot + 0.05·top]

Data sources:
  - NSE India (nsepython equity_history, 35-day window) — current price & trading date.
  - Yahoo Finance (800-day OHLC history) — swing detection & zone calculations. Falls back for price/date if nsepython fails.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from typing import List, Optional, Tuple

import pandas as pd
import yfinance as yf

from src.data.smc_models import SMCData

# Matches swingsLengthInput default in the PineScript
DEFAULT_SWING_SIZE = 50
# Enough history to find valid swings even on slow-moving stocks
_HIST_DAYS = 800

_NSE_HIST_DAYS = 35  # keeps nsepython to 1 HTTP request per symbol


class SMCFetcher:
    """Calculates LuxAlgo SMC Premium / Discount zones for NSE stocks."""

    # ── Public API ────────────────────────────────────────────────────────────

    def fetch(self, symbol: str, ref_date: Optional[date] = None,
              swing_size: int = DEFAULT_SWING_SIZE) -> SMCData:
        sym = symbol.strip().upper()
        ref_date = ref_date or date.today()
        t = yf.Ticker(f"{sym}.NS")
        try:
            start = ref_date - timedelta(days=_HIST_DAYS)
            hist = t.history(
                start=str(start),
                end=str(ref_date + timedelta(days=1)),
                auto_adjust=True,
            )
            if hist.empty:
                return SMCData(symbol=sym, error="No data from Yahoo Finance. Verify the NSE symbol.")
            company_name = self._get_company_name(t, sym)
            result = self._calc_zones(sym, company_name, hist, ref_date, swing_size)
        except Exception as exc:
            return SMCData(symbol=sym, error=f"Fetch error: {exc}")

        # NSE India override: accurate current price + trading date + reclassify zone
        nse_date, nse_price = self._fetch_nsepython_price(sym, ref_date)
        if nse_date and nse_price and not result.error:
            p = nse_price
            top = result.swing_high
            bot = result.swing_low
            if top and bot and top > bot:
                if p >= result.premium_bottom:
                    zone = "Premium"
                elif p >= result.eq_top:
                    zone = "Near Premium"
                elif p >= result.eq_bottom:
                    zone = "Equilibrium"
                elif p >= result.discount_top:
                    zone = "Near Discount"
                else:
                    zone = "Discount"
                zone_pct   = round((p - bot) / (top - bot) * 100, 1)
                buy_signal = (zone in ("Discount", "Near Discount") and result.fvg == "Bullish")
            else:
                zone, zone_pct, buy_signal = result.zone, result.zone_pct, result.buy_signal
            result = result.model_copy(update={
                "current_price": nse_price, "ref_date": nse_date,
                "zone": zone, "zone_pct": zone_pct, "buy_signal": buy_signal,
                "data_source": "NSE India",
            })
        return result

    def fetch_bulk(self, symbols: List[str], ref_date: Optional[date] = None,
                   swing_size: int = DEFAULT_SWING_SIZE,
                   max_workers: int = 5) -> List[SMCData]:
        ref_date = ref_date or date.today()
        results_map: dict = {}
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(self.fetch, sym, ref_date, swing_size): sym for sym in symbols}
            for future in as_completed(futures):
                sym = futures[future]
                try:
                    results_map[sym] = future.result(timeout=120)
                except Exception as exc:
                    results_map[sym] = SMCData(symbol=sym.upper(), error=str(exc))
        return [results_map.get(s, SMCData(symbol=s, error="Not fetched")) for s in symbols]

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
    def _find_swing_extremes(high_s: pd.Series, low_s: pd.Series, size: int):
        """
        Returns (last_swing_high_idx, last_swing_low_idx).

        Swing HIGH at bar j: high[j] > max(high[j+1 : j+size+1])
        Swing LOW  at bar j: low[j]  < min(low[j+1  : j+size+1])

        Mirrors the PineScript leg() function which uses right-side confirmation only.
        """
        n = len(high_s)
        sh_idx = None   # last (most recent) confirmed swing high index
        sl_idx = None   # last (most recent) confirmed swing low index

        for j in range(n - 1 - size, -1, -1):
            end = j + size + 1  # exclusive right bound

            if sh_idx is None:
                right_max = high_s.iloc[j + 1: end].max()
                if high_s.iloc[j] > right_max:
                    sh_idx = j

            if sl_idx is None:
                right_min = low_s.iloc[j + 1: end].min()
                if low_s.iloc[j] < right_min:
                    sl_idx = j

            if sh_idx is not None and sl_idx is not None:
                break

        return sh_idx, sl_idx

    @classmethod
    def _calc_zones(cls, sym: str, company_name: str,
                    hist: pd.DataFrame, ref_date: date,
                    swing_size: int) -> SMCData:
        hist.index = pd.to_datetime(hist.index)
        if hist.index.tz is not None:
            hist.index = hist.index.tz_convert(None)

        hist = hist[hist.index.date <= ref_date]
        if hist.empty:
            return SMCData(symbol=sym, company_name=company_name,
                           error=f"No trading data on or before {ref_date}")

        highs  = hist["High"].dropna()
        lows   = hist["Low"].dropna()
        closes = hist["Close"].dropna()

        if closes.empty:
            return SMCData(symbol=sym, company_name=company_name, error="No close price data")

        actual_date   = str(closes.index[-1].date())
        current_price = round(float(closes.iloc[-1]), 2)

        sh_idx, sl_idx = cls._find_swing_extremes(highs, lows, swing_size)

        # trailing.top  = max(all highs from last swing high → current bar)
        # trailing.bottom = min(all lows from last swing low  → current bar)
        trailing_top    = round(float(highs.iloc[sh_idx:].max()), 2) if sh_idx is not None else round(float(highs.max()), 2)
        trailing_bottom = round(float(lows.iloc[sl_idx:].min()),  2) if sl_idx is not None else round(float(lows.min()),  2)

        if trailing_top <= trailing_bottom:
            return SMCData(symbol=sym, company_name=company_name,
                           ref_date=actual_date, current_price=current_price,
                           error="Invalid swing range (top ≤ bottom)")

        top = trailing_top
        bot = trailing_bottom

        # Zone boundary prices (from drawPremiumDiscountZones in PineScript)
        premium_bottom_price  = round(0.95 * top + 0.05 * bot, 2)
        eq_top_price          = round(0.525 * top + 0.475 * bot, 2)
        eq_bottom_price       = round(0.525 * bot + 0.475 * top, 2)
        discount_top_price    = round(0.95 * bot + 0.05 * top, 2)

        # Classify zone
        p = current_price
        if p >= premium_bottom_price:
            zone = "Premium"
        elif p >= eq_top_price:
            zone = "Near Premium"
        elif p >= eq_bottom_price:
            zone = "Equilibrium"
        elif p >= discount_top_price:
            zone = "Near Discount"
        else:
            zone = "Discount"

        # Position within range (0 = at bottom, 100 = at top)
        zone_pct = round((p - bot) / (top - bot) * 100, 1)

        # Reference-date candle OHLC (last bar = actual trading day used)
        last_bar  = hist.iloc[-1]
        day_open  = round(float(last_bar["Open"]),  2)
        day_high  = round(float(last_bar["High"]),  2)
        day_low   = round(float(last_bar["Low"]),   2)
        day_close = round(float(last_bar["Close"]), 2)

        # Reached Discount? → True when the day's LOW touched/entered the Discount zone
        # Discount zone upper boundary = discount_top_price (= 0.95·bot + 0.05·top)
        reached_discount = day_low <= discount_top_price

        # Fair Value Gap (FVG) detection — 3-candle imbalance pattern ending on the ref date
        # Bullish FVG: bar[-3].High < bar[-1].Low  (gap up — unfilled supply imbalance)
        # Bearish FVG: bar[-3].Low  > bar[-1].High (gap down — unfilled demand imbalance)
        fvg = None
        if len(hist) >= 3:
            c0 = hist.iloc[-3]
            c2 = hist.iloc[-1]
            if float(c0["High"]) < float(c2["Low"]):
                fvg = "Bullish"
            elif float(c0["Low"]) > float(c2["High"]):
                fvg = "Bearish"

        # BUY SIGNAL: stock is in the Discount or Near Discount zone AND a Bullish FVG formed
        buy_signal = (zone in ("Discount", "Near Discount") and fvg == "Bullish")

        return SMCData(
            symbol=sym,
            company_name=company_name,
            ref_date=actual_date,
            current_price=current_price,
            swing_high=top,
            swing_low=bot,
            premium_top=top,
            premium_bottom=premium_bottom_price,
            eq_top=eq_top_price,
            eq_bottom=eq_bottom_price,
            discount_top=discount_top_price,
            discount_bottom=bot,
            zone=zone,
            zone_pct=zone_pct,
            day_open=day_open,
            day_high=day_high,
            day_low=day_low,
            day_close=day_close,
            reached_discount=reached_discount,
            fvg=fvg,
            buy_signal=buy_signal,
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
