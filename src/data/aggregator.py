from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

_IST = timezone(timedelta(hours=5, minutes=30))
from typing import List

from src.data.cache_manager import cache_manager
from src.data.models import StockData
from src.scrapers.nse_live_fetcher import NSELiveFetcher
from src.scrapers.screener_scraper import ScreenerScraper


def _fmt_timestamp() -> str:
    now = datetime.now()
    hour = now.hour % 12 or 12
    minute = now.strftime("%M")
    ampm = "a.m." if now.hour < 12 else "p.m."
    return f"{now.day} {now.strftime('%b')} {hour}:{minute} {ampm}"


class DataAggregator:
    """
    Combines two sources:
      • NSE India (nsepython)  — live current price with decimal precision
      • screener.in            — all fundamental ratios (P/E, ROCE, ROE, etc.)
    Price-dependent derived metrics are recalculated using the live NSE price.
    """

    def __init__(self) -> None:
        self._screener = ScreenerScraper()
        self._nse = NSELiveFetcher()

    # ── Public ────────────────────────────────────────────────────────────────

    def fetch_stock(self, symbol: str) -> StockData:
        sym = symbol.strip().upper()
        cached = cache_manager.get(sym)
        if cached:
            return StockData(**cached)
        try:
            data = self._aggregate(sym)
        except Exception as exc:
            data = StockData(symbol=sym, company_name=sym, error=str(exc), data_source="error")
        # Only cache successful results
        if not data.error:
            cache_manager.set(sym, data.model_dump())
        return data

    def fetch_stocks_bulk(self, symbols: List[str]) -> List[StockData]:
        results: List[StockData] = []
        with ThreadPoolExecutor(max_workers=3) as executor:
            future_map = {executor.submit(self.fetch_stock, sym): sym for sym in symbols}
            for future in as_completed(future_map):
                sym = future_map[future]
                try:
                    results.append(future.result(timeout=90))
                except Exception as exc:
                    results.append(StockData(
                        symbol=sym.upper(), company_name=sym.upper(),
                        error=str(exc), data_source="error",
                    ))
        return results

    # ── Private ───────────────────────────────────────────────────────────────

    def _aggregate(self, symbol: str) -> StockData:
        # Fetch both sources
        sc = self._screener.fetch_company_data(symbol) or {}
        nse = self._nse.fetch_live_price(symbol) or {}

        if not sc and not nse:
            return StockData(
                symbol=symbol,
                company_name=symbol,
                error="No data from screener.in or NSE. Verify the NSE symbol is correct.",
                data_source="error",
            )

        # ── Price: NSE live takes priority, screener as fallback ──────────────
        price = nse.get("current_price") or sc.get("current_price")
        price_change_pct = nse.get("price_change_pct") or sc.get("price_change_pct")

        # ── 52-week high/low: prefer NSE (more accurate), fallback screener ───
        high_52w = nse.get("week_high") or sc.get("high_52w")
        low_52w  = nse.get("week_low")  or sc.get("low_52w")

        # ── Fundamentals from screener.in ─────────────────────────────────────
        bv   = sc.get("book_value")
        roe  = sc.get("roe")
        ronw = sc.get("ronw") or roe
        mcap = sc.get("market_cap_cr")
        rev  = sc.get("revenue_cr")

        # ── Recalculate price-sensitive metrics using live NSE price ──────────
        # P/B = Live Price / Book Value
        pb = None
        if price and bv and bv > 0:
            pb = round(price / bv, 2)
        elif sc.get("pb_ratio"):
            pb = sc.get("pb_ratio")

        # PRONW = Live Price / RONW%
        pronw = None
        if price and ronw and ronw > 0:
            pronw = round(price / ronw, 2)

        # NSPS = Revenue_Cr × Live Price / Market Cap_Cr  (₹ per share)
        nsps = None
        if price and mcap and rev and mcap > 0:
            nsps = round(rev * price / mcap, 2)

        # PNSPS = Live Price / NSPS
        pnsps = None
        if price and nsps and nsps > 0:
            pnsps = round(price / nsps, 2)

        # Price to Sales = Market Cap / Revenue (screener MCap, stays stable intraday)
        pts = sc.get("price_to_sales")
        if pts is None and mcap and rev and rev > 0:
            pts = round(mcap / rev, 2)

        # ── Determine data source label ───────────────────────────────────────
        if nse and sc:
            source = "NSE (live) + screener.in"
        elif nse:
            source = "NSE (live)"
        else:
            source = "screener.in"

        return StockData(
            symbol=symbol,
            company_name=sc.get("company_name") or symbol,

            current_price=price,
            price_change_pct=round(price_change_pct, 2) if price_change_pct is not None else None,
            data_as_of=sc.get("data_timestamp") or _fmt_timestamp(),

            market_cap=sc.get("market_cap_str"),
            market_cap_cr=mcap,
            high_52w=high_52w,
            low_52w=low_52w,

            book_value=bv,
            face_value=sc.get("face_value"),
            nsps=nsps,
            eps=sc.get("eps") or sc.get("eps_ttm"),

            dividend_yield=sc.get("dividend_yield"),

            roce=sc.get("roce"),
            roe=roe,
            ronw=ronw,

            stock_pe=sc.get("stock_pe"),
            pb_ratio=pb,
            pnsps=pnsps,
            price_to_sales=pts,
            peg_ratio=sc.get("peg_ratio"),
            pronw=pronw,

            bse_code=sc.get("bse_code"),
            nse_code=sc.get("nse_code"),

            data_source=source,
            fetched_at=datetime.now(_IST),
        )
