from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class StockData(BaseModel):
    symbol: str
    company_name: str

    # Price info
    current_price: Optional[float] = None
    price_change_pct: Optional[float] = None   # % change today
    data_as_of: Optional[str] = None           # "19 Nov 9:48 a.m." style

    # Market / valuation
    market_cap: Optional[str] = None           # "₹8,50,576 Cr."
    market_cap_cr: Optional[float] = None      # raw Crores
    high_52w: Optional[float] = None
    low_52w: Optional[float] = None

    # Per-share
    book_value: Optional[float] = None
    face_value: Optional[float] = None
    nsps: Optional[float] = None               # Net Sales Per Share (₹)
    eps: Optional[float] = None

    # Yield / dividend
    dividend_yield: Optional[float] = None     # %

    # Return ratios
    roce: Optional[float] = None               # %
    roe: Optional[float] = None                # %
    ronw: Optional[float] = None               # Return on Net Worth %

    # Valuation multiples
    stock_pe: Optional[float] = None
    median_pe_5y: Optional[float] = None
    pb_ratio: Optional[float] = None           # Price to Book
    pnsps: Optional[float] = None             # Price / NSPS  (= P/S per share)
    price_to_sales: Optional[float] = None    # Market Cap / Revenue
    ev_ebitda: Optional[float] = None
    peg_ratio: Optional[float] = None
    pronw: Optional[float] = None             # Price / RONW%
    p_roe: Optional[float] = None             # Current Price / ROE% (lower = better value)
    ltvi: Optional[float] = None              # P/B + P/EPS + P/ROE + P/NSPS composite

    # Debt
    debt: Optional[float] = None              # Total Debt, Crores
    enterprise_value: Optional[float] = None  # MCap + Borrowings in Cr
    debt_equity: Optional[float] = None       # Borrowings / Shareholders Equity

    # Operating metrics
    operating_profit: Optional[float] = None  # Operating Profit in Cr
    promoter_pledging: Optional[float] = None # % of promoter shares pledged
    sales_cr: Optional[float] = None           # Annual/TTM Revenue in Crores
    num_shares_cr: Optional[float] = None      # Equity shares in Crore units (equity_cap/face_val)
    marking: Optional[float] = None            # P/E + P/B + Price/Sales + EV/EBITDA
    qtr_profit_var: Optional[float] = None     # Latest quarter YoY net profit growth %

    # Identifiers
    bse_code: Optional[str] = None
    nse_code: Optional[str] = None
    sector: Optional[str] = None
    industry: Optional[str] = None

    # Meta
    data_source: str = "unknown"
    fetched_at: Optional[datetime] = None
    error: Optional[str] = None


class UserInfo(BaseModel):
    phone_number: str
    is_admin: bool = False
    is_active: bool = True
