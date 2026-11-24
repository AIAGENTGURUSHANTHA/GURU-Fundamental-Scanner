from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class SMCData(BaseModel):
    symbol: str
    company_name: str = ""
    ref_date: str = ""
    current_price: Optional[float] = None

    # Trailing swing extremes (trailing.top / trailing.bottom in PineScript)
    swing_high: Optional[float] = None
    swing_low: Optional[float] = None

    # Zone boundary prices
    premium_top: Optional[float] = None      # = swing_high
    premium_bottom: Optional[float] = None   # = 0.95*top + 0.05*bottom
    eq_top: Optional[float] = None           # = 0.525*top + 0.475*bottom
    eq_bottom: Optional[float] = None        # = 0.525*bottom + 0.475*top
    discount_top: Optional[float] = None     # = 0.95*bottom + 0.05*top
    discount_bottom: Optional[float] = None  # = swing_low

    # Zone classification
    zone: str = ""                           # Premium / Near Premium / Equilibrium / Near Discount / Discount
    zone_pct: Optional[float] = None        # 0 (at bottom) → 100 (at top)

    # Reference-date candle OHLC
    day_open:  Optional[float] = None
    day_high:  Optional[float] = None
    day_low:   Optional[float] = None
    day_close: Optional[float] = None

    # True when the day's LOW touched or entered the Discount zone (day_low ≤ discount_top)
    reached_discount: Optional[bool] = None

    # Fair Value Gap on the reference-date candle: "Bullish", "Bearish", or None
    fvg: Optional[str] = None

    # True when zone in ("Discount", "Near Discount") AND fvg == "Bullish" → MUST BUY / ACCUMULATE signal
    buy_signal: Optional[bool] = None

    price_source: str = "Yahoo Finance (NSE)"
    nse_live_time: Optional[str] = None

    error: Optional[str] = None
    data_source: str = "Yahoo Finance (NSE)"
    fetched_at: Optional[datetime] = None
