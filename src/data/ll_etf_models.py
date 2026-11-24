from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class LLETFData(BaseModel):
    """Life Long ETF analysis record — 52W Low, 3Y Avg, 5Y Avg vs Current Price."""
    symbol: str
    company_name: str = ""

    # Price info
    current_price: Optional[float] = None
    ref_date: str = ""            # Actual trading day used (YYYY-MM-DD)
    price_on_date: Optional[float] = None  # same as current_price at ref_date

    # 52-Week Low
    week52_low: Optional[float] = None
    diff_52w_low: Optional[float] = None        # current_price − 52W low
    pct_vs_52w_low: Optional[float] = None      # % change

    # 3-Year Average Price
    avg_price_3y: Optional[float] = None
    diff_3y: Optional[float] = None             # current_price − 3Y avg
    pct_vs_3y: Optional[float] = None           # % change

    # 5-Year Average Price
    avg_price_5y: Optional[float] = None
    diff_5y: Optional[float] = None             # current_price − 5Y avg
    pct_vs_5y: Optional[float] = None           # % change

    # Ranks (set after bulk calculation)
    rank_52w: Optional[int] = None
    rank_3y: Optional[int] = None
    rank_5y: Optional[int] = None

    # Live-price enrichment
    price_source: str = "Yahoo Finance (NSE)"
    nse_live_time: Optional[str] = None

    error: Optional[str] = None
    data_source: str = "Yahoo Finance (NSE)"
    fetched_at: Optional[datetime] = None
