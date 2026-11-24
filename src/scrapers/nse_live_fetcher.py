"""
Live price fetcher from NSE India using nsepython.
Returns current market price with decimal precision.
"""
import logging
import warnings
from typing import Any, Dict, Optional

# Suppress nsepython's deprecation warning (it uses a newer endpoint internally)
logging.getLogger("root").setLevel(logging.ERROR)
warnings.filterwarnings("ignore")


class NSELiveFetcher:
    """Fetches live market price from NSE India via nsepython."""

    def fetch_live_price(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Returns live NSE price data for the symbol.
        Falls back gracefully to None if the symbol is invalid or network fails.
        """
        try:
            # Import here so the app still starts even if nsepython is unavailable
            import logging as _log
            _log.disable(_log.WARNING)
            from nsepython import nse_eq
            _log.disable(_log.NOTSET)

            data = nse_eq(symbol.strip().upper())
            if not data:
                return None

            pi = data.get("priceInfo", {})
            last_price = pi.get("lastPrice")
            if not last_price:
                return None

            whl = pi.get("weekHighLow", {})
            idhl = pi.get("intraDayHighLow", {})

            return {
                "current_price":    round(float(last_price), 2),
                "price_change":     pi.get("change"),
                "price_change_pct": pi.get("pChange"),
                "prev_close":       pi.get("previousClose"),
                "open_price":       pi.get("open"),
                "vwap":             pi.get("vwap"),
                "intraday_high":    idhl.get("max"),
                "intraday_low":     idhl.get("min"),
                "week_high":        whl.get("max"),
                "week_low":         whl.get("min"),
            }

        except Exception as exc:
            # 404 = invalid symbol; other errors = network/API issue
            # Either way fall back to screener.in price silently
            if "404" not in str(exc):
                print(f"[NSELiveFetcher] WARN: {symbol}: {exc}")
            return None
