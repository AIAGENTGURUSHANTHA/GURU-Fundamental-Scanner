from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class ETFLiveData(BaseModel):
    """Live market data for a single NSE-listed ETF, fetched from the NSE API."""
    symbol: str
    assets: str = ""               # Underlying asset description
    series: str = ""               # Series (EQ, etc.)

    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    prev_close: Optional[float] = None
    ltp: Optional[float] = None           # Last Traded Price
    indicative_close: Optional[float] = None
    change: Optional[float] = None        # Absolute change from prev close
    pct_change: Optional[float] = None    # % change
    volume: Optional[int] = None          # Shares traded
    traded_value_cr: Optional[float] = None  # Traded value in crores
    nav: Optional[float] = None
    week52_high: Optional[float] = None
    week52_low: Optional[float] = None
    pct_change_30d: Optional[float] = None
    pct_change_365d: Optional[float] = None

    error: Optional[str] = None
    data_source: str = "NSE India API"
    fetched_at: Optional[datetime] = None


class ETFMarketSummary(BaseModel):
    """Aggregate market summary returned alongside the ETF list."""
    advances: int = 0
    declines: int = 0
    unchanged: int = 0
    total_traded_value_cr: Optional[float] = None
    total_traded_volume: Optional[int] = None
    nav_date: str = ""
    nse_timestamp: str = ""
