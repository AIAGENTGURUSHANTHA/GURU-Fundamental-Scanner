from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class RSIData(BaseModel):
    """14-period RSI across Daily / Weekly / Monthly timeframes for one NSE symbol."""
    symbol: str
    company_name: str = ""
    ref_date: str = ""

    current_price: Optional[float] = None
    daily_rsi: Optional[float] = None
    weekly_rsi: Optional[float] = None
    monthly_rsi: Optional[float] = None

    price_source: str = "Yahoo Finance (NSE)"
    nse_live_time: Optional[str] = None
    prev_close: Optional[float] = None

    error: Optional[str] = None
    data_source: str = "Yahoo Finance (NSE)"
    fetched_at: Optional[datetime] = None
