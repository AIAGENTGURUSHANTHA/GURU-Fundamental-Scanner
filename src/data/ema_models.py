from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class EMAData(BaseModel):
    symbol: str
    company_name: str = ""
    ref_date: str = ""          # Actual trading day used (YYYY-MM-DD)
    current_price: Optional[float] = None

    ema1:   Optional[float] = None
    ema2:   Optional[float] = None
    ema3:   Optional[float] = None
    ema4:   Optional[float] = None
    ema5:   Optional[float] = None
    ema6:   Optional[float] = None
    ema7:   Optional[float] = None
    ema10:  Optional[float] = None
    ema20:  Optional[float] = None
    ema22:  Optional[float] = None
    ema50:  Optional[float] = None
    ema100: Optional[float] = None
    ema200: Optional[float] = None

    # How many of the 13 EMAs the current price is above
    above_ema_count: Optional[int] = None

    price_source: str = "Yahoo Finance (NSE)"
    nse_live_time: Optional[str] = None

    error: Optional[str] = None
    data_source: str = "Yahoo Finance (NSE)"
    fetched_at: Optional[datetime] = None
