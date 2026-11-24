from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class GURUBuyData(BaseModel):
    symbol: str
    company_name: str = ""
    ref_date: str = ""
    current_price: Optional[float] = None

    # Rainbow EMA values (each is EMA(2) of the previous)
    ema:   Optional[float] = None   # EMA(close, 2)
    emad1: Optional[float] = None   # EMA(EMA,   2)
    emad2: Optional[float] = None   # EMA(EMAD1, 2)
    emad3: Optional[float] = None   # EMA(EMAD2, 2)
    emad4: Optional[float] = None   # EMA(EMAD3, 2)
    emad5: Optional[float] = None   # EMA(EMAD4, 2)
    emad6: Optional[float] = None   # EMA(EMAD5, 2)

    # Signal per line: True = green (BUY), False = red (SELL)
    # Based on Pine Script color logic
    sig_ema:  Optional[bool] = None   # EMA  >= EMAD1
    sig_d1:   Optional[bool] = None   # EMAD1 >= EMAD2
    sig_d2:   Optional[bool] = None   # EMAD2 >= EMAD3
    sig_d3:   Optional[bool] = None   # EMAD3 >= EMAD4
    sig_d4:   Optional[bool] = None   # EMAD4 >= EMAD5
    sig_d5:   Optional[bool] = None   # EMAD5 >= EMAD6
    sig_d6:   Optional[bool] = None   # EMA   >= EMAD6  (green when EMAD6 NOT > EMA)

    buy_count:   Optional[int] = None  # 0–7 green signals
    guru_signal: str = ""              # STRONG BUY / BUY / NEUTRAL / SELL / STRONG SELL

    price_source: str = "Yahoo Finance (NSE)"
    nse_live_time: Optional[str] = None

    error: Optional[str] = None
    data_source: str = "Yahoo Finance (NSE)"
    fetched_at: Optional[datetime] = None
