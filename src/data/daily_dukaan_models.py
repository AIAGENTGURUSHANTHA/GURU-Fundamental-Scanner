from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class DailyDukaanData(BaseModel):
    """20-DMA analysis record for a single NSE stock or ETF."""
    symbol: str
    company_name: str = ""
    underlying_asset: str = ""    # sector/industry for stocks; underlying index/commodity for ETFs
    ref_date: str = ""            # Actual trading day used (YYYY-MM-DD)

    current_price: Optional[float] = None
    dma20: Optional[float] = None           # 20-day simple moving average of close prices
    price_vs_dma20: Optional[float] = None  # current_price − dma20  (positive = above DMA)
    pct_vs_dma20: Optional[float] = None    # (current_price − dma20) / dma20 × 100

    # Live-price enrichment (set when fetched from NSE during trading hours)
    price_source: str = "Yahoo Finance (NSE)"   # "NSE Live" during open market hours
    nse_live_time: Optional[str] = None          # NSE last-update timestamp for live prices
    prev_close: Optional[float] = None           # Previous trading day's closing price

    error: Optional[str] = None
    data_source: str = "Yahoo Finance (NSE)"
    fetched_at: Optional[datetime] = None
