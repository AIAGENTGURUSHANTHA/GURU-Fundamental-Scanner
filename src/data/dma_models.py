from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class DMAData(BaseModel):
    symbol: str
    company_name: str = ""
    ref_date: str = ""          # Actual trading day used (YYYY-MM-DD)
    current_price: Optional[float] = None

    dma1:   Optional[float] = None
    dma2:   Optional[float] = None
    dma3:   Optional[float] = None
    dma4:   Optional[float] = None
    dma5:   Optional[float] = None
    dma6:   Optional[float] = None
    dma7:   Optional[float] = None
    dma10:  Optional[float] = None
    dma20:  Optional[float] = None
    dma22:  Optional[float] = None
    dma50:  Optional[float] = None
    dma100: Optional[float] = None
    dma200: Optional[float] = None

    # How many of the 13 DMAs the current price is above
    above_dma_count: Optional[int] = None

    price_source: str = "Yahoo Finance (NSE)"
    nse_live_time: Optional[str] = None

    error: Optional[str] = None
    data_source: str = "Yahoo Finance (NSE)"
    fetched_at: Optional[datetime] = None
