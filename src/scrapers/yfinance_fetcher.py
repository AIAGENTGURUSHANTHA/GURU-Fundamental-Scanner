from typing import Any, Dict, Optional

import numpy as np
import pandas as pd
import yfinance as yf

from .base_scraper import BaseScraper


class YFinanceFetcher(BaseScraper):
    """Fetches stock fundamentals from Yahoo Finance (NSE/BSE)."""

    NSE_SUFFIX = ".NS"
    BSE_SUFFIX = ".BO"

    def fetch_stock_data(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Try NSE then BSE. Returns dict or None."""
        for suffix in (self.NSE_SUFFIX, self.BSE_SUFFIX):
            data = self._fetch(symbol, suffix)
            if data:
                return data
        return None

    # ── private ──────────────────────────────────────────────────────────────

    def _fetch(self, symbol: str, suffix: str) -> Optional[Dict[str, Any]]:
        try:
            ticker = yf.Ticker(f"{symbol.upper()}{suffix}")
            info = ticker.info or {}

            price = info.get("regularMarketPrice") or info.get("currentPrice")
            if not price:
                return None

            market_cap_raw = info.get("marketCap")
            market_cap_cr = round(market_cap_raw / 1e7, 2) if market_cap_raw else None

            roe_raw = info.get("returnOnEquity")
            roe_pct = round(roe_raw * 100, 2) if roe_raw is not None else None

            total_debt = info.get("totalDebt")
            debt_cr = round(total_debt / 1e7, 2) if total_debt else None

            roce = self._calculate_roce(ticker)
            median_pe = self._calculate_median_pe_5y(ticker)

            pe = info.get("trailingPE")
            earnings_growth = info.get("earningsGrowth")
            peg = info.get("pegRatio")
            if peg is None and pe and earnings_growth and earnings_growth > 0:
                peg = round(pe / (earnings_growth * 100), 2)

            # Supplementary metrics not available on screener.in
            ev_ebitda = info.get("enterpriseToEbitda")
            nsps = info.get("revenuePerShare")          # Net Sales Per Share
            pts = info.get("priceToSalesTrailing12Months")  # Price to Sales
            chg_pct = info.get("regularMarketChangePercent")

            return {
                "symbol": symbol.upper(),
                "company_name": info.get("longName") or info.get("shortName") or symbol.upper(),
                "current_price": price,
                "price_change_pct": round(chg_pct, 2) if chg_pct is not None else None,
                "market_cap": self._fmt_crores(market_cap_cr),
                "market_cap_cr": market_cap_cr,
                "book_value": info.get("bookValue"),
                "stock_pe": round(pe, 2) if pe else None,
                "forward_pe": round(info.get("forwardPE"), 2) if info.get("forwardPE") else None,
                "pb_ratio": round(info.get("priceToBook"), 2) if info.get("priceToBook") else None,
                "roe": roe_pct,
                "roce": roce,
                "debt": debt_cr,
                "peg_ratio": round(peg, 2) if peg else None,
                "median_pe_5y": median_pe,
                "ev_ebitda": round(ev_ebitda, 2) if ev_ebitda else None,
                "nsps": round(nsps, 2) if nsps else None,
                "price_to_sales": round(pts, 2) if pts else None,
                "high_52w": info.get("fiftyTwoWeekHigh"),
                "low_52w": info.get("fiftyTwoWeekLow"),
                "eps": info.get("trailingEps"),
                "sector": info.get("sector"),
                "industry": info.get("industry"),
                "source": "yfinance",
            }
        except Exception as exc:
            self._log_error(f"_fetch({symbol}{suffix}): {exc}")
            return None

    def _calculate_roce(self, ticker: yf.Ticker) -> Optional[float]:
        """ROCE = EBIT / Capital Employed × 100"""
        try:
            income = ticker.income_stmt
            balance = ticker.balance_sheet
            if income is None or income.empty or balance is None or balance.empty:
                return None

            ebit = None
            for field in ("EBIT", "Operating Income", "Normalized EBITDA"):
                if field in income.index:
                    v = income.loc[field].iloc[0]
                    if pd.notna(v):
                        ebit = float(v)
                        break
            if ebit is None:
                return None

            total_assets = None
            if "Total Assets" in balance.index:
                v = balance.loc["Total Assets"].iloc[0]
                if pd.notna(v):
                    total_assets = float(v)

            current_liabilities = None
            for field in (
                "Current Liabilities",
                "Total Current Liabilities Net Minority Interest",
                "Current Liabilities And Short Term Debt",
            ):
                if field in balance.index:
                    v = balance.loc[field].iloc[0]
                    if pd.notna(v):
                        current_liabilities = float(v)
                        break

            if total_assets is None or current_liabilities is None:
                return None

            capital_employed = total_assets - current_liabilities
            if capital_employed <= 0:
                return None

            return round((ebit / capital_employed) * 100, 2)
        except Exception:
            return None

    def _calculate_median_pe_5y(self, ticker: yf.Ticker) -> Optional[float]:
        """Median trailing P/E over the last 5 years using monthly prices + quarterly EPS."""
        try:
            hist = ticker.history(period="5y", interval="1mo")
            if hist.empty or len(hist) < 12:
                return None

            try:
                earnings = ticker.quarterly_earnings
            except Exception:
                earnings = None

            if earnings is None or earnings.empty:
                return None

            eps_col = "Earnings" if "Earnings" in earnings.columns else earnings.columns[0]

            pe_values = []
            for date, row in hist.iterrows():
                price = float(row["Close"])
                if price <= 0:
                    continue
                # Normalize timezone
                if hasattr(date, "tz_localize"):
                    cmp_date = date.tz_localize(None) if date.tzinfo else date
                else:
                    cmp_date = date

                idx = earnings.index
                if hasattr(idx[0], "tzinfo") and idx[0].tzinfo:
                    idx = idx.tz_localize(None) if not idx.tzinfo else idx.tz_convert(None)

                past_q = earnings[idx <= cmp_date].tail(4)
                if len(past_q) < 4:
                    continue
                trailing_eps = float(past_q[eps_col].sum())
                if trailing_eps > 0:
                    pe = price / trailing_eps
                    if 0 < pe < 1000:
                        pe_values.append(pe)

            if len(pe_values) < 6:
                return None
            return round(float(np.median(pe_values)), 2)
        except Exception:
            return None

    @staticmethod
    def _fmt_crores(cr: Optional[float]) -> Optional[str]:
        if cr is None:
            return None
        if cr >= 100_000:
            return f"₹{cr / 100_000:.2f}L Cr"
        if cr >= 1_000:
            return f"₹{cr / 1_000:.2f}K Cr"
        return f"₹{cr:,.0f} Cr"
