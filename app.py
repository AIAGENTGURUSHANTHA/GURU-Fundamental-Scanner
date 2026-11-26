"""
GURU Fundamental Scanner — Main Streamlit Application
Run:  streamlit run app.py
"""

import base64
import io
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone, timedelta

# Indian Standard Time — used for all displayed timestamps
IST = timezone(timedelta(hours=5, minutes=30))


def _now_ist() -> datetime:
    """Current datetime in IST (works correctly on cloud servers running UTC)."""
    return datetime.now(IST)
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
import streamlit as st

# Bootstrap secrets from Streamlit Cloud (or .env locally) before anything else reads config
from config import config
config.bootstrap()

from src.auth.auth_manager import auth_manager
from src.data.aggregator import DataAggregator
from src.data.cache_manager import cache_manager
from src.data.models import StockData
from src.database.db_manager import db
from src.ui.styles import get_theme_css
from src.utils.file_parser import FileParser


@st.cache_data(show_spinner=False)
def _pkj_banner_b64() -> str:
    """Load, crop, and base64-encode the PKJ tree banner once per session."""
    path = os.path.join(os.path.dirname(__file__), "static", "pkj_banner.png")
    try:
        with open(path, "rb") as _f:
            return base64.b64encode(_f.read()).decode()
    except Exception:
        return ""

st.set_page_config(
    page_title=config.APP_NAME,
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
    menu_items={"About": f"**{config.APP_NAME}** v{config.VERSION}"},
)

# PWA / mobile meta tags — enables "Add to Home Screen" on Android & iPhone
st.markdown("""
<link rel="manifest" href="/app/static/manifest.json">
<link rel="apple-touch-icon" href="/app/static/icon-192.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="GURU Scanner">
<meta name="mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#1f4e79">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=5">
""", unsafe_allow_html=True)


@st.cache_resource
def _get_aggregator() -> DataAggregator:
    """Cache the aggregator (and its scraper session) across Streamlit reruns.
    Without this, every rerun creates a fresh ScreenerScraper session which
    causes screener.in to rate-limit the server IP after a few page loads."""
    return DataAggregator()


_aggregator = _get_aggregator()
_parser = FileParser()


# ── Session token store (L1 memory cache + L2 SQLite — survives restarts) ────

@st.cache_resource
def _get_session_store() -> dict:
    """In-memory L1 cache for session tokens. Warmed from DB on first miss."""
    return {}


def _create_session_token(user: dict) -> str:
    import secrets
    token = secrets.token_urlsafe(32)
    # Write to both memory cache and persistent DB
    _get_session_store()[token] = {"user": user, "ts": datetime.utcnow()}
    db.save_session_token(token, user["phone_number"])
    # Opportunistically remove expired tokens from DB
    try:
        db.cleanup_expired_sessions()
    except Exception:
        pass
    return token


def _get_session_user(token: str):
    # L1: memory cache (fast path)
    store = _get_session_store()
    entry = store.get(token)
    if entry:
        if (datetime.utcnow() - entry["ts"]).days > 30:
            store.pop(token, None)
        else:
            return entry["user"]
    # L2: SQLite (survives server restart / redeploy)
    try:
        phone = db.get_session_phone(token)
        if phone:
            user = db.get_user(phone)
            if user:
                # Warm L1 cache for subsequent requests
                store[token] = {"user": user, "ts": datetime.utcnow()}
                return user
    except Exception:
        pass
    return None


def _revoke_session_token(token: str) -> None:
    _get_session_store().pop(token, None)
    try:
        db.delete_session_token(token)
    except Exception:
        pass


def _spinner_html(msg: str) -> str:
    """Return HTML for an inline circular spinner + message."""
    return (
        "<div style='display:flex;align-items:center;gap:0.6rem;"
        "padding:0.55rem 0.85rem;background:rgba(59,130,246,0.08);"
        "border-radius:8px;margin:0.1rem 0'>"
        "<div style='width:20px;height:20px;border:2.5px solid #cbd5e1;"
        "border-top-color:#3b82f6;border-radius:50%;"
        "animation:_guru_spin 0.7s linear infinite;flex-shrink:0'></div>"
        f"<span style='font-size:0.88rem;font-weight:500;color:#1d4ed8'>{msg}</span>"
        "</div>"
        "<style>@keyframes _guru_spin{to{transform:rotate(360deg)}}</style>"
    )


# ── Session defaults ──────────────────────────────────────────────────────────

def _init():
    # Auto-restore session from persisted query param (survives page refresh)
    _tok = st.query_params.get("_s", "")
    if _tok and not st.session_state.get("authenticated"):
        _stored = _get_session_user(_tok)
        if _stored:
            _db_user = db.get_user(_stored["phone_number"])
            if _db_user and _db_user.get("is_active"):
                st.session_state["authenticated"] = True
                st.session_state["user"]          = _db_user
            else:
                _revoke_session_token(_tok)
                st.query_params.clear()
        else:
            st.query_params.clear()

    # Restore last active section from URL (survives browser refresh)
    _valid_sections = {
        "fundamentals", "technicals", "guru_smart_business",
        "base_price_screener", "rsi_screener", "turtle_trading",
        "my_holdings", "paiso_ka_jungle", "admin",
    }
    _qsec = st.query_params.get("_p", "")
    if _qsec in _valid_sections and "section" not in st.session_state:
        st.session_state["section"] = _qsec

    defaults = {
        "authenticated": False,
        "user": None,
        "dark_mode": False,
        "page": "dashboard",
        "section": "fundamentals",
        "otp_sent": False,
        "dev_otp": None,
        "phone_input": "",
        # Fundamentals
        "stock_data": [],
        "active_scan_name": None,
        "last_fetched_at": None,
        "last_scan_symbols": [],
        "_do_refresh": False,
        # DMA / Technicals
        "dma_data": [],
        "dma_last_fetched_at": None,
        "dma_last_scan_symbols": [],
        "dma_do_refresh": False,
        "dma_active_scan_name": None,
        "dma_ref_date": None,
        # EMA / Technicals
        "ema_data": [],
        "ema_last_fetched_at": None,
        "ema_last_scan_symbols": [],
        "ema_do_refresh": False,
        "ema_active_scan_name": None,
        "ema_ref_date": None,
        # GURU BUY / Technicals
        "guru_data": [],
        "guru_last_fetched_at": None,
        "guru_last_scan_symbols": [],
        "guru_do_refresh": False,
        "guru_active_scan_name": None,
        "guru_ref_date": None,
        # SMC Premium/Discount Zones / Technicals
        "smc_data": [],
        "smc_last_fetched_at": None,
        "smc_last_scan_symbols": [],
        "smc_do_refresh": False,
        "smc_active_scan_name": None,
        "smc_ref_date": None,
        "smc_alert_stocks": [],   # stocks in Discount zone with Bullish FVG → MUST BUY signal
        # Daily Dukaan / GURU SMART BUSINESS
        "dd_data": [],
        "dd_last_fetched_at": None,
        "dd_last_scan_symbols": [],
        "dd_do_refresh": False,
        "dd_active_scan_name": None,
        "dd_ref_date": None,
        # ETF Analysis
        "etf_live_data": [],          # List[ETFLiveData] from NSE API
        "etf_live_fetched_at": None,
        "etf_live_summary": None,     # ETFMarketSummary
        "etf_ema_data": [],           # List[EMAData] for selected ETFs
        "etf_ema_last_fetched_at": None,
        "etf_ema_last_scan_symbols": [],
        "etf_ema_do_refresh": False,
        "etf_ema_active_scan_name": None,
        "etf_ema_ref_date": None,
        # Life Long ETFs
        "ll_etf_data": [],
        "ll_etf_last_fetched_at": None,
        "ll_etf_last_scan_symbols": [],
        "ll_etf_do_refresh": False,
        "ll_etf_active_scan_name": None,
        "ll_etf_ref_date": None,
        # Eshwarana Fruit Forest
        "eff_data": [],
        "eff_last_fetched_at": None,
        "eff_last_scan_symbols": [],
        "eff_do_refresh": False,
        "eff_active_scan_name": None,
        "eff_ref_date": None,
        # RSI Screener — Stocks
        "rsi_stock_data": [],
        "rsi_stock_last_fetched_at": None,
        "rsi_stock_last_scan_symbols": [],
        "rsi_stock_do_refresh": False,
        "rsi_stock_active_scan_name": None,
        "rsi_stock_ref_date": None,
        # RSI Screener — ETFs
        "rsi_etf_data": [],
        "rsi_etf_last_fetched_at": None,
        "rsi_etf_last_scan_symbols": [],
        "rsi_etf_do_refresh": False,
        "rsi_etf_active_scan_name": None,
        "rsi_etf_ref_date": None,
        # Base Price Screener
        "bps_stock_data": [],
        "bps_stock_last_fetched_at": None,
        "bps_etf_data": [],
        "bps_etf_last_fetched_at": None,
        # Turtle Trading
        "tt_data": [],
        "tt_last_fetched_at": None,
        "tt_last_scan_symbols": [],
        "tt_do_refresh": False,
        "tt_active_scan_name": None,
        "tt_ref_date": None,
        # My Holdings
        "holdings_active_tab": "equity",
        "holdings_edit_id": None,
        "holdings_sell_buy_id": None,
        "holdings_live_prices": {},    # {symbol: price} refreshed on demand
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

    # ── Auto-create admin on first run (cloud deployment) ─────────────────
    if not st.session_state.get("_admin_checked"):
        st.session_state["_admin_checked"] = True
        admin_phone = config.ADMIN_PHONE
        if admin_phone:
            existing = db.get_user(admin_phone)
            if not existing:
                db.add_user(admin_phone, is_active=True, is_admin=True, added_by="auto-setup")
            elif not existing.get("is_admin"):
                db.update_user(admin_phone, is_admin=True, is_active=True)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _chg_color(pct):
    if pct is None:
        return "#888"
    return "#16a34a" if pct >= 0 else "#dc2626"


def _chg_arrow(pct):
    if pct is None:
        return ""
    return "▲" if pct >= 0 else "▼"


def _is_market_open() -> bool:
    """True when the current IST time is within NSE normal market hours (Mon–Fri, 09:15–15:30)."""
    from datetime import timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(IST)
    if now.weekday() >= 5:                              # Saturday / Sunday
        return False
    start = now.replace(hour=9,  minute=15, second=0, microsecond=0)
    end   = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return start <= now <= end


def _live_btn(tab_prefix: str, refresh_key: str, btn_key: str) -> None:
    """
    Render a 📡 Live button for any tab.
    - During market hours (09:15–15:30 IST): green primary button, fetches real-time prices.
    - Outside market hours: secondary button, fetches the latest official NSE close.

    Sets _live_pending_<tab_prefix> so the render function can apply today's date
    to the date picker BEFORE the widget is instantiated on the next run, avoiding
    StreamlitWidgetAlreadyInstantiatedError.
    """
    market_open = _is_market_open()
    if st.button(
        "📡 Live",
        key=btn_key,
        use_container_width=True,
        type="primary" if market_open else "secondary",
        help=(
            "🟢 Market OPEN — fetches real-time NSE prices for all loaded symbols."
            if market_open
            else "⏸ Market closed — fetches the latest official NSE close price."
        ),
    ):
        st.session_state[f"_live_pending_{tab_prefix}"] = True
        st.session_state[refresh_key] = True
        st.rerun()


def _fetch_nse_live_prices_bulk(symbols: list) -> dict:
    """
    Fetch live traded prices from NSE India only.
    Two-layer approach — both layers hit nseindia.com directly:
      1a. GetQuoteApi with series=EQ  (equity stocks)
      1b. GetQuoteApi without series  (ETFs / other series not covered by EQ)
      2.  nsepython nse_eq()          (uses NSE's equity-quote page API)
    Returns {symbol: {"price": float, "time": str}}.
    """
    out: dict = {}

    # ── Layer 1: NSE GetQuoteApi (nseindia.com) ───────────────────────────────
    try:
        from nsepython.rahu import _get_nse_session, headers as _nse_headers
        sess = _get_nse_session()

        def _parse_get_quote(j: dict) -> tuple:
            eq = j.get("equityResponse", [{}])
            if not eq:
                return None, ""
            r     = eq[0]
            order = r.get("orderBook", {}) or {}
            trade = r.get("tradeInfo", {}) or {}
            price = order.get("lastPrice") or trade.get("lastPrice")
            ts    = r.get("lastUpdateTime", "") or ""
            return price, ts

        for sym in symbols:
            # 1a: EQ series (works for most equity stocks)
            _got = False
            for _url in (
                f"https://www.nseindia.com/api/NextApi/apiClient/GetQuoteApi"
                f"?functionName=getSymbolData&marketType=N&series=EQ&symbol={sym}",
                # 1b: no series filter — catches ETFs, bonds, other series
                f"https://www.nseindia.com/api/NextApi/apiClient/GetQuoteApi"
                f"?functionName=getSymbolData&marketType=N&symbol={sym}",
            ):
                if _got:
                    break
                try:
                    resp = sess.get(_url, headers=_nse_headers, timeout=10)
                    if resp.status_code == 200:
                        price, ts = _parse_get_quote(resp.json())
                        if price is not None:
                            out[sym] = {
                                "price": float(str(price).replace(",", "")),
                                "time":  str(ts),
                            }
                            _got = True
                except Exception:
                    pass
    except ImportError:
        pass

    # ── Layer 2: nsepython nse_eq() — NSE equity-quote page API ──────────────
    _missing = [s for s in symbols if s not in out]
    if _missing:
        try:
            import logging as _lg
            _lg.disable(_lg.WARNING)
            from nsepython import nse_eq as _nse_eq
            _lg.disable(_lg.NOTSET)
            for sym in _missing:
                try:
                    _d  = _nse_eq(sym.upper())
                    _px = (
                        _d.get("priceInfo", {}).get("lastPrice")
                        or _d.get("marketDeptOrderBook", {}).get("tradeInfo", {}).get("lastPrice")
                    )
                    if _px is not None:
                        out[sym] = {
                            "price": float(str(_px).replace(",", "")),
                            "time":  _d.get("metadata", {}).get("lastUpdateTime", "nse_eq"),
                        }
                except Exception:
                    pass
        except Exception:
            pass

    return out


def _load_scan_session(s: dict) -> None:
    raw = json.loads(s["results_json"])
    results = []
    for rd in raw:
        try:
            results.append(StockData(**rd))
        except Exception:
            pass
    st.session_state.stock_data = results
    st.session_state.active_scan_name = s["scan_name"]
    st.session_state.last_fetched_at = None   # loaded from history, not live
    st.session_state.last_scan_symbols = [r.symbol for r in results]


def _load_dma_session(s: dict) -> None:
    from src.data.dma_models import DMAData as _DMAData
    raw = json.loads(s["results_json"])
    results = []
    for rd in raw:
        try:
            results.append(_DMAData(**rd))
        except Exception:
            pass
    st.session_state.dma_data = results
    st.session_state.dma_active_scan_name = s["scan_name"]
    st.session_state.dma_last_fetched_at = None
    st.session_state.dma_last_scan_symbols = [r.symbol for r in results]


def _fmt_ts(ts: datetime) -> str:
    """Format a datetime (tz-aware or naive) as a readable IST string."""
    if ts is None:
        return ""
    # Ensure IST — convert if tz-aware, assume IST if naive (legacy values)
    if ts.tzinfo is not None:
        ts = ts.astimezone(IST)
    hour = ts.hour % 12 or 12
    ampm = "a.m." if ts.hour < 12 else "p.m."
    return f"{ts.day} {ts.strftime('%b %Y')}, {hour}:{ts.strftime('%M')} {ampm} IST"


def _fmt_fetched_at() -> str:
    """Return a human-readable 'Fetched at' string from session state."""
    return _fmt_ts(st.session_state.get("last_fetched_at"))


def _do_fetch(symbols: list, scan_name: str, save_to_history: bool = False,
              force_refresh: bool = False) -> None:
    """Fetch symbols, update session state. Save to scan history only when requested."""
    # Invalidate cache for each symbol so a forced refresh always hits screener.in
    if force_refresh:
        from src.data.cache_manager import cache_manager as _cm
        for sym in symbols:
            _cm.invalidate(sym.strip().upper())
    progress = st.progress(0.0, text="Starting…")
    slot = st.empty()
    results = []
    for i, sym in enumerate(symbols):
        slot.markdown(_spinner_html(f"⏳ Fetching <b>{sym}</b> ({i+1}/{len(symbols)})…"), unsafe_allow_html=True)
        results.append(_aggregator.fetch_stock(sym))
        progress.progress((i + 1) / len(symbols))
        if i < len(symbols) - 1:
            time.sleep(0.8)
    progress.empty()
    slot.empty()

    st.session_state.stock_data = results
    st.session_state.last_fetched_at = _now_ist()
    st.session_state.last_scan_symbols = symbols

    # Only save file-upload scans to history (once); never on refresh or single-stock lookups
    if save_to_history and st.session_state.user and results:
        user_phone = st.session_state.user["phone_number"]
        dicts = [r.model_dump() for r in results]
        db.save_scan_session(user_phone, scan_name, dicts)


def _do_dma_fetch(symbols: list, scan_name: str, ref_date=None,
                   save_to_history: bool = False) -> None:
    """Fetch DMA in parallel (5 workers), show live count, optionally save to history."""
    from src.scrapers.dma_fetcher import DMAFetcher
    from src.data.dma_models import DMAData as _DMAData

    fetcher = DMAFetcher()
    slot = st.empty()

    if len(symbols) == 1:
        slot.markdown(_spinner_html(f"⏳ Fetching DMA for <b>{symbols[0]}</b>…"), unsafe_allow_html=True)
        results = [fetcher.fetch_dma(symbols[0], ref_date)]
        slot.empty()
    else:
        results_map: dict = {}
        done_count = [0]
        slot.markdown(_spinner_html(f"⏳ Fetching DMA for {len(symbols)} symbols…  0 / {len(symbols)}"), unsafe_allow_html=True)

        with ThreadPoolExecutor(max_workers=5) as ex:
            futures = {ex.submit(fetcher.fetch_dma, sym, ref_date): sym for sym in symbols}
            for future in as_completed(futures):
                sym = futures[future]
                try:
                    results_map[sym] = future.result(timeout=120)
                except Exception as exc:
                    results_map[sym] = _DMAData(symbol=sym, error=str(exc))
                done_count[0] += 1
                slot.markdown(
                    _spinner_html(f"⏳ Fetching DMA for {len(symbols)} symbols…  {done_count[0]} / {len(symbols)}"),
                    unsafe_allow_html=True,
                )

        slot.empty()
        results = [results_map.get(s, _DMAData(symbol=s, error="Not fetched")) for s in symbols]

    # NSE live price enrichment — mirrors Daily Dukaan two-scenario logic
    nse_slot   = st.empty()
    market_open = _is_market_open()
    today_str   = str(date.today())
    is_today    = (ref_date is not None and str(ref_date) == today_str)
    is_weekday  = date.today().weekday() < 5
    today_sess  = is_today and is_weekday

    nse_slot.markdown(
        _spinner_html(
            "📡 Fetching live NSE prices…" if market_open
            else "📡 Fetching today's closing prices from NSE…" if today_sess
            else "📡 Fetching last-close prices from NSE…"
        ),
        unsafe_allow_html=True,
    )
    try:
        nse_px = _fetch_nse_live_prices_bulk(symbols)
        enriched = []
        for r in results:
            lp = nse_px.get(r.symbol)
            if lp and not r.error:
                cp     = round(lp["price"], 2)
                source = "NSE Live" if market_open else "NSE Close"
                nse_date = r.ref_date
                if today_sess:
                    if lp.get("time"):
                        try:
                            nse_date = datetime.strptime(lp["time"], "%d-%b-%Y %H:%M:%S").strftime("%Y-%m-%d")
                        except Exception:
                            try:
                                nse_date = datetime.strptime(lp["time"][:11], "%d-%b-%Y").strftime("%Y-%m-%d")
                            except Exception:
                                pass
                else:
                    if lp.get("time"):
                        try:
                            candidate = datetime.strptime(lp["time"], "%d-%b-%Y %H:%M:%S").strftime("%Y-%m-%d")
                        except Exception:
                            try:
                                candidate = datetime.strptime(lp["time"][:11], "%d-%b-%Y").strftime("%Y-%m-%d")
                            except Exception:
                                candidate = None
                        if candidate and candidate > (nse_date or ""):
                            nse_date = candidate
                dmas = [r.dma1, r.dma2, r.dma3, r.dma4, r.dma5, r.dma6, r.dma7,
                        r.dma10, r.dma20, r.dma22, r.dma50, r.dma100, r.dma200]
                above_count = sum(1 for v in dmas if v is not None and cp > v)
                enriched.append(r.model_copy(update={
                    "current_price": cp, "ref_date": nse_date,
                    "price_source": source, "nse_live_time": lp["time"],
                    "above_dma_count": above_count,
                }))
            else:
                enriched.append(r)
        # Post-enrichment: snap all NSE-enriched records to max NSE date
        nse_dates = [r.ref_date for r in enriched if r.ref_date and r.price_source in ("NSE Live", "NSE Close")]
        if nse_dates:
            max_nse_date = max(nse_dates)
            enriched = [
                r.model_copy(update={"ref_date": max_nse_date})
                if (r.price_source in ("NSE Live", "NSE Close") and r.ref_date and r.ref_date < max_nse_date)
                else r
                for r in enriched
            ]
        results = enriched
        nse_slot.empty()
    except Exception as exc:
        nse_slot.empty()
        st.warning(f"⚠️ Could not fetch NSE prices: {exc}")

    st.session_state.dma_data = results
    st.session_state.dma_last_fetched_at = _now_ist()
    st.session_state.dma_last_scan_symbols = symbols
    st.session_state.dma_active_scan_name = scan_name

    if save_to_history and st.session_state.user and results:
        user_phone = st.session_state.user["phone_number"]
        dicts = [r.model_dump() for r in results]
        db.save_scan_session(user_phone, scan_name, dicts, scan_type="dma")


def _load_ema_session(s: dict) -> None:
    from src.data.ema_models import EMAData as _EMAData
    raw = json.loads(s["results_json"])
    results = []
    for rd in raw:
        try:
            results.append(_EMAData(**rd))
        except Exception:
            pass
    st.session_state.ema_data = results
    st.session_state.ema_active_scan_name = s["scan_name"]
    st.session_state.ema_last_fetched_at = None
    st.session_state.ema_last_scan_symbols = [r.symbol for r in results]


def _do_ema_fetch(symbols: list, scan_name: str, ref_date=None,
                   save_to_history: bool = False) -> None:
    """Fetch EMA in parallel (5 workers), show live count, optionally save to history."""
    from src.scrapers.ema_fetcher import EMAFetcher
    from src.data.ema_models import EMAData as _EMAData

    fetcher = EMAFetcher()
    slot = st.empty()

    if len(symbols) == 1:
        slot.markdown(_spinner_html(f"⏳ Fetching EMA for <b>{symbols[0]}</b>…"), unsafe_allow_html=True)
        results = [fetcher.fetch_ema(symbols[0], ref_date)]
        slot.empty()
    else:
        results_map: dict = {}
        done_count = [0]
        slot.markdown(_spinner_html(f"⏳ Fetching EMA for {len(symbols)} symbols…  0 / {len(symbols)}"), unsafe_allow_html=True)

        with ThreadPoolExecutor(max_workers=5) as ex:
            futures = {ex.submit(fetcher.fetch_ema, sym, ref_date): sym for sym in symbols}
            for future in as_completed(futures):
                sym = futures[future]
                try:
                    results_map[sym] = future.result(timeout=120)
                except Exception as exc:
                    results_map[sym] = _EMAData(symbol=sym, error=str(exc))
                done_count[0] += 1
                slot.markdown(
                    _spinner_html(f"⏳ Fetching EMA for {len(symbols)} symbols…  {done_count[0]} / {len(symbols)}"),
                    unsafe_allow_html=True,
                )

        slot.empty()
        results = [results_map.get(s, _EMAData(symbol=s, error="Not fetched")) for s in symbols]

    # NSE live price enrichment — mirrors Daily Dukaan two-scenario logic
    nse_slot   = st.empty()
    market_open = _is_market_open()
    today_str   = str(date.today())
    is_today    = (ref_date is not None and str(ref_date) == today_str)
    is_weekday  = date.today().weekday() < 5
    today_sess  = is_today and is_weekday

    nse_slot.markdown(
        _spinner_html(
            "📡 Fetching live NSE prices…" if market_open
            else "📡 Fetching today's closing prices from NSE…" if today_sess
            else "📡 Fetching last-close prices from NSE…"
        ),
        unsafe_allow_html=True,
    )
    try:
        nse_px = _fetch_nse_live_prices_bulk(symbols)
        enriched = []
        for r in results:
            lp = nse_px.get(r.symbol)
            if lp and not r.error:
                cp     = round(lp["price"], 2)
                source = "NSE Live" if market_open else "NSE Close"
                nse_date = r.ref_date
                if today_sess:
                    if lp.get("time"):
                        try:
                            nse_date = datetime.strptime(lp["time"], "%d-%b-%Y %H:%M:%S").strftime("%Y-%m-%d")
                        except Exception:
                            try:
                                nse_date = datetime.strptime(lp["time"][:11], "%d-%b-%Y").strftime("%Y-%m-%d")
                            except Exception:
                                pass
                else:
                    if lp.get("time"):
                        try:
                            candidate = datetime.strptime(lp["time"], "%d-%b-%Y %H:%M:%S").strftime("%Y-%m-%d")
                        except Exception:
                            try:
                                candidate = datetime.strptime(lp["time"][:11], "%d-%b-%Y").strftime("%Y-%m-%d")
                            except Exception:
                                candidate = None
                        if candidate and candidate > (nse_date or ""):
                            nse_date = candidate
                emas = [r.ema1, r.ema2, r.ema3, r.ema4, r.ema5, r.ema6, r.ema7,
                        r.ema10, r.ema20, r.ema22, r.ema50, r.ema100, r.ema200]
                above_count = sum(1 for v in emas if v is not None and cp > v)
                enriched.append(r.model_copy(update={
                    "current_price": cp, "ref_date": nse_date,
                    "price_source": source, "nse_live_time": lp["time"],
                    "above_ema_count": above_count,
                }))
            else:
                enriched.append(r)
        # Post-enrichment: snap all NSE-enriched records to max NSE date
        nse_dates = [r.ref_date for r in enriched if r.ref_date and r.price_source in ("NSE Live", "NSE Close")]
        if nse_dates:
            max_nse_date = max(nse_dates)
            enriched = [
                r.model_copy(update={"ref_date": max_nse_date})
                if (r.price_source in ("NSE Live", "NSE Close") and r.ref_date and r.ref_date < max_nse_date)
                else r
                for r in enriched
            ]
        results = enriched
        nse_slot.empty()
    except Exception as exc:
        nse_slot.empty()
        st.warning(f"⚠️ Could not fetch NSE prices: {exc}")

    st.session_state.ema_data = results
    st.session_state.ema_last_fetched_at = _now_ist()
    st.session_state.ema_last_scan_symbols = symbols
    st.session_state.ema_active_scan_name = scan_name

    if save_to_history and st.session_state.user and results:
        user_phone = st.session_state.user["phone_number"]
        dicts = [r.model_dump() for r in results]
        db.save_scan_session(user_phone, scan_name, dicts, scan_type="ema")


def _load_guru_buy_session(s: dict) -> None:
    from src.data.guru_buy_models import GURUBuyData as _GBD
    raw = json.loads(s["results_json"])
    results = []
    for rd in raw:
        try:
            results.append(_GBD(**rd))
        except Exception:
            pass
    st.session_state.guru_data = results
    st.session_state.guru_active_scan_name = s["scan_name"]
    st.session_state.guru_last_fetched_at = None
    st.session_state.guru_last_scan_symbols = [r.symbol for r in results]


def _do_guru_buy_fetch(symbols: list, scan_name: str, ref_date=None,
                        save_to_history: bool = False) -> None:
    from src.scrapers.guru_buy_fetcher import GURUBuyFetcher
    from src.data.guru_buy_models import GURUBuyData as _GBD

    fetcher = GURUBuyFetcher()
    slot = st.empty()

    if len(symbols) == 1:
        slot.markdown(_spinner_html(f"⏳ Fetching GURU BUY for <b>{symbols[0]}</b>…"), unsafe_allow_html=True)
        results = [fetcher.fetch(symbols[0], ref_date)]
        slot.empty()
    else:
        results_map: dict = {}
        done_count = [0]
        slot.markdown(_spinner_html(f"⏳ Calculating GURU BUY for {len(symbols)} symbols…  0 / {len(symbols)}"), unsafe_allow_html=True)
        with ThreadPoolExecutor(max_workers=5) as ex:
            futures = {ex.submit(fetcher.fetch, sym, ref_date): sym for sym in symbols}
            for future in as_completed(futures):
                sym = futures[future]
                try:
                    results_map[sym] = future.result(timeout=120)
                except Exception as exc:
                    results_map[sym] = _GBD(symbol=sym, error=str(exc))
                done_count[0] += 1
                slot.markdown(
                    _spinner_html(f"⏳ Calculating GURU BUY for {len(symbols)} symbols…  {done_count[0]} / {len(symbols)}"),
                    unsafe_allow_html=True,
                )
        slot.empty()
        results = [results_map.get(s, _GBD(symbol=s, error="Not fetched")) for s in symbols]

    # NSE live price enrichment — mirrors Daily Dukaan two-scenario logic
    nse_slot   = st.empty()
    market_open = _is_market_open()
    today_str   = str(date.today())
    is_today    = (ref_date is not None and str(ref_date) == today_str)
    is_weekday  = date.today().weekday() < 5
    today_sess  = is_today and is_weekday

    nse_slot.markdown(
        _spinner_html(
            "📡 Fetching live NSE prices…" if market_open
            else "📡 Fetching today's closing prices from NSE…" if today_sess
            else "📡 Fetching last-close prices from NSE…"
        ),
        unsafe_allow_html=True,
    )
    try:
        nse_px = _fetch_nse_live_prices_bulk(symbols)
        enriched = []
        for r in results:
            lp = nse_px.get(r.symbol)
            if lp and not r.error:
                cp     = round(lp["price"], 2)
                source = "NSE Live" if market_open else "NSE Close"
                nse_date = r.ref_date
                if today_sess:
                    if lp.get("time"):
                        try:
                            nse_date = datetime.strptime(lp["time"], "%d-%b-%Y %H:%M:%S").strftime("%Y-%m-%d")
                        except Exception:
                            try:
                                nse_date = datetime.strptime(lp["time"][:11], "%d-%b-%Y").strftime("%Y-%m-%d")
                            except Exception:
                                pass
                else:
                    if lp.get("time"):
                        try:
                            candidate = datetime.strptime(lp["time"], "%d-%b-%Y %H:%M:%S").strftime("%Y-%m-%d")
                        except Exception:
                            try:
                                candidate = datetime.strptime(lp["time"][:11], "%d-%b-%Y").strftime("%Y-%m-%d")
                            except Exception:
                                candidate = None
                        if candidate and candidate > (nse_date or ""):
                            nse_date = candidate
                enriched.append(r.model_copy(update={
                    "current_price": cp, "ref_date": nse_date,
                    "price_source": source, "nse_live_time": lp["time"],
                }))
            else:
                enriched.append(r)
        # Post-enrichment: snap all NSE-enriched records to max NSE date
        nse_dates = [r.ref_date for r in enriched if r.ref_date and r.price_source in ("NSE Live", "NSE Close")]
        if nse_dates:
            max_nse_date = max(nse_dates)
            enriched = [
                r.model_copy(update={"ref_date": max_nse_date})
                if (r.price_source in ("NSE Live", "NSE Close") and r.ref_date and r.ref_date < max_nse_date)
                else r
                for r in enriched
            ]
        results = enriched
        nse_slot.empty()
    except Exception as exc:
        nse_slot.empty()
        st.warning(f"⚠️ Could not fetch NSE prices: {exc}")

    st.session_state.guru_data = results
    st.session_state.guru_last_fetched_at = _now_ist()
    st.session_state.guru_last_scan_symbols = symbols
    st.session_state.guru_active_scan_name = scan_name

    if save_to_history and st.session_state.user and results:
        user_phone = st.session_state.user["phone_number"]
        dicts = [r.model_dump() for r in results]
        db.save_scan_session(user_phone, scan_name, dicts, scan_type="guru_buy")


def _load_smc_session(s: dict) -> None:
    from src.data.smc_models import SMCData as _SMCD
    raw = json.loads(s["results_json"])
    results = []
    for rd in raw:
        try:
            results.append(_SMCD(**rd))
        except Exception:
            pass
    st.session_state.smc_data = results
    st.session_state.smc_active_scan_name = s["scan_name"]
    st.session_state.smc_last_fetched_at = None
    st.session_state.smc_last_scan_symbols = [r.symbol for r in results]


def _load_dd_session(s: dict) -> None:
    from src.data.daily_dukaan_models import DailyDukaanData as _DD
    raw = json.loads(s["results_json"])
    results = []
    for rd in raw:
        try:
            results.append(_DD(**rd))
        except Exception:
            pass
    st.session_state.dd_data                = results
    st.session_state.dd_active_scan_name    = s["scan_name"]
    st.session_state.dd_last_fetched_at     = None
    st.session_state.dd_last_scan_symbols   = [r.symbol for r in results]


def _do_dd_fetch(symbols: list, scan_name: str, ref_date=None,
                 save_to_history: bool = False) -> None:
    """
    Fetch 20-DMA data from NSE India (nsepython), then enrich with NSE quote-equity prices.

    Two-step approach:
    - Step 1 (nsepython equity_history): 35 days of OHLC → 20-day SMA.
      Bhavcopy is published after end-of-day processing, so today's close only
      appears here ~15–60 min after market close. During market hours it returns
      the previous trading day as the last row.
    - Step 2 (NSE quote-equity API): live lastPrice during market hours; official
      close price immediately after market close. Always authoritative for today's
      price and trading date.
    """
    from src.scrapers.daily_dukaan_fetcher import DailyDukaanFetcher
    from src.data.daily_dukaan_models import DailyDukaanData as _DD

    fetcher = DailyDukaanFetcher()
    slot    = st.empty()

    # ── Step 1: 20 DMA via NSE India (nsepython) with Yahoo Finance fallback ───
    if len(symbols) == 1:
        slot.markdown(_spinner_html(f"⏳ Fetching 20 DMA for <b>{symbols[0]}</b> from NSE India…"), unsafe_allow_html=True)
        results = [fetcher.fetch(symbols[0], ref_date)]
        slot.empty()
    else:
        results_map: dict = {}
        done_count = [0]
        slot.markdown(_spinner_html(f"⏳ Fetching 20 DMA from NSE India for {len(symbols)} symbols…  0 / {len(symbols)}"), unsafe_allow_html=True)

        with ThreadPoolExecutor(max_workers=10) as ex:
            futures = {ex.submit(fetcher.fetch, sym, ref_date): sym for sym in symbols}
            for future in as_completed(futures):
                sym = futures[future]
                try:
                    results_map[sym] = future.result(timeout=120)
                except Exception as exc:
                    results_map[sym] = _DD(symbol=sym, error=str(exc))
                done_count[0] += 1
                slot.markdown(
                    _spinner_html(f"⏳ Fetching 20 DMA from NSE India for {len(symbols)} symbols…  {done_count[0]} / {len(symbols)}"),
                    unsafe_allow_html=True,
                )

        slot.empty()
        results = [results_map.get(s, _DD(symbol=s, error="Not fetched")) for s in symbols]

    # ── Step 2: NSE price enrichment ─────────────────────────────────────────
    # Always fetch from NSE quote-equity (the only authoritative source).
    # Three scenarios:
    #   A. Today is a weekday and user picked today's date
    #      → Trust the NSE timestamp as the definitive trading date.
    #        Liquid ETFs will show today; low-liquidity ones show their last
    #        actual trade date (correct — they didn't trade today).
    #   B. Weekend / holiday selected
    #      → Use max(historical_date, nse_timestamp) to fix any yfinance lag.
    nse_slot = st.empty()
    market_open  = _is_market_open()
    today_str    = str(date.today())
    is_today     = (ref_date is not None and str(ref_date) == today_str)
    is_weekday   = date.today().weekday() < 5          # Mon–Fri
    today_session = is_today and is_weekday            # any weekday (open OR just closed)

    nse_slot.markdown(
        _spinner_html(
            "📡 Fetching live NSE prices…" if market_open
            else "📡 Fetching today's closing prices from NSE…" if today_session
            else "📡 Fetching last-close prices from NSE…"
        ),
        unsafe_allow_html=True,
    )
    try:
        nse_px = _fetch_nse_live_prices_bulk(symbols)
        enriched = []
        for r in results:
            lp = nse_px.get(r.symbol)
            if lp and not r.error:
                cp    = round(lp["price"], 2)
                dma20 = r.dma20
                source = "NSE Live" if market_open else "NSE Close"

                if today_session:
                    # Scenario A — weekday, today selected:
                    # Trust the NSE timestamp directly as the trading date.
                    # This covers both intraday (market open) and post-close correctly.
                    nse_date = r.ref_date  # historical fallback
                    if lp.get("time"):
                        try:
                            parsed = datetime.strptime(lp["time"], "%d-%b-%Y %H:%M:%S")
                            nse_date = parsed.strftime("%Y-%m-%d")
                        except Exception:
                            try:
                                parsed = datetime.strptime(lp["time"][:11], "%d-%b-%Y")
                                nse_date = parsed.strftime("%Y-%m-%d")
                            except Exception:
                                pass
                else:
                    # Scenario B — weekend / holiday / past date selected:
                    # max(yfinance_date, nse_timestamp) fixes yfinance weekend lag
                    # without letting low-liquidity ETFs pull the date backward.
                    nse_date = r.ref_date
                    if lp.get("time"):
                        try:
                            parsed = datetime.strptime(lp["time"], "%d-%b-%Y %H:%M:%S")
                            candidate = parsed.strftime("%Y-%m-%d")
                        except Exception:
                            try:
                                parsed = datetime.strptime(lp["time"][:11], "%d-%b-%Y")
                                candidate = parsed.strftime("%Y-%m-%d")
                            except Exception:
                                candidate = None
                        if candidate and candidate > (nse_date or ""):
                            nse_date = candidate

                enriched.append(r.model_copy(update={
                    "current_price":  cp,
                    "ref_date":       nse_date,
                    "price_source":   source,
                    "nse_live_time":  lp["time"],
                    "price_vs_dma20": round(cp - dma20, 2) if dma20 else None,
                    "pct_vs_dma20":   round((cp - dma20) / dma20 * 100, 2) if dma20 else None,
                }))
            else:
                enriched.append(r)
        results = enriched
        nse_slot.empty()
    except Exception as exc:
        nse_slot.empty()
        st.warning(f"⚠️ Could not fetch NSE prices: {exc}")

    st.session_state.dd_data              = results
    st.session_state.dd_last_fetched_at   = _now_ist()
    st.session_state.dd_last_scan_symbols = symbols
    st.session_state.dd_active_scan_name  = scan_name

    if save_to_history:
        user_phone = st.session_state.user["phone_number"]
        dicts = [r.model_dump() for r in results]
        db.save_scan_session(user_phone, scan_name, dicts, scan_type="daily_dukaan")


def _do_smc_fetch(symbols: list, scan_name: str, ref_date=None,
                  save_to_history: bool = False) -> None:
    from src.scrapers.smc_fetcher import SMCFetcher
    from src.data.smc_models import SMCData as _SMCD

    fetcher = SMCFetcher()
    slot = st.empty()

    if len(symbols) == 1:
        slot.markdown(_spinner_html(f"⏳ Calculating SMC zones for <b>{symbols[0]}</b>…"), unsafe_allow_html=True)
        results = [fetcher.fetch(symbols[0], ref_date)]
        slot.empty()
    else:
        results_map: dict = {}
        done_count = [0]
        slot.markdown(_spinner_html(f"⏳ Calculating SMC zones for {len(symbols)} symbols…  0 / {len(symbols)}"), unsafe_allow_html=True)
        with ThreadPoolExecutor(max_workers=5) as ex:
            futures = {ex.submit(fetcher.fetch, sym, ref_date): sym for sym in symbols}
            for future in as_completed(futures):
                sym = futures[future]
                try:
                    results_map[sym] = future.result(timeout=120)
                except Exception as exc:
                    results_map[sym] = _SMCD(symbol=sym, error=str(exc))
                done_count[0] += 1
                slot.markdown(
                    _spinner_html(f"⏳ Calculating SMC zones for {len(symbols)} symbols…  {done_count[0]} / {len(symbols)}"),
                    unsafe_allow_html=True,
                )
        slot.empty()
        results = [results_map.get(s, _SMCD(symbol=s, error="Not fetched")) for s in symbols]

    # NSE live price enrichment — mirrors Daily Dukaan two-scenario logic
    nse_slot   = st.empty()
    market_open = _is_market_open()
    today_str   = str(date.today())
    is_today    = (ref_date is not None and str(ref_date) == today_str)
    is_weekday  = date.today().weekday() < 5
    today_sess  = is_today and is_weekday

    nse_slot.markdown(
        _spinner_html(
            "📡 Fetching live NSE prices…" if market_open
            else "📡 Fetching today's closing prices from NSE…" if today_sess
            else "📡 Fetching last-close prices from NSE…"
        ),
        unsafe_allow_html=True,
    )
    try:
        nse_px = _fetch_nse_live_prices_bulk(symbols)
        enriched = []
        for r in results:
            lp = nse_px.get(r.symbol)
            if lp and not r.error:
                cp     = round(lp["price"], 2)
                source = "NSE Live" if market_open else "NSE Close"
                nse_date = r.ref_date
                if today_sess:
                    if lp.get("time"):
                        try:
                            nse_date = datetime.strptime(lp["time"], "%d-%b-%Y %H:%M:%S").strftime("%Y-%m-%d")
                        except Exception:
                            try:
                                nse_date = datetime.strptime(lp["time"][:11], "%d-%b-%Y").strftime("%Y-%m-%d")
                            except Exception:
                                pass
                else:
                    if lp.get("time"):
                        try:
                            candidate = datetime.strptime(lp["time"], "%d-%b-%Y %H:%M:%S").strftime("%Y-%m-%d")
                        except Exception:
                            try:
                                candidate = datetime.strptime(lp["time"][:11], "%d-%b-%Y").strftime("%Y-%m-%d")
                            except Exception:
                                candidate = None
                        if candidate and candidate > (nse_date or ""):
                            nse_date = candidate
                # Reclassify SMC zone with the live/close price
                p = cp
                top = r.swing_high
                bot = r.swing_low
                if top and bot and top > bot:
                    if p >= r.premium_bottom:
                        zone = "Premium"
                    elif p >= r.eq_top:
                        zone = "Near Premium"
                    elif p >= r.eq_bottom:
                        zone = "Equilibrium"
                    elif p >= r.discount_top:
                        zone = "Near Discount"
                    else:
                        zone = "Discount"
                    zone_pct   = round((p - bot) / (top - bot) * 100, 1)
                    buy_signal = (zone in ("Discount", "Near Discount") and r.fvg == "Bullish")
                else:
                    zone, zone_pct, buy_signal = r.zone, r.zone_pct, r.buy_signal
                enriched.append(r.model_copy(update={
                    "current_price": cp, "ref_date": nse_date,
                    "price_source": source, "nse_live_time": lp["time"],
                    "zone": zone, "zone_pct": zone_pct, "buy_signal": buy_signal,
                }))
            else:
                enriched.append(r)
        # Post-enrichment: snap all NSE-enriched records to max NSE date
        nse_dates = [r.ref_date for r in enriched if r.ref_date and r.price_source in ("NSE Live", "NSE Close")]
        if nse_dates:
            max_nse_date = max(nse_dates)
            enriched = [
                r.model_copy(update={"ref_date": max_nse_date})
                if (r.price_source in ("NSE Live", "NSE Close") and r.ref_date and r.ref_date < max_nse_date)
                else r
                for r in enriched
            ]
        results = enriched
        nse_slot.empty()
    except Exception as exc:
        nse_slot.empty()
        st.warning(f"⚠️ Could not fetch NSE prices: {exc}")

    st.session_state.smc_data = results
    st.session_state.smc_last_fetched_at = _now_ist()
    st.session_state.smc_last_scan_symbols = symbols
    st.session_state.smc_active_scan_name = scan_name
    # Persist alert stocks (Discount / Near Discount zone + Bullish FVG) across reruns — after enrichment
    st.session_state.smc_alert_stocks = [r for r in results if r.buy_signal]

    if save_to_history and st.session_state.user and results:
        user_phone = st.session_state.user["phone_number"]
        dicts = [r.model_dump() for r in results]
        db.save_scan_session(user_phone, scan_name, dicts, scan_type="smc")


def _load_ll_etf_session(s: dict) -> None:
    from src.data.ll_etf_models import LLETFData as _LLD
    raw = json.loads(s["results_json"])
    results = []
    for rd in raw:
        try:
            results.append(_LLD(**rd))
        except Exception:
            pass
    st.session_state.ll_etf_data              = results
    st.session_state.ll_etf_active_scan_name  = s["scan_name"]
    st.session_state.ll_etf_last_fetched_at   = None
    st.session_state.ll_etf_last_scan_symbols = [r.symbol for r in results]


def _do_ll_etf_fetch(symbols: list, scan_name: str, ref_date=None,
                     save_to_history: bool = False) -> None:
    """Fetch Life Long ETF data (52W Low, 3Y Avg, 5Y Avg) in parallel."""
    from src.scrapers.ll_etf_fetcher import LLETFFetcher
    from src.data.ll_etf_models import LLETFData as _LLD

    fetcher = LLETFFetcher()
    slot    = st.empty()

    if len(symbols) == 1:
        slot.markdown(_spinner_html(f"⏳ Fetching Life Long ETF data for <b>{symbols[0]}</b>…"), unsafe_allow_html=True)
        results = [fetcher.fetch(symbols[0], ref_date)]
        slot.empty()
    else:
        results_map: dict = {}
        done_count = [0]
        slot.markdown(_spinner_html(f"⏳ Fetching Life Long ETF data for {len(symbols)} symbols…  0 / {len(symbols)}"), unsafe_allow_html=True)

        with ThreadPoolExecutor(max_workers=8) as ex:
            futures = {ex.submit(fetcher.fetch, sym, ref_date): sym for sym in symbols}
            for future in as_completed(futures):
                sym = futures[future]
                try:
                    results_map[sym] = future.result(timeout=180)
                except Exception as exc:
                    results_map[sym] = _LLD(symbol=sym, error=str(exc))
                done_count[0] += 1
                slot.markdown(
                    _spinner_html(f"⏳ Fetching Life Long ETF data for {len(symbols)} symbols…  {done_count[0]} / {len(symbols)}"),
                    unsafe_allow_html=True,
                )

        slot.empty()
        results = [results_map.get(s, _LLD(symbol=s, error="Not fetched")) for s in symbols]

    # ── Step 2: NSE quote-equity enrichment — identical to Daily Dukaan ─────────
    nse_slot      = st.empty()
    market_open   = _is_market_open()
    today_str     = str(date.today())
    is_today      = (ref_date is not None and str(ref_date) == today_str)
    is_weekday    = date.today().weekday() < 5
    today_session = is_today and is_weekday

    nse_slot.markdown(
        _spinner_html(
            "📡 Fetching live NSE prices…" if market_open
            else "📡 Fetching today's closing prices from NSE…" if today_session
            else "📡 Fetching last-close prices from NSE…"
        ),
        unsafe_allow_html=True,
    )
    try:
        nse_px = _fetch_nse_live_prices_bulk(symbols)
        enriched = []
        for r in results:
            lp = nse_px.get(r.symbol)
            if lp and not r.error:
                cp     = round(lp["price"], 2)
                source = "NSE Live" if market_open else "NSE Close"

                if today_session:
                    nse_date = r.ref_date
                    if lp.get("time"):
                        try:
                            parsed = datetime.strptime(lp["time"], "%d-%b-%Y %H:%M:%S")
                            nse_date = parsed.strftime("%Y-%m-%d")
                        except Exception:
                            try:
                                parsed = datetime.strptime(lp["time"][:11], "%d-%b-%Y")
                                nse_date = parsed.strftime("%Y-%m-%d")
                            except Exception:
                                pass
                else:
                    nse_date = r.ref_date
                    if lp.get("time"):
                        try:
                            parsed = datetime.strptime(lp["time"], "%d-%b-%Y %H:%M:%S")
                            candidate = parsed.strftime("%Y-%m-%d")
                        except Exception:
                            try:
                                parsed = datetime.strptime(lp["time"][:11], "%d-%b-%Y")
                                candidate = parsed.strftime("%Y-%m-%d")
                            except Exception:
                                candidate = None
                        if candidate and candidate > (nse_date or ""):
                            nse_date = candidate

                w52l = r.week52_low
                a3y  = r.avg_price_3y
                a5y  = r.avg_price_5y
                enriched.append(r.model_copy(update={
                    "current_price":    cp,
                    "price_on_date":    cp,
                    "ref_date":         nse_date,
                    "price_source":     source,
                    "nse_live_time":    lp["time"],
                    "diff_52w_low":     round(cp - w52l,  2) if w52l  else r.diff_52w_low,
                    "pct_vs_52w_low":   round((cp - w52l)  / w52l  * 100, 2) if w52l  else r.pct_vs_52w_low,
                    "diff_3y":          round(cp - a3y, 2) if a3y else r.diff_3y,
                    "pct_vs_3y":        round((cp - a3y) / a3y * 100, 2) if a3y else r.pct_vs_3y,
                    "diff_5y":          round(cp - a5y, 2) if a5y else r.diff_5y,
                    "pct_vs_5y":        round((cp - a5y) / a5y * 100, 2) if a5y else r.pct_vs_5y,
                }))
            else:
                enriched.append(r)
        results = enriched
        nse_slot.empty()
    except Exception as exc:
        nse_slot.empty()
        st.warning(f"⚠️ Could not fetch NSE prices: {exc}")

    st.session_state.ll_etf_data              = results
    st.session_state.ll_etf_last_fetched_at   = _now_ist()
    st.session_state.ll_etf_last_scan_symbols = symbols
    st.session_state.ll_etf_active_scan_name  = scan_name

    if save_to_history and st.session_state.user and results:
        user_phone = st.session_state.user["phone_number"]
        dicts = [r.model_dump() for r in results]
        db.save_scan_session(user_phone, scan_name, dicts, scan_type="ll_etf")


def _load_eff_session(s: dict) -> None:
    from src.data.eff_models import EFFData as _EFFD
    raw = json.loads(s["results_json"])
    results = []
    for rd in raw:
        try:
            results.append(_EFFD(**rd))
        except Exception:
            pass
    st.session_state.eff_data              = results
    st.session_state.eff_active_scan_name  = s["scan_name"]
    st.session_state.eff_last_fetched_at   = None
    st.session_state.eff_last_scan_symbols = [r.symbol for r in results]


def _do_eff_fetch(symbols: list, scan_name: str, ref_date=None,
                  save_to_history: bool = False) -> None:
    """Fetch Eshwarana Fruit Forest data (52W Low, 3Y Avg, 5Y Avg) in parallel."""
    from src.scrapers.eff_fetcher import EFFFetcher
    from src.data.eff_models import EFFData as _EFFD

    fetcher = EFFFetcher()
    slot    = st.empty()

    if len(symbols) == 1:
        slot.markdown(_spinner_html(f"⏳ Fetching Eshwarana Fruit Forest data for <b>{symbols[0]}</b>…"), unsafe_allow_html=True)
        results = [fetcher.fetch(symbols[0], ref_date)]
        slot.empty()
    else:
        results_map: dict = {}
        done_count = [0]
        slot.markdown(_spinner_html(f"⏳ Fetching Eshwarana Fruit Forest data for {len(symbols)} symbols…  0 / {len(symbols)}"), unsafe_allow_html=True)

        with ThreadPoolExecutor(max_workers=8) as ex:
            futures = {ex.submit(fetcher.fetch, sym, ref_date): sym for sym in symbols}
            for future in as_completed(futures):
                sym = futures[future]
                try:
                    results_map[sym] = future.result(timeout=180)
                except Exception as exc:
                    results_map[sym] = _EFFD(symbol=sym, error=str(exc))
                done_count[0] += 1
                slot.markdown(
                    _spinner_html(f"⏳ Fetching Eshwarana Fruit Forest data for {len(symbols)} symbols…  {done_count[0]} / {len(symbols)}"),
                    unsafe_allow_html=True,
                )

        slot.empty()
        results = [results_map.get(s, _EFFD(symbol=s, error="Not fetched")) for s in symbols]

    # ── Step 2: NSE quote-equity enrichment — identical to Daily Dukaan ─────────
    nse_slot      = st.empty()
    market_open   = _is_market_open()
    today_str     = str(date.today())
    is_today      = (ref_date is not None and str(ref_date) == today_str)
    is_weekday    = date.today().weekday() < 5
    today_session = is_today and is_weekday

    nse_slot.markdown(
        _spinner_html(
            "📡 Fetching live NSE prices…" if market_open
            else "📡 Fetching today's closing prices from NSE…" if today_session
            else "📡 Fetching last-close prices from NSE…"
        ),
        unsafe_allow_html=True,
    )
    try:
        nse_px = _fetch_nse_live_prices_bulk(symbols)
        enriched = []
        for r in results:
            lp = nse_px.get(r.symbol)
            if lp and not r.error:
                cp     = round(lp["price"], 2)
                source = "NSE Live" if market_open else "NSE Close"

                if today_session:
                    nse_date = r.ref_date
                    if lp.get("time"):
                        try:
                            parsed = datetime.strptime(lp["time"], "%d-%b-%Y %H:%M:%S")
                            nse_date = parsed.strftime("%Y-%m-%d")
                        except Exception:
                            try:
                                parsed = datetime.strptime(lp["time"][:11], "%d-%b-%Y")
                                nse_date = parsed.strftime("%Y-%m-%d")
                            except Exception:
                                pass
                else:
                    nse_date = r.ref_date
                    if lp.get("time"):
                        try:
                            parsed = datetime.strptime(lp["time"], "%d-%b-%Y %H:%M:%S")
                            candidate = parsed.strftime("%Y-%m-%d")
                        except Exception:
                            try:
                                parsed = datetime.strptime(lp["time"][:11], "%d-%b-%Y")
                                candidate = parsed.strftime("%Y-%m-%d")
                            except Exception:
                                candidate = None
                        if candidate and candidate > (nse_date or ""):
                            nse_date = candidate

                w52l = r.week52_low
                a3y  = r.avg_price_3y
                a5y  = r.avg_price_5y
                enriched.append(r.model_copy(update={
                    "current_price":    cp,
                    "price_on_date":    cp,
                    "ref_date":         nse_date,
                    "price_source":     source,
                    "nse_live_time":    lp["time"],
                    "diff_52w_low":     round(cp - w52l,  2) if w52l  else r.diff_52w_low,
                    "pct_vs_52w_low":   round((cp - w52l)  / w52l  * 100, 2) if w52l  else r.pct_vs_52w_low,
                    "diff_3y":          round(cp - a3y, 2) if a3y else r.diff_3y,
                    "pct_vs_3y":        round((cp - a3y) / a3y * 100, 2) if a3y else r.pct_vs_3y,
                    "diff_5y":          round(cp - a5y, 2) if a5y else r.diff_5y,
                    "pct_vs_5y":        round((cp - a5y) / a5y * 100, 2) if a5y else r.pct_vs_5y,
                }))
            else:
                enriched.append(r)
        results = enriched
        nse_slot.empty()
    except Exception as exc:
        nse_slot.empty()
        st.warning(f"⚠️ Could not fetch NSE prices: {exc}")

    st.session_state.eff_data              = results
    st.session_state.eff_last_fetched_at   = _now_ist()
    st.session_state.eff_last_scan_symbols = symbols
    st.session_state.eff_active_scan_name  = scan_name

    if save_to_history and st.session_state.user and results:
        user_phone = st.session_state.user["phone_number"]
        dicts = [r.model_dump() for r in results]
        db.save_scan_session(user_phone, scan_name, dicts, scan_type="eff")


def _do_etf_live_fetch() -> None:
    """Fetch the full NSE ETF live market data list and store in session state."""
    from src.scrapers.etf_fetcher import ETFFetcher
    slot = st.empty()
    slot.markdown(_spinner_html("⏳ Fetching NSE ETF live data…"), unsafe_allow_html=True)
    # Clear before fetch so stale values never cause AttributeError if this fetch fails
    st.session_state.etf_live_summary = None
    try:
        etf_list, summary = ETFFetcher().fetch_live_list()
        st.session_state.etf_live_data       = etf_list
        st.session_state.etf_live_fetched_at = _now_ist()
        st.session_state.etf_live_summary    = summary
        slot.empty()
    except Exception as exc:
        slot.empty()
        st.error(f"❌ Failed to fetch NSE ETF data: {exc}")


def _do_etf_ema_fetch(symbols: list, scan_name: str, ref_date=None,
                      scheme_map: Optional[dict] = None) -> None:
    """Calculate EMA(1-200) for a list of ETF symbols and store in session state."""
    from src.scrapers.etf_fetcher import ETFFetcher
    from src.data.ema_models import EMAData as _EMAD

    fetcher    = ETFFetcher()
    scheme_map = scheme_map or {}
    slot       = st.empty()

    if len(symbols) == 1:
        slot.markdown(_spinner_html(f"⏳ Calculating EMA for <b>{symbols[0]}</b>…"), unsafe_allow_html=True)
        results = [fetcher.fetch_ema(symbols[0], ref_date, scheme_map.get(symbols[0], ""))]
        slot.empty()
    else:
        results_map: dict = {}
        done_count = [0]
        slot.markdown(_spinner_html(f"⏳ Calculating EMA for {len(symbols)} ETF(s)…  0 / {len(symbols)}"), unsafe_allow_html=True)
        with ThreadPoolExecutor(max_workers=5) as ex:
            futures = {
                ex.submit(fetcher.fetch_ema, sym, ref_date,
                          scheme_map.get(sym, "")): sym
                for sym in symbols
            }
            for future in as_completed(futures):
                sym = futures[future]
                try:
                    results_map[sym] = future.result(timeout=120)
                except Exception as exc:
                    results_map[sym] = _EMAD(symbol=sym, error=str(exc))
                done_count[0] += 1
                slot.markdown(
                    _spinner_html(f"⏳ Calculating EMA for {len(symbols)} ETF(s)…  {done_count[0]} / {len(symbols)}"),
                    unsafe_allow_html=True,
                )
        slot.empty()
        results = [results_map.get(s, _EMAD(symbol=s, error="Not fetched")) for s in symbols]

    st.session_state.etf_ema_data              = results
    st.session_state.etf_ema_last_fetched_at   = _now_ist()
    st.session_state.etf_ema_last_scan_symbols = symbols
    st.session_state.etf_ema_active_scan_name  = scan_name


# ── Login ─────────────────────────────────────────────────────────────────────

def _auto_restore_scan(data_key: str, load_fn, scan_type: str) -> None:
    """
    If `data_key` in session state is empty (e.g. after a page refresh),
    silently load the most recent saved scan of `scan_type` from the database.
    This lets users see their last results without re-fetching.
    """
    if not st.session_state.get(data_key) and st.session_state.get("user"):
        recent = db.get_scan_sessions(
            st.session_state.user["phone_number"], limit=1, scan_type=scan_type
        )
        if recent:
            load_fn(recent[0])


def render_login():
    st.markdown(get_theme_css(st.session_state.dark_mode), unsafe_allow_html=True)
    _, col, _ = st.columns([1, 2, 1])
    with col:
        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown(
            f"<h1 style='text-align:center'>📊</h1>"
            f"<h2 style='text-align:center;margin-top:0'>{config.APP_NAME}</h2>"
            f"<p style='text-align:center;color:gray'>Fundamental Scanner — NSE / BSE</p>",
            unsafe_allow_html=True,
        )

        with st.form("login_form"):
            phone = st.text_input(
                "Mobile Number",
                value=st.session_state.phone_input,
                placeholder="+91 9876543210  or  9876543210",
            )

            if not st.session_state.otp_sent:
                if st.form_submit_button("📱 Send OTP", use_container_width=True, type="primary"):
                    if not phone.strip():
                        st.error("Please enter your mobile number.")
                    else:
                        with st.spinner("Sending OTP…"):
                            ok, msg, dev_otp = auth_manager.request_otp(phone.strip())
                        if ok:
                            st.session_state.otp_sent = True
                            st.session_state.phone_input = phone.strip()
                            st.session_state.dev_otp = dev_otp
                            st.rerun()
                        else:
                            st.error(f"❌ {msg}")
            else:
                st.info(f"OTP sent to: **{st.session_state.phone_input}**")
                if st.session_state.dev_otp:
                    st.warning(
                        f"🔧 **DEV MODE** — OTP: `{st.session_state.dev_otp}` "
                        f"*(Twilio not configured)*"
                    )
                otp_val = st.text_input("Enter OTP", max_chars=6, placeholder="6-digit code")
                c1, c2 = st.columns(2)
                with c1:
                    verify = st.form_submit_button("✅ Login", use_container_width=True, type="primary")
                with c2:
                    resend = st.form_submit_button("🔄 Resend", use_container_width=True)

                if verify:
                    if not otp_val.strip():
                        st.error("Please enter the OTP.")
                    else:
                        ok, msg, user = auth_manager.verify_login(
                            st.session_state.phone_input, otp_val.strip()
                        )
                        if ok:
                            _tok = _create_session_token(user)
                            st.session_state.update(
                                authenticated=True, user=user,
                                otp_sent=False, dev_otp=None
                            )
                            st.query_params["_s"] = _tok
                            # Clean up any duplicate scan history entries on login
                            db.deduplicate_scan_sessions(user["phone_number"])
                            st.rerun()
                        else:
                            st.error(f"❌ {msg}")
                if resend:
                    st.session_state.otp_sent = False
                    st.session_state.dev_otp = None
                    st.rerun()

        dark = st.toggle("🌙 Dark Mode", value=st.session_state.dark_mode)
        if dark != st.session_state.dark_mode:
            st.session_state.dark_mode = dark
            st.rerun()


# ── Sidebar ───────────────────────────────────────────────────────────────────

def render_sidebar():
    with st.sidebar:
        # Keep URL query param in sync so browser refresh restores the active section
        _cur_section = st.session_state.get("section", "fundamentals")
        if st.query_params.get("_p", "") != _cur_section:
            st.query_params["_p"] = _cur_section

        st.markdown(f"### 📊 {config.APP_NAME}")
        st.caption(f"v{config.VERSION}")
        st.markdown("---")
        user = st.session_state.user
        st.markdown(f"**👤 {user['phone_number']}**")
        if user.get("is_admin"):
            st.markdown("🔑 *Admin*")
        st.markdown("---")

        dark = st.toggle("🌙 Dark Mode", value=st.session_state.dark_mode)
        if dark != st.session_state.dark_mode:
            st.session_state.dark_mode = dark
            st.rerun()
        st.markdown("---")

        # ── Database status indicator ────────────────────────────────────────
        _is_neon = config.DATABASE_URL.startswith("postgresql")
        if _is_neon:
            st.markdown(
                '<div style="background:#16a34a;color:#ffffff;border-radius:0.5rem;'
                'padding:0.55rem 0.75rem;font-weight:600;font-size:0.85rem;'
                'text-align:center;letter-spacing:0.01em;">'
                '✅ &nbsp;Neon PostgreSQL</div>',
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                '<div style="background:#b45309;color:#ffffff;border-radius:0.5rem;'
                'padding:0.55rem 0.75rem;font-weight:600;font-size:0.85rem;'
                'text-align:center;letter-spacing:0.01em;">'
                '⚠️ &nbsp;SQLite — data not saved to cloud</div>',
                unsafe_allow_html=True,
            )
        st.markdown("---")

        # ── Main section navigation ──────────────────────────────────────────
        st.markdown("**📂 Navigation**")
        active = st.session_state.section

        fund_type     = "primary" if active == "fundamentals"       else "secondary"
        tech_type     = "primary" if active == "technicals"          else "secondary"
        gsb_type      = "primary" if active == "guru_smart_business" else "secondary"
        bps_type      = "primary" if active == "base_price_screener" else "secondary"
        rsi_type      = "primary" if active == "rsi_screener"        else "secondary"
        tt_type       = "primary" if active == "turtle_trading"       else "secondary"
        holdings_type = "primary" if active == "my_holdings"         else "secondary"

        if st.button("📊 Fundamentals", use_container_width=True, type=fund_type, key="nav_fund"):
            st.session_state.section = "fundamentals"
            st.session_state.page = "dashboard"
            st.rerun()
        if st.button("📈 Technicals", use_container_width=True, type=tech_type, key="nav_tech"):
            st.session_state.section = "technicals"
            st.rerun()
        if st.button("🏪 GURU SMART BUSINESS", use_container_width=True, type=gsb_type, key="nav_gsb"):
            st.session_state.section = "guru_smart_business"
            st.rerun()
        if st.button("📐 Base Price Screener", use_container_width=True, type=bps_type, key="nav_bps"):
            st.session_state.section = "base_price_screener"
            st.rerun()
        if st.button("📉 RSI Screener", use_container_width=True, type=rsi_type, key="nav_rsi"):
            st.session_state.section = "rsi_screener"
            st.rerun()
        if st.button("🐢 Turtle Trading", use_container_width=True, type=tt_type, key="nav_tt"):
            st.session_state.section = "turtle_trading"
            st.rerun()

        if user.get("is_admin"):
            admin_type = "primary" if active == "admin" else "secondary"
            if st.button("⚙️ Admin Panel", use_container_width=True, type=admin_type, key="nav_admin"):
                st.session_state.section = "admin"
                st.rerun()

        st.markdown("---")

        # ── Portfolio section ─────────────────────────────────────────────────
        st.markdown("**💼 Portfolio**")
        if st.button("💼 My Holdings", use_container_width=True, type=holdings_type, key="nav_holdings"):
            st.session_state.section = "my_holdings"
            st.rerun()
        _pkj_active = active == "paiso_ka_jungle"
        _pkj_b64 = _pkj_banner_b64()
        if _pkj_b64:
            _pkj_bdr = "2px solid #1f4e79" if _pkj_active else "1px solid #d0d2d6"
            st.markdown(
                f'<img src="data:image/png;base64,{_pkj_b64}" '
                f'style="width:100%;border-radius:0.5rem 0.5rem 0 0;'
                f'border:{_pkj_bdr};border-bottom:none;display:block;" />',
                unsafe_allow_html=True,
            )
        pkj_type = "primary" if _pkj_active else "secondary"
        if st.button("🌳 Paiso Ka Jungle", use_container_width=True, type=pkj_type, key="nav_pkj"):
            st.session_state.section = "paiso_ka_jungle"
            st.rerun()

        st.markdown("---")

        # ── Scan history ─────────────────────────────────────────────────────
        if st.session_state.section == "fundamentals":
            st.markdown("### 📂 My Scans")
            user_phone = user["phone_number"]
            sessions = db.get_scan_sessions(user_phone, limit=15, scan_type="fundamentals")
            if sessions:
                for s in sessions:
                    icon = "📁" if "." in s["scan_name"] else "📝"
                    label = f"{icon} {s['scan_name'][:22]} ({s['symbol_count']})"
                    ts = s["created_at"][5:16]
                    c_load, c_del = st.columns([5, 1])
                    with c_load:
                        if st.button(label, key=f"load_{s['id']}",
                                     help=f"{s['scan_name']} — {ts}",
                                     use_container_width=True):
                            _load_scan_session(s)
                            st.session_state.section = "fundamentals"
                            st.session_state.page = "dashboard"
                            st.rerun()
                    with c_del:
                        if st.button("🗑", key=f"del_{s['id']}", help="Delete"):
                            db.delete_scan_session(s["id"], user_phone)
                            st.rerun()
            else:
                st.caption("No saved scans yet.")
            st.markdown("---")

        elif st.session_state.section == "technicals":
            user_phone = user["phone_number"]

            st.markdown("### 📊 My DMA Scans")
            dma_sessions = db.get_scan_sessions(user_phone, limit=15, scan_type="dma")
            if dma_sessions:
                for s in dma_sessions:
                    icon = "📁" if "." in s["scan_name"] else "📝"
                    label = f"{icon} {s['scan_name'][:22]} ({s['symbol_count']})"
                    ts = s["created_at"][5:16]
                    c_load, c_del = st.columns([5, 1])
                    with c_load:
                        if st.button(label, key=f"dma_load_{s['id']}",
                                     help=f"{s['scan_name']} — {ts}",
                                     use_container_width=True):
                            _load_dma_session(s)
                            st.rerun()
                    with c_del:
                        if st.button("🗑", key=f"dma_del_{s['id']}", help="Delete"):
                            db.delete_scan_session(s["id"], user_phone)
                            st.rerun()
            else:
                st.caption("No DMA scans saved yet.")

            st.markdown("### 📈 My EMA Scans")
            ema_sessions = db.get_scan_sessions(user_phone, limit=15, scan_type="ema")
            if ema_sessions:
                for s in ema_sessions:
                    icon = "📁" if "." in s["scan_name"] else "📝"
                    label = f"{icon} {s['scan_name'][:22]} ({s['symbol_count']})"
                    ts = s["created_at"][5:16]
                    c_load, c_del = st.columns([5, 1])
                    with c_load:
                        if st.button(label, key=f"ema_load_{s['id']}",
                                     help=f"{s['scan_name']} — {ts}",
                                     use_container_width=True):
                            _load_ema_session(s)
                            st.rerun()
                    with c_del:
                        if st.button("🗑", key=f"ema_del_{s['id']}", help="Delete"):
                            db.delete_scan_session(s["id"], user_phone)
                            st.rerun()
            else:
                st.caption("No EMA scans saved yet.")
            st.markdown("### 🟢 My GURU BUY Scans")
            guru_sessions = db.get_scan_sessions(user_phone, limit=15, scan_type="guru_buy")
            if guru_sessions:
                for s in guru_sessions:
                    icon = "📁" if "." in s["scan_name"] else "📝"
                    label = f"{icon} {s['scan_name'][:22]} ({s['symbol_count']})"
                    ts = s["created_at"][5:16]
                    c_load, c_del = st.columns([5, 1])
                    with c_load:
                        if st.button(label, key=f"gb_load_{s['id']}",
                                     help=f"{s['scan_name']} — {ts}",
                                     use_container_width=True):
                            _load_guru_buy_session(s)
                            st.rerun()
                    with c_del:
                        if st.button("🗑", key=f"gb_del_{s['id']}", help="Delete"):
                            db.delete_scan_session(s["id"], user_phone)
                            st.rerun()
            else:
                st.caption("No GURU BUY scans saved yet.")

            st.markdown("### 💹 My SMC Zone Scans")

            # Alert badge — show if any MUST BUY stocks are in the current session
            alert_syms = st.session_state.get("smc_alert_stocks", [])
            if alert_syms:
                dark = st.session_state.get("dark_mode", False)
                badge_bg = "#052e16" if dark else "#dcfce7"
                badge_c  = "#16a34a"
                names = ", ".join(d.symbol for d in alert_syms)
                st.markdown(
                    f"<div style='background:{badge_bg};border:1px solid {badge_c};"
                    f"border-radius:8px;padding:0.5rem 0.8rem;margin-bottom:0.5rem;"
                    f"font-size:0.82rem;color:{badge_c};font-weight:700;'>"
                    f"🚨 MUST BUY ({len(alert_syms)}): {names}</div>",
                    unsafe_allow_html=True,
                )
            smc_sessions = db.get_scan_sessions(user_phone, limit=15, scan_type="smc")
            if smc_sessions:
                for s in smc_sessions:
                    icon = "📁" if "." in s["scan_name"] else "📝"
                    label = f"{icon} {s['scan_name'][:22]} ({s['symbol_count']})"
                    ts = s["created_at"][5:16]
                    c_load, c_del = st.columns([5, 1])
                    with c_load:
                        if st.button(label, key=f"smc_load_{s['id']}",
                                     help=f"{s['scan_name']} — {ts}",
                                     use_container_width=True):
                            _load_smc_session(s)
                            st.rerun()
                    with c_del:
                        if st.button("🗑", key=f"smc_del_{s['id']}", help="Delete"):
                            db.delete_scan_session(s["id"], user_phone)
                            st.rerun()
            else:
                st.caption("No SMC Zone scans saved yet.")
            st.markdown("---")

        elif st.session_state.section == "guru_smart_business":
            user_phone = user["phone_number"]
            st.markdown("### 🏪 My Daily Dukaan Scans")
            dd_sessions = db.get_scan_sessions(user_phone, limit=15, scan_type="daily_dukaan")
            if dd_sessions:
                for s in dd_sessions:
                    icon  = "📁" if "." in s["scan_name"] else "📝"
                    label = f"{icon} {s['scan_name'][:22]} ({s['symbol_count']})"
                    ts    = s["created_at"][5:16]
                    c_load, c_del = st.columns([5, 1])
                    with c_load:
                        if st.button(label, key=f"dd_load_{s['id']}",
                                     help=f"{s['scan_name']} — {ts}",
                                     use_container_width=True):
                            _load_dd_session(s)
                            st.rerun()
                    with c_del:
                        if st.button("🗑", key=f"dd_del_{s['id']}", help="Delete"):
                            db.delete_scan_session(s["id"], user_phone)
                            st.rerun()
            else:
                st.caption("No Daily Dukaan scans saved yet.")

            st.markdown("### 🌱 My Life Long ETF Scans")
            ll_sessions = db.get_scan_sessions(user_phone, limit=15, scan_type="ll_etf")
            if ll_sessions:
                for s in ll_sessions:
                    icon  = "📁" if "." in s["scan_name"] else "📝"
                    label = f"{icon} {s['scan_name'][:22]} ({s['symbol_count']})"
                    ts    = s["created_at"][5:16]
                    c_load, c_del = st.columns([5, 1])
                    with c_load:
                        if st.button(label, key=f"ll_load_{s['id']}",
                                     help=f"{s['scan_name']} — {ts}",
                                     use_container_width=True):
                            _load_ll_etf_session(s)
                            st.rerun()
                    with c_del:
                        if st.button("🗑", key=f"ll_del_{s['id']}", help="Delete"):
                            db.delete_scan_session(s["id"], user_phone)
                            st.rerun()
            else:
                st.caption("No Life Long ETF scans saved yet.")

            st.markdown("### 🌳 My Eshwarana Fruit Forest Scans")
            eff_sessions = db.get_scan_sessions(user_phone, limit=15, scan_type="eff")
            if eff_sessions:
                for s in eff_sessions:
                    icon  = "📁" if "." in s["scan_name"] else "📝"
                    label = f"{icon} {s['scan_name'][:22]} ({s['symbol_count']})"
                    ts    = s["created_at"][5:16]
                    c_load, c_del = st.columns([5, 1])
                    with c_load:
                        if st.button(label, key=f"eff_load_{s['id']}",
                                     help=f"{s['scan_name']} — {ts}",
                                     use_container_width=True):
                            _load_eff_session(s)
                            st.rerun()
                    with c_del:
                        if st.button("🗑", key=f"eff_del_{s['id']}", help="Delete"):
                            db.delete_scan_session(s["id"], user_phone)
                            st.rerun()
            else:
                st.caption("No Eshwarana Fruit Forest scans saved yet.")
            st.markdown("---")

        elif st.session_state.section == "rsi_screener":
            user_phone = user["phone_number"]
            st.markdown("### 📉 My Stock RSI Scans")
            rsi_s_sessions = db.get_scan_sessions(user_phone, limit=15, scan_type="rsi_stock")
            if rsi_s_sessions:
                for s in rsi_s_sessions:
                    icon  = "📁" if "." in s["scan_name"] else "📝"
                    label = f"{icon} {s['scan_name'][:22]} ({s['symbol_count']})"
                    ts    = s["created_at"][5:16]
                    c_load, c_del = st.columns([5, 1])
                    with c_load:
                        if st.button(label, key=f"rsi_s_load_{s['id']}",
                                     help=f"{s['scan_name']} — {ts}",
                                     use_container_width=True):
                            _load_rsi_session(s, "rsi_stock")
                            st.rerun()
                    with c_del:
                        if st.button("🗑", key=f"rsi_s_del_{s['id']}", help="Delete"):
                            db.delete_scan_session(s["id"], user_phone)
                            st.rerun()
            else:
                st.caption("No Stock RSI scans saved yet.")

            st.markdown("### 📉 My ETF RSI Scans")
            rsi_e_sessions = db.get_scan_sessions(user_phone, limit=15, scan_type="rsi_etf")
            if rsi_e_sessions:
                for s in rsi_e_sessions:
                    icon  = "📁" if "." in s["scan_name"] else "📝"
                    label = f"{icon} {s['scan_name'][:22]} ({s['symbol_count']})"
                    ts    = s["created_at"][5:16]
                    c_load, c_del = st.columns([5, 1])
                    with c_load:
                        if st.button(label, key=f"rsi_e_load_{s['id']}",
                                     help=f"{s['scan_name']} — {ts}",
                                     use_container_width=True):
                            _load_rsi_session(s, "rsi_etf")
                            st.rerun()
                    with c_del:
                        if st.button("🗑", key=f"rsi_e_del_{s['id']}", help="Delete"):
                            db.delete_scan_session(s["id"], user_phone)
                            st.rerun()
            else:
                st.caption("No ETF RSI scans saved yet.")
            st.markdown("---")

        if st.button("🚪 Logout", use_container_width=True):
            _tok = st.query_params.get("_s", "")
            if _tok:
                _revoke_session_token(_tok)
            for k in list(st.session_state.keys()):
                del st.session_state[k]
            st.query_params.clear()
            st.rerun()

        st.markdown("---")
        import os as _os
        _pledging_configured = bool(
            _os.environ.get("SCREENER_USERNAME") or _os.environ.get("SCREENER_EMAIL")
        )
        if not _pledging_configured:
            try:
                _cfg = st.secrets.get("screener", {})
                _pledging_configured = bool(_cfg.get("username") or _cfg.get("email"))
                # Also check top-level keys (user may enter without [screener] section)
                if not _pledging_configured:
                    _pledging_configured = bool(
                        st.secrets.get("username") or st.secrets.get("email") or st.secrets.get("SCREENER_USERNAME")
                    )
            except Exception:
                pass
        if _pledging_configured:
            st.caption("🔐 screener.in login active — full data + Pledged % enabled")
        else:
            st.warning(
                "⚠️ **screener.in not logged in.**  \n"
                "Add credentials as environment variables (`SCREENER_USERNAME`, `SCREENER_PASSWORD`), "
                "or in Streamlit Cloud Secrets:  \n"
                "```\n[screener]\nusername = \"your@email.com\"\n"
                "password = \"yourpassword\"\n```  \n"
                "Without login, screener.in may block the server and show None values.",
                icon=None,
            )


# ── Dashboard ─────────────────────────────────────────────────────────────────

def render_dashboard():
    st.markdown("## 📈 Fundamental Stock Scanner")

    # Auto-load the most recent saved scan when user returns
    _auto_restore_scan("stock_data", _load_scan_session, "fundamentals")

    tab_manual, tab_file = st.tabs(["📝 Enter Symbols", "📁 Upload File"])
    symbols: list = []

    with tab_manual:
        txt = st.text_area(
            "Stock symbols",
            placeholder="Single stock:  TCS\nMultiple:  RELIANCE, INFY, TCS\nOne per line also works",
            height=100,
            label_visibility="collapsed",
        )
        c1, c2 = st.columns([1, 5])
        with c1:
            scan_btn = st.button("🔍 Scan", type="primary", use_container_width=True, key="scan_m")
        with c2:
            st.caption("Use NSE symbols. Single stock → detailed card view. Multiple → table.")
        if scan_btn and txt.strip():
            st.session_state["_pending_scan_name"] = f"Manual — {_now_ist().strftime('%d %b %H:%M')}"
            st.session_state["_is_file_upload"] = False
            symbols = _parser.parse_text_input(txt)

    with tab_file:
        up = st.file_uploader("Upload CSV / Excel / TXT", type=["csv", "xlsx", "xls", "txt"],
                               label_visibility="collapsed")
        c1, c2, c3 = st.columns([1, 1, 4])
        with c1:
            scan_f = st.button("🔍 Scan File", type="primary", use_container_width=True, key="scan_f")
        with c2:
            sample = pd.DataFrame({"symbol": ["RELIANCE", "INFY", "TCS", "HDFCBANK", "WIPRO"]})
            st.download_button("📥 Sample", sample.to_csv(index=False).encode(),
                               "sample_stocks.csv", "text/csv")
        with c3:
            st.caption("CSV/Excel: 'symbol' column or first column. TXT: one per line.")
        if scan_f and up:
            st.session_state["_pending_scan_name"] = up.name
            st.session_state["_is_file_upload"] = True   # mark for history save
            symbols = _parser.parse_file(up)
            if not symbols:
                st.warning("⚠️ No valid symbols found in file.")

    # Fetch new symbols
    if symbols:
        scan_name = st.session_state.pop("_pending_scan_name", "Manual Scan")
        is_file = st.session_state.pop("_is_file_upload", False)
        st.markdown(
            f"**Scanning {len(symbols)} symbol(s):** "
            f"{', '.join(symbols[:15])}{'…' if len(symbols) > 15 else ''}"
        )
        # Only save file uploads to history; single/manual scans are not stored
        _do_fetch(symbols, scan_name, save_to_history=True)

    # Handle refresh request (triggered from results view)
    if st.session_state._do_refresh:
        st.session_state._do_refresh = False
        syms = st.session_state.last_scan_symbols
        if not syms and st.session_state.stock_data:
            syms = [s.symbol for s in st.session_state.stock_data]
        if syms:
            scan_name = st.session_state.get("active_scan_name") or "Refresh"
            st.markdown(f"**Refreshing {len(syms)} symbol(s)…**")
            # Refresh bypasses cache and saves the fresh data back to the database
            _do_fetch(syms, scan_name, save_to_history=True, force_refresh=True)

    # Render results
    data = st.session_state.stock_data
    if not data:
        return

    # Warn if loaded from saved scan (PEG/EV/EBITDA missing) — prompt a Refresh
    if len(data) > 1:
        missing_peg = sum(1 for s in data if s.peg_ratio is None and not s.error)
        if missing_peg > len(data) // 2:
            st.info(
                "ℹ️ This scan was saved before PEG & EV/EBITDA data was available. "
                "Click **🔄 Refresh** below to reload fresh data with these columns."
            )

    if len(data) == 1:
        _render_single_card(data[0])
    else:
        _render_table(data)


# ── Single-stock card view ────────────────────────────────────────────────────

def _render_single_card(s: StockData):
    if s.error:
        st.error(f"❌ Could not fetch data for **{s.symbol}**: {s.error}")
        return

    dark = st.session_state.dark_mode
    card_bg = "#161b22" if dark else "#ffffff"
    border_c = "#30363d" if dark else "#dde3ec"
    label_c = "#8b949e" if dark else "#6b7280"
    val_c = "#c9d1d9" if dark else "#111827"
    name_c = "#58a6ff" if dark else "#1f4e79"

    pct = s.price_change_pct
    pct_color = _chg_color(pct)
    pct_arrow = _chg_arrow(pct)
    pct_str = f"{pct_arrow} {abs(pct):.2f}%" if pct is not None else "—"

    # Build company abbreviation for the logo placeholder
    abbr = "".join(w[0] for w in (s.nse_code or s.symbol).split()[:3]).upper()[:3]

    # Header links
    links_html = ""
    if s.bse_code:
        links_html += f'<span style="margin-right:16px;color:#2563eb;font-size:0.85rem">🔗 BSE: {s.bse_code}</span>'
    nse_code = s.nse_code or s.symbol
    links_html += f'<span style="margin-right:16px;color:#2563eb;font-size:0.85rem">🔗 NSE: {nse_code}</span>'

    # Header card
    st.markdown(f"""
    <div style="background:{card_bg};border:1px solid {border_c};border-radius:16px;
                padding:1.5rem 2rem;margin-bottom:1rem;">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:1rem;">
        <div style="display:flex;align-items:center;gap:1rem;">
          <div style="width:56px;height:56px;border-radius:12px;background:#1f4e79;
                      display:flex;align-items:center;justify-content:center;
                      color:white;font-weight:700;font-size:1rem;flex-shrink:0;">
            {abbr}
          </div>
          <div>
            <div style="font-size:1.6rem;font-weight:700;color:{name_c};line-height:1.2;">
              {s.company_name}
            </div>
            <div style="margin-top:4px">{links_html}</div>
          </div>
        </div>
        <div style="text-align:right;">
          <div style="font-size:1.8rem;font-weight:700;color:{val_c};">
            ₹ {s.current_price:,.0f}
          </div>
          <div style="font-size:1rem;font-weight:600;color:{pct_color};">{pct_str}</div>
          <div style="font-size:0.8rem;color:{label_c};margin-top:2px;">
            {s.data_as_of or "—"}
          </div>
        </div>
      </div>
    </div>
    """, unsafe_allow_html=True)

    # Metric grid helper
    def metric_cell(label, value, unit=""):
        v_str = f"{value}{unit}" if value is not None else "—"
        return f"""
        <div style="background:{card_bg};border:1px solid {border_c};border-radius:10px;
                    padding:0.85rem 1rem;">
          <div style="font-size:0.78rem;color:{label_c};margin-bottom:4px;">{label}</div>
          <div style="font-size:1.05rem;font-weight:600;color:{val_c};">{v_str}</div>
        </div>"""

    def row_html(*cells):
        inner = "".join(cells)
        return f'<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:0.6rem;margin-bottom:0.6rem;">{inner}</div>'

    high_low = (
        f"₹ {s.high_52w:,.0f} / {s.low_52w:,.0f}"
        if s.high_52w and s.low_52w else "—"
    )
    pb_str = f"{s.pb_ratio:.2f}" if s.pb_ratio else "—"
    pe_str = f"{s.stock_pe:.1f}" if s.stock_pe else "—"
    bv_str = f"₹ {s.book_value:.0f}" if s.book_value else "—"
    dy_str = f"{s.dividend_yield:.2f} %" if s.dividend_yield else "—"
    roce_str = f"{s.roce:.1f} %" if s.roce else "—"
    roe_str = f"{s.roe:.1f} %" if s.roe else "—"
    fv_str = f"₹ {s.face_value:.2f}" if s.face_value else "—"
    nsps_str = f"₹ {s.nsps:.0f}" if s.nsps else "—"
    pnsps_str = f"{s.pnsps:.2f}" if s.pnsps else "—"
    pts_str = f"{s.price_to_sales:.2f}" if s.price_to_sales else "—"
    ptb_str = f"{s.pb_ratio:.2f}" if s.pb_ratio else "—"
    ronw_str = f"{s.ronw:.1f} %" if s.ronw else "—"
    pronw_str = f"{s.pronw:.1f}" if s.pronw else "—"
    ev_str = f"{s.ev_ebitda:.1f}" if s.ev_ebitda else "—"
    mpe_str = f"{s.median_pe_5y:.1f}" if s.median_pe_5y else "—"
    peg_str = f"{s.peg_ratio:.2f}" if s.peg_ratio else "—"
    debt_str = f"₹ {s.debt:,.0f} Cr" if s.debt else "—"

    grid = ""
    grid += row_html(
        metric_cell("Market Cap", s.market_cap or "—"),
        metric_cell("Current Price", f"₹ {s.current_price:,.0f}" if s.current_price else "—"),
        metric_cell("High / Low", high_low),
    )
    grid += row_html(
        metric_cell("Stock P/E", pe_str),
        metric_cell("Book Value", bv_str),
        metric_cell("Dividend Yield", dy_str),
    )
    grid += row_html(
        metric_cell("ROCE", roce_str),
        metric_cell("ROE", roe_str),
        metric_cell("Face Value", fv_str),
    )
    grid += row_html(
        metric_cell("NSPS", nsps_str),
        metric_cell("PNSPS", pnsps_str),
        metric_cell("Price to Sales", pts_str),
    )
    grid += row_html(
        metric_cell("Price to Earning", pe_str),
        metric_cell("Price to Book Value", ptb_str),
        metric_cell("Return on Equity", roe_str),
    )
    grid += row_html(
        metric_cell("EV / EBITDA", ev_str),
        metric_cell("RONW", ronw_str),
        metric_cell("PRONW", pronw_str),
    )
    grid += row_html(
        metric_cell("Median P/E (5Y)", mpe_str),
        metric_cell("PEG Ratio", peg_str),
        metric_cell("Total Debt", debt_str),
    )

    st.markdown(grid, unsafe_allow_html=True)

    # Source & timestamp footer
    ts_str = _fmt_fetched_at()
    fetched_label = f"App fetched: {ts_str}" if ts_str else "App fetched: —"
    st.markdown(
        f"<p style='color:{label_c};font-size:0.78rem;margin-top:0.5rem;'>"
        f"Data source: {s.data_source} &nbsp;·&nbsp; "
        f"screener.in updated: {s.data_as_of or '—'} &nbsp;·&nbsp; "
        f"🕐 {fetched_label}"
        f"</p>",
        unsafe_allow_html=True,
    )

    st.markdown("---")

    # ── Refresh + Navigation buttons ──────────────────────────────────────────
    nse_sym   = s.nse_code or s.symbol
    nse_url   = f"https://www.nseindia.com/get-quotes/equity?symbol={nse_sym}"
    tv_url    = f"https://in.tradingview.com/chart/?symbol=NSE%3A{nse_sym}&interval=D"
    sc_url    = f"https://www.screener.in/company/{s.symbol}/"

    st.markdown("**🔗 Open in:**")
    nc1, nc2, nc3, nc4, nc5 = st.columns([2, 2, 2, 2, 2])
    with nc1:
        st.link_button("📈 NSE Chart", nse_url, use_container_width=True, type="primary")
    with nc2:
        st.link_button("📊 TradingView", tv_url, use_container_width=True)
    with nc3:
        st.link_button("🔍 Screener.in", sc_url, use_container_width=True)
    with nc4:
        if st.button("📋 Table View", use_container_width=True):
            _render_table(st.session_state.stock_data)
    with nc5:
        if st.button("🔄 Refresh", use_container_width=True, type="primary",
                     help="Clear cache and re-fetch latest data from screener.in"):
            cache_manager.invalidate(s.symbol)
            st.session_state._do_refresh = True
            st.rerun()


# ── Filter + Sort toolbar (reusable across all tables) ───────────────────────

def _df_filter_sort(df: pd.DataFrame, key: str) -> pd.DataFrame:
    """Renders a collapsible filter+sort toolbar above a table, returns the filtered/sorted df."""
    with st.expander("🔍 Filter & Sort", expanded=False):
        c1, c2, c3 = st.columns([3, 2, 1])
        with c1:
            search = st.text_input(
                "Search (Symbol / Company)",
                placeholder="e.g. HDFC or Bank...",
                key=f"_flt_search_{key}",
                label_visibility="collapsed",
            )
        with c2:
            numeric_cols = [c for c in df.columns
                            if pd.api.types.is_numeric_dtype(df[c]) and df[c].notna().any()]
            sort_by = st.multiselect(
                "Sort columns",
                options=numeric_cols,
                key=f"_flt_sort_{key}",
                placeholder="Pick column(s) to sort by…",
                label_visibility="collapsed",
            )
        with c3:
            sort_asc = st.checkbox("Asc ↑", value=False, key=f"_flt_asc_{key}")

        # Numeric range filters — up to 8 populated numeric columns
        range_candidates = [c for c in numeric_cols if df[c].notna().sum() > 1][:8]
        active_ranges: list = []
        if range_candidates:
            st.markdown("**Range filters** (leave at extremes to include all):")
            rcols = st.columns(min(4, len(range_candidates)))
            for i, col in enumerate(range_candidates):
                col_data = df[col].dropna()
                if col_data.empty:
                    continue
                mn, mx = float(col_data.min()), float(col_data.max())
                if mn >= mx:
                    continue
                with rcols[i % 4]:
                    rng = st.slider(
                        col, mn, mx, (mn, mx),
                        key=f"_flt_rng_{key}_{col}",
                        format="%.1f",
                    )
                    if rng != (mn, mx):
                        active_ranges.append((col, rng[0], rng[1]))

        # Apply text search
        if search:
            mask = pd.Series(False, index=df.index)
            for col in ("Symbol", "Company", "symbol", "company", "Company Name"):
                if col in df.columns:
                    mask |= df[col].astype(str).str.contains(search, case=False, na=False)
            df = df[mask]

        # Apply range filters
        for col, lo, hi in active_ranges:
            df = df[(df[col].isna()) | ((df[col] >= lo) & (df[col] <= hi))]

        # Apply sort
        if sort_by:
            valid = [c for c in sort_by if c in df.columns]
            if valid:
                df = df.sort_values(valid, ascending=sort_asc)

    return df.reset_index(drop=True)


# ── Multi-stock table view ────────────────────────────────────────────────────

def _render_table(stock_list: list):
    rows = []
    for s in stock_list:
        chg = f"{_chg_arrow(s.price_change_pct)} {abs(s.price_change_pct):.2f}%" if s.price_change_pct is not None else "—"
        rows.append({
            "Symbol":               s.symbol,
            "Company":              s.company_name,
            "Current Price (₹)":    s.current_price,
            "Change %":             chg,
            "As of":                s.data_as_of or "—",
            "Market Cap":           s.market_cap or "—",
            "Book Value (₹)":       s.book_value,
            "ROCE %":               s.roce,
            "ROE %":                s.roe,
            "RONW %":               s.ronw,
            "Stock P/E":            s.stock_pe,
            "P/B":                  s.pb_ratio,
            "NSPS (₹)":             s.nsps,
            "PNSPS":                s.pnsps,
            "P/S":                  s.price_to_sales,
            "EV/EBITDA":            s.ev_ebitda,
            "PEG":                  s.peg_ratio,
            "PRONW":                s.pronw,
            "Div Yield %":      s.dividend_yield,
            "Face Value (₹)":   s.face_value,
            # ── 8 New columns ──
            "Pledged %":            getattr(s, "promoter_pledging", None),
            "EPS (₹)":              s.eps,
            "Enterprise Value (Cr)": getattr(s, "enterprise_value", None),
            "P/E":                  s.stock_pe,
            "P/ROE":                getattr(s, "p_roe", None),
            "LTVI":                 getattr(s, "ltvi", None),
            "Debt/Equity":          getattr(s, "debt_equity", None),
            "Oper. Profit (Cr)":    getattr(s, "operating_profit", None),
            # ── 4 Additional columns ──
            "Sales (Cr)":           getattr(s, "sales_cr", None),
            "Shares (Cr)":          getattr(s, "num_shares_cr", None),
            "Marking":              getattr(s, "marking", None),
            "Qtr Profit Var %":     getattr(s, "qtr_profit_var", None),
        })
        if s.error:
            rows[-1]["Error"] = s.error

    df = pd.DataFrame(rows)
    df = _df_filter_sort(df, "fundamentals")

    st.markdown("---")

    # Results header: title + fetched timestamp + refresh button
    h1, h2, h3 = st.columns([4, 3, 1])
    with h1:
        st.markdown(f"### 📊 Results — {len(df)} stock(s)")
    with h2:
        ts_str = _fmt_fetched_at()
        if ts_str:
            st.markdown(
                f"<p style='margin-top:0.6rem;color:gray;font-size:0.85rem;'>🕐 Fetched: {ts_str}</p>",
                unsafe_allow_html=True,
            )
    with h3:
        if st.button("🔄 Refresh", key="refresh_table", use_container_width=True, type="primary",
                     help="Clear cache and re-fetch latest data from screener.in"):
            for s in stock_list:
                cache_manager.invalidate(s.symbol)
            st.session_state._do_refresh = True
            st.rerun()

    # Summary row
    pe_ok   = df["Stock P/E"].dropna()
    roce_ok = df["ROCE %"].dropna()
    retrieved = df["Current Price (₹)"].notna().sum()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Stocks Scanned", len(df))
    c2.metric("Data Retrieved", int(retrieved))
    c3.metric("Avg P/E", f"{pe_ok.mean():.1f}" if not pe_ok.empty else "—")
    c4.metric("Avg ROCE %", f"{roce_ok.mean():.1f}" if not roce_ok.empty else "—")

    st.markdown("---")

    # Export buttons
    ec1, ec2, *_ = st.columns([1, 1, 4])
    with ec1:
        st.download_button(
            "📥 CSV", df.to_csv(index=False).encode(),
            f"guru_scan_{_now_ist().strftime('%Y%m%d_%H%M')}.csv", "text/csv",
        )
    with ec2:
        try:
            import io
            buf = io.BytesIO()
            df.to_excel(buf, index=False, engine="openpyxl")
            st.download_button("📥 Excel", buf.getvalue(),
                               f"guru_scan_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx")
        except Exception:
            pass

    evt = st.dataframe(
        df, width='stretch', hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        column_config={
            "Symbol":               st.column_config.TextColumn("Symbol", width="small"),
            "Company":              st.column_config.TextColumn("Company", width="medium"),
            "Current Price (₹)":    st.column_config.NumberColumn("Current Price (₹)", format="₹%.2f"),
            "Change %":             st.column_config.TextColumn("Change %", width="small"),
            "As of":                st.column_config.TextColumn("As of", width="small"),
            "Market Cap":           st.column_config.TextColumn("Market Cap"),
            "Book Value (₹)":       st.column_config.NumberColumn("Book Value", format="₹%.2f"),
            "ROCE %":               st.column_config.NumberColumn("ROCE %", format="%.2f"),
            "ROE %":                st.column_config.NumberColumn("ROE %", format="%.2f"),
            "RONW %":               st.column_config.NumberColumn("RONW %", format="%.2f"),
            "Stock P/E":            st.column_config.NumberColumn("Stock P/E", format="%.2f"),
            "P/B":                  st.column_config.NumberColumn("P/B", format="%.2f"),
            "NSPS (₹)":             st.column_config.NumberColumn("NSPS (₹)", format="₹%.2f"),
            "PNSPS":                st.column_config.NumberColumn("PNSPS", format="%.2f"),
            "P/S":                  st.column_config.NumberColumn("P/S", format="%.2f"),
            "PEG":                  st.column_config.NumberColumn("PEG", format="%.2f"),
            "EV/EBITDA":            st.column_config.NumberColumn("EV/EBITDA", format="%.2f"),
            "PRONW":                st.column_config.NumberColumn("PRONW", format="%.2f"),
            "Div Yield %":          st.column_config.NumberColumn("Div Yield %", format="%.2f"),
            "Face Value (₹)":       st.column_config.NumberColumn("Face Value (₹)", format="₹%.2f"),
            # ── 8 New columns ──
            "Pledged %":            st.column_config.NumberColumn("Pledged %", format="%.2f",
                                        help="% of promoter shares pledged (data not publicly available from screener.in without login — shows N/A)"),
            "EPS (₹)":              st.column_config.NumberColumn("EPS (₹)", format="₹%.2f"),
            "Enterprise Value (Cr)": st.column_config.NumberColumn("Enterprise Value (Cr)", format="%.0f"),
            "P/E":                  st.column_config.NumberColumn("P/E", format="%.2f"),
            "P/ROE":                st.column_config.NumberColumn("P/ROE",  format="%.2f",
                                        help="Current Price ÷ ROE% — lower = better value"),
            "LTVI":                 st.column_config.NumberColumn("LTVI",   format="%.2f",
                                        help="Long-Term Value Index = P/B + P/EPS + P/ROE + P/NSPS; lower = cheaper"),
            "Debt/Equity":          st.column_config.NumberColumn("Debt/Equity", format="%.2f"),
            "Oper. Profit (Cr)":    st.column_config.NumberColumn("Oper. Profit (Cr)", format="%.0f"),
            # ── 4 Additional columns ──
            "Sales (Cr)":           st.column_config.NumberColumn("Sales (Cr)", format="%.0f",
                                        help="Annual/TTM Revenue in Crores"),
            "Shares (Cr)":          st.column_config.NumberColumn("Shares (Cr)", format="%.2f",
                                        help="Number of equity shares outstanding in Crores"),
            "Marking":              st.column_config.NumberColumn("Marking", format="%.2f",
                                        help="P/E + P/B + Price/Sales + EV/EBITDA — composite overvaluation indicator; lower = cheaper"),
            "Qtr Profit Var %":     st.column_config.NumberColumn("Qtr Profit Var %", format="%.2f",
                                        help="Latest quarter Net Profit YoY growth % (same quarter vs 1 year ago)"),
        },
    )
    st.caption("💡 Drag column headers to reorder. Click headers to sort. Use 🔍 Filter & Sort above for multi-column filtering.")

    # Row selection → navigation buttons
    selected_rows = evt.selection.rows if hasattr(evt, "selection") else []
    selected_sym = None
    if selected_rows:
        idx = selected_rows[0]
        selected_sym = df.iloc[idx]["Symbol"]
        st.info(f"Selected: **{df.iloc[idx]['Company']}** ({selected_sym})")

    col_btn1, col_btn2, col_btn3, col_clr = st.columns([2, 2, 2, 2])
    with col_clr:
        if st.button("🗑️ Clear Results", key="clr_results"):
            st.session_state.stock_data = []
            st.rerun()
    if selected_sym:
        nse_code_sel = selected_sym
        for _s in stock_list:
            if _s.symbol == selected_sym:
                nse_code_sel = _s.nse_code or _s.symbol
                break
        with col_btn1:
            st.link_button(
                "📈 NSE Chart",
                f"https://www.nseindia.com/get-quotes/equity?symbol={nse_code_sel}",
                width='stretch', type="primary",
            )
        with col_btn2:
            st.link_button(
                "📊 TradingView",
                f"https://in.tradingview.com/chart/?symbol=NSE%3A{nse_code_sel}&interval=D",
                width='stretch',
            )
        with col_btn3:
            st.link_button(
                "🔍 Screener.in",
                f"https://www.screener.in/company/{selected_sym}/",
                width='stretch',
            )
    else:
        with col_btn1:
            st.button("📈 NSE Chart", disabled=True, width='stretch', key="nse_dis")
        with col_btn2:
            st.button("📊 TradingView", disabled=True, width='stretch', key="tv_dis")
        with col_btn3:
            st.button("🔍 Screener.in", disabled=True, width='stretch', key="scr_dis")
        st.caption("⬆️ Click a row in the table above to enable navigation buttons")


# ── ETF Analysis ──────────────────────────────────────────────────────────────

def _render_etf_live_table(etf_list: list) -> list:
    """
    Render the NSE live ETF market data table.
    Columns match the NSE ETF page: Symbol, Underlying Asset, Open, High, Low,
    Prev Close, LTP, Indicative Close, Change (₹), % Change, NAV, 52W High, 52W Low,
    Volume, Value (Cr), 30D %, 1Y %.
    Returns list of symbols currently selected (for 'Run EMA on these' feature).
    """
    rows = []
    for d in etf_list:
        rows.append({
            "Symbol":           d.symbol,
            "Underlying Asset": d.assets or "—",
            "Series":           d.series or "EQ",
            "Open (₹)":         d.open,
            "High (₹)":         d.high,
            "Low (₹)":          d.low,
            "Prev Close (₹)":   d.prev_close,
            "LTP (₹)":          d.ltp,
            "Ind. Close (₹)":   d.indicative_close if d.indicative_close and d.indicative_close != 0 else None,
            # Two separate signed columns — positive shows +, negative shows −
            "Change (₹)":       d.change,
            "% Change":         d.pct_change,
            "NAV (₹)":          d.nav,
            "52W High (₹)":     d.week52_high,
            "52W Low (₹)":      d.week52_low,
            "Volume":           d.volume,
            "Value (Cr)":       d.traded_value_cr,
            "30D %":            d.pct_change_30d,
            "1Y %":             d.pct_change_365d,
        })

    df = pd.DataFrame(rows)
    df = _df_filter_sort(df, "etf_live")
    num_col = st.column_config.NumberColumn
    txt_col = st.column_config.TextColumn
    evt = st.dataframe(
        df, width="stretch", hide_index=True,
        on_select="rerun", selection_mode="multi-row",
        key="etf_live_df",
        column_config={
            "Symbol":           txt_col("Symbol",           width="small"),
            "Underlying Asset": txt_col("Underlying Asset", width="large"),
            "Series":           txt_col("Series",           width="small"),
            "Open (₹)":         num_col("Open (₹)",         format="₹%.2f"),
            "High (₹)":         num_col("High (₹)",         format="₹%.2f"),
            "Low (₹)":          num_col("Low (₹)",          format="₹%.2f"),
            "Prev Close (₹)":   num_col("Prev Close (₹)",   format="₹%.2f"),
            "LTP (₹)":          num_col("LTP (₹)",          format="₹%.2f"),
            "Ind. Close (₹)":   num_col("Ind. Close (₹)",   format="₹%.2f"),
            "Change (₹)":       num_col("Change (₹)",       format="%+.2f",  width="small"),
            "% Change":         num_col("% Change",         format="%+.2f%%", width="small"),
            "NAV (₹)":          num_col("NAV (₹)",          format="₹%.4f"),
            "52W High (₹)":     num_col("52W High (₹)",     format="₹%.2f"),
            "52W Low (₹)":      num_col("52W Low (₹)",      format="₹%.2f"),
            "Volume":           num_col("Volume",           format="%d"),
            "Value (Cr)":       num_col("Value (Cr)",       format="₹%.2f Cr"),
            "30D %":            num_col("30D %",            format="%+.2f%%"),
            "1Y %":             num_col("1Y %",             format="%+.2f%%"),
        },
    )
    selected_rows = evt.selection.rows if hasattr(evt, "selection") else []
    return [df.iloc[i]["Symbol"] for i in selected_rows]


def _render_etf_ema_table(ema_list: list) -> None:
    """Render EMA analysis table for ETFs (same layout as the EMA tab)."""
    rows = []
    for d in ema_list:
        total = sum(1 for x in [d.ema1, d.ema2, d.ema3, d.ema4, d.ema5, d.ema6, d.ema7,
                                  d.ema10, d.ema20, d.ema22, d.ema50, d.ema100, d.ema200]
                    if x is not None)
        cnt    = d.above_ema_count or 0
        arrow  = "↑" if cnt > total / 2 else "↓"
        signal = f"{arrow} {cnt}/{total}" if total > 0 else "—"
        row = {
            "Symbol":     d.symbol,
            "Scheme Name": d.company_name or d.symbol,
            "Ref Date":   d.ref_date or "—",
            "Price (₹)":  d.current_price,
            "EMA 1":      d.ema1,  "EMA 2":   d.ema2,  "EMA 3":   d.ema3,
            "EMA 4":      d.ema4,  "EMA 5":   d.ema5,  "EMA 6":   d.ema6,
            "EMA 7":      d.ema7,  "EMA 10":  d.ema10, "EMA 20":  d.ema20,
            "EMA 22":     d.ema22, "EMA 50":  d.ema50, "EMA 100": d.ema100,
            "EMA 200":    d.ema200,
            "Above EMAs": signal,
        }
        if d.error:
            row["Error"] = d.error
        rows.append(row)

    df = pd.DataFrame(rows)
    st.markdown("---")
    h1, h2, h3, h4 = st.columns([3, 2, 1, 1])
    with h1:
        st.markdown(f"### 📈 ETF EMA Results — {len(df)} ETF(s)")
    with h2:
        ts = st.session_state.get("etf_ema_last_fetched_at")
        if ts:
            hour = ts.hour % 12 or 12
            ampm = "a.m." if ts.hour < 12 else "p.m."
            st.markdown(
                f"<p style='margin-top:0.6rem;color:gray;font-size:0.85rem;'>"
                f"🕐 Fetched: {ts.day} {ts.strftime('%b %Y')}, "
                f"{hour}:{ts.strftime('%M')} {ampm}</p>",
                unsafe_allow_html=True,
            )
    with h3:
        if st.button("🔄 Refresh", key="etf_ema_refresh", width='stretch', type="primary"):
            st.session_state.etf_ema_do_refresh = True
            st.rerun()
    with h4:
        _live_btn("etf_ema", "etf_ema_do_refresh", "etf_ema_live_table")

    retrieved  = df["Price (₹)"].notna().sum()
    _all_ref_dates = [d.ref_date for d in ema_list if d.ref_date and not d.error]
    latest_ref = max(_all_ref_dates) if _all_ref_dates else "—"
    c1, c2, c3 = st.columns(3)
    c1.metric("ETFs Scanned",    len(df))
    c2.metric("Data Retrieved",  int(retrieved))
    c3.metric("Trading Day",     latest_ref, help=f"Trading day used for analysis: {latest_ref}")
    st.markdown("---")
    ec1, ec2, *_ = st.columns([1, 1, 4])
    with ec1:
        st.download_button(
            "📥 CSV", df.to_csv(index=False).encode(),
            f"etf_ema_{_now_ist().strftime('%Y%m%d_%H%M')}.csv", "text/csv",
            key="etf_ema_csv_dl",
        )
    with ec2:
        try:
            import io
            buf = io.BytesIO()
            df.to_excel(buf, index=False, engine="openpyxl")
            st.download_button("📥 Excel", buf.getvalue(),
                               f"etf_ema_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx",
                               key="etf_ema_xls_dl")
        except Exception:
            pass

    num_col = st.column_config.NumberColumn
    txt_col = st.column_config.TextColumn
    df = _df_filter_sort(df, "etf_ema")
    evt = st.dataframe(
        df, width="stretch", hide_index=True,
        on_select="rerun", selection_mode="single-row",
        key="etf_ema_df",
        column_config={
            "Symbol":      txt_col("Symbol",      width="small"),
            "Scheme Name": txt_col("Scheme Name", width="large"),
            "Ref Date":    txt_col("Ref Date",    width="small"),
            "Price (₹)":   num_col("Price (₹)",   format="₹%.2f"),
            "EMA 1":       num_col("EMA 1",   format="₹%.2f"),
            "EMA 2":       num_col("EMA 2",   format="₹%.2f"),
            "EMA 3":       num_col("EMA 3",   format="₹%.2f"),
            "EMA 4":       num_col("EMA 4",   format="₹%.2f"),
            "EMA 5":       num_col("EMA 5",   format="₹%.2f"),
            "EMA 6":       num_col("EMA 6",   format="₹%.2f"),
            "EMA 7":       num_col("EMA 7",   format="₹%.2f"),
            "EMA 10":      num_col("EMA 10",  format="₹%.2f"),
            "EMA 20":      num_col("EMA 20",  format="₹%.2f"),
            "EMA 22":      num_col("EMA 22",  format="₹%.2f"),
            "EMA 50":      num_col("EMA 50",  format="₹%.2f"),
            "EMA 100":     num_col("EMA 100", format="₹%.2f"),
            "EMA 200":     num_col("EMA 200", format="₹%.2f"),
            "Above EMAs":  txt_col("Above EMAs", width="small"),
        },
    )
    selected_rows = evt.selection.rows if hasattr(evt, "selection") else []
    if selected_rows:
        idx = selected_rows[0]
        st.info(f"Selected: **{df.iloc[idx]['Scheme Name']}** ({df.iloc[idx]['Symbol']})")

    col1, col2, col3, col4 = st.columns([2, 2, 2, 2])
    with col4:
        if st.button("🗑️ Clear EMA Results", key="etf_ema_clr"):
            st.session_state.etf_ema_data = []
            st.rerun()
    sym = df.iloc[selected_rows[0]]["Symbol"] if selected_rows else None
    if sym:
        with col1:
            st.link_button("📈 NSE Chart",
                           f"https://www.nseindia.com/get-quotes/equity?symbol={sym}",
                           width='stretch', type="primary")
        with col2:
            st.link_button("📊 TradingView",
                           f"https://in.tradingview.com/chart/?symbol=NSE%3A{sym}&interval=D",
                           width='stretch')
        with col3:
            st.link_button("🔍 Screener.in",
                           f"https://www.screener.in/company/{sym}/",
                           width='stretch')
    else:
        with col1:
            st.button("📈 NSE Chart",    disabled=True, width='stretch', key="etf_nse_dis")
        with col2:
            st.button("📊 TradingView", disabled=True, width='stretch', key="etf_tv_dis")
        with col3:
            st.button("🔍 Screener.in", disabled=True, width='stretch', key="etf_sc_dis")
        st.caption("⬆️ Click a row in the EMA table above to enable navigation buttons")


def _render_etf_analysis():
    from datetime import date as _date

    st.markdown(
        "<p style='color:gray;font-size:0.9rem;margin-bottom:0.5rem;'>"
        "Live ETF data from NSE India · EMA analysis via Yahoo Finance (NSE)</p>",
        unsafe_allow_html=True,
    )

    # ── Section 1 : NSE ETF Live Market Data ─────────────────────────────────
    with st.expander("📡 NSE ETF Live Market Data", expanded=True):
        live_data = st.session_state.get("etf_live_data", [])
        c_btn, c_info = st.columns([2, 5])
        with c_btn:
            if st.button("🔄 Fetch / Refresh NSE ETF Data", type="primary",
                         width='stretch', key="etf_live_fetch_btn"):
                _do_etf_live_fetch()
                st.rerun()
        with c_info:
            ts  = st.session_state.get("etf_live_fetched_at")
            _smr_raw = st.session_state.get("etf_live_summary")
            # Guard against stale session state that may hold a plain string
            smr = _smr_raw if hasattr(_smr_raw, "nse_timestamp") else None
            if ts:
                hour = ts.hour % 12 or 12
                ampm = "a.m." if ts.hour < 12 else "p.m."
                info_str = (
                    f"🕐 Fetched at {ts.day} {ts.strftime('%b %Y')}, "
                    f"{hour}:{ts.strftime('%M')} {ampm}"
                )
                if smr and smr.nse_timestamp:
                    info_str += f"  ·  NSE market time: {smr.nse_timestamp}"
                st.markdown(
                    f"<p style='color:gray;font-size:0.82rem;margin-top:0.5rem;'>{info_str}</p>",
                    unsafe_allow_html=True,
                )

        if live_data:
            valid = [d for d in live_data if not d.error]
            _smr_raw2 = st.session_state.get("etf_live_summary")
            smr = _smr_raw2 if hasattr(_smr_raw2, "nse_timestamp") else None
            # Advances / Declines / Unchanged summary bar
            if smr:
                mc1, mc2, mc3, mc4, mc5 = st.columns(5)
                mc1.metric("Total ETFs",    len(valid))
                mc2.metric("▲ Advances",    smr.advances)
                mc3.metric("▼ Declines",    smr.declines)
                mc4.metric("— Unchanged",   smr.unchanged)
                if smr.total_traded_value_cr:
                    mc5.metric("Total Value", f"₹{smr.total_traded_value_cr:,.0f} Cr")
            else:
                st.markdown(f"**{len(valid)} ETF(s)** listed on NSE")
            selected_syms = _render_etf_live_table(live_data)

            # Shortcut: run EMA on selection from the live table
            if selected_syms:
                sc1, sc2 = st.columns([2, 5])
                with sc1:
                    if st.button(
                        f"📈 Run EMA on {len(selected_syms)} selected ETF(s)",
                        type="primary", width='stretch', key="etf_run_sel_ema",
                    ):
                        scheme_map = {d.symbol: d.assets for d in live_data}
                        today = _date.today()
                        ref = st.session_state.get("etf_ema_date_picker") or today
                        _do_etf_ema_fetch(
                            selected_syms,
                            f"ETF EMA — {_now_ist().strftime('%d %b %H:%M')}",
                            ref_date=ref, scheme_map=scheme_map,
                        )
                        st.rerun()
                with sc2:
                    st.caption(f"Selected: {', '.join(selected_syms[:10])}{'…' if len(selected_syms) > 10 else ''}")

            if st.button("📈 Run EMA on ALL NSE ETFs", key="etf_run_all_ema",
                         use_container_width=False):
                scheme_map = {d.symbol: d.assets for d in live_data if not d.error}
                today = _date.today()
                ref = st.session_state.get("etf_ema_date_picker") or today
                _do_etf_ema_fetch(
                    [d.symbol for d in live_data if not d.error],
                    f"ETF EMA All — {_now_ist().strftime('%d %b %H:%M')}",
                    ref_date=ref, scheme_map=scheme_map,
                )
                st.rerun()
        else:
            st.info("Click **Fetch / Refresh NSE ETF Data** to load the live ETF list from NSE India.")

    st.markdown("---")

    # ── Section 2 : ETF EMA Analysis ─────────────────────────────────────────
    st.markdown("### 📈 ETF EMA Analysis")

    today = _date.today()
    if st.session_state.pop("_live_pending_etf_ema", False):
        st.session_state["etf_ema_date_picker"] = today
    dcol1, dcol2 = st.columns([2, 5])
    with dcol1:
        _saved_date = st.session_state.get("etf_ema_ref_date") or today
        # Reset stale weekend dates to today so the date picker doesn't start on a Saturday/Sunday
        if _saved_date.weekday() >= 5:
            _saved_date = today
        ref_date_val = st.date_input(
            "Reference Date",
            value=_saved_date,
            max_value=today,
            help="EMA values are calculated as of this date. Weekends / market holidays auto-roll back to the previous trading day.",
            key="etf_ema_date_picker",
        )
        st.session_state.etf_ema_ref_date = ref_date_val
    with dcol2:
        st.markdown(
            "<p style='color:gray;font-size:0.85rem;margin-top:1.9rem;'>"
            "If the selected date falls on a weekend or NSE holiday, the previous available trading day is used automatically.</p>",
            unsafe_allow_html=True,
        )

    # Input tabs: manual entry | file upload | from live data
    live_syms = [d.symbol for d in st.session_state.get("etf_live_data", []) if not d.error]
    tab_labels = ["📝 Enter Symbols", "📁 Upload File"]
    if live_syms:
        tab_labels.append("📡 From Live Data")

    input_tabs = st.tabs(tab_labels)
    symbols: list = []

    with input_tabs[0]:
        txt = st.text_area(
            "ETF symbols",
            placeholder="Single ETF:  NIFTYBEES\nMultiple:  NIFTYBEES, BANKBEES, GOLDBEES\nOne per line also works",
            height=100,
            label_visibility="collapsed",
            key="etf_manual_input",
        )
        c1, c2 = st.columns([1, 5])
        with c1:
            scan_btn = st.button("🔍 Scan", type="primary", width='stretch', key="etf_scan_m")
        with c2:
            st.caption("Use NSE ETF symbols. Data fetched from Yahoo Finance (NSE: prefix applied internally).")
        if scan_btn and txt.strip():
            st.session_state["_etf_pending_scan_name"] = f"ETF EMA — {_now_ist().strftime('%d %b %H:%M')}"
            symbols = _parser.parse_text_input(txt)

    with input_tabs[1]:
        up = st.file_uploader(
            "Upload CSV / Excel / TXT", type=["csv", "xlsx", "xls", "txt"],
            label_visibility="collapsed", key="etf_file_uploader",
        )
        c1, c2, c3 = st.columns([1, 1, 4])
        with c1:
            scan_f = st.button("🔍 Scan File", type="primary", width='stretch', key="etf_scan_f")
        with c2:
            sample = pd.DataFrame({"symbol": ["NIFTYBEES", "BANKBEES", "GOLDBEES", "JUNIORBEES", "ITBEES"]})
            st.download_button("📥 Sample", sample.to_csv(index=False).encode(),
                               "sample_etfs.csv", "text/csv", key="etf_sample_dl")
        with c3:
            st.caption("CSV/Excel: 'symbol' column or first column. TXT: one per line.")
        if scan_f and up:
            st.session_state["_etf_pending_scan_name"] = up.name
            symbols = _parser.parse_file(up)
            if not symbols:
                st.warning("⚠️ No valid symbols found in file.")

    if len(tab_labels) > 2:
        with input_tabs[2]:
            st.caption(f"{len(live_syms)} ETF(s) loaded from NSE. Select which to analyse:")
            chosen = st.multiselect(
                "Select ETFs",
                options=live_syms,
                default=[],
                label_visibility="collapsed",
                key="etf_live_multisel",
                placeholder="Type to search…",
            )
            c1, c2 = st.columns([1, 5])
            with c1:
                run_live = st.button("🔍 Run EMA", type="primary",
                                     width='stretch', key="etf_scan_live")
            with c2:
                if st.button("Select All", key="etf_sel_all"):
                    st.session_state["_etf_sel_all"] = True
            if st.session_state.pop("_etf_sel_all", False):
                # mark all ETFs for scanning
                symbols = live_syms
                st.session_state["_etf_pending_scan_name"] = f"ETF EMA All — {_now_ist().strftime('%d %b %H:%M')}"
            elif run_live and chosen:
                symbols = chosen
                st.session_state["_etf_pending_scan_name"] = f"ETF EMA — {_now_ist().strftime('%d %b %H:%M')}"

    # ── Trigger scan ──────────────────────────────────────────────────────────
    if symbols:
        scan_name  = st.session_state.pop("_etf_pending_scan_name", "ETF EMA Scan")
        scheme_map = {d.symbol: d.assets for d in st.session_state.get("etf_live_data", [])}
        st.markdown(
            f"**Calculating EMA for {len(symbols)} ETF(s):** "
            f"{', '.join(symbols[:15])}{'…' if len(symbols) > 15 else ''}"
        )
        _do_etf_ema_fetch(symbols, scan_name, ref_date=ref_date_val, scheme_map=scheme_map)
        # Snap date picker to actual trading day (use max across all results)
        _snap_dates = [r.ref_date for r in st.session_state.get("etf_ema_data", [])
                       if r.ref_date and not r.error]
        if _snap_dates:
            try:
                from datetime import date as _dp
                _actual = _dp.fromisoformat(max(_snap_dates))
                if _actual != ref_date_val:
                    st.session_state["etf_ema_date_picker"] = _actual
                    st.session_state["etf_ema_ref_date"] = _actual
                    st.rerun()
            except Exception:
                pass

    # ── Refresh ───────────────────────────────────────────────────────────────
    if st.session_state.get("etf_ema_do_refresh"):
        st.session_state.etf_ema_do_refresh = False
        syms = st.session_state.get("etf_ema_last_scan_symbols", [])
        if not syms:
            syms = [d.symbol for d in st.session_state.get("etf_ema_data", [])]
        if syms:
            scheme_map = {d.symbol: d.assets for d in st.session_state.get("etf_live_data", [])}
            scan_name  = st.session_state.get("etf_ema_active_scan_name") or "ETF EMA Refresh"
            st.markdown(f"**Refreshing EMA for {len(syms)} ETF(s)…**")
            _do_etf_ema_fetch(syms, scan_name, ref_date=ref_date_val, scheme_map=scheme_map)
            # Snap date picker to actual trading day (use max across all results)
            _snap_dates2 = [r.ref_date for r in st.session_state.get("etf_ema_data", [])
                            if r.ref_date and not r.error]
            if _snap_dates2:
                try:
                    from datetime import date as _dp2
                    _actual2 = _dp2.fromisoformat(max(_snap_dates2))
                    if _actual2 != ref_date_val:
                        st.session_state["etf_ema_date_picker"] = _actual2
                        st.session_state["etf_ema_ref_date"] = _actual2
                        st.rerun()
                except Exception:
                    pass

    # ── Render results ────────────────────────────────────────────────────────
    ema_data = st.session_state.get("etf_ema_data", [])
    if ema_data:
        _render_etf_ema_table(ema_data)




def render_technicals():
    st.markdown("## 📈 Technical Analysis")
    tab_dma, tab_ema, tab_guru, tab_smc, tab_etf = st.tabs(
        ["📊 DMA Analysis", "📈 EMA Analysis", "🟢 GURU BUY", "💹 Discounted Price", "📊 ETF Analysis"]
    )
    with tab_dma:
        _render_dma_analysis()
    with tab_ema:
        _render_ema_analysis()
    with tab_guru:
        _render_guru_buy_analysis()
    with tab_smc:
        _render_smc_analysis()
    with tab_etf:
        _render_etf_analysis()


def _render_dma_analysis():
    from datetime import date as _date
    _auto_restore_scan("dma_data", _load_dma_session, "dma")

    # ── Reference date ────────────────────────────────────────────────────────
    today = _date.today()
    if st.session_state.pop("_live_pending_dma", False):
        st.session_state["dma_date_picker"] = today
    dcol1, dcol2 = st.columns([2, 5])
    with dcol1:
        saved_date = st.session_state.get("dma_ref_date") or today
        ref_date_val = st.date_input(
            "Reference Date",
            value=saved_date,
            max_value=today,
            help="DMA values are calculated as of this date. Weekends / market holidays automatically fall back to the previous trading day.",
            key="dma_date_picker",
        )
        st.session_state.dma_ref_date = ref_date_val
    with dcol2:
        st.markdown(
            "<p style='color:gray;font-size:0.85rem;margin-top:1.9rem;'>"
            "If the selected date falls on a weekend or NSE holiday, the previous available trading day is used automatically.</p>",
            unsafe_allow_html=True,
        )

    st.markdown("---")

    # ── Input tabs ────────────────────────────────────────────────────────────
    tab_manual, tab_file = st.tabs(["📝 Enter Symbols", "📁 Upload File"])
    symbols: list = []

    with tab_manual:
        txt = st.text_area(
            "Stock symbols",
            placeholder="Single stock:  TCS\nMultiple:  RELIANCE, INFY, TCS\nOne per line also works",
            height=100,
            label_visibility="collapsed",
            key="dma_manual_input",
        )
        c1, c2 = st.columns([1, 5])
        with c1:
            scan_btn = st.button("🔍 Scan", type="primary", width='stretch', key="dma_scan_m")
        with c2:
            st.caption("Use NSE symbols. Data fetched from Yahoo Finance (NSE: prefix applied internally).")
        if scan_btn and txt.strip():
            st.session_state["_dma_pending_scan_name"] = f"DMA — {_now_ist().strftime('%d %b %H:%M')}"
            st.session_state["_dma_is_file_upload"] = False
            symbols = _parser.parse_text_input(txt)

    with tab_file:
        up = st.file_uploader(
            "Upload CSV / Excel / TXT", type=["csv", "xlsx", "xls", "txt"],
            label_visibility="collapsed", key="dma_file_uploader",
        )
        c1, c2, c3 = st.columns([1, 1, 4])
        with c1:
            scan_f = st.button("🔍 Scan File", type="primary", width='stretch', key="dma_scan_f")
        with c2:
            sample = pd.DataFrame({"symbol": ["RELIANCE", "INFY", "TCS", "HDFCBANK", "WIPRO"]})
            st.download_button("📥 Sample", sample.to_csv(index=False).encode(),
                               "sample_stocks.csv", "text/csv", key="dma_sample_dl")
        with c3:
            st.caption("CSV/Excel: 'symbol' column or first column. TXT: one per line.")
        if scan_f and up:
            st.session_state["_dma_pending_scan_name"] = up.name
            st.session_state["_dma_is_file_upload"] = True
            symbols = _parser.parse_file(up)
            if not symbols:
                st.warning("⚠️ No valid symbols found in file.")

    # ── Fetch ─────────────────────────────────────────────────────────────────
    if symbols:
        scan_name = st.session_state.pop("_dma_pending_scan_name", "DMA Scan")
        is_file = st.session_state.pop("_dma_is_file_upload", False)
        st.markdown(
            f"**Calculating DMA for {len(symbols)} symbol(s):** "
            f"{', '.join(symbols[:15])}{'…' if len(symbols) > 15 else ''}"
        )
        _do_dma_fetch(symbols, scan_name, ref_date=ref_date_val, save_to_history=True)
        # Snap date picker to actual trading day (may differ when ref_date is a holiday)
        for _rd in st.session_state.get("dma_data", []):
            if _rd.ref_date and not _rd.error:
                try:
                    from datetime import date as _dp
                    _actual = _dp.fromisoformat(_rd.ref_date)
                    if _actual != ref_date_val:
                        st.session_state["dma_date_picker"] = _actual
                        st.rerun()
                except Exception:
                    pass
                break

    # ── Refresh ───────────────────────────────────────────────────────────────
    if st.session_state.get("dma_do_refresh"):
        st.session_state.dma_do_refresh = False
        syms = st.session_state.get("dma_last_scan_symbols", [])
        if not syms:
            syms = [d.symbol for d in st.session_state.get("dma_data", [])]
        if syms:
            scan_name = st.session_state.get("dma_active_scan_name") or "DMA Refresh"
            st.markdown(f"**Refreshing DMA for {len(syms)} symbol(s)…**")
            _do_dma_fetch(syms, scan_name, ref_date=ref_date_val, save_to_history=True)
            # Snap date picker to actual trading day
            for _rd in st.session_state.get("dma_data", []):
                if _rd.ref_date and not _rd.error:
                    try:
                        from datetime import date as _dp2
                        _actual2 = _dp2.fromisoformat(_rd.ref_date)
                        if _actual2 != ref_date_val:
                            st.session_state["dma_date_picker"] = _actual2
                            st.rerun()
                    except Exception:
                        pass
                    break

    # ── Render results ────────────────────────────────────────────────────────
    data = st.session_state.get("dma_data", [])
    if not data:
        return

    if len(data) == 1:
        _render_dma_card(data[0])
    else:
        _render_dma_table(data)


# ── DMA single-stock card ─────────────────────────────────────────────────────

def _render_dma_card(d):
    dark = st.session_state.dark_mode
    card_bg  = "#161b22" if dark else "#ffffff"
    border_c = "#30363d" if dark else "#dde3ec"
    label_c  = "#8b949e" if dark else "#6b7280"
    val_c    = "#c9d1d9" if dark else "#111827"
    name_c   = "#58a6ff" if dark else "#1f4e79"

    if d.error:
        st.error(f"❌ Could not fetch DMA for **{d.symbol}**: {d.error}")
    else:
        # Trend strength
        total = sum(1 for x in [d.dma1, d.dma2, d.dma3, d.dma4, d.dma5, d.dma6, d.dma7,
                                  d.dma10, d.dma20, d.dma22, d.dma50, d.dma100, d.dma200]
                    if x is not None)
        cnt = d.above_dma_count or 0
        if total > 0:
            ratio = cnt / total
            if ratio >= 12/13:   trend, trend_icon = "Strong Uptrend",    "✅"
            elif ratio >= 9/13:  trend, trend_icon = "Moderate Uptrend",  "📈"
            elif ratio >= 5/13:  trend, trend_icon = "Mixed / Neutral",   "➡️"
            elif ratio >= 2/13:  trend, trend_icon = "Moderate Downtrend","📉"
            else:                trend, trend_icon = "Strong Downtrend",  "❌"
        else:
            trend, trend_icon = "N/A", "—"

        abbr = "".join(w[0] for w in d.symbol.split()[:3]).upper()[:3]
        price_str = f"₹ {d.current_price:,.2f}" if d.current_price else "—"

        st.markdown(f"""
        <div style="background:{card_bg};border:1px solid {border_c};border-radius:16px;
                    padding:1.5rem 2rem;margin-bottom:1rem;">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:1rem;">
            <div style="display:flex;align-items:center;gap:1rem;">
              <div style="width:56px;height:56px;border-radius:12px;background:#1f4e79;
                          display:flex;align-items:center;justify-content:center;
                          color:white;font-weight:700;font-size:1rem;flex-shrink:0;">{abbr}</div>
              <div>
                <div style="font-size:1.6rem;font-weight:700;color:{name_c};line-height:1.2;">
                  {d.company_name or d.symbol}
                </div>
                <div style="color:{label_c};font-size:0.85rem;margin-top:4px;">
                  NSE: {d.symbol} &nbsp;·&nbsp; Ref Date: {d.ref_date or '—'}
                </div>
              </div>
            </div>
            <div style="text-align:right;">
              <div style="font-size:1.8rem;font-weight:700;color:{val_c};">{price_str}</div>
              <div style="font-size:1rem;font-weight:600;color:{label_c};">
                {trend_icon} {trend} &nbsp;·&nbsp; ↑ {cnt}/{total} DMAs
              </div>
            </div>
          </div>
        </div>
        """, unsafe_allow_html=True)

        # DMA grid helper
        def dma_cell(label, dma_val):
            if dma_val is None:
                bg = "#374151" if dark else "#f3f4f6"
                indicator = "—"
                iv_c = label_c
                dv_c = label_c
            elif d.current_price and d.current_price > dma_val:
                bg = "#052e16" if dark else "#f0fdf4"
                indicator = "▲"
                iv_c = "#16a34a"
                dv_c = val_c
            else:
                bg = "#450a0a" if dark else "#fff1f2"
                indicator = "▼"
                iv_c = "#dc2626"
                dv_c = val_c

            val_text = f"₹{dma_val:,.2f}" if dma_val is not None else "—"
            return f"""
            <div style="background:{bg};border:1px solid {border_c};border-radius:10px;
                        padding:0.75rem 1rem;">
              <div style="font-size:0.76rem;color:{label_c};margin-bottom:3px;">{label}</div>
              <div style="font-size:1rem;font-weight:600;color:{dv_c};">
                <span style="color:{iv_c};margin-right:4px;">{indicator}</span>{val_text}
              </div>
            </div>"""

        def row3(*cells):
            return f'<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:0.5rem;margin-bottom:0.5rem;">' \
                   + "".join(cells) + "</div>"

        def section_header(title):
            return f'<p style="color:{label_c};font-size:0.8rem;font-weight:600;margin:0.8rem 0 0.4rem;">{title}</p>'

        grid = section_header("Short-term (DMA 1–7)")
        grid += row3(dma_cell("DMA 1",  d.dma1),  dma_cell("DMA 2",  d.dma2),  dma_cell("DMA 3",  d.dma3))
        grid += row3(dma_cell("DMA 4",  d.dma4),  dma_cell("DMA 5",  d.dma5),  dma_cell("DMA 6",  d.dma6))
        grid += row3(dma_cell("DMA 7",  d.dma7),  dma_cell("", None), dma_cell("", None))
        grid += section_header("Medium-term (DMA 10–22)")
        grid += row3(dma_cell("DMA 10", d.dma10), dma_cell("DMA 20", d.dma20), dma_cell("DMA 22", d.dma22))
        grid += section_header("Long-term (DMA 50–200)")
        grid += row3(dma_cell("DMA 50",  d.dma50), dma_cell("DMA 100", d.dma100), dma_cell("DMA 200", d.dma200))

        st.markdown(grid, unsafe_allow_html=True)

    st.markdown("---")

    # Buttons
    nse_sym = d.symbol
    nse_url = f"https://www.nseindia.com/get-quotes/equity?symbol={nse_sym}"
    tv_url  = f"https://in.tradingview.com/chart/?symbol=NSE%3A{nse_sym}&interval=D"
    sc_url  = f"https://www.screener.in/company/{nse_sym}/"

    st.markdown("**🔗 Open in:**")
    bc1, bc2, bc3, bc4, bc5 = st.columns(5)
    with bc1:
        st.link_button("📈 NSE Chart", nse_url, width='stretch', type="primary")
    with bc2:
        st.link_button("📊 TradingView", tv_url, width='stretch')
    with bc3:
        st.link_button("🔍 Screener.in", sc_url, width='stretch')
    with bc4:
        if st.button("🔄 Refresh DMA", width='stretch', type="primary",
                     help="Re-fetch latest data from NSE",
                     key="dma_refresh_card"):
            st.session_state.dma_do_refresh = True
            st.rerun()
    with bc5:
        _live_btn("dma", "dma_do_refresh", "dma_live_card")


# ── DMA multi-stock table ─────────────────────────────────────────────────────

def _render_dma_table(dma_list: list):
    rows = []
    for d in dma_list:
        total = sum(1 for x in [d.dma1, d.dma2, d.dma3, d.dma4, d.dma5, d.dma6, d.dma7,
                                  d.dma10, d.dma20, d.dma22, d.dma50, d.dma100, d.dma200]
                    if x is not None)
        cnt = d.above_dma_count or 0
        if total > 0:
            arrow = "↑" if cnt > total / 2 else "↓"
            signal = f"{arrow} {cnt}/{total}"
        else:
            signal = "—"

        row = {
            "Symbol":       d.symbol,
            "Company":      d.company_name or d.symbol,
            "Ref Date":     d.ref_date or "—",
            "Price (₹)":    d.current_price,
            "DMA 1":        d.dma1,
            "DMA 2":        d.dma2,
            "DMA 3":        d.dma3,
            "DMA 4":        d.dma4,
            "DMA 5":        d.dma5,
            "DMA 6":        d.dma6,
            "DMA 7":        d.dma7,
            "DMA 10":       d.dma10,
            "DMA 20":       d.dma20,
            "DMA 22":       d.dma22,
            "DMA 50":       d.dma50,
            "DMA 100":      d.dma100,
            "DMA 200":      d.dma200,
            "Above DMAs":   signal,
        }
        if d.error:
            row["Error"] = d.error
        rows.append(row)

    df = pd.DataFrame(rows)
    st.markdown("---")

    # Results header
    h1, h2, h3, h4 = st.columns([3, 2, 1, 1])
    with h1:
        st.markdown(f"### 📊 DMA Results — {len(df)} stock(s)")
    with h2:
        ts = st.session_state.get("dma_last_fetched_at")
        if ts:
            hour = ts.hour % 12 or 12
            ampm = "a.m." if ts.hour < 12 else "p.m."
            ts_str = f"{ts.day} {ts.strftime('%b %Y')}, {hour}:{ts.strftime('%M')} {ampm}"
            st.markdown(
                f"<p style='margin-top:0.6rem;color:gray;font-size:0.85rem;'>🕐 Fetched: {ts_str}</p>",
                unsafe_allow_html=True,
            )
    with h3:
        if st.button("🔄 Refresh", key="dma_refresh_table", width='stretch', type="primary",
                     help="Re-fetch latest data from NSE"):
            st.session_state.dma_do_refresh = True
            st.rerun()
    with h4:
        _live_btn("dma", "dma_do_refresh", "dma_live_table")

    # Summary row
    retrieved = df["Price (₹)"].notna().sum()
    ref_dates = df["Ref Date"].dropna()
    latest_ref = ref_dates.iloc[0] if not ref_dates.empty else "—"
    c1, c2, c3 = st.columns(3)
    c1.metric("Stocks Scanned",  len(df))
    c2.metric("Data Retrieved",  int(retrieved))
    c3.metric("Trading Day Used", latest_ref, help=f"Trading day used for analysis: {latest_ref}")

    st.markdown("---")

    # Export
    ec1, ec2, *_ = st.columns([1, 1, 4])
    with ec1:
        st.download_button(
            "📥 CSV", df.to_csv(index=False).encode(),
            f"dma_scan_{_now_ist().strftime('%Y%m%d_%H%M')}.csv", "text/csv",
            key="dma_csv_dl",
        )
    with ec2:
        try:
            import io
            buf = io.BytesIO()
            df.to_excel(buf, index=False, engine="openpyxl")
            st.download_button("📥 Excel", buf.getvalue(),
                               f"dma_scan_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx",
                               key="dma_xls_dl")
        except Exception:
            pass

    num_col = st.column_config.NumberColumn
    df = _df_filter_sort(df, "dma")
    evt = st.dataframe(
        df, width="stretch", hide_index=True,
        on_select="rerun", selection_mode="single-row",
        column_config={
            "Symbol":       st.column_config.TextColumn("Symbol",   width="small"),
            "Company":      st.column_config.TextColumn("Company",  width="medium"),
            "Ref Date":     st.column_config.TextColumn("Ref Date", width="small"),
            "Price (₹)":    num_col("Price (₹)",  format="₹%.2f"),
            "DMA 1":        num_col("DMA 1",   format="₹%.2f"),
            "DMA 2":        num_col("DMA 2",   format="₹%.2f"),
            "DMA 3":        num_col("DMA 3",   format="₹%.2f"),
            "DMA 4":        num_col("DMA 4",   format="₹%.2f"),
            "DMA 5":        num_col("DMA 5",   format="₹%.2f"),
            "DMA 6":        num_col("DMA 6",   format="₹%.2f"),
            "DMA 7":        num_col("DMA 7",   format="₹%.2f"),
            "DMA 10":       num_col("DMA 10",  format="₹%.2f"),
            "DMA 20":       num_col("DMA 20",  format="₹%.2f"),
            "DMA 22":       num_col("DMA 22",  format="₹%.2f"),
            "DMA 50":       num_col("DMA 50",  format="₹%.2f"),
            "DMA 100":      num_col("DMA 100", format="₹%.2f"),
            "DMA 200":      num_col("DMA 200", format="₹%.2f"),
            "Above DMAs":   st.column_config.TextColumn("Above DMAs", width="small"),
        },
    )

    # Row selection → navigation
    selected_rows = evt.selection.rows if hasattr(evt, "selection") else []
    selected_sym = None
    if selected_rows:
        idx = selected_rows[0]
        selected_sym = df.iloc[idx]["Symbol"]
        st.info(f"Selected: **{df.iloc[idx]['Company']}** ({selected_sym})")

    col1, col2, col3, col4 = st.columns([2, 2, 2, 2])
    with col4:
        if st.button("🗑️ Clear Results", key="dma_clr"):
            st.session_state.dma_data = []
            st.rerun()
    if selected_sym:
        with col1:
            st.link_button(
                "📈 NSE Chart",
                f"https://www.nseindia.com/get-quotes/equity?symbol={selected_sym}",
                width='stretch', type="primary",
            )
        with col2:
            st.link_button(
                "📊 TradingView",
                f"https://in.tradingview.com/chart/?symbol=NSE%3A{selected_sym}&interval=D",
                width='stretch',
            )
        with col3:
            st.link_button(
                "🔍 Screener.in",
                f"https://www.screener.in/company/{selected_sym}/",
                width='stretch',
            )
    else:
        with col1:
            st.button("📈 NSE Chart",    disabled=True, width='stretch', key="dma_nse_dis")
        with col2:
            st.button("📊 TradingView", disabled=True, width='stretch', key="dma_tv_dis")
        with col3:
            st.button("🔍 Screener.in", disabled=True, width='stretch', key="dma_sc_dis")
        st.caption("⬆️ Click a row in the table above to enable navigation buttons")


# ── EMA Analysis ──────────────────────────────────────────────────────────────

def _render_ema_analysis():
    from datetime import date as _date
    _auto_restore_scan("ema_data", _load_ema_session, "ema")

    today = _date.today()
    if st.session_state.pop("_live_pending_ema", False):
        st.session_state["ema_date_picker"] = today
    dcol1, dcol2 = st.columns([2, 5])
    with dcol1:
        saved_date = st.session_state.get("ema_ref_date") or today
        ref_date_val = st.date_input(
            "Reference Date",
            value=saved_date,
            max_value=today,
            help="EMA values are calculated as of this date. Weekends / market holidays automatically fall back to the previous trading day.",
            key="ema_date_picker",
        )
        st.session_state.ema_ref_date = ref_date_val
    with dcol2:
        st.markdown(
            "<p style='color:gray;font-size:0.85rem;margin-top:1.9rem;'>"
            "If the selected date falls on a weekend or NSE holiday, the previous available trading day is used automatically.</p>",
            unsafe_allow_html=True,
        )

    st.markdown("---")

    tab_manual, tab_file = st.tabs(["📝 Enter Symbols", "📁 Upload File"])
    symbols: list = []

    with tab_manual:
        txt = st.text_area(
            "Stock symbols",
            placeholder="Single stock:  TCS\nMultiple:  RELIANCE, INFY, TCS\nOne per line also works",
            height=100,
            label_visibility="collapsed",
            key="ema_manual_input",
        )
        c1, c2 = st.columns([1, 5])
        with c1:
            scan_btn = st.button("🔍 Scan", type="primary", width='stretch', key="ema_scan_m")
        with c2:
            st.caption("Use NSE symbols. EMA data fetched from Yahoo Finance.")
        if scan_btn and txt.strip():
            st.session_state["_ema_pending_scan_name"] = f"EMA — {_now_ist().strftime('%d %b %H:%M')}"
            st.session_state["_ema_is_file_upload"] = False
            symbols = _parser.parse_text_input(txt)

    with tab_file:
        up = st.file_uploader(
            "Upload CSV / Excel / TXT", type=["csv", "xlsx", "xls", "txt"],
            label_visibility="collapsed", key="ema_file_uploader",
        )
        c1, c2, c3 = st.columns([1, 1, 4])
        with c1:
            scan_f = st.button("🔍 Scan File", type="primary", width='stretch', key="ema_scan_f")
        with c2:
            sample = pd.DataFrame({"symbol": ["RELIANCE", "INFY", "TCS", "HDFCBANK", "WIPRO"]})
            st.download_button("📥 Sample", sample.to_csv(index=False).encode(),
                               "sample_stocks.csv", "text/csv", key="ema_sample_dl")
        with c3:
            st.caption("CSV/Excel: 'symbol' column or first column. TXT: one per line.")
        if scan_f and up:
            st.session_state["_ema_pending_scan_name"] = up.name
            st.session_state["_ema_is_file_upload"] = True
            symbols = _parser.parse_file(up)
            if not symbols:
                st.warning("⚠️ No valid symbols found in file.")

    if symbols:
        scan_name = st.session_state.pop("_ema_pending_scan_name", "EMA Scan")
        is_file = st.session_state.pop("_ema_is_file_upload", False)
        st.markdown(
            f"**Calculating EMA for {len(symbols)} symbol(s):** "
            f"{', '.join(symbols[:15])}{'…' if len(symbols) > 15 else ''}"
        )
        _do_ema_fetch(symbols, scan_name, ref_date=ref_date_val, save_to_history=True)
        # Snap date picker to actual trading day (may differ when ref_date is a holiday)
        for _rd in st.session_state.get("ema_data", []):
            if _rd.ref_date and not _rd.error:
                try:
                    from datetime import date as _dp
                    _actual = _dp.fromisoformat(_rd.ref_date)
                    if _actual != ref_date_val:
                        st.session_state["ema_date_picker"] = _actual
                        st.rerun()
                except Exception:
                    pass
                break

    if st.session_state.get("ema_do_refresh"):
        st.session_state.ema_do_refresh = False
        syms = st.session_state.get("ema_last_scan_symbols", [])
        if not syms:
            syms = [d.symbol for d in st.session_state.get("ema_data", [])]
        if syms:
            scan_name = st.session_state.get("ema_active_scan_name") or "EMA Refresh"
            st.markdown(f"**Refreshing EMA for {len(syms)} symbol(s)…**")
            _do_ema_fetch(syms, scan_name, ref_date=ref_date_val, save_to_history=True)
            # Snap date picker to actual trading day
            for _rd in st.session_state.get("ema_data", []):
                if _rd.ref_date and not _rd.error:
                    try:
                        from datetime import date as _dp2
                        _actual2 = _dp2.fromisoformat(_rd.ref_date)
                        if _actual2 != ref_date_val:
                            st.session_state["ema_date_picker"] = _actual2
                            st.rerun()
                    except Exception:
                        pass
                    break

    data = st.session_state.get("ema_data", [])
    if not data:
        return

    if len(data) == 1:
        _render_ema_card(data[0])
    else:
        _render_ema_table(data)


def _render_ema_card(d):
    dark = st.session_state.dark_mode
    card_bg  = "#161b22" if dark else "#ffffff"
    border_c = "#30363d" if dark else "#dde3ec"
    label_c  = "#8b949e" if dark else "#6b7280"
    val_c    = "#c9d1d9" if dark else "#111827"
    name_c   = "#58a6ff" if dark else "#1f4e79"

    if d.error:
        st.error(f"❌ Could not fetch EMA for **{d.symbol}**: {d.error}")
    else:
        total = sum(1 for x in [d.ema1, d.ema2, d.ema3, d.ema4, d.ema5, d.ema6, d.ema7,
                                  d.ema10, d.ema20, d.ema22, d.ema50, d.ema100, d.ema200]
                    if x is not None)
        cnt = d.above_ema_count or 0
        if total > 0:
            ratio = cnt / total
            if ratio >= 12/13:   trend, trend_icon = "Strong Uptrend",    "✅"
            elif ratio >= 9/13:  trend, trend_icon = "Moderate Uptrend",  "📈"
            elif ratio >= 5/13:  trend, trend_icon = "Mixed / Neutral",   "➡️"
            elif ratio >= 2/13:  trend, trend_icon = "Moderate Downtrend","📉"
            else:                trend, trend_icon = "Strong Downtrend",  "❌"
        else:
            trend, trend_icon = "N/A", "—"

        abbr = "".join(w[0] for w in d.symbol.split()[:3]).upper()[:3]
        price_str = f"₹ {d.current_price:,.2f}" if d.current_price else "—"

        st.markdown(f"""
        <div style="background:{card_bg};border:1px solid {border_c};border-radius:16px;
                    padding:1.5rem 2rem;margin-bottom:1rem;">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:1rem;">
            <div style="display:flex;align-items:center;gap:1rem;">
              <div style="width:56px;height:56px;border-radius:12px;background:#1f4e79;
                          display:flex;align-items:center;justify-content:center;
                          color:white;font-weight:700;font-size:1rem;flex-shrink:0;">{abbr}</div>
              <div>
                <div style="font-size:1.6rem;font-weight:700;color:{name_c};line-height:1.2;">
                  {d.company_name or d.symbol}
                </div>
                <div style="color:{label_c};font-size:0.85rem;margin-top:4px;">
                  NSE: {d.symbol} &nbsp;·&nbsp; Ref Date: {d.ref_date or '—'}
                </div>
              </div>
            </div>
            <div style="text-align:right;">
              <div style="font-size:1.8rem;font-weight:700;color:{val_c};">{price_str}</div>
              <div style="font-size:1rem;font-weight:600;color:{label_c};">
                {trend_icon} {trend} &nbsp;·&nbsp; ↑ {cnt}/{total} EMAs
              </div>
            </div>
          </div>
        </div>
        """, unsafe_allow_html=True)

        def ema_cell(label, ema_val):
            if ema_val is None:
                bg = "#374151" if dark else "#f3f4f6"
                indicator, iv_c, dv_c = "—", label_c, label_c
            elif d.current_price and d.current_price > ema_val:
                bg = "#052e16" if dark else "#f0fdf4"
                indicator, iv_c, dv_c = "▲", "#16a34a", val_c
            else:
                bg = "#450a0a" if dark else "#fff1f2"
                indicator, iv_c, dv_c = "▼", "#dc2626", val_c
            val_text = f"₹{ema_val:,.2f}" if ema_val is not None else "—"
            return f"""
            <div style="background:{bg};border:1px solid {border_c};border-radius:10px;
                        padding:0.75rem 1rem;">
              <div style="font-size:0.76rem;color:{label_c};margin-bottom:3px;">{label}</div>
              <div style="font-size:1rem;font-weight:600;color:{dv_c};">
                <span style="color:{iv_c};margin-right:4px;">{indicator}</span>{val_text}
              </div>
            </div>"""

        def row3(*cells):
            return (
                '<div style="display:grid;grid-template-columns:repeat(3,1fr);'
                'gap:0.5rem;margin-bottom:0.5rem;">' + "".join(cells) + "</div>"
            )

        def section_hdr(title):
            return f'<p style="color:{label_c};font-size:0.8rem;font-weight:600;margin:0.8rem 0 0.4rem;">{title}</p>'

        grid = section_hdr("Short-term (EMA 1–7)")
        grid += row3(ema_cell("EMA 1", d.ema1), ema_cell("EMA 2", d.ema2), ema_cell("EMA 3", d.ema3))
        grid += row3(ema_cell("EMA 4", d.ema4), ema_cell("EMA 5", d.ema5), ema_cell("EMA 6", d.ema6))
        grid += row3(ema_cell("EMA 7", d.ema7), ema_cell("", None), ema_cell("", None))
        grid += section_hdr("Medium-term (EMA 10–22)")
        grid += row3(ema_cell("EMA 10", d.ema10), ema_cell("EMA 20", d.ema20), ema_cell("EMA 22", d.ema22))
        grid += section_hdr("Long-term (EMA 50–200)")
        grid += row3(ema_cell("EMA 50", d.ema50), ema_cell("EMA 100", d.ema100), ema_cell("EMA 200", d.ema200))
        st.markdown(grid, unsafe_allow_html=True)

    st.markdown("---")
    nse_sym = d.symbol
    st.markdown("**🔗 Open in:**")
    bc1, bc2, bc3, bc4, bc5 = st.columns(5)
    with bc1:
        st.link_button("📈 NSE Chart",
                       f"https://www.nseindia.com/get-quotes/equity?symbol={nse_sym}",
                       width='stretch', type="primary")
    with bc2:
        st.link_button("📊 TradingView",
                       f"https://in.tradingview.com/chart/?symbol=NSE%3A{nse_sym}&interval=D",
                       width='stretch')
    with bc3:
        st.link_button("🔍 Screener.in",
                       f"https://www.screener.in/company/{nse_sym}/",
                       width='stretch')
    with bc4:
        if st.button("🔄 Refresh EMA", width='stretch', type="primary",
                     key="ema_refresh_card"):
            st.session_state.ema_do_refresh = True
            st.rerun()
    with bc5:
        _live_btn("ema", "ema_do_refresh", "ema_live_card")


def _render_ema_table(ema_list: list):
    rows = []
    for d in ema_list:
        total = sum(1 for x in [d.ema1, d.ema2, d.ema3, d.ema4, d.ema5, d.ema6, d.ema7,
                                  d.ema10, d.ema20, d.ema22, d.ema50, d.ema100, d.ema200]
                    if x is not None)
        cnt = d.above_ema_count or 0
        arrow = "↑" if cnt > total / 2 else "↓"
        signal = f"{arrow} {cnt}/{total}" if total > 0 else "—"

        row = {
            "Symbol":       d.symbol,
            "Company":      d.company_name or d.symbol,
            "Ref Date":     d.ref_date or "—",
            "Price (₹)":    d.current_price,
            "EMA 1":        d.ema1,
            "EMA 2":        d.ema2,
            "EMA 3":        d.ema3,
            "EMA 4":        d.ema4,
            "EMA 5":        d.ema5,
            "EMA 6":        d.ema6,
            "EMA 7":        d.ema7,
            "EMA 10":       d.ema10,
            "EMA 20":       d.ema20,
            "EMA 22":       d.ema22,
            "EMA 50":       d.ema50,
            "EMA 100":      d.ema100,
            "EMA 200":      d.ema200,
            "Above EMAs":   signal,
        }
        if d.error:
            row["Error"] = d.error
        rows.append(row)

    df = pd.DataFrame(rows)
    st.markdown("---")

    h1, h2, h3, h4 = st.columns([3, 2, 1, 1])
    with h1:
        st.markdown(f"### 📈 EMA Results — {len(df)} stock(s)")
    with h2:
        ts = st.session_state.get("ema_last_fetched_at")
        if ts:
            hour = ts.hour % 12 or 12
            ampm = "a.m." if ts.hour < 12 else "p.m."
            ts_str = f"{ts.day} {ts.strftime('%b %Y')}, {hour}:{ts.strftime('%M')} {ampm}"
            st.markdown(
                f"<p style='margin-top:0.6rem;color:gray;font-size:0.85rem;'>🕐 Fetched: {ts_str}</p>",
                unsafe_allow_html=True,
            )
    with h3:
        if st.button("🔄 Refresh", key="ema_refresh_table", width='stretch', type="primary"):
            st.session_state.ema_do_refresh = True
            st.rerun()
    with h4:
        _live_btn("ema", "ema_do_refresh", "ema_live_table")

    retrieved = df["Price (₹)"].notna().sum()
    ref_dates = df["Ref Date"].dropna()
    latest_ref = ref_dates.iloc[0] if not ref_dates.empty else "—"
    c1, c2, c3 = st.columns(3)
    c1.metric("Stocks Scanned",   len(df))
    c2.metric("Data Retrieved",   int(retrieved))
    c3.metric("Trading Day Used", latest_ref, help=f"Trading day used for analysis: {latest_ref}")

    st.markdown("---")

    ec1, ec2, *_ = st.columns([1, 1, 4])
    with ec1:
        st.download_button(
            "📥 CSV", df.to_csv(index=False).encode(),
            f"ema_scan_{_now_ist().strftime('%Y%m%d_%H%M')}.csv", "text/csv",
            key="ema_csv_dl",
        )
    with ec2:
        try:
            import io
            buf = io.BytesIO()
            df.to_excel(buf, index=False, engine="openpyxl")
            st.download_button("📥 Excel", buf.getvalue(),
                               f"ema_scan_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx",
                               key="ema_xls_dl")
        except Exception:
            pass

    num_col = st.column_config.NumberColumn
    df = _df_filter_sort(df, "ema")
    evt = st.dataframe(
        df, width="stretch", hide_index=True,
        on_select="rerun", selection_mode="single-row",
        column_config={
            "Symbol":       st.column_config.TextColumn("Symbol",   width="small"),
            "Company":      st.column_config.TextColumn("Company",  width="medium"),
            "Ref Date":     st.column_config.TextColumn("Ref Date", width="small"),
            "Price (₹)":    num_col("Price (₹)",  format="₹%.2f"),
            "EMA 1":        num_col("EMA 1",   format="₹%.2f"),
            "EMA 2":        num_col("EMA 2",   format="₹%.2f"),
            "EMA 3":        num_col("EMA 3",   format="₹%.2f"),
            "EMA 4":        num_col("EMA 4",   format="₹%.2f"),
            "EMA 5":        num_col("EMA 5",   format="₹%.2f"),
            "EMA 6":        num_col("EMA 6",   format="₹%.2f"),
            "EMA 7":        num_col("EMA 7",   format="₹%.2f"),
            "EMA 10":       num_col("EMA 10",  format="₹%.2f"),
            "EMA 20":       num_col("EMA 20",  format="₹%.2f"),
            "EMA 22":       num_col("EMA 22",  format="₹%.2f"),
            "EMA 50":       num_col("EMA 50",  format="₹%.2f"),
            "EMA 100":      num_col("EMA 100", format="₹%.2f"),
            "EMA 200":      num_col("EMA 200", format="₹%.2f"),
            "Above EMAs":   st.column_config.TextColumn("Above EMAs", width="small"),
        },
    )

    selected_rows = evt.selection.rows if hasattr(evt, "selection") else []
    selected_sym = None
    if selected_rows:
        idx = selected_rows[0]
        selected_sym = df.iloc[idx]["Symbol"]
        st.info(f"Selected: **{df.iloc[idx]['Company']}** ({selected_sym})")

    col1, col2, col3, col4 = st.columns([2, 2, 2, 2])
    with col4:
        if st.button("🗑️ Clear Results", key="ema_clr"):
            st.session_state.ema_data = []
            st.rerun()
    if selected_sym:
        with col1:
            st.link_button(
                "📈 NSE Chart",
                f"https://www.nseindia.com/get-quotes/equity?symbol={selected_sym}",
                width='stretch', type="primary",
            )
        with col2:
            st.link_button(
                "📊 TradingView",
                f"https://in.tradingview.com/chart/?symbol=NSE%3A{selected_sym}&interval=D",
                width='stretch',
            )
        with col3:
            st.link_button(
                "🔍 Screener.in",
                f"https://www.screener.in/company/{selected_sym}/",
                width='stretch',
            )
    else:
        with col1:
            st.button("📈 NSE Chart",    disabled=True, width='stretch', key="ema_nse_dis")
        with col2:
            st.button("📊 TradingView", disabled=True, width='stretch', key="ema_tv_dis")
        with col3:
            st.button("🔍 Screener.in", disabled=True, width='stretch', key="ema_sc_dis")
        st.caption("⬆️ Click a row in the table above to enable navigation buttons")


# ── GURU BUY (Rainbow indicator) ─────────────────────────────────────────────

def _sig_label(val: bool) -> str:
    return "✅ BUY" if val else "❌ SELL"


def _guru_badge(signal: str) -> str:
    if signal == "STRONG BUY":  return "🟢 STRONG BUY"
    if signal == "BUY":         return "🟢 BUY"
    if signal == "NEUTRAL":     return "🟡 NEUTRAL"
    if signal == "SELL":        return "🔴 SELL"
    return "🔴 STRONG SELL"


def _render_guru_buy_analysis():
    from datetime import date as _date
    _auto_restore_scan("guru_data", _load_guru_buy_session, "guru_buy")

    today = _date.today()
    if st.session_state.pop("_live_pending_guru", False):
        st.session_state["guru_date_picker"] = today
    dcol1, dcol2 = st.columns([2, 5])
    with dcol1:
        saved_date = st.session_state.get("guru_ref_date") or today
        ref_date_val = st.date_input(
            "Reference Date",
            value=saved_date, max_value=today,
            help="Rainbow EMA values calculated as of this date.",
            key="guru_date_picker",
        )
        st.session_state.guru_ref_date = ref_date_val
    with dcol2:
        st.markdown(
            "<p style='color:gray;font-size:0.85rem;margin-top:1.9rem;'>"
            "Weekends / NSE holidays auto-roll back to the previous trading day.</p>",
            unsafe_allow_html=True,
        )

    st.markdown("---")

    tab_manual, tab_file = st.tabs(["📝 Enter Symbols", "📁 Upload File"])
    symbols: list = []

    with tab_manual:
        txt = st.text_area(
            "Stock symbols",
            placeholder="Single stock:  TCS\nMultiple:  RELIANCE, INFY, TCS",
            height=100, label_visibility="collapsed", key="guru_manual_input",
        )
        c1, c2 = st.columns([1, 5])
        with c1:
            scan_btn = st.button("🔍 Scan", type="primary", width='stretch', key="guru_scan_m")
        with c2:
            st.caption("Rainbow EMA(2)×7 indicator — based on Pine Script logic.")
        if scan_btn and txt.strip():
            st.session_state["_guru_pending"] = f"GURU BUY — {_now_ist().strftime('%d %b %H:%M')}"
            st.session_state["_guru_is_file"] = False
            symbols = _parser.parse_text_input(txt)

    with tab_file:
        up = st.file_uploader("Upload CSV / Excel / TXT", type=["csv", "xlsx", "xls", "txt"],
                               label_visibility="collapsed", key="guru_file_uploader")
        c1, c2, c3 = st.columns([1, 1, 4])
        with c1:
            scan_f = st.button("🔍 Scan File", type="primary", width='stretch', key="guru_scan_f")
        with c2:
            sample = pd.DataFrame({"symbol": ["RELIANCE", "INFY", "TCS", "HDFCBANK", "WIPRO"]})
            st.download_button("📥 Sample", sample.to_csv(index=False).encode(),
                               "sample_stocks.csv", "text/csv", key="guru_sample_dl")
        with c3:
            st.caption("CSV/Excel: 'symbol' column or first column. TXT: one per line.")
        if scan_f and up:
            st.session_state["_guru_pending"] = up.name
            st.session_state["_guru_is_file"] = True
            symbols = _parser.parse_file(up)
            if not symbols:
                st.warning("⚠️ No valid symbols found in file.")

    if symbols:
        scan_name = st.session_state.pop("_guru_pending", "GURU BUY Scan")
        is_file   = st.session_state.pop("_guru_is_file", False)
        st.markdown(
            f"**Calculating GURU BUY for {len(symbols)} symbol(s):** "
            f"{', '.join(symbols[:15])}{'…' if len(symbols) > 15 else ''}"
        )
        _do_guru_buy_fetch(symbols, scan_name, ref_date=ref_date_val, save_to_history=True)
        # Snap date picker to actual trading day (may differ when ref_date is a holiday)
        for _rd in st.session_state.get("guru_data", []):
            if _rd.ref_date and not _rd.error:
                try:
                    from datetime import date as _dp
                    _actual = _dp.fromisoformat(_rd.ref_date)
                    if _actual != ref_date_val:
                        st.session_state["guru_date_picker"] = _actual
                        st.rerun()
                except Exception:
                    pass
                break

    if st.session_state.get("guru_do_refresh"):
        st.session_state.guru_do_refresh = False
        syms = st.session_state.get("guru_last_scan_symbols", [])
        if not syms:
            syms = [d.symbol for d in st.session_state.get("guru_data", [])]
        if syms:
            _do_guru_buy_fetch(syms, st.session_state.get("guru_active_scan_name") or "GURU BUY",
                               ref_date=ref_date_val, save_to_history=True)
            # Snap date picker to actual trading day
            for _rd in st.session_state.get("guru_data", []):
                if _rd.ref_date and not _rd.error:
                    try:
                        from datetime import date as _dp2
                        _actual2 = _dp2.fromisoformat(_rd.ref_date)
                        if _actual2 != ref_date_val:
                            st.session_state["guru_date_picker"] = _actual2
                            st.rerun()
                    except Exception:
                        pass
                    break

    data = st.session_state.get("guru_data", [])
    if not data:
        return
    if len(data) == 1:
        _render_guru_buy_card(data[0])
    else:
        _render_guru_buy_table(data)


def _render_guru_buy_card(d):
    dark = st.session_state.dark_mode
    card_bg  = "#161b22" if dark else "#ffffff"
    border_c = "#30363d" if dark else "#dde3ec"
    label_c  = "#8b949e" if dark else "#6b7280"
    val_c    = "#c9d1d9" if dark else "#111827"
    name_c   = "#58a6ff" if dark else "#1f4e79"

    if d.error:
        st.error(f"❌ Could not fetch GURU BUY for **{d.symbol}**: {d.error}")
    else:
        abbr = "".join(w[0] for w in d.symbol.split()[:3]).upper()[:3]
        price_str = f"₹ {d.current_price:,.2f}" if d.current_price else "—"
        cnt = d.buy_count or 0
        badge = _guru_badge(d.guru_signal)

        st.markdown(f"""
        <div style="background:{card_bg};border:1px solid {border_c};border-radius:16px;
                    padding:1.5rem 2rem;margin-bottom:1rem;">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:1rem;">
            <div style="display:flex;align-items:center;gap:1rem;">
              <div style="width:56px;height:56px;border-radius:12px;background:#1f4e79;
                          display:flex;align-items:center;justify-content:center;
                          color:white;font-weight:700;font-size:1rem;">{abbr}</div>
              <div>
                <div style="font-size:1.6rem;font-weight:700;color:{name_c};">{d.company_name or d.symbol}</div>
                <div style="color:{label_c};font-size:0.85rem;margin-top:4px;">
                  NSE: {d.symbol} &nbsp;·&nbsp; Ref Date: {d.ref_date or '—'}
                </div>
              </div>
            </div>
            <div style="text-align:right;">
              <div style="font-size:1.8rem;font-weight:700;color:{val_c};">{price_str}</div>
              <div style="font-size:1.1rem;font-weight:700;margin-top:4px;">{badge} &nbsp;({cnt}/7)</div>
            </div>
          </div>
        </div>
        """, unsafe_allow_html=True)

        def rainbow_cell(label, val, is_buy):
            if val is None:
                bg, ind, ic = ("#374151" if dark else "#f3f4f6"), "—", label_c
            elif is_buy:
                bg, ind, ic = ("#052e16" if dark else "#f0fdf4"), "✅ BUY", "#16a34a"
            else:
                bg, ind, ic = ("#450a0a" if dark else "#fff1f2"), "❌ SELL", "#dc2626"
            vt = f"₹{val:,.2f}" if val is not None else "—"
            return f"""
            <div style="background:{bg};border:1px solid {border_c};border-radius:10px;
                        padding:0.75rem 1rem;">
              <div style="font-size:0.76rem;color:{label_c};margin-bottom:3px;">{label}</div>
              <div style="font-size:1rem;font-weight:600;color:{val_c};">
                {vt}
              </div>
              <div style="font-size:0.82rem;font-weight:700;color:{ic};margin-top:2px;">{ind}</div>
            </div>"""

        def row3(*cells):
            return ('<div style="display:grid;grid-template-columns:repeat(3,1fr);'
                    'gap:0.5rem;margin-bottom:0.5rem;">' + "".join(cells) + "</div>")

        def hdr(t):
            return f'<p style="color:{label_c};font-size:0.8rem;font-weight:600;margin:0.8rem 0 0.4rem;">{t}</p>'

        # Signal descriptions for each line
        grid  = hdr("Rainbow EMA Lines — value &amp; buy/sell signal")
        grid += row3(
            rainbow_cell("EMA   (line 1 — fastest)\nGreen if EMA ≥ EMAD1",    d.ema,   d.sig_ema),
            rainbow_cell("EMAD1 (line 2)\nGreen if EMAD1 ≥ EMAD2",           d.emad1, d.sig_d1),
            rainbow_cell("EMAD2 (line 3)\nGreen if EMAD2 ≥ EMAD3",           d.emad2, d.sig_d2),
        )
        grid += row3(
            rainbow_cell("EMAD3 (line 4)\nGreen if EMAD3 ≥ EMAD4",           d.emad3, d.sig_d3),
            rainbow_cell("EMAD4 (line 5)\nGreen if EMAD4 ≥ EMAD5",           d.emad4, d.sig_d4),
            rainbow_cell("EMAD5 (line 6)\nGreen if EMAD5 ≥ EMAD6",           d.emad5, d.sig_d5),
        )
        grid += row3(
            rainbow_cell("EMAD6 (line 7 — slowest)\nGreen if EMA ≥ EMAD6",   d.emad6, d.sig_d6),
            "", "",
        )
        st.markdown(grid, unsafe_allow_html=True)

    st.markdown("---")
    nse_sym = d.symbol
    st.markdown("**🔗 Open in:**")
    bc1, bc2, bc3, bc4, bc5 = st.columns(5)
    with bc1:
        st.link_button("📈 NSE Chart",
                       f"https://www.nseindia.com/get-quotes/equity?symbol={nse_sym}",
                       width='stretch', type="primary")
    with bc2:
        st.link_button("📊 TradingView",
                       f"https://in.tradingview.com/chart/?symbol=NSE%3A{nse_sym}&interval=D",
                       width='stretch')
    with bc3:
        st.link_button("🔍 Screener.in",
                       f"https://www.screener.in/company/{nse_sym}/",
                       width='stretch')
    with bc4:
        if st.button("🔄 Refresh", width='stretch', type="primary", key="guru_refresh_card"):
            st.session_state.guru_do_refresh = True
            st.rerun()
    with bc5:
        _live_btn("guru", "guru_do_refresh", "guru_live_card")


def _render_guru_zone_table(items: list, zone_key: str):
    """Renders a filtered zone sub-table with its own export, navigation, and Refresh."""
    rows = []
    for d in items:
        rows.append({
            "Symbol":    d.symbol,
            "Company":   d.company_name or d.symbol,
            "Ref Date":  d.ref_date or "—",
            "Price (₹)": d.current_price,
            "EMA":       d.ema,
            "EMAD1":     d.emad1,
            "EMAD2":     d.emad2,
            "EMAD3":     d.emad3,
            "EMAD4":     d.emad4,
            "EMAD5":     d.emad5,
            "EMAD6":     d.emad6,
            "EMA Sig":   _sig_label(d.sig_ema)  if d.sig_ema  is not None else "—",
            "D1 Sig":    _sig_label(d.sig_d1)   if d.sig_d1   is not None else "—",
            "D2 Sig":    _sig_label(d.sig_d2)   if d.sig_d2   is not None else "—",
            "D3 Sig":    _sig_label(d.sig_d3)   if d.sig_d3   is not None else "—",
            "D4 Sig":    _sig_label(d.sig_d4)   if d.sig_d4   is not None else "—",
            "D5 Sig":    _sig_label(d.sig_d5)   if d.sig_d5   is not None else "—",
            "D6 Sig":    _sig_label(d.sig_d6)   if d.sig_d6   is not None else "—",
            "GURU BUY":  f"{_guru_badge(d.guru_signal)} ({d.buy_count}/7)" if d.buy_count is not None else "—",
        })

    zdf = pd.DataFrame(rows)

    # Export
    ec1, ec2, *_ = st.columns([1, 1, 4])
    with ec1:
        st.download_button(
            "📥 CSV", zdf.to_csv(index=False).encode(),
            f"guru_{zone_key}_{_now_ist().strftime('%Y%m%d_%H%M')}.csv", "text/csv",
            key=f"{zone_key}_csv_dl",
        )
    with ec2:
        try:
            import io as _io
            _buf = _io.BytesIO()
            zdf.to_excel(_buf, index=False, engine="openpyxl")
            st.download_button("📥 Excel", _buf.getvalue(),
                               f"guru_{zone_key}_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx",
                               key=f"{zone_key}_xls_dl")
        except Exception:
            pass

    num_col = st.column_config.NumberColumn
    txt_col = st.column_config.TextColumn
    zdf = _df_filter_sort(zdf, f"guru_zone_{zone_key}")
    zevt = st.dataframe(
        zdf, width="stretch", hide_index=True,
        on_select="rerun", selection_mode="single-row",
        key=f"{zone_key}_df",
        column_config={
            "Symbol":    txt_col("Symbol",   width="small"),
            "Company":   txt_col("Company",  width="medium"),
            "Ref Date":  txt_col("Ref Date", width="small"),
            "Price (₹)": num_col("Price (₹)", format="₹%.2f"),
            "EMA":       num_col("EMA",   format="₹%.2f"),
            "EMAD1":     num_col("EMAD1", format="₹%.2f"),
            "EMAD2":     num_col("EMAD2", format="₹%.2f"),
            "EMAD3":     num_col("EMAD3", format="₹%.2f"),
            "EMAD4":     num_col("EMAD4", format="₹%.2f"),
            "EMAD5":     num_col("EMAD5", format="₹%.2f"),
            "EMAD6":     num_col("EMAD6", format="₹%.2f"),
            "EMA Sig":   txt_col("EMA Sig",  width="small"),
            "D1 Sig":    txt_col("D1 Sig",   width="small"),
            "D2 Sig":    txt_col("D2 Sig",   width="small"),
            "D3 Sig":    txt_col("D3 Sig",   width="small"),
            "D4 Sig":    txt_col("D4 Sig",   width="small"),
            "D5 Sig":    txt_col("D5 Sig",   width="small"),
            "D6 Sig":    txt_col("D6 Sig",   width="small"),
            "GURU BUY":  txt_col("GURU BUY", width="medium"),
        },
    )

    # Row selection → navigation
    sel_rows = zevt.selection.rows if hasattr(zevt, "selection") else []
    sel_sym  = None
    if sel_rows:
        idx = sel_rows[0]
        sel_sym = zdf.iloc[idx]["Symbol"]
        st.info(f"Selected: **{zdf.iloc[idx]['Company']}** ({sel_sym})"
                f"  —  {zdf.iloc[idx]['GURU BUY']}")

    col1, col2, col3, col4 = st.columns([2, 2, 2, 2])
    with col4:
        if st.button("🔄 Refresh", key=f"{zone_key}_refresh", width='stretch', type="primary"):
            st.session_state.guru_do_refresh = True
            st.rerun()
    if sel_sym:
        with col1:
            st.link_button("📈 NSE Chart",
                           f"https://www.nseindia.com/get-quotes/equity?symbol={sel_sym}",
                           width='stretch', type="primary")
        with col2:
            st.link_button("📊 TradingView",
                           f"https://in.tradingview.com/chart/?symbol=NSE%3A{sel_sym}&interval=D",
                           width='stretch')
        with col3:
            st.link_button("🔍 Screener.in",
                           f"https://www.screener.in/company/{sel_sym}/",
                           width='stretch')
    else:
        with col1:
            st.button("📈 NSE Chart",    disabled=True, width='stretch', key=f"{zone_key}_nse_dis")
        with col2:
            st.button("📊 TradingView", disabled=True, width='stretch', key=f"{zone_key}_tv_dis")
        with col3:
            st.button("🔍 Screener.in", disabled=True, width='stretch', key=f"{zone_key}_sc_dis")
        st.caption("⬆️ Click a row in the table above to enable navigation buttons")


def _render_guru_buy_table(guru_list: list):
    rows = []
    for d in guru_list:
        rows.append({
            "Symbol":       d.symbol,
            "Company":      d.company_name or d.symbol,
            "Ref Date":     d.ref_date or "—",
            "Price (₹)":    d.current_price,
            # EMA values
            "EMA":          d.ema,
            "EMAD1":        d.emad1,
            "EMAD2":        d.emad2,
            "EMAD3":        d.emad3,
            "EMAD4":        d.emad4,
            "EMAD5":        d.emad5,
            "EMAD6":        d.emad6,
            # Signal per line
            "EMA Sig":      _sig_label(d.sig_ema)  if d.sig_ema  is not None else "—",
            "D1 Sig":       _sig_label(d.sig_d1)   if d.sig_d1   is not None else "—",
            "D2 Sig":       _sig_label(d.sig_d2)   if d.sig_d2   is not None else "—",
            "D3 Sig":       _sig_label(d.sig_d3)   if d.sig_d3   is not None else "—",
            "D4 Sig":       _sig_label(d.sig_d4)   if d.sig_d4   is not None else "—",
            "D5 Sig":       _sig_label(d.sig_d5)   if d.sig_d5   is not None else "—",
            "D6 Sig":       _sig_label(d.sig_d6)   if d.sig_d6   is not None else "—",
            # Overall
            "GURU BUY":     f"{_guru_badge(d.guru_signal)} ({d.buy_count}/7)" if d.buy_count is not None else "—",
        })
        if d.error:
            rows[-1]["Error"] = d.error

    df = pd.DataFrame(rows)
    st.markdown("---")

    h1, h2, h3, h4 = st.columns([3, 2, 1, 1])
    with h1:
        st.markdown(f"### 🟢 GURU BUY Results — {len(df)} stock(s)")
    with h2:
        ts = st.session_state.get("guru_last_fetched_at")
        if ts:
            hour = ts.hour % 12 or 12
            ampm = "a.m." if ts.hour < 12 else "p.m."
            ts_str = f"{ts.day} {ts.strftime('%b %Y')}, {hour}:{ts.strftime('%M')} {ampm}"
            st.markdown(
                f"<p style='margin-top:0.6rem;color:gray;font-size:0.85rem;'>🕐 Fetched: {ts_str}</p>",
                unsafe_allow_html=True,
            )
    with h3:
        if st.button("🔄 Refresh", key="guru_refresh_tbl", width='stretch', type="primary"):
            st.session_state.guru_do_refresh = True
            st.rerun()
    with h4:
        _live_btn("guru", "guru_do_refresh", "guru_live_table")

    # Summary
    retrieved = df["Price (₹)"].notna().sum()
    strong_buy = df["GURU BUY"].str.contains("STRONG BUY", na=False).sum()
    buy_total  = df["GURU BUY"].str.contains("BUY", na=False).sum()
    ref_dates  = df["Ref Date"].dropna()
    latest_ref = ref_dates.iloc[0] if not ref_dates.empty else "—"
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Stocks Scanned",   len(df))
    c2.metric("Data Retrieved",   int(retrieved))
    c3.metric("🟢 BUY signals",   int(buy_total))
    c4.metric("Trading Day",      latest_ref, help=f"Trading day used for analysis: {latest_ref}")

    st.markdown("---")

    # Export
    ec1, ec2, *_ = st.columns([1, 1, 4])
    with ec1:
        st.download_button(
            "📥 CSV", df.to_csv(index=False).encode(),
            f"guru_buy_{_now_ist().strftime('%Y%m%d_%H%M')}.csv", "text/csv",
            key="guru_csv_dl",
        )
    with ec2:
        try:
            import io
            buf = io.BytesIO()
            df.to_excel(buf, index=False, engine="openpyxl")
            st.download_button("📥 Excel", buf.getvalue(),
                               f"guru_buy_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx",
                               key="guru_xls_dl")
        except Exception:
            pass

    num_col = st.column_config.NumberColumn
    txt_col = st.column_config.TextColumn
    df = _df_filter_sort(df, "guru_buy")
    evt = st.dataframe(
        df, width="stretch", hide_index=True,
        on_select="rerun", selection_mode="single-row",
        column_config={
            "Symbol":    txt_col("Symbol",   width="small"),
            "Company":   txt_col("Company",  width="medium"),
            "Ref Date":  txt_col("Ref Date", width="small"),
            "Price (₹)": num_col("Price (₹)", format="₹%.2f"),
            "EMA":       num_col("EMA",   format="₹%.2f"),
            "EMAD1":     num_col("EMAD1", format="₹%.2f"),
            "EMAD2":     num_col("EMAD2", format="₹%.2f"),
            "EMAD3":     num_col("EMAD3", format="₹%.2f"),
            "EMAD4":     num_col("EMAD4", format="₹%.2f"),
            "EMAD5":     num_col("EMAD5", format="₹%.2f"),
            "EMAD6":     num_col("EMAD6", format="₹%.2f"),
            "EMA Sig":   txt_col("EMA Sig",  width="small"),
            "D1 Sig":    txt_col("D1 Sig",   width="small"),
            "D2 Sig":    txt_col("D2 Sig",   width="small"),
            "D3 Sig":    txt_col("D3 Sig",   width="small"),
            "D4 Sig":    txt_col("D4 Sig",   width="small"),
            "D5 Sig":    txt_col("D5 Sig",   width="small"),
            "D6 Sig":    txt_col("D6 Sig",   width="small"),
            "GURU BUY":  txt_col("GURU BUY", width="medium"),
        },
    )

    # Row selection → navigation
    selected_rows = evt.selection.rows if hasattr(evt, "selection") else []
    selected_sym = None
    if selected_rows:
        idx = selected_rows[0]
        selected_sym = df.iloc[idx]["Symbol"]
        st.info(f"Selected: **{df.iloc[idx]['Company']}** ({selected_sym})"
                f"  —  {df.iloc[idx]['GURU BUY']}")

    col1, col2, col3, col4 = st.columns([2, 2, 2, 2])
    with col4:
        if st.button("🗑️ Clear Results", key="guru_clr"):
            st.session_state.guru_data = []
            st.rerun()
    if selected_sym:
        with col1:
            st.link_button("📈 NSE Chart",
                           f"https://www.nseindia.com/get-quotes/equity?symbol={selected_sym}",
                           width='stretch', type="primary")
        with col2:
            st.link_button("📊 TradingView",
                           f"https://in.tradingview.com/chart/?symbol=NSE%3A{selected_sym}&interval=D",
                           width='stretch')
        with col3:
            st.link_button("🔍 Screener.in",
                           f"https://www.screener.in/company/{selected_sym}/",
                           width='stretch')
    else:
        with col1:
            st.button("📈 NSE Chart",    disabled=True, width='stretch', key="guru_nse_dis")
        with col2:
            st.button("📊 TradingView", disabled=True, width='stretch', key="guru_tv_dis")
        with col3:
            st.button("🔍 Screener.in", disabled=True, width='stretch', key="guru_sc_dis")
        st.caption("⬆️ Click a row in the table above to enable navigation buttons")

    # ── Buying Zone ───────────────────────────────────────────────────────────
    buying_zone = [d for d in guru_list if d.sig_ema is True and d.sig_d1 is True]
    with st.expander(
        f"🟢 Buying Zone Stocks — {len(buying_zone)} stock(s)  (EMA Sig ✅ AND D1 Sig ✅)",
        expanded=True,
    ):
        if buying_zone:
            _render_guru_zone_table(buying_zone, zone_key="bz")
        else:
            st.info("No stocks currently in the Buying Zone (need EMA Sig ✅ AND D1 Sig ✅).")

    # ── Selling Zone ──────────────────────────────────────────────────────────
    selling_zone = [d for d in guru_list if d.sig_d5 is False and d.sig_d6 is False]
    with st.expander(
        f"🔴 Selling Zone Stocks — {len(selling_zone)} stock(s)  (D5 Sig ❌ AND D6 Sig ❌)",
        expanded=True,
    ):
        if selling_zone:
            _render_guru_zone_table(selling_zone, zone_key="sz")
        else:
            st.info("No stocks currently in the Selling Zone (need D5 Sig ❌ AND D6 Sig ❌).")


# ── SMC Premium / Discount Zones ─────────────────────────────────────────────

_ZONE_EMOJI = {
    "Premium":      "🔴",
    "Near Premium": "🟠",
    "Equilibrium":  "🟡",
    "Near Discount":"🟢",
    "Discount":     "💚",
}

_ZONE_DESC = {
    "Premium":      "Price is in the top 5 % of the swing range — expensive territory.",
    "Near Premium": "Price is in the upper half of the swing range — above equilibrium.",
    "Equilibrium":  "Price is near the midpoint of the swing range — fair value.",
    "Near Discount":"Price is in the lower half of the swing range — below equilibrium.",
    "Discount":     "Price is in the bottom 5 % of the swing range — discounted territory.",
}


def _zone_badge(zone: str) -> str:
    return f"{_ZONE_EMOJI.get(zone, '—')} {zone}" if zone else "—"


def _reach_label(val) -> str:
    if val is True:  return "✅ Yes"
    if val is False: return "❌ No"
    return "—"


def _fvg_label(val) -> str:
    if val == "Bullish": return "✅ Bullish"
    if val == "Bearish": return "⚠️ Bearish"
    return "❌ None"


def _render_smc_analysis():
    from datetime import date as _date
    _auto_restore_scan("smc_data", _load_smc_session, "smc")

    today = _date.today()
    if st.session_state.pop("_live_pending_smc", False):
        st.session_state["smc_date_picker"] = today
    dcol1, dcol2 = st.columns([2, 5])
    with dcol1:
        saved_date = st.session_state.get("smc_ref_date") or today
        ref_date_val = st.date_input(
            "Reference Date",
            value=saved_date,
            max_value=today,
            help="Zones are calculated as of this date. Weekends / market holidays auto-roll back to the previous trading day.",
            key="smc_date_picker",
        )
        st.session_state.smc_ref_date = ref_date_val
    with dcol2:
        st.markdown(
            "<p style='color:gray;font-size:0.85rem;margin-top:1.9rem;'>"
            "Implements the LuxAlgo <b>Smart Money Concepts</b> Premium &amp; Discount Zones logic. "
            "Swing size = 50 bars (default). Weekends / holidays auto-roll back.</p>",
            unsafe_allow_html=True,
        )

    st.markdown("---")

    tab_manual, tab_file = st.tabs(["📝 Enter Symbols", "📁 Upload File"])
    symbols: list = []

    with tab_manual:
        txt = st.text_area(
            "Stock symbols",
            placeholder="Single stock:  TCS\nMultiple:  RELIANCE, INFY, TCS\nOne per line also works",
            height=100,
            label_visibility="collapsed",
            key="smc_manual_input",
        )
        c1, c2 = st.columns([1, 5])
        with c1:
            scan_btn = st.button("🔍 Scan", type="primary", width='stretch', key="smc_scan_m")
        with c2:
            st.caption("Use NSE symbols. Data fetched from Yahoo Finance — same source as the LuxAlgo SMC indicator.")
        if scan_btn and txt.strip():
            st.session_state["_smc_pending_scan_name"] = f"SMC — {_now_ist().strftime('%d %b %H:%M')}"
            st.session_state["_smc_is_file_upload"] = False
            symbols = _parser.parse_text_input(txt)

    with tab_file:
        up = st.file_uploader(
            "Upload CSV / Excel / TXT", type=["csv", "xlsx", "xls", "txt"],
            label_visibility="collapsed", key="smc_file_uploader",
        )
        c1, c2, c3 = st.columns([1, 1, 4])
        with c1:
            scan_f = st.button("🔍 Scan File", type="primary", width='stretch', key="smc_scan_f")
        with c2:
            sample = pd.DataFrame({"symbol": ["RELIANCE", "INFY", "TCS", "HDFCBANK", "WIPRO"]})
            st.download_button("📥 Sample", sample.to_csv(index=False).encode(),
                               "sample_stocks.csv", "text/csv", key="smc_sample_dl")
        with c3:
            st.caption("CSV/Excel: 'symbol' column or first column. TXT: one per line.")
        if scan_f and up:
            st.session_state["_smc_pending_scan_name"] = up.name
            st.session_state["_smc_is_file_upload"] = True
            symbols = _parser.parse_file(up)
            if not symbols:
                st.warning("⚠️ No valid symbols found in file.")

    if symbols:
        scan_name = st.session_state.pop("_smc_pending_scan_name", "SMC Scan")
        is_file = st.session_state.pop("_smc_is_file_upload", False)
        st.markdown(
            f"**Calculating SMC zones for {len(symbols)} symbol(s):** "
            f"{', '.join(symbols[:15])}{'…' if len(symbols) > 15 else ''}"
        )
        _do_smc_fetch(symbols, scan_name, ref_date=ref_date_val, save_to_history=True)
        # Snap date picker to the actual trading day used (may differ when ref_date is a holiday)
        for _rd in st.session_state.get("smc_data", []):
            if _rd.ref_date and not _rd.error:
                try:
                    from datetime import date as _dp
                    _actual = _dp.fromisoformat(_rd.ref_date)
                    if _actual != ref_date_val:
                        st.session_state["smc_date_picker"] = _actual
                        st.rerun()
                except Exception:
                    pass
                break

    if st.session_state.get("smc_do_refresh"):
        st.session_state.smc_do_refresh = False
        syms = st.session_state.get("smc_last_scan_symbols", [])
        if not syms:
            syms = [d.symbol for d in st.session_state.get("smc_data", [])]
        if syms:
            scan_name = st.session_state.get("smc_active_scan_name") or "SMC Refresh"
            st.markdown(f"**Refreshing SMC zones for {len(syms)} symbol(s)…**")
            _do_smc_fetch(syms, scan_name, ref_date=ref_date_val, save_to_history=True)
            # Snap date picker to the actual trading day used
            for _rd in st.session_state.get("smc_data", []):
                if _rd.ref_date and not _rd.error:
                    try:
                        from datetime import date as _dp2
                        _actual2 = _dp2.fromisoformat(_rd.ref_date)
                        if _actual2 != ref_date_val:
                            st.session_state["smc_date_picker"] = _actual2
                            st.rerun()
                    except Exception:
                        pass
                    break

    data = st.session_state.get("smc_data", [])
    if not data:
        return

    if len(data) == 1:
        _render_smc_card(data[0])
    else:
        _render_smc_table(data)


def _render_smc_card(d):
    dark = st.session_state.dark_mode
    card_bg  = "#161b22" if dark else "#ffffff"
    border_c = "#30363d" if dark else "#dde3ec"
    label_c  = "#8b949e" if dark else "#6b7280"
    val_c    = "#c9d1d9" if dark else "#111827"
    name_c   = "#58a6ff" if dark else "#1f4e79"

    if d.error:
        st.error(f"❌ Could not calculate SMC zones for **{d.symbol}**: {d.error}")
    else:
        abbr = "".join(w[0] for w in d.symbol.split()[:3]).upper()[:3]
        price_str = f"₹ {d.current_price:,.2f}" if d.current_price else "—"
        badge = _zone_badge(d.zone)
        pct_str = f"{d.zone_pct:.1f} %" if d.zone_pct is not None else "—"
        reach_str = _reach_label(d.reached_discount)
        reach_color = "#16a34a" if d.reached_discount else ("#dc2626" if d.reached_discount is False else "#888")

        # Zone colours for the visual bar
        zone_colors = {
            "Premium":       "#F23645",
            "Near Premium":  "#FF7849",
            "Equilibrium":   "#878b94",
            "Near Discount": "#089981",
            "Discount":      "#16a34a",
        }
        zone_color = zone_colors.get(d.zone, "#888")

        st.markdown(f"""
        <div style="background:{card_bg};border:1px solid {border_c};border-radius:16px;
                    padding:1.5rem 2rem;margin-bottom:1rem;">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:1rem;">
            <div style="display:flex;align-items:center;gap:1rem;">
              <div style="width:56px;height:56px;border-radius:12px;background:#1f4e79;
                          display:flex;align-items:center;justify-content:center;
                          color:white;font-weight:700;font-size:1rem;flex-shrink:0;">{abbr}</div>
              <div>
                <div style="font-size:1.6rem;font-weight:700;color:{name_c};line-height:1.2;">
                  {d.company_name or d.symbol}
                </div>
                <div style="color:{label_c};font-size:0.85rem;margin-top:4px;">
                  NSE: {d.symbol} &nbsp;·&nbsp; Ref Date: {d.ref_date or '—'}
                </div>
              </div>
            </div>
            <div style="text-align:right;">
              <div style="font-size:1.8rem;font-weight:700;color:{val_c};">{price_str}</div>
              <div style="font-size:1.1rem;font-weight:700;color:{zone_color};margin-top:4px;">
                {badge}
              </div>
              <div style="font-size:0.95rem;font-weight:700;color:{reach_color};margin-top:4px;">
                Reached Discount: {reach_str}
              </div>
              <div style="font-size:0.85rem;color:{label_c};margin-top:2px;">
                Position in range: {pct_str}
              </div>
            </div>
          </div>
        </div>
        """, unsafe_allow_html=True)

        # Zone description
        st.info(f"**{badge}** — {_ZONE_DESC.get(d.zone, '')}")

        # Visual zone bar
        pct = d.zone_pct or 0.0
        bar_pct = max(0.0, min(100.0, pct))
        st.markdown(
            f"<div style='margin:0.5rem 0 1rem;'>"
            f"  <div style='display:flex;justify-content:space-between;font-size:0.75rem;color:{label_c};margin-bottom:4px;'>"
            f"    <span>Discount</span><span>Equilibrium</span><span>Premium</span>"
            f"  </div>"
            f"  <div style='background:#374151;border-radius:8px;height:16px;overflow:hidden;'>"
            f"    <div style='height:100%;width:{bar_pct:.1f}%;background:{zone_color};border-radius:8px;'></div>"
            f"  </div>"
            f"  <div style='display:flex;justify-content:space-between;font-size:0.75rem;color:{label_c};margin-top:3px;'>"
            f"    <span>₹{d.swing_low:,.2f}</span><span>₹{(d.swing_high+d.swing_low)/2:,.2f}</span><span>₹{d.swing_high:,.2f}</span>"
            f"  </div>"
            f"</div>",
            unsafe_allow_html=True,
        )

        # Zone boundary grid
        def zone_cell(label, price, is_current_zone=False):
            bg = zone_colors.get(d.zone, "#888") if is_current_zone else (
                "#374151" if dark else "#f3f4f6"
            )
            txt_color = "#ffffff" if is_current_zone else val_c
            price_txt = f"₹{price:,.2f}" if price is not None else "—"
            return f"""
            <div style="background:{bg};border:1px solid {border_c};border-radius:10px;
                        padding:0.75rem 1rem;">
              <div style="font-size:0.76rem;color:{'#ffffff' if is_current_zone else label_c};margin-bottom:3px;">{label}</div>
              <div style="font-size:1rem;font-weight:600;color:{txt_color};">{price_txt}</div>
            </div>"""

        def row3(*cells):
            return ('<div style="display:grid;grid-template-columns:repeat(3,1fr);'
                    'gap:0.5rem;margin-bottom:0.5rem;">' + "".join(cells) + "</div>")

        def hdr(t):
            return f'<p style="color:{label_c};font-size:0.8rem;font-weight:600;margin:0.8rem 0 0.4rem;">{t}</p>'

        grid  = hdr("Zone Boundaries (LuxAlgo SMC)")
        grid += row3(
            zone_cell("Swing High (top)", d.swing_high, d.zone == "Premium"),
            zone_cell("Premium Bottom   (95%)", d.premium_bottom, d.zone == "Premium"),
            zone_cell("Near Premium Top (52.5%)", d.eq_top, d.zone == "Near Premium"),
        )
        grid += row3(
            zone_cell("Equilibrium Top  (52.5%)", d.eq_top,    d.zone == "Equilibrium"),
            zone_cell("Equilibrium Bottom (47.5%)", d.eq_bottom, d.zone == "Equilibrium"),
            zone_cell("Near Discount Bottom (47.5%)", d.eq_bottom, d.zone == "Near Discount"),
        )
        grid += row3(
            zone_cell("Discount Top     (5%)", d.discount_top, d.zone == "Discount"),
            zone_cell("Swing Low (bottom)", d.swing_low, d.zone == "Discount"),
            zone_cell("Current Price (Close)", d.current_price),
        )
        grid += hdr(f"Reference-Date Candle  ({d.ref_date})")
        # Highlight day_low cell in green if it reached the discount zone
        disc_low = d.reached_discount is True
        grid += row3(
            zone_cell("Day Open",  d.day_open),
            zone_cell("Day High",  d.day_high),
            zone_cell("Day Low  ← Discount check", d.day_low, disc_low),
        )
        st.markdown(grid, unsafe_allow_html=True)

        # Explicit reached-discount callout
        if d.reached_discount is True:
            st.success(
                f"✅ **Reached Discount Zone on {d.ref_date}** — "
                f"Day Low ₹{d.day_low:,.2f} ≤ Discount boundary ₹{d.discount_top:,.2f}"
            )
        elif d.reached_discount is False:
            st.warning(
                f"❌ **Did NOT reach Discount Zone on {d.ref_date}** — "
                f"Day Low ₹{d.day_low:,.2f} > Discount boundary ₹{d.discount_top:,.2f}"
            )

    st.markdown("---")
    nse_sym = d.symbol
    st.markdown("**🔗 Open in:**")
    bc1, bc2, bc3, bc4, bc5 = st.columns(5)
    with bc1:
        st.link_button("📈 NSE Chart",
                       f"https://www.nseindia.com/get-quotes/equity?symbol={nse_sym}",
                       width='stretch', type="primary")
    with bc2:
        st.link_button("📊 TradingView",
                       f"https://in.tradingview.com/chart/?symbol=NSE%3A{nse_sym}&interval=D",
                       width='stretch')
    with bc3:
        st.link_button("🔍 Screener.in",
                       f"https://www.screener.in/company/{nse_sym}/",
                       width='stretch')
    with bc4:
        if st.button("🔄 Refresh SMC", width='stretch', type="primary",
                     key="smc_refresh_card"):
            st.session_state.smc_do_refresh = True
            st.rerun()
    with bc5:
        _live_btn("smc", "smc_do_refresh", "smc_live_card")


def _render_smc_table(smc_list: list):
    rows = []
    for d in smc_list:
        rows.append({
            "Symbol":              d.symbol,
            "Company":             d.company_name or d.symbol,
            "Close (₹)":           d.day_close,
            "Price (₹)":           d.current_price,
            "Zone":                _zone_badge(d.zone) if d.zone else ("❌ " + (d.error or "Error")),
            "Zone %":              d.zone_pct,
            "Reached Discount?":   _reach_label(d.reached_discount),
            "FVG?":                _fvg_label(d.fvg),
            "Day Low":             d.day_low,
            "Discount ↑":          d.discount_top,
            "Swing High":          d.swing_high,
            "SH Date":             getattr(d, "swing_high_date", None) or "",
            "Swing Low":           d.swing_low,
            "SL Date":             getattr(d, "swing_low_date", None) or "",
            "Premium ↓":           d.premium_bottom,
            "Eq Top":              d.eq_top,
            "Eq Bottom":           d.eq_bottom,
        })
        if d.error:
            rows[-1]["Error"] = d.error

    df = pd.DataFrame(rows)

    # ── 🚨 MUST BUY / ACCUMULATE Alert ───────────────────────────────────────
    alert_stocks = [d for d in smc_list if d.buy_signal]
    if alert_stocks:
        dark = st.session_state.get("dark_mode", False)
        alert_bg     = "#052e16" if dark else "#f0fdf4"
        alert_border = "#16a34a"
        alert_text   = "#bbf7d0" if dark else "#14532d"
        symbols_str  = "  |  ".join(
            f"**{d.symbol}** ({d.company_name or d.symbol})" for d in alert_stocks
        )
        st.markdown(
            f"""
<div style="background:{alert_bg};border:2px solid {alert_border};border-radius:12px;
            padding:1rem 1.4rem;margin-bottom:1rem;">
  <div style="font-size:1.3rem;font-weight:800;color:{alert_border};margin-bottom:0.4rem;">
    🚨 MUST BUY / START ACCUMULATE — {len(alert_stocks)} stock(s) flagged
  </div>
  <div style="color:{alert_text};font-size:0.9rem;line-height:1.7;">
    Criteria: <b>Discount / Near Discount Zone</b> (bottom 47.5% of swing range) + <b>Bullish FVG</b> (gap-up imbalance) on the reference date.<br>
    {symbols_str}
  </div>
</div>
""",
            unsafe_allow_html=True,
        )

        # Alert detail table
        num_col_a = st.column_config.NumberColumn
        txt_col_a = st.column_config.TextColumn
        alert_rows = [{
            "Symbol":          d.symbol,
            "Company":         d.company_name or d.symbol,
            "Close (₹)":       d.day_close,
            "Zone":            _zone_badge(d.zone),
            "Zone %":          d.zone_pct,
            "FVG?":            _fvg_label(d.fvg),
            "Reached Disc?":   _reach_label(d.reached_discount),
            "Day Low":         d.day_low,
            "Discount ↑":      d.discount_top,
            "Swing Low":       d.swing_low,
        } for d in alert_stocks]
        st.dataframe(
            pd.DataFrame(alert_rows),
            width="stretch", hide_index=True,
            key="smc_alert_df",
            column_config={
                "Symbol":        txt_col_a("Symbol",      width="small"),
                "Company":       txt_col_a("Company",     width="medium"),
                "Close (₹)":     num_col_a("Close (₹)",   format="₹%.2f"),
                "Zone":          txt_col_a("Zone",         width="medium"),
                "Zone %":        num_col_a("Zone %",       format="%.1f %%"),
                "FVG?":          txt_col_a("FVG?",         width="small"),
                "Reached Disc?": txt_col_a("Reached Disc?", width="small"),
                "Day Low":       num_col_a("Day Low",      format="₹%.2f"),
                "Discount ↑":    num_col_a("Discount ↑",   format="₹%.2f"),
                "Swing Low":     num_col_a("Swing Low",    format="₹%.2f"),
            },
        )

    st.markdown("---")

    h1, h2, h3, h4 = st.columns([3, 2, 1, 1])
    with h1:
        st.markdown(f"### 💹 SMC Zone Results — {len(df)} stock(s)")
    with h2:
        ts = st.session_state.get("smc_last_fetched_at")
        if ts:
            hour = ts.hour % 12 or 12
            ampm = "a.m." if ts.hour < 12 else "p.m."
            ts_str = f"{ts.day} {ts.strftime('%b %Y')}, {hour}:{ts.strftime('%M')} {ampm}"
            st.markdown(
                f"<p style='margin-top:0.6rem;color:gray;font-size:0.85rem;'>🕐 Fetched: {ts_str}</p>",
                unsafe_allow_html=True,
            )
    with h3:
        if st.button("🔄 Refresh", key="smc_refresh_tbl", width='stretch', type="primary"):
            st.session_state.smc_do_refresh = True
            st.rerun()
    with h4:
        _live_btn("smc", "smc_do_refresh", "smc_live_table")

    # Summary metrics
    retrieved    = df["Price (₹)"].notna().sum()
    discount_ct  = df["Zone"].str.contains("Discount", na=False).sum()
    premium_ct   = df["Zone"].str.contains("Premium", na=False).sum()
    reached_ct   = (df["Reached Discount?"] == "✅ Yes").sum()
    fvg_ct       = (df["FVG?"] != "❌ None").sum()
    buy_sig_ct   = sum(1 for d in smc_list if d.buy_signal)
    latest_ref   = next((d.ref_date for d in smc_list if d.ref_date and not d.error), "—")
    c1, c2, c3, c4, c5, c6, c7, c8 = st.columns(8)
    c1.metric("Stocks Scanned",      len(df))
    c2.metric("Data Retrieved",      int(retrieved))
    c3.metric("Trading Day",         latest_ref, help=f"Trading day used for analysis: {latest_ref}")
    c4.metric("💚 Discount Zone",    int(discount_ct))
    c5.metric("🔴 Premium Zone",     int(premium_ct))
    c6.metric("✅ Reached Discount", int(reached_ct))
    c7.metric("📊 FVG",              int(fvg_ct))
    c8.metric("🚨 MUST BUY",         int(buy_sig_ct))

    st.markdown("---")

    # Export
    ec1, ec2, *_ = st.columns([1, 1, 4])
    with ec1:
        st.download_button(
            "📥 CSV", df.to_csv(index=False).encode(),
            f"smc_zones_{_now_ist().strftime('%Y%m%d_%H%M')}.csv", "text/csv",
            key="smc_csv_dl",
        )
    with ec2:
        try:
            import io
            buf = io.BytesIO()
            df.to_excel(buf, index=False, engine="openpyxl")
            st.download_button("📥 Excel", buf.getvalue(),
                               f"smc_zones_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx",
                               key="smc_xls_dl")
        except Exception:
            pass

    num_col = st.column_config.NumberColumn
    txt_col = st.column_config.TextColumn
    df = _df_filter_sort(df, "smc")
    evt = st.dataframe(
        df, width="stretch", hide_index=True,
        on_select="rerun", selection_mode="single-row",
        column_config={
            "Symbol":              txt_col("Symbol",   width="small"),
            "Company":             txt_col("Company",  width="medium"),
            "Close (₹)":           num_col("Close (₹)", format="₹%.2f"),
            "Price (₹)":           num_col("Price (₹)", format="₹%.2f"),
            "Zone":                txt_col("Zone",      width="medium"),
            "Zone %":              num_col("Zone %",    format="%.1f %%"),
            "Reached Discount?":   txt_col("Reached Discount?", width="small"),
            "FVG?":                txt_col("FVG?",      width="small"),
            "Day Low":             num_col("Day Low",   format="₹%.2f"),
            "Discount ↑":          num_col("Discount ↑", format="₹%.2f"),
            "Swing High":          num_col("Swing High",  format="₹%.2f"),
            "SH Date":             txt_col("SH Date",     width="small"),
            "Swing Low":           num_col("Swing Low",   format="₹%.2f"),
            "SL Date":             txt_col("SL Date",     width="small"),
            "Premium ↓":           num_col("Premium ↓",   format="₹%.2f"),
            "Eq Top":              num_col("Eq Top",      format="₹%.2f"),
            "Eq Bottom":           num_col("Eq Bottom",   format="₹%.2f"),
        },
    )

    # Row selection → navigation
    selected_rows = evt.selection.rows if hasattr(evt, "selection") else []
    selected_sym = None
    if selected_rows:
        idx = selected_rows[0]
        selected_sym = df.iloc[idx]["Symbol"]
        st.info(f"Selected: **{df.iloc[idx]['Company']}** ({selected_sym})  —  {df.iloc[idx]['Zone']}")

    # Zone filter expanders
    buy_signal_stocks  = [d for d in smc_list if d.buy_signal]
    reached_stocks     = [d for d in smc_list if d.reached_discount is True]
    discount_stocks    = [d for d in smc_list if d.zone == "Discount"]
    near_disc_stocks   = [d for d in smc_list if d.zone == "Near Discount"]
    eq_stocks          = [d for d in smc_list if d.zone == "Equilibrium"]
    near_prem_stocks   = [d for d in smc_list if d.zone == "Near Premium"]
    premium_stocks     = [d for d in smc_list if d.zone == "Premium"]

    def _zone_mini_table(items, key_prefix):
        if not items:
            return
        mini_rows = [{
            "Symbol":              d.symbol,
            "Company":             d.company_name or d.symbol,
            "Close (₹)":           d.day_close,
            "Price (₹)":           d.current_price,
            "Zone":                _zone_badge(d.zone),
            "Zone %":              d.zone_pct,
            "Reached Discount?":   _reach_label(d.reached_discount),
            "FVG?":                _fvg_label(d.fvg),
            "Day Low":             d.day_low,
            "Discount ↑":          d.discount_top,
            "Swing High":          d.swing_high,
            "SH Date":             getattr(d, "swing_high_date", None) or "",
            "Swing Low":           d.swing_low,
            "SL Date":             getattr(d, "swing_low_date", None) or "",
        } for d in items]
        mdf = pd.DataFrame(mini_rows)
        mini_evt = st.dataframe(
            mdf, width="stretch", hide_index=True,
            on_select="rerun", selection_mode="single-row",
            key=f"{key_prefix}_df",
            column_config={
                "Symbol":             txt_col("Symbol",   width="small"),
                "Company":            txt_col("Company",  width="medium"),
                "Close (₹)":          num_col("Close (₹)", format="₹%.2f"),
                "Price (₹)":          num_col("Price (₹)", format="₹%.2f"),
                "Zone":               txt_col("Zone",      width="medium"),
                "Zone %":             num_col("Zone %",    format="%.1f %%"),
                "Reached Discount?":  txt_col("Reached Discount?", width="small"),
                "FVG?":               txt_col("FVG?",      width="small"),
                "Day Low":            num_col("Day Low",   format="₹%.2f"),
                "Discount ↑":         num_col("Discount ↑", format="₹%.2f"),
                "Swing High":         num_col("Swing High", format="₹%.2f"),
                "SH Date":            txt_col("SH Date",    width="small"),
                "Swing Low":          num_col("Swing Low",  format="₹%.2f"),
                "SL Date":            txt_col("SL Date",    width="small"),
            },
        )
        # Row selection navigation
        sel = mini_evt.selection.rows if hasattr(mini_evt, "selection") else []
        sel_sym = mdf.iloc[sel[0]]["Symbol"] if sel else None
        col1, col2, col3, col4 = st.columns([2, 2, 2, 2])
        with col4:
            if st.button("🔄 Refresh", key=f"{key_prefix}_ref", width='stretch', type="primary"):
                st.session_state.smc_do_refresh = True
                st.rerun()
        if sel_sym:
            with col1:
                st.link_button("📈 NSE Chart",
                               f"https://www.nseindia.com/get-quotes/equity?symbol={sel_sym}",
                               width='stretch', type="primary")
            with col2:
                st.link_button("📊 TradingView",
                               f"https://in.tradingview.com/chart/?symbol=NSE%3A{sel_sym}&interval=D",
                               width='stretch')
            with col3:
                st.link_button("🔍 Screener.in",
                               f"https://www.screener.in/company/{sel_sym}/",
                               width='stretch')
        else:
            with col1:
                st.button("📈 NSE Chart",    disabled=True, width='stretch', key=f"{key_prefix}_nse_dis")
            with col2:
                st.button("📊 TradingView", disabled=True, width='stretch', key=f"{key_prefix}_tv_dis")
            with col3:
                st.button("🔍 Screener.in", disabled=True, width='stretch', key=f"{key_prefix}_sc_dis")
            st.caption("⬆️ Click a row above to enable navigation buttons")

    col1_b, col2_b, col3_b, col4_b = st.columns([2, 2, 2, 2])
    with col4_b:
        if st.button("🗑️ Clear Results", key="smc_clr"):
            st.session_state.smc_data = []
            st.rerun()
    if selected_sym:
        with col1_b:
            st.link_button(
                "📈 NSE Chart",
                f"https://www.nseindia.com/get-quotes/equity?symbol={selected_sym}",
                width='stretch', type="primary",
            )
        with col2_b:
            st.link_button(
                "📊 TradingView",
                f"https://in.tradingview.com/chart/?symbol=NSE%3A{selected_sym}&interval=D",
                width='stretch',
            )
        with col3_b:
            st.link_button(
                "🔍 Screener.in",
                f"https://www.screener.in/company/{selected_sym}/",
                width='stretch',
            )
    else:
        with col1_b:
            st.button("📈 NSE Chart",    disabled=True, width='stretch', key="smc_nse_dis")
        with col2_b:
            st.button("📊 TradingView", disabled=True, width='stretch', key="smc_tv_dis")
        with col3_b:
            st.button("🔍 Screener.in", disabled=True, width='stretch', key="smc_sc_dis")
        st.caption("⬆️ Click a row in the table above to enable navigation buttons")

    st.markdown("---")

    with st.expander(f"🚨 MUST BUY / ACCUMULATE — {len(buy_signal_stocks)} stock(s)  (Discount / Near Discount + Bullish FVG)", expanded=bool(buy_signal_stocks)):
        if buy_signal_stocks:
            _zone_mini_table(buy_signal_stocks, "buy")
        else:
            st.info("No stocks currently meet the MUST BUY criteria (Discount / Near Discount zone + Bullish FVG).")

    with st.expander(f"✅ Reached Discount Zone — {len(reached_stocks)} stock(s)  (Day Low ≤ Discount boundary on ref date)", expanded=True):
        if reached_stocks:
            _zone_mini_table(reached_stocks, "rch")
        else:
            st.info("No stocks touched the Discount zone on this trading date.")

    with st.expander(f"💚 Discount Zone — {len(discount_stocks)} stock(s)  (closing price in bottom 5% of swing range)", expanded=False):
        if discount_stocks:
            _zone_mini_table(discount_stocks, "dsc")
        else:
            st.info("No stocks currently in the Discount zone.")

    with st.expander(f"🟢 Near Discount — {len(near_disc_stocks)} stock(s)  (5%–47.5%)", expanded=False):
        if near_disc_stocks:
            _zone_mini_table(near_disc_stocks, "ndsc")
        else:
            st.info("No stocks currently in the Near Discount zone.")

    with st.expander(f"🟡 Equilibrium — {len(eq_stocks)} stock(s)  (47.5%–52.5%)", expanded=False):
        if eq_stocks:
            _zone_mini_table(eq_stocks, "eq")
        else:
            st.info("No stocks currently in the Equilibrium zone.")

    with st.expander(f"🟠 Near Premium — {len(near_prem_stocks)} stock(s)  (52.5%–95%)", expanded=False):
        if near_prem_stocks:
            _zone_mini_table(near_prem_stocks, "nprem")
        else:
            st.info("No stocks currently in the Near Premium zone.")

    with st.expander(f"🔴 Premium Zone — {len(premium_stocks)} stock(s)  (top 5% of swing range)", expanded=False):
        if premium_stocks:
            _zone_mini_table(premium_stocks, "prem")
        else:
            st.info("No stocks currently in the Premium zone.")


# ── My Holdings ───────────────────────────────────────────────────────────────

_ASSET_TABS = [
    ("equity",       "📈 Equity"),
    ("gold_silver",  "🥇 Gold & Silver"),
    ("etf",          "🏦 ETF"),
    ("reits_invits", "🏢 REITs & InvITs"),
]
_THRESHOLD = 3.14   # P&L% above this → Red card; below → Green card


def _holding_pnl(h: dict, live_prices: dict) -> tuple:
    """Return (pnl_amount, pnl_pct, holding_days, is_open, effective_price).

    For SELL rows: realized P&L = (sell_price − buy_price) × sell_qty.
    For open BUY rows: virtual P&L = (live_price − buy_price) × remaining_qty.
    For legacy closed BUY rows (sell_date filled on the buy row): realized P&L.
    """
    from datetime import date as _date
    t_type    = h.get("transaction_type", "BUY")
    buy_price = float(h["buy_price"] or 0)

    if t_type == "SELL":
        sell_qty   = float(h.get("sell_qty") or h.get("buy_qty") or 0)
        sell_price = float(h.get("sell_price") or 0)
        invested   = buy_price * sell_qty
        pnl_amt    = round(sell_price * sell_qty - invested, 2)
        pnl_pct    = round((pnl_amt / invested * 100) if invested else 0, 2)
        try:
            d1   = _date.fromisoformat(h["buy_date"])
            d2   = _date.fromisoformat(h["sell_date"])
            days = (d2 - d1).days
        except Exception:
            days = 0
        return pnl_amt, pnl_pct, days, False, sell_price

    # BUY row — legacy closed (sell_date filled directly on the buy row)
    if h.get("sell_date") and h.get("sell_price") and h.get("sell_qty"):
        s_qty    = float(h["sell_qty"])
        invested = buy_price * s_qty
        sell_val = float(h["sell_price"]) * s_qty
        pnl_amt  = round(sell_val - invested, 2)
        pnl_pct  = round((pnl_amt / invested * 100) if invested else 0, 2)
        try:
            d1   = _date.fromisoformat(h["buy_date"])
            d2   = _date.fromisoformat(h["sell_date"])
            days = (d2 - d1).days
        except Exception:
            days = 0
        return pnl_amt, pnl_pct, days, False, float(h["sell_price"])

    # Open BUY row — virtual P&L
    remaining = float(h.get("remaining_qty") or h.get("buy_qty") or 0)
    sym       = h["symbol"]
    live      = live_prices.get(sym)
    invested  = buy_price * remaining
    try:
        d1   = _date.fromisoformat(h["buy_date"])
        days = (_date.today() - d1).days
    except Exception:
        days = 0
    if live and remaining > 0:
        pnl_amt = round(live * remaining - invested, 2)
        pnl_pct = round((pnl_amt / invested * 100) if invested else 0, 2)
        return pnl_amt, pnl_pct, days, True, live
    return None, None, days, True, None


def _group_open_buys(open_entries: list, live_prices: dict) -> list:
    """Merge multiple open BUY rows for the same symbol into one aggregated tuple."""
    from collections import defaultdict as _dd2
    from datetime import date as _date2

    groups: dict = _dd2(list)
    for entry in open_entries:
        groups[entry[0]["symbol"]].append(entry)

    result = []
    for sym, entries in groups.items():
        total_cost = 0.0
        total_remaining = 0.0
        earliest_date: str = ""
        name = ""

        for h, *_ in entries:
            remaining = float(h.get("remaining_qty") or h.get("buy_qty") or 0)
            total_cost      += float(h["buy_price"]) * remaining
            total_remaining += remaining
            bd = h.get("buy_date", "") or ""
            if bd and (not earliest_date or bd < earliest_date):
                earliest_date = bd
            if not name and h.get("company_name"):
                name = h["company_name"]

        avg_price = (total_cost / total_remaining) if total_remaining > 0 else 0.0
        live      = live_prices.get(sym)

        if live and total_remaining > 0:
            pnl_amt = round(live * total_remaining - total_cost, 2)
            pnl_pct = round((pnl_amt / total_cost * 100) if total_cost else 0.0, 2)
            eff_price = live
        else:
            pnl_amt = pnl_pct = eff_price = None

        try:
            days = (_date2.today() - _date2.fromisoformat(earliest_date)).days
        except Exception:
            days = 0

        merged_h = {
            "symbol":           sym,
            "company_name":     name or sym,
            "transaction_type": "BUY",
            "buy_price":        avg_price,
            "buy_qty":          total_remaining,
            "remaining_qty":    total_remaining,
            "buy_date":         earliest_date,
            "notes":            "",
            "_grouped":         len(entries) > 1,
            "_entry_count":     len(entries),
        }
        result.append((merged_h, pnl_amt, pnl_pct, days, True, eff_price))

    return result


def _holding_card_html(h: dict, pnl_amt, pnl_pct, days: int,
                        is_open: bool, eff_price, dark: bool) -> str:
    t_type  = h.get("transaction_type", "BUY")
    is_sell = (t_type == "SELL")

    positive = (pnl_amt is not None and pnl_amt >= 0)
    above    = (pnl_pct is not None and pnl_pct > _THRESHOLD and not is_sell)

    if is_sell:
        brd = "#16a34a" if positive else "#dc2626"
        bg  = ("rgba(20,83,45,0.35)"  if dark else "rgba(220,252,231,0.6)") if positive else \
              ("rgba(69,10,10,0.35)"  if dark else "rgba(254,226,226,0.6)")
        txt = ("#4ade80" if dark else "#15803d") if positive else ("#f87171" if dark else "#b91c1c")
    elif above:
        brd = "#dc2626"
        bg  = "rgba(69,10,10,0.35)"  if dark else "rgba(254,226,226,0.6)"
        txt = "#f87171"              if dark else "#b91c1c"
    elif pnl_amt is not None and pnl_amt < 0:
        brd = "#dc2626"
        bg  = "rgba(69,10,10,0.35)"  if dark else "rgba(254,226,226,0.6)"
        txt = "#f87171"              if dark else "#b91c1c"
    else:
        brd = "#16a34a"
        bg  = "rgba(20,83,45,0.35)"  if dark else "rgba(220,252,231,0.6)"
        txt = "#4ade80"              if dark else "#15803d"

    lbl = "#94a3b8" if dark else "#64748b"

    if is_sell:
        signal = "✅ Realized Profit" if positive else "❌ Realized Loss"
    else:
        signal = "🔴 Book Profit" if above else ("🟢 Averaging Zone" if pnl_amt is not None else "⏳ Awaiting Price")

    if pnl_amt is not None:
        pnl_str = f"{pnl_amt:+,.2f} ({pnl_pct:+.2f}%)"
    else:
        pnl_str = "Live price needed"

    price_str = f"₹{eff_price:,.2f}" if eff_price else "—"
    sym  = h["symbol"]
    name = h["company_name"] or sym

    if is_sell:
        sell_qty   = float(h.get("sell_qty") or h.get("buy_qty") or 0)
        detail_line = (
            f"Sold: ₹{h['sell_price']:,.2f} × {sell_qty:,.0f}"
            f" &nbsp;·&nbsp; Bought @ ₹{h['buy_price']:,.2f}"
            f" &nbsp;·&nbsp; {h['sell_date']} &nbsp;·&nbsp; {days}d"
        )
        badge = "SELL"
    else:
        remaining = float(h.get("remaining_qty") or h.get("buy_qty") or 0)
        invested  = h["buy_price"] * h["buy_qty"]
        is_grouped = h.get("_grouped", False)
        entry_count = h.get("_entry_count", 1)
        price_label = f"Avg ({entry_count} lots):" if is_grouped else "Bought:"
        date_label  = "Since" if is_grouped else ""
        detail_line = (
            f"{price_label} ₹{h['buy_price']:,.2f} × {h['buy_qty']:,.0f} = ₹{invested:,.2f}"
            f" &nbsp;·&nbsp; {date_label} {h['buy_date']} &nbsp;·&nbsp; {days}d"
            f" &nbsp;·&nbsp; Remaining: {remaining:,.4g}"
        )
        badge = "BUY"

    status = "Realized" if is_sell else ("Open" if is_open else "Closed")

    return f"""
<div style="background:{bg};border:1.5px solid {brd};border-radius:10px;
            padding:0.85rem 1.1rem;margin-bottom:0.6rem;">
  <div style="display:flex;justify-content:space-between;align-items:flex-start;">
    <div>
      <div style="display:flex;align-items:center;gap:0.4rem;">
        <span style="font-size:1.3rem;font-weight:800;color:{brd};">{sym}</span>
        <span style="font-size:0.65rem;font-weight:700;padding:1px 6px;border-radius:4px;
                     background:{brd}22;color:{brd};border:1px solid {brd};">{badge}</span>
      </div>
      <div style="font-size:0.82rem;color:{lbl};">{name}</div>
      <div style="font-size:0.78rem;color:{lbl};margin-top:2px;">{detail_line}</div>
    </div>
    <div style="text-align:right;">
      <div style="font-size:1.1rem;font-weight:800;color:{txt};">₹{pnl_str}</div>
      <div style="font-size:0.82rem;color:{lbl};">{signal} &nbsp;·&nbsp; {status} @ {price_str}</div>
    </div>
  </div>
  <div style="display:flex;gap:0.5rem;margin-top:0.7rem;padding-top:0.6rem;border-top:1px solid {brd}40;">
    <a href="https://www.tradingview.com/chart/?symbol=NSE:{sym}" target="_blank"
       style="display:inline-flex;align-items:center;gap:0.3rem;font-size:0.78rem;font-weight:600;
              color:{brd};background:rgba(255,255,255,0.4);border:1px solid {brd};
              border-radius:6px;padding:0.25rem 0.7rem;text-decoration:none;">
      📈 TradingView
    </a>
    <a href="https://www.nseindia.com/get-quotes/equity?symbol={sym}" target="_blank"
       style="display:inline-flex;align-items:center;gap:0.3rem;font-size:0.78rem;font-weight:600;
              color:{brd};background:rgba(255,255,255,0.4);border:1px solid {brd};
              border-radius:6px;padding:0.25rem 0.7rem;text-decoration:none;">
      🏛️ NSE
    </a>
  </div>
</div>"""


def render_my_holdings():
    st.markdown("## 💼 My Holdings")
    st.markdown(
        "<p style='color:gray;font-size:0.9rem;margin-bottom:0.5rem;'>"
        "Track your portfolio across Equity, Gold &amp; Silver, ETF, REITs &amp; InvITs. "
        "Data saved to your account — persists across sessions.</p>",
        unsafe_allow_html=True,
    )
    tabs = st.tabs([label for _, label in _ASSET_TABS])
    for (atype, _), tab in zip(_ASSET_TABS, tabs):
        with tab:
            _render_holdings_tab(atype)


_PKJ_TABS = [
    ("pkj_sbi_nifty50",    "📊 SBI NIFTY50 PED"),
    ("pkj_nippon_itbees",  "📈 NIPPON ITBEES PED"),
]

_PKJ_DEFAULTS = {
    "pkj_sbi_nifty50":   ("SBINIFTY50",  "SBI - ETF Nifty 50"),
    "pkj_nippon_itbees": ("ITBEES",      "Nippon India ETF Nifty IT BeES"),
}


def render_paiso_ka_jungle():
    st.markdown("## 🌳 Paiso Ka Jungle")
    st.markdown(
        "<p style='color:gray;font-size:0.9rem;margin-bottom:0.5rem;'>"
        "Track your SIP &amp; lump-sum investments in index ETFs. "
        "Data saved to your account — persists across sessions. "
        "Additional calculation columns will be added soon.</p>",
        unsafe_allow_html=True,
    )
    # Pre-populate fixed ETF symbol and company name for each PKJ tab.
    # Always overwrite the symbol so a renamed symbol (e.g. SETFNIF50→SBINIFTY50)
    # is picked up immediately without requiring the user to clear their session.
    for atype, (sym, name) in _PKJ_DEFAULTS.items():
        _k = f"hf_{atype}_"
        st.session_state[f"{_k}sym"] = sym
        if not st.session_state.get(f"{_k}name"):
            st.session_state[f"{_k}name"] = name
    tabs = st.tabs([label for _, label in _PKJ_TABS])
    for (atype, _), tab in zip(_PKJ_TABS, tabs):
        with tab:
            _render_holdings_tab(atype)


def _render_holdings_tab(asset_type: str) -> None:
    dark        = st.session_state.get("dark_mode", False)
    user_phone  = st.session_state.user["phone_number"]
    live_prices = st.session_state.get("holdings_live_prices", {})
    edit_id     = st.session_state.get("holdings_edit_id")

    holdings = db.get_holdings(user_phone, asset_type)

    # ── Auto-fetch live prices (runs on every render, 5-min TTL) ─────────────
    _open_buy_pre = [
        h for h in holdings
        if h.get("transaction_type", "BUY") == "BUY"
        and (h.get("remaining_qty") or 0) > 0.0001
        and not h.get("sell_date")
    ]
    _open_syms = list({h["symbol"] for h in _open_buy_pre})
    if _open_syms:
        _fkey  = f"holdings_prices_at_{asset_type}"
        _last  = st.session_state.get(_fkey)
        _miss  = any(s not in live_prices for s in _open_syms)
        _stale = (_last is None or
                  (datetime.now() - _last).total_seconds() > 300)
        if _miss or _stale:
            with st.spinner("📡 Fetching live NSE prices for open positions…"):
                _fresh = _fetch_nse_live_prices_bulk(_open_syms)
            _new = dict(live_prices)
            _new.update({s: v["price"] for s, v in _fresh.items() if v.get("price")})
            st.session_state["holdings_live_prices"] = _new
            st.session_state[_fkey] = datetime.now()
            live_prices = _new
        else:
            # Prices are fresh — schedule an auto-rerun in 5 min so the page
            # refreshes automatically even without user interaction
            _secs_left = max(1, 300 - int((datetime.now() - _last).total_seconds()))
            st.markdown(
                f'<meta http-equiv="refresh" content="{_secs_left}">',
                unsafe_allow_html=True,
            )

    # ── Summary bar + Portfolio card ──────────────────────────────────────────
    if holdings:
        buy_rows  = [h for h in holdings if h.get("transaction_type", "BUY") == "BUY"]
        sell_rows = [h for h in holdings if h.get("transaction_type", "BUY") == "SELL"]
        open_buy  = _open_buy_pre   # already computed above

        # Compute portfolio numbers for the summary card
        invested_open    = sum(
            float(h["buy_price"]) * float(h.get("remaining_qty") or h.get("buy_qty") or 0)
            for h in open_buy
        )
        cur_val_open     = sum(
            float(live_prices[h["symbol"]]) * float(h.get("remaining_qty") or h.get("buy_qty") or 0)
            for h in open_buy if live_prices.get(h["symbol"])
        )
        invested_priced  = sum(
            float(h["buy_price"]) * float(h.get("remaining_qty") or h.get("buy_qty") or 0)
            for h in open_buy if live_prices.get(h["symbol"])
        )
        virt_pnl_total   = cur_val_open - invested_priced
        priced_cnt       = sum(1 for h in open_buy if live_prices.get(h["symbol"]))
        has_prices       = priced_cnt > 0

        # Card styling
        tab_label = dict(_ASSET_TABS).get(asset_type, asset_type.replace("_"," ").title())
        lbl_c = "#94a3b8" if dark else "#64748b"
        if has_prices:
            pos   = virt_pnl_total >= 0
            brd_c = "#16a34a" if pos else "#dc2626"
            bg_c  = ("rgba(20,83,45,0.35)" if pos else "rgba(69,10,10,0.35)") if dark else \
                    ("rgba(220,252,231,0.7)" if pos else "rgba(254,226,226,0.7)")
            txt_c = ("#4ade80" if pos else "#f87171") if dark else ("#15803d" if pos else "#b91c1c")
            pct   = (virt_pnl_total / invested_priced * 100) if invested_priced else 0
            pnl_str = f"₹{virt_pnl_total:+,.2f} &nbsp; ({pct:+.2f}%)"
            price_note = f"{priced_cnt} of {len(open_buy)} positions priced"
        else:
            brd_c = "#64748b"; bg_c = "rgba(100,116,139,0.1)"
            txt_c = "#64748b"; pnl_str = "—"
            price_note = "Click 📡 Refresh Prices to see virtual P&L"
        cur_str = f"₹{cur_val_open:,.2f}" if has_prices else "—"

        st.markdown(f"""
<div style="background:{bg_c};border:2px solid {brd_c};border-radius:12px;
            padding:1rem 1.5rem;margin-bottom:1rem;
            display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:0.75rem;">
  <div>
    <div style="font-size:0.78rem;font-weight:700;color:{brd_c};text-transform:uppercase;letter-spacing:0.07em;">
      💼 {tab_label} Portfolio
    </div>
    <div style="font-size:0.76rem;color:{lbl_c};margin-top:3px;">
      {len(open_buy)} open &nbsp;·&nbsp; {len(buy_rows)} BUY rows &nbsp;·&nbsp; {len(sell_rows)} SELL txns
    </div>
  </div>
  <div style="text-align:center;">
    <div style="font-size:0.76rem;color:{lbl_c};">Invested (open)</div>
    <div style="font-size:1.1rem;font-weight:700;">₹{invested_open:,.2f}</div>
  </div>
  <div style="text-align:center;">
    <div style="font-size:0.76rem;color:{lbl_c};">Current Value</div>
    <div style="font-size:1.1rem;font-weight:700;">{cur_str}</div>
  </div>
  <div style="text-align:right;">
    <div style="font-size:0.76rem;color:{lbl_c};">Virtual P&L &nbsp;·&nbsp; {price_note}</div>
    <div style="font-size:1.6rem;font-weight:900;color:{txt_c};">{pnl_str}</div>
  </div>
</div>""", unsafe_allow_html=True)

        # Metric strip
        total_invested = sum(h["buy_price"] * h["buy_qty"] for h in buy_rows)
        virt_pnl, virt_cnt = 0.0, 0
        for h in open_buy:
            live = live_prices.get(h["symbol"])
            if live:
                rem = float(h.get("remaining_qty") or h.get("buy_qty") or 0)
                virt_pnl += (live - float(h["buy_price"])) * rem
                virt_cnt += 1

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("BUY Rows",          len(buy_rows))
        c2.metric("Open Positions",    len(open_buy))
        c3.metric("SELL Transactions", len(sell_rows))
        c4.metric("Total Invested",    f"₹{total_invested:,.2f}")
        if virt_cnt > 0:
            _pnl_col  = ("#4ade80" if virt_pnl >= 0 else "#f87171") if dark else ("#15803d" if virt_pnl >= 0 else "#b91c1c")
            _card_bg  = "#1e2130" if dark else "#ffffff"
            _card_br  = "rgba(255,255,255,0.1)" if dark else "rgba(49,51,63,0.1)"
            c5.markdown(
                f'<div style="background:{_card_bg};border:1px solid {_card_br};'
                f'border-radius:0.5rem;padding:0.6rem 0.75rem;'
                f'box-shadow:0 1px 3px rgba(49,51,63,0.08);">'
                f'<div style="font-size:0.8rem;color:#64748b;margin-bottom:0.3rem;">Virtual P&amp;L (open)</div>'
                f'<div style="font-size:1.6rem;font-weight:700;color:{_pnl_col};line-height:1.1;">₹{virt_pnl:+,.2f}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )
        else:
            c5.metric("Virtual P&L (open)", "—")

        # Manual force-refresh + last-fetched timestamp
        if _open_syms:
            lc1, lc2 = st.columns([1, 5])
            with lc1:
                if st.button("🔄 Refresh Now", key=f"hlive_{asset_type}",
                             width='stretch'):
                    # Clear both the timestamp AND the cached prices for these symbols
                    # so stale values never persist across a manual refresh
                    st.session_state.pop(f"holdings_prices_at_{asset_type}", None)
                    _cur = dict(st.session_state.get("holdings_live_prices", {}))
                    for _s in _open_syms:
                        _cur.pop(_s, None)
                    st.session_state["holdings_live_prices"] = _cur
                    st.rerun()
            with lc2:
                _last_at = st.session_state.get(f"holdings_prices_at_{asset_type}")
                if _last_at:
                    _ago = int((datetime.now() - _last_at).total_seconds())
                    fetched_syms = [s for s in _open_syms if s in live_prices]
                    st.caption(
                        f"✅ Auto-updated {_ago}s ago — {', '.join(fetched_syms)}. "
                        f"Auto-refreshes every 5 min."
                    )
                else:
                    st.caption("Prices will be fetched automatically on next render.")
        st.markdown("---")

    # ── Add / Edit form (branches on BUY vs SELL) ─────────────────────────────
    sell_buy_id = st.session_state.get("holdings_sell_buy_id")

    editing = None
    if edit_id is not None:
        editing = next((h for h in holdings if h["id"] == edit_id), None)

    editing_is_sell = editing is not None and editing.get("transaction_type") == "SELL"

    # ── SELL edit form ────────────────────────────────────────────────────────
    if editing_is_sell:
        _se   = f"hfe_{asset_type}_"
        se_target = f"sell_{editing['id']}"
        parent_row = next(
            (h for h in holdings if h["id"] == editing.get("parent_buy_id")), None
        )
        old_qty = float(editing["buy_qty"] or 0)
        max_qty = old_qty + float(parent_row.get("remaining_qty") or 0) if parent_row else old_qty

        if st.session_state.get(f"{_se}target") != se_target:
            from datetime import date as _di
            st.session_state[f"{_se}sell_qty"]   = old_qty
            st.session_state[f"{_se}sell_price"] = float(editing.get("sell_price") or 0)
            st.session_state[f"{_se}sell_date"]  = (
                _di.fromisoformat(editing["sell_date"]) if editing.get("sell_date") else _di.today()
            )
            st.session_state[f"{_se}notes"]      = editing.get("notes") or ""
            st.session_state[f"{_se}target"]     = se_target

        _se_sfx = ("sell_qty", "sell_price", "sell_date", "notes", "target")

        with st.expander(f"✏️ Edit SELL — {editing['symbol']}", expanded=True):
            st.info(
                f"Original BUY · ₹{editing['buy_price']:,.2f} × {editing['buy_qty']:,.4g}"
                f"  |  Buy Date: {editing['buy_date']}"
                f"  |  Max editable qty: **{max_qty:,.4g}**"
            )
            ef1, ef2 = st.columns([1, 1])
            with ef1:
                st.number_input(
                    f"Sell Qty (max {max_qty:,.4g}) *",
                    key=f"{_se}sell_qty",
                    min_value=0.0, max_value=float(max_qty), format="%.4f",
                )
                st.number_input(
                    "Sell Price (₹) *", key=f"{_se}sell_price",
                    min_value=0.0, format="%.4f",
                )
            with ef2:
                st.date_input(
                    "Sell Date *", key=f"{_se}sell_date", max_value=date.today(),
                )
                st.text_area("Notes", key=f"{_se}notes", height=68)

            se1, se2 = st.columns([1, 1])
            with se1:
                if st.button("💾 Save SELL", type="primary", width='stretch',
                             key=f"{_se}save"):
                    new_qty   = float(st.session_state.get(f"{_se}sell_qty",   0))
                    new_price = float(st.session_state.get(f"{_se}sell_price", 0))
                    if new_qty <= 0:
                        st.error("Sell qty must be greater than zero.")
                    elif new_price <= 0:
                        st.error("Sell price must be greater than zero.")
                    else:
                        ok, msg = db.update_sell_transaction(
                            editing["id"], user_phone,
                            {
                                "sell_qty":   new_qty,
                                "sell_price": new_price,
                                "sell_date":  str(st.session_state.get(f"{_se}sell_date", date.today())),
                                "notes":      st.session_state.get(f"{_se}notes", ""),
                            },
                        )
                        if ok:
                            st.success(f"✅ SELL updated.")
                            for _sfx in _se_sfx:
                                st.session_state.pop(f"{_se}{_sfx}", None)
                            st.session_state["holdings_edit_id"] = None
                            st.rerun()
                        else:
                            st.error(f"Error: {msg}")
            with se2:
                if st.button("✖ Cancel", width='stretch', key=f"{_se}cancel"):
                    for _sfx in _se_sfx:
                        st.session_state.pop(f"{_se}{_sfx}", None)
                    st.session_state["holdings_edit_id"] = None
                    st.rerun()

    else:
        # ── BUY add / edit form ───────────────────────────────────────────────
        _k      = f"hf_{asset_type}_"
        _buy_sfx = ("sym", "name", "buy_date", "buy_price", "buy_qty", "notes", "lookup", "target")

        target = editing["id"] if editing else "new"

        if st.session_state.get(f"{_k}target") != target:
            from datetime import date as _di
            st.session_state[f"{_k}sym"]       = editing["symbol"]                      if editing else ""
            st.session_state[f"{_k}name"]      = (editing.get("company_name") or "")    if editing else ""
            st.session_state[f"{_k}buy_date"]  = _di.fromisoformat(editing["buy_date"]) if editing else _di.today()
            st.session_state[f"{_k}buy_price"] = float(editing["buy_price"])            if editing else 0.0
            st.session_state[f"{_k}buy_qty"]   = float(editing["buy_qty"])              if editing else 0.0
            st.session_state[f"{_k}notes"]     = (editing.get("notes") or "")           if editing else ""
            st.session_state[f"{_k}target"]    = target
            st.session_state[f"{_k}lookup"]    = ""

        pending_sym = st.session_state.get(f"{_k}lookup", "")
        if pending_sym:
            st.session_state[f"{_k}lookup"] = ""
            cached = st.session_state.get(f"_yf_name_{pending_sym}")
            if cached is not None:
                if cached:
                    st.session_state[f"{_k}name"] = cached
            else:
                with st.spinner(f"🔍 Looking up company name for {pending_sym}…"):
                    _name = ""
                    # Try NSE via nsepython first (most accurate for Indian equity stocks)
                    try:
                        import logging as _lg; _lg.disable(_lg.WARNING)
                        from nsepython import nse_eq as _nse_eq
                        _lg.disable(_lg.NOTSET)
                        _nse_data = _nse_eq(pending_sym.upper())
                        _name = (_nse_data.get("info", {}).get("companyName")
                                 or _nse_data.get("metadata", {}).get("companyName") or "")
                    except Exception:
                        pass
                    # Fall back to yfinance — try NSE:{sym}, then {sym}.NS
                    if not _name:
                        try:
                            import yfinance as _yf
                            for _ticker in (f"NSE:{pending_sym}", f"{pending_sym}.NS"):
                                _info = _yf.Ticker(_ticker).info or {}
                                _name = _info.get("longName") or _info.get("shortName") or ""
                                if _name:
                                    break
                        except Exception:
                            pass
                    st.session_state[f"_yf_name_{pending_sym}"] = _name
                    if _name:
                        st.session_state[f"{_k}name"] = _name

        def _on_sym_change():
            sym_val = st.session_state.get(f"{_k}sym", "").strip().upper()
            if sym_val:
                st.session_state[f"{_k}lookup"] = sym_val

        buy_form_title = f"✏️ Edit BUY — {editing['symbol']}" if editing else "➕ Add New BUY"
        with st.expander(buy_form_title, expanded=(editing is not None or not holdings)):
            st.caption(
                "💾 Your data is permanently saved to your account — it stays until you delete it. "
                "Click **Save BUY** when ready."
            )
            fc1, fc2 = st.columns([1, 1])
            with fc1:
                st.text_input("Symbol *", key=f"{_k}sym", placeholder="e.g. RELIANCE",
                              on_change=_on_sym_change,
                              help="Tab out — company name is fetched automatically")
                st.text_input("Company Name", key=f"{_k}name",
                              placeholder="Auto-filled when symbol is entered · editable")
                st.date_input("Buy Date *", key=f"{_k}buy_date", max_value=date.today())
            with fc2:
                st.number_input("Buy Price (₹) *", key=f"{_k}buy_price",
                                min_value=0.0, format="%.4f")
                st.number_input("Buy Qty *", key=f"{_k}buy_qty",
                                min_value=0.0, format="%.4f")
                st.text_area("Notes", key=f"{_k}notes", height=68)

            sc1, sc2 = st.columns([1, 1])
            with sc1:
                if st.button("💾 Save BUY", type="primary", width='stretch',
                             key=f"{_k}save_btn"):
                    sym_val   = st.session_state.get(f"{_k}sym", "").strip().upper()
                    buy_price = float(st.session_state.get(f"{_k}buy_price", 0))
                    buy_qty   = float(st.session_state.get(f"{_k}buy_qty",   0))
                    if not sym_val:
                        st.error("Symbol is required.")
                    elif buy_price <= 0 or buy_qty <= 0:
                        st.error("Buy Price and Buy Qty must be greater than zero.")
                    else:
                        payload = {
                            "symbol":       sym_val,
                            "company_name": st.session_state.get(f"{_k}name", "").strip(),
                            "buy_date":     str(st.session_state.get(f"{_k}buy_date", date.today())),
                            "buy_price":    buy_price,
                            "buy_qty":      buy_qty,
                            "notes":        st.session_state.get(f"{_k}notes", ""),
                        }
                        if editing:
                            db.update_holding(editing["id"], user_phone, payload)
                            st.success(f"✅ Updated {sym_val}")
                        else:
                            db.add_holding(user_phone, asset_type, payload)
                            st.success(f"✅ Added {sym_val}")
                        for _sfx in _buy_sfx:
                            st.session_state.pop(f"{_k}{_sfx}", None)
                        st.session_state["holdings_edit_id"] = None
                        st.rerun()
            with sc2:
                if st.button("✖ Cancel", width='stretch', key=f"{_k}cancel_btn"):
                    for _sfx in _buy_sfx:
                        st.session_state.pop(f"{_k}{_sfx}", None)
                    st.session_state["holdings_edit_id"] = None
                    st.rerun()

    # ── Sell Transaction form ─────────────────────────────────────────────────
    if sell_buy_id is not None:
        sell_src = next((h for h in holdings if h["id"] == sell_buy_id), None)
        if sell_src:
            _sk   = f"hfs_{asset_type}_"
            s_rem = float(sell_src.get("remaining_qty") or sell_src.get("buy_qty") or 0)

            st.markdown(f"### 📤 Record Sell — **{sell_src['symbol']}**")
            st.caption(
                f"Original buy: ₹{sell_src['buy_price']:,.2f} × {sell_src['buy_qty']:,.0f} · "
                f"Remaining available to sell: **{s_rem:,.4g}**"
            )
            sf1, sf2 = st.columns([1, 1])
            with sf1:
                s_qty = st.number_input(
                    f"Sell Qty (max {s_rem:,.4g}) *",
                    key=f"{_sk}sell_qty", min_value=0.0, max_value=float(s_rem),
                    format="%.4f",
                )
                s_price = st.number_input(
                    "Sell Price (₹) *", key=f"{_sk}sell_price",
                    min_value=0.0, format="%.4f",
                )
            with sf2:
                s_date  = st.date_input("Sell Date *", key=f"{_sk}sell_date",
                                        max_value=date.today())
                s_notes = st.text_area("Notes", key=f"{_sk}notes", height=68)

            ss1, ss2 = st.columns([1, 1])
            with ss1:
                if st.button("💾 Confirm Sell", type="primary",
                             width='stretch', key=f"{_sk}confirm"):
                    sell_qty_v = float(st.session_state.get(f"{_sk}sell_qty", 0))
                    sell_px_v  = float(st.session_state.get(f"{_sk}sell_price", 0))
                    if sell_qty_v <= 0:
                        st.error("Sell qty must be greater than zero.")
                    elif sell_px_v <= 0:
                        st.error("Sell price must be greater than zero.")
                    else:
                        ok, msg, _ = db.add_sell_transaction(
                            user_phone, sell_buy_id,
                            {
                                "sell_qty":   sell_qty_v,
                                "sell_price": sell_px_v,
                                "sell_date":  str(st.session_state.get(f"{_sk}sell_date", date.today())),
                                "notes":      st.session_state.get(f"{_sk}notes", ""),
                            }
                        )
                        if ok:
                            st.success(f"✅ Sell of {sell_qty_v:,.4g} {sell_src['symbol']} recorded.")
                            for _sfx in ("sell_qty","sell_price","sell_date","notes"):
                                st.session_state.pop(f"{_sk}{_sfx}", None)
                            st.session_state["holdings_sell_buy_id"] = None
                            st.rerun()
                        else:
                            st.error(f"Error: {msg}")
            with ss2:
                if st.button("✖ Cancel Sell", width='stretch', key=f"{_sk}cancel"):
                    for _sfx in ("sell_qty","sell_price","sell_date","notes"):
                        st.session_state.pop(f"{_sk}{_sfx}", None)
                    st.session_state["holdings_sell_buy_id"] = None
                    st.rerun()

            st.markdown("---")

    # ── No data yet ───────────────────────────────────────────────────────────
    if not holdings:
        st.info("No holdings added yet. Use the form above to add your first BUY entry.")
        return

    # ── Open Positions with virtual P&L and direct Sell button ───────────────
    open_buy_rows = [h for h in holdings
                     if h.get("transaction_type", "BUY") == "BUY"
                     and (h.get("remaining_qty") or 0) > 0.0001
                     and not h.get("sell_date")]

    if open_buy_rows:
        st.markdown("### 📂 Open Positions")

        _g = "#dcfce7"; _r = "#fee2e2"; _gc = "#15803d"; _rc = "#b91c1c"
        if dark:
            _g = "rgba(20,83,45,0.4)"; _r = "rgba(69,10,10,0.4)"
            _gc = "#4ade80";           _rc = "#f87171"

        for h in open_buy_rows:
            pnl_amt, pnl_pct, days, _, eff_price = _holding_pnl(h, live_prices)
            remaining = float(h.get("remaining_qty") or h.get("buy_qty") or 0)
            invested  = h["buy_price"] * remaining

            if pnl_amt is not None:
                bg_col = _g if pnl_amt >= 0 else _r
                fc_col = _gc if pnl_amt >= 0 else _rc
                pnl_label = f"₹{pnl_amt:+,.2f} ({pnl_pct:+.2f}%)"
            else:
                bg_col = "rgba(100,116,139,0.1)"
                fc_col = "#64748b"
                pnl_label = "⏳ Refresh for P&L"

            cur_price_str = f"₹{eff_price:,.2f}" if eff_price else "—"

            oc1, oc2, oc3, oc4, oc5, oc6, oc7 = st.columns([2, 2, 1, 2, 2, 2, 1])
            with oc1:
                st.markdown(
                    f"<div style='font-weight:700;font-size:1rem;'>{h['symbol']}</div>"
                    f"<div style='font-size:0.75rem;color:#64748b;'>{h['company_name'] or ''}</div>",
                    unsafe_allow_html=True,
                )
            with oc2:
                st.markdown(
                    f"<div style='font-size:0.82rem;color:#64748b;'>Buy Price</div>"
                    f"<div style='font-weight:600;'>₹{h['buy_price']:,.2f}</div>",
                    unsafe_allow_html=True,
                )
            with oc3:
                st.markdown(
                    f"<div style='font-size:0.82rem;color:#64748b;'>Remaining</div>"
                    f"<div style='font-weight:600;'>{remaining:,.4g}</div>",
                    unsafe_allow_html=True,
                )
            with oc4:
                st.markdown(
                    f"<div style='font-size:0.82rem;color:#64748b;'>Invested (₹)</div>"
                    f"<div style='font-weight:600;'>₹{invested:,.2f}</div>",
                    unsafe_allow_html=True,
                )
            with oc5:
                st.markdown(
                    f"<div style='font-size:0.82rem;color:#64748b;'>Current Price</div>"
                    f"<div style='font-weight:600;'>{cur_price_str}</div>",
                    unsafe_allow_html=True,
                )
            with oc6:
                st.markdown(
                    f"<div style='font-size:0.82rem;color:#64748b;'>Virtual P&L &nbsp;({days}d)</div>"
                    f"<div style='font-weight:700;color:{fc_col};background:{bg_col};"
                    f"padding:2px 6px;border-radius:4px;display:inline-block;'>{pnl_label}</div>",
                    unsafe_allow_html=True,
                )
            with oc7:
                if st.button("📤 Sell", key=f"op_sell_{h['id']}_{asset_type}",
                             width='stretch'):
                    st.session_state["holdings_sell_buy_id"] = h["id"]
                    st.session_state["holdings_edit_id"]     = None
                    st.rerun()

        st.markdown("---")

    # ── Main table ────────────────────────────────────────────────────────────
    st.markdown("### 📋 All Transactions")

    # Per-symbol aggregations for Base Investment, Avg Price, Net Qty
    from collections import defaultdict as _dd
    _sym_stats: dict = _dd(lambda: {"buy_cost": 0.0, "buy_qty": 0.0, "sell_qty": 0.0})
    for _h in holdings:
        _sym = _h["symbol"]
        _tt  = _h.get("transaction_type", "BUY")
        if _tt == "BUY":
            _sym_stats[_sym]["buy_cost"] += float(_h.get("buy_price") or 0) * float(_h.get("buy_qty") or 0)
            _sym_stats[_sym]["buy_qty"]  += float(_h.get("buy_qty") or 0)
        else:
            _sym_stats[_sym]["sell_qty"] += float(_h.get("sell_qty") or _h.get("buy_qty") or 0)

    rows = []
    for h in holdings:
        pnl_amt, pnl_pct, days, is_open, eff_price = _holding_pnl(h, live_prices)
        t_type    = h.get("transaction_type", "BUY")
        remaining = h.get("remaining_qty")
        if t_type == "BUY" and remaining is None:
            remaining = h.get("buy_qty")

        _ss       = _sym_stats[h["symbol"]]
        _base_inv = _ss["buy_cost"]
        _avg_px   = _ss["buy_cost"] / _ss["buy_qty"] if _ss["buy_qty"] > 0 else 0.0
        _net_qty  = _ss["buy_qty"] - _ss["sell_qty"]

        rows.append({
            "ID":            h["id"],
            "Txn":           t_type,
            "Symbol":        h["symbol"],
            "Company":       h["company_name"] or h["symbol"],
            "Buy Date":      h["buy_date"],
            "Buy Price (₹)": h["buy_price"],
            "Qty":           h["buy_qty"],
            "Invested (₹)":  round(float(h["buy_price"] or 0) * float(h["buy_qty"] or 0), 2),
            "Remaining":     remaining if t_type == "BUY" else None,
            "Base Inv (₹)":  _base_inv,
            "Avg Price (₹)": _avg_px,
            "Net Qty":       _net_qty,
            "Current Price":  eff_price,
            "P&L (₹)":       pnl_amt,
            "P&L %":         pnl_pct,
            "Days":          days,
            "Sell Date":     h.get("sell_date") or "—",
            "Sell Price (₹)": h.get("sell_price"),
            "Sell Qty":      h.get("sell_qty"),
        })

    df = pd.DataFrame(rows)
    df_disp = df.drop(columns=["ID"])

    num_col = st.column_config.NumberColumn
    txt_col = st.column_config.TextColumn
    col_cfg = {
        "Txn":            txt_col("Txn",             width="small"),
        "Symbol":         txt_col("Symbol",           width="small"),
        "Company":        txt_col("Company",           width="medium"),
        "Buy Date":       txt_col("Buy Date",          width="small"),
        "Buy Price (₹)":  num_col("Buy Price (₹)",    format="₹%.2f"),
        "Qty":            num_col("Qty",               format="%.4f"),
        "Invested (₹)":   num_col("Invested Amount",   format="₹%,.2f",
                                   help="Amount invested in this transaction = Buy Price × Buy Qty."),
        "Remaining":      num_col("Remaining",         format="%.4f"),
        "Base Inv (₹)":   num_col("Base Inv (₹)",     format="₹%,.2f",
                                   help="Total capital invested in this symbol via all BUY transactions. Never reduced by sells."),
        "Avg Price (₹)":  num_col("Avg Price (₹)",    format="₹%.2f",
                                   help="Weighted average buy price = Total buy cost ÷ Total shares bought."),
        "Net Qty":        num_col("Net Qty",           format="%.4f",
                                   help="Current holding = Total shares bought − Total shares sold."),
        "Current Price":  num_col("Current Price",   format="₹%.2f"),
        "P&L (₹)":        num_col("P&L (₹)",          format="%+,.2f"),
        "P&L %":          num_col("P&L %",             format="%+.2f%%"),
        "Days":           num_col("Days",              format="%d d"),
        "Sell Date":      txt_col("Sell Date",         width="small"),
        "Sell Price (₹)": num_col("Sell Price (₹)",   format="₹%.2f"),
        "Sell Qty":       num_col("Sell Qty",          format="%.4f"),
    }

    def _style_pnl_abs(val):
        try:
            v = float(val)
        except (TypeError, ValueError):
            return ""
        if v > 0:
            return "background-color:#14532d;color:#4ade80" if dark else "background-color:#dcfce7;color:#15803d"
        if v < 0:
            return "background-color:#450a0a;color:#f87171" if dark else "background-color:#fee2e2;color:#b91c1c"
        return ""

    styled = df_disp.style
    if "P&L (₹)" in df_disp.columns:
        styled = styled.map(_style_pnl_abs, subset=["P&L (₹)"])
    if "P&L %" in df_disp.columns:
        styled = styled.map(_style_pnl_abs, subset=["P&L %"])

    evt = st.dataframe(
        styled, hide_index=True, width='stretch',
        on_select="rerun", selection_mode="single-row",
        column_config=col_cfg,
        key=f"holdings_df_{asset_type}",
    )

    # Row-action buttons
    selected = evt.selection.rows if hasattr(evt, "selection") else []
    if selected:
        sel_row   = df.iloc[selected[0]]
        sel_id    = int(sel_row["ID"])
        sel_sym   = sel_row["Symbol"]
        sel_type  = sel_row["Txn"]
        sel_rem   = float(sel_row["Remaining"]) if sel_row["Remaining"] is not None and str(sel_row["Remaining"]) not in ("nan", "None", "") else 0.0

        btns = [1, 1, 1, 3] if (sel_type == "BUY" and sel_rem > 0.0001) else [1, 1, 4]
        if sel_type == "BUY" and sel_rem > 0.0001:
            ac1, ac2, ac3, _ = st.columns(btns)
        else:
            ac1, ac2, _ = st.columns(btns)

        with ac1:
            if st.button(f"✏️ Edit", key=f"h_edit_{asset_type}", width='stretch'):
                st.session_state["holdings_edit_id"]   = sel_id
                st.session_state["holdings_sell_buy_id"] = None
                st.rerun()
        with ac2:
            if st.button(f"🗑 Delete", key=f"h_del_{asset_type}",
                         width='stretch', type="secondary"):
                db.delete_holding(sel_id, user_phone)
                st.session_state["holdings_edit_id"]   = None
                st.session_state["holdings_sell_buy_id"] = None
                st.rerun()
        if sel_type == "BUY" and sel_rem > 0.0001:
            with ac3:
                if st.button(f"📤 Sell {sel_sym}", key=f"h_sell_{asset_type}",
                             width='stretch'):
                    st.session_state["holdings_sell_buy_id"] = sel_id
                    st.session_state["holdings_edit_id"]     = None
                    st.rerun()

    # CSV / Excel export
    ec1, ec2 = st.columns([1, 1])
    with ec1:
        st.download_button(
            "📥 CSV", df_disp.to_csv(index=False).encode(),
            f"holdings_{asset_type}_{_now_ist().strftime('%Y%m%d_%H%M')}.csv",
            "text/csv", key=f"h_csv_{asset_type}",
        )
    with ec2:
        try:
            import io
            buf = io.BytesIO()
            df_disp.to_excel(buf, index=False, engine="openpyxl")
            st.download_button("📥 Excel", buf.getvalue(),
                               f"holdings_{asset_type}_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx",
                               key=f"h_xls_{asset_type}")
        except Exception:
            pass

    st.markdown("---")

    # ── Signal Sections ───────────────────────────────────────────────────────
    computed = []
    for h in holdings:
        pnl_amt, pnl_pct, days, is_open, eff_price = _holding_pnl(h, live_prices)
        computed.append((h, pnl_amt, pnl_pct, days, is_open, eff_price))

    # Group open BUY positions by symbol (one card per symbol)
    all_open_buys = [(h, pa, pp, d, io, ep) for h, pa, pp, d, io, ep in computed
                     if h.get("transaction_type", "BUY") == "BUY" and io]
    grouped_open  = _group_open_buys(all_open_buys, live_prices)

    open_above = [(h, pa, pp, d, io, ep) for h, pa, pp, d, io, ep in grouped_open
                  if pp is not None and pp > _THRESHOLD]
    open_below = [(h, pa, pp, d, io, ep) for h, pa, pp, d, io, ep in grouped_open
                  if pp is not None and pp <= _THRESHOLD]
    no_price   = [(h, pa, pp, d, io, ep) for h, pa, pp, d, io, ep in grouped_open
                  if pp is None]

    sell_pnl   = [(h, pa, pp, d, io, ep) for h, pa, pp, d, io, ep in computed
                  if h.get("transaction_type", "BUY") == "SELL" and pp is not None]

    open_above.sort(key=lambda x: x[2], reverse=True)
    open_below.sort(key=lambda x: x[2])
    sell_pnl.sort(key=lambda x: x[2], reverse=True)

    st.markdown("### 📊 Signal Sections")

    if open_above:
        with st.expander(f"🔴 Virtual P&L > {_THRESHOLD}% — Book Profit Candidates ({len(open_above)})",
                         expanded=True):
            cols = st.columns(min(3, len(open_above)))
            for i, (h, pa, pp, d, io, ep) in enumerate(open_above):
                with cols[i % 3]:
                    st.markdown(_holding_card_html(h, pa, pp, d, io, ep, dark),
                                unsafe_allow_html=True)

    if open_below:
        with st.expander(f"🟢 Virtual P&L ≤ {_THRESHOLD}% — Averaging Zone ({len(open_below)})",
                         expanded=False):
            cols = st.columns(min(3, len(open_below)))
            for i, (h, pa, pp, d, io, ep) in enumerate(open_below):
                with cols[i % 3]:
                    st.markdown(_holding_card_html(h, pa, pp, d, io, ep, dark),
                                unsafe_allow_html=True)

    if sell_pnl:
        profit_sells = [(h, pa, pp, d, io, ep) for h, pa, pp, d, io, ep in sell_pnl if pa >= 0]
        loss_sells   = [(h, pa, pp, d, io, ep) for h, pa, pp, d, io, ep in sell_pnl if pa < 0]
        if profit_sells:
            with st.expander(f"✅ Realized Profit — SELL Transactions ({len(profit_sells)})",
                             expanded=False):
                cols = st.columns(min(3, len(profit_sells)))
                for i, (h, pa, pp, d, io, ep) in enumerate(profit_sells):
                    with cols[i % 3]:
                        st.markdown(_holding_card_html(h, pa, pp, d, io, ep, dark),
                                    unsafe_allow_html=True)
        if loss_sells:
            with st.expander(f"❌ Realized Loss — SELL Transactions ({len(loss_sells)})",
                             expanded=False):
                cols = st.columns(min(3, len(loss_sells)))
                for i, (h, pa, pp, d, io, ep) in enumerate(loss_sells):
                    with cols[i % 3]:
                        st.markdown(_holding_card_html(h, pa, pp, d, io, ep, dark),
                                    unsafe_allow_html=True)

    if no_price:
        with st.expander(f"⏳ Live Price Needed — Open Positions ({len(no_price)})",
                         expanded=False):
            st.caption("Click **Refresh Prices** above to load live NSE prices for these positions.")
            for h, *_ in no_price:
                remaining = float(h.get("remaining_qty") or h.get("buy_qty") or 0)
                invested  = h["buy_price"] * remaining
                st.markdown(
                    f"**{h['symbol']}** — {h['company_name'] or h['symbol']} · "
                    f"Buy ₹{h['buy_price']:,.2f} × {remaining:,.4g} = ₹{invested:,.2f} · "
                    f"Since {h['buy_date']}"
                )


# ── RSI Screener ─────────────────────────────────────────────────────────────

def _load_rsi_session(s: dict, prefix: str) -> None:
    from src.data.rsi_models import RSIData as _RSID
    rows    = json.loads(s["results_json"]) if isinstance(s["results_json"], str) else s["results_json"]
    results = []
    for r in rows:
        try:
            results.append(_RSID(**r))
        except Exception:
            results.append(_RSID(symbol=r.get("symbol", "?"), error="Load error"))
    st.session_state[f"{prefix}_data"]              = results
    st.session_state[f"{prefix}_active_scan_name"]  = s["scan_name"]
    st.session_state[f"{prefix}_last_fetched_at"]   = None
    st.session_state[f"{prefix}_last_scan_symbols"] = [r.symbol for r in results]


def _do_rsi_fetch(prefix: str, symbols: list, scan_name: str,
                  ref_date=None, save_to_history: bool = False) -> None:
    """Fetch RSI data then enrich current price from NSE GetQuoteApi."""
    from src.scrapers.rsi_fetcher import RSIFetcher
    from src.data.rsi_models import RSIData as _RSID

    fetcher   = RSIFetcher()
    slot      = st.empty()
    done_cnt  = [0]

    if len(symbols) == 1:
        slot.markdown(_spinner_html(f"⏳ Calculating RSI for <b>{symbols[0]}</b>…"), unsafe_allow_html=True)
        results = [fetcher.fetch(symbols[0], ref_date)]
        slot.empty()
    else:
        results_map: dict = {}
        slot.markdown(_spinner_html(f"⏳ Calculating RSI for {len(symbols)} symbols…  0 / {len(symbols)}"), unsafe_allow_html=True)
        with ThreadPoolExecutor(max_workers=8) as ex:
            futures = {ex.submit(fetcher.fetch, sym, ref_date): sym for sym in symbols}
            for future in as_completed(futures):
                sym = futures[future]
                try:
                    results_map[sym] = future.result(timeout=120)
                except Exception as exc:
                    results_map[sym] = _RSID(symbol=sym, error=str(exc))
                done_cnt[0] += 1
                slot.markdown(
                    _spinner_html(f"⏳ Calculating RSI for {len(symbols)} symbols…  {done_cnt[0]} / {len(symbols)}"),
                    unsafe_allow_html=True,
                )
        slot.empty()
        results = [results_map.get(s, _RSID(symbol=s, error="Not fetched")) for s in symbols]

    # NSE live price enrichment (same two-scenario logic as Daily Dukaan)
    nse_slot    = st.empty()
    market_open = _is_market_open()
    today_str   = str(date.today())
    is_today    = (ref_date is not None and str(ref_date) == today_str)
    is_weekday  = date.today().weekday() < 5
    today_sess  = is_today and is_weekday

    nse_slot.markdown(
        _spinner_html(
            "📡 Fetching live NSE prices…" if market_open
            else "📡 Fetching today's closing prices from NSE…" if today_sess
            else "📡 Fetching last-close prices from NSE…"
        ),
        unsafe_allow_html=True,
    )
    try:
        nse_px   = _fetch_nse_live_prices_bulk(symbols)
        enriched = []
        for r in results:
            lp = nse_px.get(r.symbol)
            if lp and not r.error:
                cp     = round(lp["price"], 2)
                source = "NSE Live" if market_open else "NSE Close"
                nse_date = r.ref_date
                if lp.get("time"):
                    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y"):
                        try:
                            parsed    = datetime.strptime(lp["time"][:len(fmt)], fmt)
                            candidate = parsed.strftime("%Y-%m-%d")
                            if today_sess or (candidate > (nse_date or "")):
                                nse_date = candidate
                            break
                        except Exception:
                            pass
                enriched.append(r.model_copy(update={
                    "current_price": cp,
                    "ref_date":      nse_date,
                    "price_source":  source,
                    "nse_live_time": lp["time"],
                }))
            else:
                enriched.append(r)
        results = enriched
        nse_slot.empty()
    except Exception as exc:
        nse_slot.empty()
        st.warning(f"⚠️ Could not fetch NSE prices: {exc}")

    st.session_state[f"{prefix}_data"]              = results
    st.session_state[f"{prefix}_last_fetched_at"]   = _now_ist()
    st.session_state[f"{prefix}_last_scan_symbols"] = symbols
    st.session_state[f"{prefix}_active_scan_name"]  = scan_name

    if save_to_history:
        user_phone = st.session_state.user["phone_number"]
        scan_type  = "rsi_stock" if prefix == "rsi_stock" else "rsi_etf"
        dicts = [r.model_dump() for r in results]
        db.save_scan_session(user_phone, scan_name, dicts, scan_type=scan_type)


def render_rsi_screener():
    st.markdown("## 📉 RSI Screener")
    tab_stock, tab_etf = st.tabs(["📊 Stock RSI Screener", "🏦 ETF RSI Screener"])
    with tab_stock:
        _render_rsi_tab("rsi_stock", label="Stock", sample_syms=["RELIANCE", "INFY", "TCS", "HDFCBANK", "WIPRO"])
    with tab_etf:
        _render_rsi_tab("rsi_etf",   label="ETF",   sample_syms=["NIFTYBEES", "BANKBEES", "GOLDBEES", "JUNIORBEES", "ITBEES"])


def _render_rsi_tab(prefix: str, label: str, sample_syms: list) -> None:
    from datetime import date as _date
    scan_type_key = "rsi_stock" if prefix == "rsi_stock" else "rsi_etf"
    _auto_restore_scan(f"{prefix}_data", lambda s: _load_rsi_session(s, prefix), scan_type_key)

    st.markdown(
        f"<p style='color:gray;font-size:0.9rem;margin-bottom:0.5rem;'>"
        f"14-period RSI across Daily / Weekly / Monthly timeframes for NSE {label}s · "
        f"RSI &lt; 35 = Oversold (green) · RSI &gt; 70 = Overbought (red)</p>",
        unsafe_allow_html=True,
    )

    today = _date.today()
    if st.session_state.pop(f"_live_pending_{prefix}", False):
        st.session_state[f"{prefix}_date_picker"] = today
    dcol1, dcol2 = st.columns([2, 5])
    with dcol1:
        saved_date   = st.session_state.get(f"{prefix}_ref_date") or today
        ref_date_val = st.date_input(
            "Reference Date",
            value=saved_date,
            max_value=today,
            help="RSI is calculated as of this date. Weekends / market holidays fall back to the previous trading day.",
            key=f"{prefix}_date_picker",
        )
        st.session_state[f"{prefix}_ref_date"] = ref_date_val
    with dcol2:
        st.markdown(
            "<p style='color:gray;font-size:0.85rem;margin-top:1.9rem;'>"
            "If the selected date falls on a weekend or NSE holiday, the previous available trading day is used automatically.</p>",
            unsafe_allow_html=True,
        )

    st.markdown("---")

    tab_manual, tab_file = st.tabs(["📝 Enter Symbols", "📁 Upload File"])
    symbols: list = []

    with tab_manual:
        txt = st.text_area(
            f"{label} symbols",
            placeholder=f"Single: {sample_syms[0]}\nMultiple: {', '.join(sample_syms[:3])}\nOne per line also works",
            height=100,
            label_visibility="collapsed",
            key=f"{prefix}_manual_input",
        )
        c1, c2 = st.columns([1, 5])
        with c1:
            scan_btn = st.button("🔍 Scan", type="primary", width='stretch', key=f"{prefix}_scan_m")
        with c2:
            st.caption("Use NSE symbols. RSI data via Yahoo Finance · Live price via NSE India.")
        if scan_btn and txt.strip():
            st.session_state[f"_{prefix}_pending_scan_name"] = f"RSI {label} — {_now_ist().strftime('%d %b %H:%M')}"
            symbols = _parser.parse_text_input(txt)

    with tab_file:
        up = st.file_uploader(
            "Upload CSV / Excel / TXT", type=["csv", "xlsx", "xls", "txt"],
            label_visibility="collapsed", key=f"{prefix}_file_uploader",
        )
        c1, c2, c3 = st.columns([1, 1, 4])
        with c1:
            scan_f = st.button("🔍 Scan File", type="primary", width='stretch', key=f"{prefix}_scan_f")
        with c2:
            sample = pd.DataFrame({"symbol": sample_syms})
            st.download_button("📥 Sample", sample.to_csv(index=False).encode(),
                               f"sample_rsi_{label.lower()}.csv", "text/csv",
                               key=f"{prefix}_sample_dl")
        with c3:
            st.caption("CSV/Excel: 'symbol' column or first column. TXT: one per line.")
        if scan_f and up:
            st.session_state[f"_{prefix}_pending_scan_name"] = up.name
            symbols = _parser.parse_file(up)
            if not symbols:
                st.warning("⚠️ No valid symbols found in file.")

    if symbols:
        scan_name = st.session_state.pop(f"_{prefix}_pending_scan_name", f"RSI {label}")
        st.markdown(
            f"**Calculating RSI for {len(symbols)} symbol(s):** "
            f"{', '.join(symbols[:15])}{'…' if len(symbols) > 15 else ''}"
        )
        _do_rsi_fetch(prefix, symbols, scan_name, ref_date=ref_date_val, save_to_history=True)

    if st.session_state.get(f"{prefix}_do_refresh"):
        st.session_state[f"{prefix}_do_refresh"] = False
        syms = st.session_state.get(f"{prefix}_last_scan_symbols", [])
        if not syms:
            syms = [d.symbol for d in st.session_state.get(f"{prefix}_data", [])]
        if syms:
            scan_name = st.session_state.get(f"{prefix}_active_scan_name") or f"RSI {label} Refresh"
            st.markdown(f"**Refreshing RSI for {len(syms)} symbol(s)…**")
            _do_rsi_fetch(prefix, syms, scan_name, ref_date=ref_date_val, save_to_history=True)

    data = st.session_state.get(f"{prefix}_data", [])
    if not data:
        return

    valid      = [d for d in data if not d.error]
    errors     = [d for d in data if d.error]
    fetched_at = st.session_state.get(f"{prefix}_last_fetched_at")
    all_dates  = [d.ref_date for d in valid if d.ref_date]
    latest_ref = max(all_dates) if all_dates else "—"

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Symbols Scanned", len(data))
    c2.metric("Trading Day",     latest_ref)
    c3.metric("Loaded OK",       len(valid))
    if fetched_at:
        c4.metric("Last Fetched", _fmt_ts(fetched_at))

    rb1, rb2, rb3 = st.columns([1, 1, 4])
    with rb1:
        if st.button("🔄 Refresh", width='stretch', key=f"{prefix}_refresh_btn"):
            st.session_state[f"{prefix}_do_refresh"] = True
            st.rerun()
    with rb2:
        _live_btn(prefix, f"{prefix}_do_refresh", f"{prefix}_live_btn")
    with rb3:
        if _is_market_open():
            st.success("🟢 **MARKET OPEN** — Live fetches real-time NSE prices.")
        else:
            from datetime import timezone as _tz, timedelta as _td
            _IST = _tz(_td(hours=5, minutes=30))
            ist_now = datetime.now(_IST).strftime("%d %b %Y, %I:%M %p IST")
            st.info(f"⏸ Market closed ({ist_now}). Live / Refresh fetches the latest NSE close price.")

    st.markdown("---")
    _render_rsi_table(data, prefix)
    st.markdown("---")
    _render_rsi_sections(valid, prefix)

    if errors:
        with st.expander(f"⚠️ Errors ({len(errors)} symbol(s))", expanded=False):
            for d in errors:
                st.markdown(f"**{d.symbol}** — {d.error}")


def _render_rsi_table(data: list, prefix: str) -> None:
    """Full RSI table with color-coded RSI cells using pandas Styler."""
    dark = st.session_state.get("dark_mode", False)
    rows = []
    for d in data:
        row = {
            "Symbol":       d.symbol,
            "Company Name": d.company_name or d.symbol,
            "Price (₹)":    d.current_price,
            "Daily RSI":    d.daily_rsi,
            "Weekly RSI":   d.weekly_rsi,
            "Monthly RSI":  d.monthly_rsi,
            "Date":         d.ref_date or "—",
        }
        if d.error:
            row["Error"] = d.error
        rows.append(row)

    df = pd.DataFrame(rows)

    h1, h2, h3 = st.columns([3, 1, 1])
    with h1:
        st.markdown(f"### 📊 RSI Results — {len(df)} symbol(s)")
    with h2:
        st.download_button(
            "📥 CSV", df.to_csv(index=False).encode(),
            f"rsi_{prefix}_{_now_ist().strftime('%Y%m%d_%H%M')}.csv", "text/csv",
            key=f"{prefix}_csv_dl",
        )
    with h3:
        try:
            import io
            buf = io.BytesIO()
            df.to_excel(buf, index=False, engine="openpyxl")
            st.download_button("📥 Excel", buf.getvalue(),
                               f"rsi_{prefix}_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx",
                               key=f"{prefix}_xls_dl")
        except Exception:
            pass

    df = _df_filter_sort(df, prefix)

    def _style_rsi_cell(val):
        try:
            v = float(val)
        except (TypeError, ValueError):
            return ""
        if v < 35:
            return ("background-color:#14532d;color:#4ade80" if dark
                    else "background-color:#dcfce7;color:#15803d")
        if v > 70:
            return ("background-color:#450a0a;color:#f87171" if dark
                    else "background-color:#fee2e2;color:#b91c1c")
        return ""

    rsi_cols = [c for c in ("Daily RSI", "Weekly RSI", "Monthly RSI") if c in df.columns]
    styled = df.style.map(_style_rsi_cell, subset=rsi_cols)

    num_col = st.column_config.NumberColumn
    txt_col = st.column_config.TextColumn
    col_cfg: dict = {
        "Symbol":       txt_col("Symbol",       width="small"),
        "Company Name": txt_col("Company Name", width="large"),
        "Price (₹)":    num_col("Price (₹)",    format="₹%.2f"),
        "Daily RSI":    num_col("Daily RSI",    format="%.2f"),
        "Weekly RSI":   num_col("Weekly RSI",   format="%.2f"),
        "Monthly RSI":  num_col("Monthly RSI",  format="%.2f"),
        "Date":         txt_col("Date",          width="small"),
    }
    if "Error" in df.columns:
        col_cfg["Error"] = txt_col("Error", width="large")

    st.dataframe(
        styled, hide_index=True, width='stretch',
        column_config=col_cfg,
        key=f"{prefix}_df",
    )


def _render_rsi_sections(valid: list, prefix: str) -> None:
    """Six collapsible sections: Oversold/Overbought × Daily/Weekly/Monthly."""
    dark = st.session_state.get("dark_mode", False)

    def _card_html(d, rsi_val: float, timeframe: str, oversold: bool) -> str:
        brd = "#16a34a" if oversold else "#dc2626"
        bg  = ("rgba(20,83,45,0.35)" if dark else "rgba(220,252,231,0.6)") if oversold else \
              ("rgba(69,10,10,0.35)" if dark else "rgba(254,226,226,0.6)")
        txt = "#4ade80" if (oversold and dark) else ("#15803d" if oversold else
              ("#f87171" if dark else "#b91c1c"))
        lbl = "#94a3b8" if dark else "#64748b"
        label = "🟢 Oversold" if oversold else "🔴 Overbought"
        price_str = f"₹{d.current_price:,.2f}" if d.current_price else "—"
        return f"""
<div style="background:{bg};border:1.5px solid {brd};border-radius:10px;
            padding:0.85rem 1.1rem;margin-bottom:0.6rem;">
  <div style="display:flex;justify-content:space-between;align-items:flex-start;">
    <div>
      <div style="font-size:1.3rem;font-weight:800;color:{brd};">{d.symbol}</div>
      <div style="font-size:0.85rem;color:{lbl};">{d.company_name or d.symbol}</div>
    </div>
    <div style="text-align:right;">
      <div style="font-size:1.5rem;font-weight:800;color:{txt};">{timeframe} RSI {rsi_val:.2f}</div>
      <div style="font-size:0.82rem;color:{lbl};">
        {label} &nbsp;·&nbsp; Price {price_str} &nbsp;·&nbsp; {d.ref_date or "—"}
      </div>
    </div>
  </div>
  <div style="display:flex;gap:0.5rem;margin-top:0.7rem;padding-top:0.6rem;border-top:1px solid {brd}40;">
    <a href="https://www.tradingview.com/chart/?symbol=NSE:{d.symbol}" target="_blank"
       style="display:inline-flex;align-items:center;gap:0.3rem;font-size:0.78rem;font-weight:600;
              color:{brd};background:rgba(255,255,255,0.4);border:1px solid {brd};
              border-radius:6px;padding:0.25rem 0.7rem;text-decoration:none;">
      📈 TradingView
    </a>
    <a href="https://www.nseindia.com/get-quotes/equity?symbol={d.symbol}" target="_blank"
       style="display:inline-flex;align-items:center;gap:0.3rem;font-size:0.78rem;font-weight:600;
              color:{brd};background:rgba(255,255,255,0.4);border:1px solid {brd};
              border-radius:6px;padding:0.25rem 0.7rem;text-decoration:none;">
      🏛️ NSE
    </a>
  </div>
</div>"""

    def _section(title: str, items: list, timeframe: str, oversold: bool,
                 expanded: bool) -> None:
        if not items:
            return
        with st.expander(f"{title} ({len(items)} symbol(s))", expanded=expanded):
            cols = st.columns(min(3, len(items)))
            for i, (d, rsi_val) in enumerate(items):
                with cols[i % 3]:
                    st.markdown(_card_html(d, rsi_val, timeframe, oversold),
                                unsafe_allow_html=True)

    daily_os   = [(d, d.daily_rsi)   for d in valid if d.daily_rsi   is not None and d.daily_rsi   < 35]
    daily_ob   = [(d, d.daily_rsi)   for d in valid if d.daily_rsi   is not None and d.daily_rsi   > 70]
    weekly_os  = [(d, d.weekly_rsi)  for d in valid if d.weekly_rsi  is not None and d.weekly_rsi  < 35]
    weekly_ob  = [(d, d.weekly_rsi)  for d in valid if d.weekly_rsi  is not None and d.weekly_rsi  > 70]
    monthly_os = [(d, d.monthly_rsi) for d in valid if d.monthly_rsi is not None and d.monthly_rsi < 35]
    monthly_ob = [(d, d.monthly_rsi) for d in valid if d.monthly_rsi is not None and d.monthly_rsi > 70]

    daily_os.sort(key=lambda x: x[1])
    weekly_os.sort(key=lambda x: x[1])
    monthly_os.sort(key=lambda x: x[1])
    daily_ob.sort(key=lambda x: x[1], reverse=True)
    weekly_ob.sort(key=lambda x: x[1], reverse=True)
    monthly_ob.sort(key=lambda x: x[1], reverse=True)

    st.markdown("### 📊 RSI Signal Sections")

    _section("🟢 Daily RSI < 35 — Oversold (Daily)",    daily_os,   "Daily",   True,  True)
    _section("🔴 Daily RSI > 70 — Overbought (Daily)",   daily_ob,   "Daily",   False, False)
    _section("🟢 Weekly RSI < 35 — Oversold (Weekly)",   weekly_os,  "Weekly",  True,  False)
    _section("🔴 Weekly RSI > 70 — Overbought (Weekly)", weekly_ob,  "Weekly",  False, False)
    _section("🟢 Monthly RSI < 35 — Oversold (Monthly)", monthly_os, "Monthly", True,  False)
    _section("🔴 Monthly RSI > 70 — Overbought (Monthly)",monthly_ob,"Monthly", False, False)

    if not any([daily_os, daily_ob, weekly_os, weekly_ob, monthly_os, monthly_ob]):
        st.info("No RSI signals found. All values are between 35 and 70.")


# ── Base Price Screener ────────────────────────────────────────────────────────

def _fetch_base_price_single(symbol: str) -> dict:
    """Return 3yr SMA (738d), 5yr SMA (1230d), and last-close for one NSE symbol."""
    import yfinance as _yf
    sym = symbol.strip().upper()
    result = {
        "symbol": sym, "company_name": sym,
        "current_price": None,
        "sma_3yr": None, "diff_3yr": None, "diff_3yr_pct": None,
        "sma_5yr": None, "diff_5yr": None, "diff_5yr_pct": None,
        "error": None,
    }
    try:
        t    = _yf.Ticker(f"{sym}.NS")
        hist = t.history(period="7y", interval="1d", auto_adjust=True)
        if hist.empty:
            result["error"] = "No data from Yahoo Finance"
            return result
        closes = hist["Close"].dropna()
        result["current_price"] = round(float(closes.iloc[-1]), 2)
        # Company name
        try:
            fi = t.fast_info
            for attr in ("long_name", "longName", "short_name", "shortName"):
                val = getattr(fi, attr, None)
                if val:
                    result["company_name"] = str(val)
                    break
        except Exception:
            pass
        # 3-year SMA = last 738 trading days
        if len(closes) >= 738:
            sma3 = round(float(closes.tail(738).mean()), 2)
            result["sma_3yr"]      = sma3
            result["diff_3yr"]     = round(result["current_price"] - sma3, 2)
            result["diff_3yr_pct"] = round((result["diff_3yr"] / sma3 * 100) if sma3 else 0, 2)
        # 5-year SMA = last 1230 trading days
        if len(closes) >= 1230:
            sma5 = round(float(closes.tail(1230).mean()), 2)
            result["sma_5yr"]      = sma5
            result["diff_5yr"]     = round(result["current_price"] - sma5, 2)
            result["diff_5yr_pct"] = round((result["diff_5yr"] / sma5 * 100) if sma5 else 0, 2)
    except Exception as exc:
        result["error"] = str(exc)
    return result


def _fetch_base_price_bulk(symbols: list) -> list:
    from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _ac
    results_map: dict = {}
    _empty = lambda s: {
        "symbol": s.strip().upper(), "company_name": s, "error": "Not fetched",
        "current_price": None, "sma_3yr": None, "diff_3yr": None, "diff_3yr_pct": None,
        "sma_5yr": None, "diff_5yr": None, "diff_5yr_pct": None,
    }
    with _TPE(max_workers=6) as ex:
        futures = {ex.submit(_fetch_base_price_single, s): s for s in symbols}
        for fut in _ac(futures):
            sym = futures[fut]
            try:
                results_map[sym.strip().upper()] = fut.result(timeout=120)
            except Exception as e:
                r = _empty(sym); r["error"] = str(e)
                results_map[sym.strip().upper()] = r
    return [results_map.get(s.strip().upper(), _empty(s)) for s in symbols]


def _render_base_price_tab(tab_key: str) -> None:
    dark      = st.session_state.get("dark_mode", False)
    data_key  = f"{tab_key}_data"
    fetch_key = f"{tab_key}_last_fetched_at"

    # ── Input ─────────────────────────────────────────────────────────────────
    tab_m, tab_f = st.tabs(["📝 Enter Symbols", "📁 Upload File"])
    symbols: list = []

    with tab_m:
        txt = st.text_area(
            "Symbols", height=100, label_visibility="collapsed",
            placeholder="RELIANCE, INFY, TCS  or one per line",
            key=f"{tab_key}_manual_input",
        )
        c1, c2 = st.columns([1, 5])
        with c1:
            scan_btn = st.button("🔍 Scan", type="primary",
                                 width='stretch', key=f"{tab_key}_scan_m")
        with c2:
            st.caption("NSE symbols. Fetches 3-year (738d) & 5-year (1230d) SMA from Yahoo Finance.")
        if scan_btn and txt.strip():
            symbols = _parser.parse_text_input(txt)

    with tab_f:
        up = st.file_uploader(
            "Upload CSV/Excel/TXT", type=["csv","xlsx","xls","txt"],
            label_visibility="collapsed", key=f"{tab_key}_file_uploader",
        )
        cf1, cf2, cf3 = st.columns([1, 1, 4])
        with cf1:
            scan_f = st.button("🔍 Scan File", type="primary",
                               width='stretch', key=f"{tab_key}_scan_f")
        with cf2:
            _samp = pd.DataFrame({"symbol": ["RELIANCE","INFY","TCS","HDFCBANK","WIPRO"]})
            st.download_button("📥 Sample", _samp.to_csv(index=False).encode(),
                               "sample.csv", "text/csv", key=f"{tab_key}_sample_dl")
        with cf3:
            st.caption("CSV/Excel: 'symbol' column or first column. TXT: one per line.")
        if scan_f and up:
            symbols = _parser.parse_file(up)
            if not symbols:
                st.warning("⚠️ No valid symbols found in file.")

    # ── Fetch ─────────────────────────────────────────────────────────────────
    if symbols:
        st.markdown(
            f"**Calculating Base Price SMA for {len(symbols)} symbol(s):** "
            f"{', '.join(symbols[:15])}{'…' if len(symbols) > 15 else ''}"
        )
        with st.spinner("📡 Fetching historical data from Yahoo Finance…"):
            data = _fetch_base_price_bulk(symbols)
            valid_syms = [d["symbol"] for d in data if not d.get("error")]
            if valid_syms:
                live_px = _fetch_nse_live_prices_bulk(valid_syms)
                for d in data:
                    lp = live_px.get(d["symbol"])
                    if lp and lp.get("price"):
                        cp = float(lp["price"])
                        d["current_price"] = cp
                        if d["sma_3yr"]:
                            d["diff_3yr"]     = round(cp - d["sma_3yr"], 2)
                            d["diff_3yr_pct"] = round((d["diff_3yr"] / d["sma_3yr"] * 100), 2)
                        if d["sma_5yr"]:
                            d["diff_5yr"]     = round(cp - d["sma_5yr"], 2)
                            d["diff_5yr_pct"] = round((d["diff_5yr"] / d["sma_5yr"] * 100), 2)
        st.session_state[data_key]  = data
        st.session_state[fetch_key] = datetime.now()

    data = st.session_state.get(data_key, [])
    if not data:
        st.info("Enter NSE symbols above and click 🔍 Scan to calculate base prices.")
        return

    fetched_at = st.session_state.get(fetch_key)
    if fetched_at:
        st.caption(f"🕐 Fetched at {fetched_at.strftime('%d %b %Y %H:%M:%S')}")

    # ── Counts ─────────────────────────────────────────────────────────────────
    valid_data = [d for d in data if not d.get("error") and d.get("current_price")]
    buy_3yr  = [d for d in valid_data if d.get("diff_3yr")  is not None and d["diff_3yr"]  < 0]
    buy_5yr  = [d for d in valid_data if d.get("diff_5yr")  is not None and d["diff_5yr"]  < 0]
    sell_3yr = [d for d in valid_data if d.get("diff_3yr")  is not None and d["diff_3yr"] >= 0]
    sell_5yr = [d for d in valid_data if d.get("diff_5yr")  is not None and d["diff_5yr"] >= 0]

    mc1, mc2, mc3, mc4 = st.columns(4)
    mc1.metric("🟢 3yr Buy Signals",  len(buy_3yr))
    mc2.metric("🟢 5yr Buy Signals",  len(buy_5yr))
    mc3.metric("🔴 3yr Don't Buy",    len(sell_3yr))
    mc4.metric("🔴 5yr Don't Buy",    len(sell_5yr))

    # ── Card renderer ──────────────────────────────────────────────────────────
    _g_bg = "rgba(20,83,45,0.4)"  if dark else "#dcfce7"
    _r_bg = "rgba(69,10,10,0.4)"  if dark else "#fee2e2"
    _g_fg = "#4ade80" if dark else "#15803d"
    _r_fg = "#f87171" if dark else "#b91c1c"
    _card_bg  = "#1e2130" if dark else "#f8fafc"
    _card_brd = "rgba(255,255,255,0.08)" if dark else "#e2e8f0"

    def _fmt(v):
        return f"₹{v:,.2f}" if v is not None else "—"

    def _fmt_diff(v, p):
        if v is None:
            return "—"
        sign = "+" if v >= 0 else ""
        return f"{sign}₹{v:,.2f} ({sign}{p:.1f}%)"

    def _render_bps_rows(rows: list) -> None:
        if not rows:
            st.caption("No stocks in this group.")
            return
        for d in rows:
            sym  = d["symbol"]
            name = d.get("company_name") or sym
            if d.get("error"):
                st.error(f"**{sym}**: {d['error'][:100]}")
                continue
            cp = d.get("current_price")
            s3 = d.get("sma_3yr");  d3 = d.get("diff_3yr");  p3 = d.get("diff_3yr_pct") or 0
            s5 = d.get("sma_5yr");  d5 = d.get("diff_5yr");  p5 = d.get("diff_5yr_pct") or 0

            bg3  = _g_bg if (d3 is not None and d3 < 0) else _r_bg
            fg3  = _g_fg if (d3 is not None and d3 < 0) else _r_fg
            sig3 = "✅ BUY" if (d3 is not None and d3 < 0) else "❌ WAIT"
            bg5  = _g_bg if (d5 is not None and d5 < 0) else _r_bg
            fg5  = _g_fg if (d5 is not None and d5 < 0) else _r_fg
            sig5 = "✅ BUY" if (d5 is not None and d5 < 0) else "❌ WAIT"

            tv_url  = f"https://www.tradingview.com/chart/?symbol=NSE:{sym}"
            nse_url = f"https://www.nseindia.com/get-quotes/equity?symbol={sym}"

            st.markdown(
                f'<div style="background:{_card_bg};border-radius:10px;padding:0.7rem 1rem;'
                f'margin-bottom:0.5rem;border:1px solid {_card_brd};">'
                f'<div style="display:flex;justify-content:space-between;align-items:center;'
                f'flex-wrap:wrap;gap:0.5rem;margin-bottom:0.5rem;">'
                f'  <div>'
                f'    <span style="font-weight:800;font-size:1rem;">{sym}</span>'
                f'    <span style="font-size:0.78rem;color:#64748b;margin-left:0.6rem;">{name}</span>'
                f'  </div>'
                f'  <div style="display:flex;gap:0.4rem;">'
                f'    <a href="{tv_url}" target="_blank" style="font-size:0.73rem;padding:3px 9px;'
                f'       background:#2962ff;color:white;border-radius:4px;text-decoration:none;">📈 TradingView</a>'
                f'    <a href="{nse_url}" target="_blank" style="font-size:0.73rem;padding:3px 9px;'
                f'       background:#0d47a1;color:white;border-radius:4px;text-decoration:none;">🏛 NSE</a>'
                f'  </div>'
                f'</div>'
                f'<div style="display:flex;flex-wrap:wrap;gap:1rem;align-items:flex-start;">'
                f'  <div style="min-width:110px;">'
                f'    <div style="font-size:0.72rem;color:#64748b;">Current Price</div>'
                f'    <div style="font-weight:700;">{_fmt(cp)}</div>'
                f'  </div>'
                f'  <div style="min-width:120px;">'
                f'    <div style="font-size:0.72rem;color:#64748b;">3yr SMA (738d)</div>'
                f'    <div style="font-weight:600;">{_fmt(s3)}</div>'
                f'  </div>'
                f'  <div style="min-width:170px;background:{bg3};border-radius:6px;padding:3px 8px;">'
                f'    <div style="font-size:0.72rem;color:{fg3};">Diff vs 3yr · {sig3}</div>'
                f'    <div style="font-weight:700;color:{fg3};">{_fmt_diff(d3, p3)}</div>'
                f'  </div>'
                f'  <div style="min-width:120px;">'
                f'    <div style="font-size:0.72rem;color:#64748b;">5yr SMA (1230d)</div>'
                f'    <div style="font-weight:600;">{_fmt(s5) if s5 else "Need 5yr history"}</div>'
                f'  </div>'
                f'  <div style="min-width:170px;background:{bg5};border-radius:6px;padding:3px 8px;">'
                f'    <div style="font-size:0.72rem;color:{fg5};">Diff vs 5yr · {sig5}</div>'
                f'    <div style="font-weight:700;color:{fg5};">'
                f'      {_fmt_diff(d5, p5) if d5 is not None else "Need 5yr history"}'
                f'    </div>'
                f'  </div>'
                f'</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

    # ── Expandable sections ────────────────────────────────────────────────────
    st.markdown("---")
    with st.expander(f"🟢 3-Year Base Price — Buy Signals ({len(buy_3yr)} stocks)", expanded=True):
        st.markdown(
            "<p style='font-size:0.82rem;color:gray;'>"
            "Current price is <b>BELOW</b> the 3-year (738-day) SMA — potential buy zone.</p>",
            unsafe_allow_html=True)
        _render_bps_rows(buy_3yr)

    with st.expander(f"🟢 5-Year Base Price — Buy Signals ({len(buy_5yr)} stocks)", expanded=True):
        st.markdown(
            "<p style='font-size:0.82rem;color:gray;'>"
            "Current price is <b>BELOW</b> the 5-year (1230-day) SMA — long-term buy zone.</p>",
            unsafe_allow_html=True)
        _render_bps_rows(buy_5yr)

    with st.expander(f"🔴 3-Year Base Price — Don't Buy ({len(sell_3yr)} stocks)", expanded=False):
        st.markdown(
            "<p style='font-size:0.82rem;color:gray;'>"
            "Current price is <b>ABOVE</b> the 3-year (738-day) SMA — trading above base price.</p>",
            unsafe_allow_html=True)
        _render_bps_rows(sell_3yr)

    with st.expander(f"🔴 5-Year Base Price — Don't Buy ({len(sell_5yr)} stocks)", expanded=False):
        st.markdown(
            "<p style='font-size:0.82rem;color:gray;'>"
            "Current price is <b>ABOVE</b> the 5-year (1230-day) SMA — trading above 5-year base.</p>",
            unsafe_allow_html=True)
        _render_bps_rows(sell_5yr)

    errors = [d for d in data if d.get("error")]
    if errors:
        with st.expander(f"⚠️ Errors ({len(errors)})", expanded=False):
            for d in errors:
                st.error(f"**{d['symbol']}**: {d['error']}")


def render_base_price_screener() -> None:
    st.markdown("## 📐 Base Price Screener")
    st.markdown(
        "<p style='color:gray;font-size:0.9rem;margin-bottom:0.5rem;'>"
        "3-Year (738d) &amp; 5-Year (1230d) Simple Moving Average · "
        "🟢 Green = price below SMA (Buy) · 🔴 Red = price above SMA (Don't Buy) · "
        "Live prices from NSE India</p>",
        unsafe_allow_html=True,
    )
    tab_stock, tab_etf = st.tabs(["📊 Base Price Stock Screener", "🏦 Base Price ETF Screener"])
    with tab_stock:
        _render_base_price_tab("bps_stock")
    with tab_etf:
        _render_base_price_tab("bps_etf")


# ── TURTLE TRADING ────────────────────────────────────────────────────────────

def render_turtle_trading():
    from datetime import date as _date
    st.markdown("## 🐢 Turtle Trading")
    st.markdown(
        "<p style='color:gray;font-size:0.9rem;margin-bottom:0.5rem;'>"
        "Turtle Trading strategy screener · Enter symbols to analyse · Calculation columns coming soon</p>",
        unsafe_allow_html=True,
    )

    # ── Reference date ────────────────────────────────────────────────────────
    today = _date.today()
    dcol1, dcol2 = st.columns([2, 5])
    with dcol1:
        saved_date  = st.session_state.get("tt_ref_date") or today
        ref_date_val = st.date_input(
            "Reference Date",
            value=saved_date,
            max_value=today,
            help="Analysis is calculated as of this date.",
            key="tt_date_picker",
        )
        st.session_state.tt_ref_date = ref_date_val
    with dcol2:
        st.markdown(
            "<p style='color:gray;font-size:0.85rem;margin-top:1.9rem;'>"
            "If the selected date falls on a weekend or NSE holiday, "
            "the previous available trading day is used automatically.</p>",
            unsafe_allow_html=True,
        )

    st.markdown("---")

    # ── Input tabs ────────────────────────────────────────────────────────────
    tab_manual, tab_file = st.tabs(["📝 Enter Symbols", "📁 Upload File"])
    symbols: list = []

    with tab_manual:
        txt = st.text_area(
            "Stock symbols",
            placeholder="Single stock:  TCS\nMultiple:  RELIANCE, INFY, TCS\nOne per line also works",
            height=100,
            label_visibility="collapsed",
            key="tt_manual_input",
        )
        c1, c2 = st.columns([1, 5])
        with c1:
            scan_btn = st.button("🔍 Scan", type="primary", width='stretch', key="tt_scan_m")
        with c2:
            st.caption("Use NSE symbols. Data fetched from NSE India.")
        if scan_btn and txt.strip():
            st.session_state["_tt_pending_scan_name"] = f"Turtle Trading — {_now_ist().strftime('%d %b %H:%M')}"
            symbols = _parser.parse_text_input(txt)

    with tab_file:
        up = st.file_uploader(
            "Upload CSV / Excel / TXT", type=["csv", "xlsx", "xls", "txt"],
            label_visibility="collapsed", key="tt_file_uploader",
        )
        c1, c2, c3 = st.columns([1, 1, 4])
        with c1:
            scan_f = st.button("🔍 Scan File", type="primary", width='stretch', key="tt_scan_f")
        with c2:
            sample = pd.DataFrame({"symbol": ["RELIANCE", "INFY", "TCS", "HDFCBANK", "WIPRO"]})
            st.download_button("📥 Sample", sample.to_csv(index=False).encode(),
                               "sample_stocks.csv", "text/csv", key="tt_sample_dl")
        with c3:
            st.caption("CSV/Excel: 'symbol' column or first column. TXT: one per line.")
        if scan_f and up:
            st.session_state["_tt_pending_scan_name"] = up.name
            symbols = _parser.parse_file(up)
            if not symbols:
                st.warning("⚠️ No valid symbols found in file.")

    # ── Fetch (placeholder — columns will be added when logic is provided) ────
    if symbols:
        scan_name = st.session_state.pop("_tt_pending_scan_name", "Turtle Trading")
        st.session_state["tt_last_scan_symbols"] = symbols
        st.session_state["tt_active_scan_name"]  = scan_name
        st.session_state["tt_last_fetched_at"]   = datetime.now()
        # Store raw symbol list as placeholder data until calculation logic is added
        st.session_state["tt_data"] = [{"symbol": s} for s in symbols]

    # ── Refresh ───────────────────────────────────────────────────────────────
    if st.session_state.get("tt_do_refresh"):
        st.session_state.tt_do_refresh = False
        syms = st.session_state.get("tt_last_scan_symbols", [])
        if syms:
            st.session_state["tt_data"]          = [{"symbol": s} for s in syms]
            st.session_state["tt_last_fetched_at"] = datetime.now()

    # ── Results ───────────────────────────────────────────────────────────────
    data = st.session_state.get("tt_data", [])
    if not data:
        return

    fetched_at = st.session_state.get("tt_last_fetched_at")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Symbols Loaded", len(data))
    c2.metric("Reference Date", str(ref_date_val))
    c3.metric("Status", "Ready")
    if fetched_at:
        c4.metric("Last Fetched", _fmt_ts(fetched_at))

    rb1, rb2 = st.columns([1, 5])
    with rb1:
        if st.button("🔄 Refresh", width='stretch', key="tt_refresh_btn"):
            st.session_state.tt_do_refresh = True
            st.rerun()
    with rb2:
        if _is_market_open():
            st.success("🟢 **MARKET OPEN**")
        else:
            st.info("⏸ Market closed — showing latest available prices.")

    st.markdown("---")

    # ── Table (placeholder columns — calculation fields to be added) ──────────
    dark = st.session_state.get("dark_mode", False)
    hdr_bg  = "#1e2130" if dark else "#f1f5f9"
    hdr_col = "#94a3b8" if dark else "#475569"
    st.markdown(
        f"<div style='background:{hdr_bg};border-radius:0.5rem;padding:0.6rem 1rem;"
        f"color:{hdr_col};font-size:0.8rem;font-weight:600;margin-bottom:0.5rem;'>"
        f"⚙️ &nbsp; Calculation columns will be added here — "
        f"{len(data)} symbol(s) ready for analysis</div>",
        unsafe_allow_html=True,
    )

    df = pd.DataFrame(data)
    st.dataframe(df, width='stretch', hide_index=True)

    # ── Export ────────────────────────────────────────────────────────────────
    ec1, ec2 = st.columns([1, 1])
    with ec1:
        st.download_button(
            "📥 CSV", df.to_csv(index=False).encode(),
            f"turtle_trading_{ref_date_val}.csv", "text/csv", key="tt_csv_dl",
        )
    with ec2:
        try:
            import io as _io
            _buf = _io.BytesIO()
            df.to_excel(_buf, index=False)
            st.download_button(
                "📥 Excel", _buf.getvalue(),
                f"turtle_trading_{ref_date_val}.xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="tt_excel_dl",
            )
        except Exception:
            pass


# ── GURU SMART BUSINESS ───────────────────────────────────────────────────────

def render_guru_smart_business():
    tab_dd, tab_ll, tab_eff = st.tabs(["🛒 Daily Dukaan", "🌱 Life Long ETFs", "🌳 Eshwarana Fruit Forest"])
    with tab_dd:
        _render_daily_dukaan()
    with tab_ll:
        _render_ll_etf()
    with tab_eff:
        _render_eff()


def _render_daily_dukaan():
    from datetime import date as _date
    _auto_restore_scan("dd_data", _load_dd_session, "daily_dukaan")
    st.markdown(
        "<p style='color:gray;font-size:0.9rem;margin-bottom:0.5rem;'>"
        "20-Day Moving Average analysis for stocks and ETFs · Data via NSE India (nsepython)</p>",
        unsafe_allow_html=True,
    )

    # ── Reference date ────────────────────────────────────────────────────────
    today = _date.today()
    if st.session_state.pop("_live_pending_dd", False):
        st.session_state["dd_date_picker"] = today
    dcol1, dcol2 = st.columns([2, 5])
    with dcol1:
        saved_date  = st.session_state.get("dd_ref_date") or today
        ref_date_val = st.date_input(
            "Reference Date",
            value=saved_date,
            max_value=today,
            help="20 DMA is calculated as of this date. Weekends / market holidays automatically fall back to the previous trading day.",
            key="dd_date_picker",
        )
        st.session_state.dd_ref_date = ref_date_val
    with dcol2:
        st.markdown(
            "<p style='color:gray;font-size:0.85rem;margin-top:1.9rem;'>"
            "If the selected date falls on a weekend or NSE holiday, the previous available trading day is used automatically.</p>",
            unsafe_allow_html=True,
        )

    st.markdown("---")

    # ── Input tabs ────────────────────────────────────────────────────────────
    tab_manual, tab_file = st.tabs(["📝 Enter Symbols", "📁 Upload File"])
    symbols: list = []

    with tab_manual:
        txt = st.text_area(
            "Stock symbols",
            placeholder="Single stock:  TCS\nMultiple:  RELIANCE, INFY, TCS\nOne per line also works",
            height=100,
            label_visibility="collapsed",
            key="dd_manual_input",
        )
        c1, c2 = st.columns([1, 5])
        with c1:
            scan_btn = st.button("🔍 Scan", type="primary", use_container_width=True, key="dd_scan_m")
        with c2:
            st.caption("Use NSE symbols (stocks or ETFs). Data fetched from NSE India.")
        if scan_btn and txt.strip():
            st.session_state["_dd_pending_scan_name"] = f"Daily Dukaan — {_now_ist().strftime('%d %b %H:%M')}"
            st.session_state["_dd_is_file_upload"]    = False
            symbols = _parser.parse_text_input(txt)

    with tab_file:
        up = st.file_uploader(
            "Upload CSV / Excel / TXT", type=["csv", "xlsx", "xls", "txt"],
            label_visibility="collapsed", key="dd_file_uploader",
        )
        c1, c2, c3 = st.columns([1, 1, 4])
        with c1:
            scan_f = st.button("🔍 Scan File", type="primary", use_container_width=True, key="dd_scan_f")
        with c2:
            sample = pd.DataFrame({"symbol": ["RELIANCE", "INFY", "TCS", "HDFCBANK", "WIPRO"]})
            st.download_button("📥 Sample", sample.to_csv(index=False).encode(),
                               "sample_stocks.csv", "text/csv", key="dd_sample_dl")
        with c3:
            st.caption("CSV/Excel: 'symbol' column or first column. TXT: one per line.")
        if scan_f and up:
            st.session_state["_dd_pending_scan_name"] = up.name
            st.session_state["_dd_is_file_upload"]    = True
            symbols = _parser.parse_file(up)
            if not symbols:
                st.warning("⚠️ No valid symbols found in file.")

    # ── Fetch ─────────────────────────────────────────────────────────────────
    if symbols:
        scan_name = st.session_state.pop("_dd_pending_scan_name", "Daily Dukaan")
        is_file   = st.session_state.pop("_dd_is_file_upload", False)
        st.markdown(
            f"**Calculating 20 DMA for {len(symbols)} symbol(s):** "
            f"{', '.join(symbols[:15])}{'…' if len(symbols) > 15 else ''}"
        )
        _do_dd_fetch(symbols, scan_name, ref_date=ref_date_val, save_to_history=True)
        # Date-snap: use the most common ref_date across all valid records so a single
        # low-liquidity ETF (last traded a day earlier) doesn't drag the picker backward.
        _snap_dates = [r.ref_date for r in st.session_state.get("dd_data", [])
                       if r.ref_date and not r.error]
        if _snap_dates:
            from collections import Counter as _Ctr
            from datetime import date as _dp
            _majority_date = _Ctr(_snap_dates).most_common(1)[0][0]
            try:
                _actual = _dp.fromisoformat(_majority_date)
                if _actual != ref_date_val:
                    st.session_state["dd_date_picker"] = _actual
                    st.rerun()
            except Exception:
                pass

    # ── Refresh ───────────────────────────────────────────────────────────────
    if st.session_state.get("dd_do_refresh"):
        st.session_state.dd_do_refresh = False
        syms = st.session_state.get("dd_last_scan_symbols", [])
        if not syms:
            syms = [d.symbol for d in st.session_state.get("dd_data", [])]
        if syms:
            scan_name = st.session_state.get("dd_active_scan_name") or "Daily Dukaan Refresh"
            st.markdown(f"**Refreshing 20 DMA for {len(syms)} symbol(s)…**")
            _do_dd_fetch(syms, scan_name, ref_date=ref_date_val, save_to_history=True)
            _snap_dates2 = [r.ref_date for r in st.session_state.get("dd_data", [])
                            if r.ref_date and not r.error]
            if _snap_dates2:
                from collections import Counter as _Ctr2
                from datetime import date as _dp2
                _majority2 = _Ctr2(_snap_dates2).most_common(1)[0][0]
                try:
                    _actual2 = _dp2.fromisoformat(_majority2)
                    if _actual2 != ref_date_val:
                        st.session_state["dd_date_picker"] = _actual2
                        st.rerun()
                except Exception:
                    pass

    # ── Render results ────────────────────────────────────────────────────────
    data = st.session_state.get("dd_data", [])
    if not data:
        return

    # Trading Day = most recent ref_date across all valid records
    # (the majority of liquid stocks/ETFs determine the actual trading day)
    all_dates = [d.ref_date for d in data if d.ref_date and not d.error]
    if all_dates:
        from collections import Counter
        date_counts = Counter(all_dates)
        latest_ref  = date_counts.most_common(1)[0][0]  # most frequent date = actual trading day
    else:
        latest_ref = "—"
    fetched_at = st.session_state.get("dd_last_fetched_at")
    valid      = [d for d in data if not d.error]

    # Summary metrics
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Symbols Scanned", len(data))
    c2.metric("Trading Day",     latest_ref, help=f"Trading day used for analysis: {latest_ref}")
    c3.metric("Loaded OK",       len(valid))
    if fetched_at:
        c4.metric("Last Fetched", (_fmt_ts(fetched_at) if fetched_at else "—"))

    # Refresh + Live buttons + market status badge
    rb1, rb2, rb3 = st.columns([1, 1, 4])
    with rb1:
        if st.button("🔄 Refresh", use_container_width=True, key="dd_refresh_btn"):
            st.session_state.dd_do_refresh = True
            st.rerun()
    with rb2:
        _live_btn("dd", "dd_do_refresh", "dd_live_btn")
    with rb3:
        if _is_market_open():
            st.success("🟢 **MARKET OPEN** — Live fetches real-time NSE prices.")
        else:
            from datetime import timezone, timedelta
            IST = timezone(timedelta(hours=5, minutes=30))
            ist_now = datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")
            st.info(
                f"⏸ Market closed ({ist_now}). "
                "Live / Refresh fetches the latest official NSE close price."
            )

    st.markdown("---")
    _render_daily_dukaan_table(data)


def _render_daily_dukaan_table(data: list) -> None:
    """Main 20-DMA table + collapsible suggestions panel (top 10 cheapest)."""
    dark    = st.session_state.get("dark_mode", False)
    valid   = [d for d in data if not d.error]
    errors  = [d for d in data if d.error]

    # ── NSE price source banner ───────────────────────────────────────────────
    nse_live_records  = [d for d in valid if d.price_source == "NSE Live"  and d.nse_live_time]
    nse_close_records = [d for d in valid if d.price_source == "NSE Close" and d.nse_live_time]
    yf_records        = [d for d in valid if d.price_source not in ("NSE Live", "NSE Close")]

    if nse_live_records:
        sample_ts = nse_live_records[0].nse_live_time
        live_bg  = "#052e16" if dark else "#f0fdf4"
        live_brd = "#16a34a"
        st.markdown(
            f"<div style='background:{live_bg};border:1px solid {live_brd};border-radius:8px;"
            f"padding:0.6rem 1rem;margin-bottom:0.8rem;font-size:0.9rem;color:{live_brd};font-weight:600;'>"
            f"📡 LIVE PRICES — NSE last update: <span style='font-family:monospace;'>{sample_ts}</span>"
            f" &nbsp;·&nbsp; {len(nse_live_records)} symbol(s) live"
            f"{f', {len(yf_records)} price(s) from NSE history only (no live quote)' if yf_records else ''}"
            f"</div>",
            unsafe_allow_html=True,
        )
    elif nse_close_records:
        sample_ts  = nse_close_records[0].nse_live_time
        sample_dt  = nse_close_records[0].ref_date
        close_bg   = "#172554" if dark else "#eff6ff"
        close_brd  = "#3b82f6"
        st.markdown(
            f"<div style='background:{close_bg};border:1px solid {close_brd};border-radius:8px;"
            f"padding:0.6rem 1rem;margin-bottom:0.8rem;font-size:0.9rem;color:{close_brd};font-weight:600;'>"
            f"📊 NSE CLOSE — Trading date: <span style='font-family:monospace;'>{sample_dt}</span>"
            f" &nbsp;·&nbsp; NSE timestamp: <span style='font-family:monospace;'>{sample_ts}</span>"
            f" &nbsp;·&nbsp; {len(nse_close_records)} symbol(s)"
            f"{f', {len(yf_records)} on Yahoo Finance fallback' if yf_records else ''}"
            f"</div>",
            unsafe_allow_html=True,
        )

    # ── Stale-date warning (suppressed on any weekday when user picked today) ────
    picker_date  = st.session_state.get("dd_ref_date")
    data_date    = next((d.ref_date for d in valid if d.ref_date), None)
    has_nse_data = bool(nse_live_records or nse_close_records)
    _warn_today  = (picker_date and str(picker_date) == str(date.today()))
    _warn_wkday  = date.today().weekday() < 5
    is_today_session = _warn_today and _warn_wkday  # any weekday: open OR just closed
    if picker_date and data_date and not has_nse_data and not is_today_session:
        try:
            if str(picker_date) != data_date:
                st.warning(
                    f"⚠️ Table shows data for **{data_date}** but the date picker is set to "
                    f"**{picker_date}**. Click **🔄 Refresh** to reload for the selected date.",
                    icon=None,
                )
        except Exception:
            pass

    # ── 🥇 MUST BUY card — #1 cheapest (most below 20 DMA) ──────────────────────
    ranked_all = sorted(
        [d for d in valid if d.pct_vs_dma20 is not None],
        key=lambda x: x.pct_vs_dma20,
    )
    if ranked_all:
        best = ranked_all[0]
        if best.pct_vs_dma20 < 0:   # only show when genuinely below DMA
            mb_bg     = "#052e16" if dark else "#f0fdf4"
            mb_border = "#16a34a"
            mb_text   = "#bbf7d0" if dark else "#14532d"
            mb_label  = "#86efac" if dark else "#15803d"
            diff_str  = f"{best.price_vs_dma20:+.2f}" if best.price_vs_dma20 is not None else "—"
            ts_str    = (best.nse_live_time
                         if best.price_source == "NSE Live"
                         else (best.ref_date or "—"))
            st.markdown(
                f"""
<div style="background:{mb_bg};border:2px solid {mb_border};border-radius:12px;
            padding:1rem 1.4rem;margin-bottom:1rem;">
  <div style="font-size:1.3rem;font-weight:800;color:{mb_border};margin-bottom:0.5rem;">
    🥇 MUST BUY — Best Value Pick (Most Below 20 DMA)
  </div>
  <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:0.8rem;">
    <div>
      <div style="font-size:1.5rem;font-weight:800;color:{mb_border};">{best.symbol}</div>
      <div style="font-size:0.9rem;color:{mb_text};">{best.company_name or best.symbol}</div>
    </div>
    <div style="text-align:right;">
      <div style="font-size:1.6rem;font-weight:800;color:#dc2626;">{best.pct_vs_dma20:+.2f}%</div>
      <div style="font-size:0.85rem;color:{mb_label};">
        Price ₹{best.current_price:,.2f} &nbsp;·&nbsp; 20 DMA ₹{best.dma20:,.2f}
        &nbsp;·&nbsp; Diff {diff_str} &nbsp;·&nbsp; {ts_str}
      </div>
    </div>
  </div>
  <div style="display:flex;gap:0.5rem;margin-top:0.75rem;padding-top:0.65rem;border-top:1px solid {mb_border}40;">
    <a href="https://www.tradingview.com/chart/?symbol=NSE:{best.symbol}" target="_blank"
       style="display:inline-flex;align-items:center;gap:0.3rem;font-size:0.78rem;font-weight:600;
              color:{mb_border};background:rgba(255,255,255,0.4);border:1px solid {mb_border};
              border-radius:6px;padding:0.25rem 0.7rem;text-decoration:none;">
      📈 TradingView
    </a>
    <a href="https://www.nseindia.com/get-quotes/equity?symbol={best.symbol}" target="_blank"
       style="display:inline-flex;align-items:center;gap:0.3rem;font-size:0.78rem;font-weight:600;
              color:{mb_border};background:rgba(255,255,255,0.4);border:1px solid {mb_border};
              border-radius:6px;padding:0.25rem 0.7rem;text-decoration:none;">
      🏛️ NSE
    </a>
  </div>
</div>
""",
                unsafe_allow_html=True,
            )

    # ── Main data table ───────────────────────────────────────────────────────
    rows = []
    for d in valid:
        # "Price As Of" shows NSE live timestamp when live, otherwise the trading date
        price_as_of = d.nse_live_time if d.price_source == "NSE Live" else (d.ref_date or "—")
        rows.append({
            "Symbol":            d.symbol,
            "Company Name":      d.company_name or d.symbol,
            "Current Price (₹)": d.current_price,
            "Price As Of":       price_as_of,
            "20 DMA (₹)":        d.dma20,
            "Price − 20 DMA":    d.price_vs_dma20,
            "% vs 20 DMA":       d.pct_vs_dma20,
        })

    if rows:
        df = pd.DataFrame(rows)
        df = _df_filter_sort(df, "daily_dukaan")
        num_col = st.column_config.NumberColumn
        txt_col = st.column_config.TextColumn
        st.dataframe(
            df, hide_index=True, width='stretch',
            column_config={
                "Symbol":            txt_col("Symbol",            width="small"),
                "Company Name":      txt_col("Company Name",      width="large"),
                "Current Price (₹)": num_col("Current Price (₹)", format="₹%.2f"),
                "Price As Of":       txt_col("Price As Of",       width="medium"),
                "20 DMA (₹)":        num_col("20 DMA (₹)",        format="₹%.2f"),
                "Price − 20 DMA":    num_col("Price − 20 DMA",    format="%+.2f"),
                "% vs 20 DMA":       num_col("% vs 20 DMA",       format="%+.2f%%"),
            },
        )
    else:
        st.info("No data to display. Scan some symbols first.")

    if errors:
        with st.expander(f"⚠️ {len(errors)} symbol(s) could not be fetched"):
            for d in errors:
                st.markdown(f"- **{d.symbol}**: {d.error}")

    # ── Suggestions (collapsible) ─────────────────────────────────────────────
    ranked = sorted(
        [d for d in valid if d.pct_vs_dma20 is not None],
        key=lambda x: x.pct_vs_dma20,   # most negative first = cheapest
    )
    top10 = ranked[:10]

    if len(ranked) >= 2:
        with st.expander("💡 Top 10 Suggestions — Best Value Picks (Most Below 20 DMA)", expanded=True):
            st.markdown(
                "<p style='color:gray;font-size:0.85rem;margin-bottom:0.8rem;'>"
                "Stocks / ETFs trading furthest <b>below</b> their 20-day moving average — "
                "ranked by largest negative % gap, indicating the best potential for a mean-reversion bounce.</p>",
                unsafe_allow_html=True,
            )

            # Styled rank cards
            for rank, d in enumerate(top10, start=1):
                pct  = d.pct_vs_dma20 or 0
                diff = d.price_vs_dma20 or 0
                pct_color  = "#16a34a" if pct >= 0 else "#dc2626"
                pct_sign   = "+" if pct >= 0 else ""
                diff_sign  = "+" if diff >= 0 else ""
                medal      = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"][rank - 1]
                card_bg    = "#161b22" if dark else "#ffffff"
                border_c   = "#30363d" if dark else "#dde3ec"
                val_c      = "#c9d1d9" if dark else "#111827"
                label_c    = "#8b949e" if dark else "#6b7280"

                st.markdown(f"""
                <div style="background:{card_bg};border:1px solid {border_c};border-radius:12px;
                            padding:0.9rem 1.2rem;margin-bottom:0.5rem;">
                  <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:0.5rem;">
                    <div style="display:flex;align-items:center;gap:0.8rem;">
                      <span style="font-size:1.5rem;">{medal}</span>
                      <div>
                        <div style="font-size:1rem;font-weight:700;color:{val_c};">{d.symbol}</div>
                        <div style="font-size:0.8rem;color:{label_c};">{d.company_name or d.symbol}</div>
                        <div style="display:flex;gap:0.35rem;margin-top:0.25rem;">
                          <a href="https://www.tradingview.com/chart/?symbol=NSE:{d.symbol}" target="_blank"
                             style="font-size:0.72rem;font-weight:600;color:#2563eb;text-decoration:none;
                                    background:rgba(37,99,235,0.08);border:1px solid #bfdbfe;
                                    border-radius:4px;padding:0.1rem 0.4rem;">📈 TradingView</a>
                          <a href="https://www.nseindia.com/get-quotes/equity?symbol={d.symbol}" target="_blank"
                             style="font-size:0.72rem;font-weight:600;color:#2563eb;text-decoration:none;
                                    background:rgba(37,99,235,0.08);border:1px solid #bfdbfe;
                                    border-radius:4px;padding:0.1rem 0.4rem;">🏛️ NSE</a>
                        </div>
                      </div>
                    </div>
                    <div style="text-align:right;">
                      <div style="font-size:1.3rem;font-weight:700;color:{pct_color};">
                        {pct_sign}{pct:.2f}%
                      </div>
                      <div style="font-size:0.8rem;color:{label_c};">
                        ₹{d.current_price:,.2f} &nbsp;·&nbsp; 20 DMA ₹{d.dma20:,.2f}
                        &nbsp;·&nbsp; Diff {diff_sign}{diff:.2f}
                        &nbsp;·&nbsp; {d.nse_live_time if d.price_source == "NSE Live" else (d.ref_date or "—")}
                      </div>
                    </div>
                  </div>
                </div>
                """, unsafe_allow_html=True)

            st.markdown("<br>", unsafe_allow_html=True)

            # Also show as a sortable table for reference
            sug_rows = [
                {
                    "Rank":          i + 1,
                    "% vs 20 DMA":   top10[i].pct_vs_dma20,
                    "Symbol":        top10[i].symbol,
                    "Company Name":  top10[i].company_name or top10[i].symbol,
                }
                for i in range(len(top10))
            ]
            sdf = pd.DataFrame(sug_rows)
            st.dataframe(
                sdf, hide_index=True, width='stretch',
                column_config={
                    "Rank":         st.column_config.NumberColumn("Rank",         format="%d",        width="small"),
                    "% vs 20 DMA":  st.column_config.NumberColumn("% vs 20 DMA",  format="%+.2f%%",   width="small"),
                    "Symbol":       st.column_config.TextColumn("Symbol",         width="small"),
                    "Company Name": st.column_config.TextColumn("Company Name",   width="large"),
                },
            )


# ── Life Long ETFs ────────────────────────────────────────────────────────────

def _render_ll_etf():
    from datetime import date as _date
    _auto_restore_scan("ll_etf_data", _load_ll_etf_session, "ll_etf")

    st.markdown(
        "<p style='color:gray;font-size:0.9rem;margin-bottom:0.5rem;'>"
        "Long-term ETF analysis — 52W Low, 3-Year &amp; 5-Year average prices vs Current Price · Data via NSE India (nsepython) · 5-Year history via Yahoo Finance</p>",
        unsafe_allow_html=True,
    )

    # ── Reference date ────────────────────────────────────────────────────────
    today = _date.today()
    if st.session_state.pop("_live_pending_ll_etf", False):
        st.session_state["ll_etf_date_picker"] = today
    dcol1, dcol2 = st.columns([2, 5])
    with dcol1:
        saved_date   = st.session_state.get("ll_etf_ref_date") or today
        ref_date_val = st.date_input(
            "Reference Date",
            value=saved_date,
            max_value=today,
            help="Prices and averages are calculated as of this date. Weekends / market holidays automatically fall back to the previous trading day.",
            key="ll_etf_date_picker",
        )
        st.session_state.ll_etf_ref_date = ref_date_val
    with dcol2:
        st.markdown(
            "<p style='color:gray;font-size:0.85rem;margin-top:1.9rem;'>"
            "If the selected date falls on a weekend or NSE holiday, the previous available trading day is used automatically.</p>",
            unsafe_allow_html=True,
        )

    st.markdown("---")

    # ── Input tabs ────────────────────────────────────────────────────────────
    tab_manual, tab_file = st.tabs(["📝 Enter Symbols", "📁 Upload File"])
    symbols: list = []

    with tab_manual:
        txt = st.text_area(
            "ETF/Stock symbols",
            placeholder="Single symbol:  NIFTYBEES\nMultiple:  NIFTYBEES, BANKBEES, GOLDBEES\nOne per line also works",
            height=100,
            label_visibility="collapsed",
            key="ll_etf_manual_input",
        )
        c1, c2 = st.columns([1, 5])
        with c1:
            scan_btn = st.button("🔍 Scan", type="primary", width='stretch', key="ll_etf_scan_m")
        with c2:
            st.caption("Use NSE symbols. Current price via NSE India · 5-year history via Yahoo Finance.")
        if scan_btn and txt.strip():
            st.session_state["_ll_etf_pending_scan_name"] = f"Life Long ETF — {_now_ist().strftime('%d %b %H:%M')}"
            st.session_state["_ll_etf_is_file_upload"]    = False
            symbols = _parser.parse_text_input(txt)

    with tab_file:
        up = st.file_uploader(
            "Upload CSV / Excel / TXT", type=["csv", "xlsx", "xls", "txt"],
            label_visibility="collapsed", key="ll_etf_file_uploader",
        )
        c1, c2, c3 = st.columns([1, 1, 4])
        with c1:
            scan_f = st.button("🔍 Scan File", type="primary", width='stretch', key="ll_etf_scan_f")
        with c2:
            sample = pd.DataFrame({"symbol": ["NIFTYBEES", "BANKBEES", "GOLDBEES", "JUNIORBEES", "ITBEES"]})
            st.download_button("📥 Sample", sample.to_csv(index=False).encode(),
                               "sample_ll_etfs.csv", "text/csv", key="ll_etf_sample_dl")
        with c3:
            st.caption("CSV/Excel: 'symbol' column or first column. TXT: one per line.")
        if scan_f and up:
            st.session_state["_ll_etf_pending_scan_name"] = up.name
            st.session_state["_ll_etf_is_file_upload"]    = True
            symbols = _parser.parse_file(up)
            if not symbols:
                st.warning("⚠️ No valid symbols found in file.")

    # ── Fetch ─────────────────────────────────────────────────────────────────
    if symbols:
        scan_name = st.session_state.pop("_ll_etf_pending_scan_name", "Life Long ETF")
        is_file   = st.session_state.pop("_ll_etf_is_file_upload", False)
        st.markdown(
            f"**Fetching Life Long ETF data for {len(symbols)} symbol(s):** "
            f"{', '.join(symbols[:15])}{'…' if len(symbols) > 15 else ''}"
        )
        _do_ll_etf_fetch(symbols, scan_name, ref_date=ref_date_val, save_to_history=True)

    # ── Refresh ───────────────────────────────────────────────────────────────
    if st.session_state.get("ll_etf_do_refresh"):
        st.session_state.ll_etf_do_refresh = False
        syms = st.session_state.get("ll_etf_last_scan_symbols", [])
        if not syms:
            syms = [d.symbol for d in st.session_state.get("ll_etf_data", [])]
        if syms:
            scan_name = st.session_state.get("ll_etf_active_scan_name") or "Life Long ETF Refresh"
            st.markdown(f"**Refreshing Life Long ETF data for {len(syms)} symbol(s)…**")
            _do_ll_etf_fetch(syms, scan_name, ref_date=ref_date_val, save_to_history=True)

    # ── Render results ────────────────────────────────────────────────────────
    data = st.session_state.get("ll_etf_data", [])
    if not data:
        return

    valid      = [d for d in data if not d.error]
    fetched_at = st.session_state.get("ll_etf_last_fetched_at")
    all_dates  = [d.ref_date for d in valid if d.ref_date]
    if all_dates:
        latest_ref = max(all_dates)   # most recent NSE trading date across all records
    else:
        latest_ref = "—"

    # Summary metrics
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Symbols Scanned", len(data))
    c2.metric("Trading Day",     latest_ref, help=f"Trading day used for analysis: {latest_ref}")
    c3.metric("Loaded OK",       len(valid))
    if fetched_at:
        c4.metric("Last Fetched", (_fmt_ts(fetched_at) if fetched_at else "—"))

    # Refresh + Live buttons
    rb1, rb2, rb3 = st.columns([1, 1, 4])
    with rb1:
        if st.button("🔄 Refresh", width='stretch', key="ll_etf_refresh_btn"):
            st.session_state.ll_etf_do_refresh = True
            st.rerun()
    with rb2:
        _live_btn("ll_etf", "ll_etf_do_refresh", "ll_etf_live_btn")
    with rb3:
        if _is_market_open():
            st.success("🟢 **MARKET OPEN** — Live fetches real-time NSE prices.")
        else:
            from datetime import timezone, timedelta
            IST = timezone(timedelta(hours=5, minutes=30))
            ist_now = datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")
            st.info(
                f"⏸ Market closed ({ist_now}). "
                "Live / Refresh fetches the latest official NSE close price."
            )

    st.markdown("---")
    _render_ll_etf_table(data)


def _render_ll_etf_table(data: list) -> None:
    """Main Life Long ETF table + 3 collapsible ranked sections."""
    dark    = st.session_state.get("dark_mode", False)
    valid   = [d for d in data if not d.error]
    errors  = [d for d in data if d.error]

    # ── Main data table ───────────────────────────────────────────────────────
    rows = []
    for d in valid:
        price_as_of = d.nse_live_time if d.price_source == "NSE Live" else (d.ref_date or "—")
        rows.append({
            "Symbol":                     d.symbol,
            "Company Name":               d.company_name or d.symbol,
            "Current Price (₹)":          d.current_price,
            "Price As of Date":           price_as_of,
            "Price on the Date (₹)":      d.price_on_date,
            "52W Low (₹)":                d.week52_low,
            "Price − 52W Low":            d.diff_52w_low,
            "% vs 52W Low":               d.pct_vs_52w_low,
            "3Y Avg Price (₹)":           d.avg_price_3y,
            "5Y Avg Price (₹)":           d.avg_price_5y,
            "Price − 3Y Avg":             d.diff_3y,
            "% vs 3Y Avg":                d.pct_vs_3y,
            "Price − 5Y Avg":             d.diff_5y,
            "% vs 5Y Avg":                d.pct_vs_5y,
        })

    if rows:
        df = pd.DataFrame(rows)
        df = _df_filter_sort(df, "ll_etf")
        num_col = st.column_config.NumberColumn
        txt_col = st.column_config.TextColumn

        # Export buttons
        ec1, ec2, *_ = st.columns([1, 1, 4])
        with ec1:
            st.download_button(
                "📥 CSV", df.to_csv(index=False).encode(),
                f"ll_etf_{_now_ist().strftime('%Y%m%d_%H%M')}.csv", "text/csv",
                key="ll_etf_csv_dl",
            )
        with ec2:
            try:
                import io
                buf = io.BytesIO()
                df.to_excel(buf, index=False, engine="openpyxl")
                st.download_button("📥 Excel", buf.getvalue(),
                                   f"ll_etf_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx",
                                   key="ll_etf_xls_dl")
            except Exception:
                pass

        st.dataframe(
            df, hide_index=True, width='stretch',
            column_config={
                "Symbol":                txt_col("Symbol",              width="small"),
                "Company Name":          txt_col("Company Name",        width="large"),
                "Current Price (₹)":     num_col("Current Price (₹)",   format="₹%.2f"),
                "Price As of Date":      txt_col("Price As of Date",    width="medium"),
                "Price on the Date (₹)": num_col("Price on the Date",   format="₹%.2f"),
                "52W Low (₹)":           num_col("52W Low (₹)",         format="₹%.2f"),
                "Price − 52W Low":       num_col("Price − 52W Low",     format="%+.2f"),
                "% vs 52W Low":          num_col("% vs 52W Low",        format="%+.2f%%"),
                "3Y Avg Price (₹)":      num_col("3Y Avg Price (₹)",    format="₹%.2f"),
                "5Y Avg Price (₹)":      num_col("5Y Avg Price (₹)",    format="₹%.2f"),
                "Price − 3Y Avg":        num_col("Price − 3Y Avg",      format="%+.2f"),
                "% vs 3Y Avg":           num_col("% vs 3Y Avg",         format="%+.2f%%"),
                "Price − 5Y Avg":        num_col("Price − 5Y Avg",      format="%+.2f"),
                "% vs 5Y Avg":           num_col("% vs 5Y Avg",         format="%+.2f%%"),
            },
        )
    else:
        st.info("No data to display. Scan some symbols first.")

    if errors:
        with st.expander(f"⚠️ {len(errors)} symbol(s) could not be fetched"):
            for d in errors:
                st.markdown(f"- **{d.symbol}**: {d.error}")

    if not valid:
        return

    st.markdown("---")

    # ── Helper: render top-10 ranked section (highlight cards + table) ──────
    def _rank_section(title: str, metric_key: str, diff_key: str,
                      pct_col: str, ref_col: str, ref_label: str,
                      ref_name: str, label_fn, detail_fn,
                      expanded: bool = True) -> None:
        # Sort ascending: most negative (most below ref price) = Rank 1
        ranked_all = sorted(
            [d for d in valid if getattr(d, metric_key) is not None],
            key=lambda x: getattr(x, metric_key),
        )
        if len(ranked_all) < 2:
            return

        top10 = ranked_all[:10]

        with st.expander(title, expanded=expanded):
            st.markdown(
                "<p style='color:gray;font-size:0.85rem;margin-bottom:0.8rem;'>"
                "Top 10 symbols trading furthest <b>below</b> their reference price — "
                "ranked by largest negative % gap (Rank 1 = best value pick). "
                "Negative % = below reference · Positive % = above reference.</p>",
                unsafe_allow_html=True,
            )

            # ── Top-2 highlight cards (MUST BUY style) ────────────────────────
            highlight_colors = [
                # (border/text, bg-light, bg-dark, text-light, text-dark, label-light, label-dark)
                ("#16a34a", "#f0fdf4", "#052e16", "#14532d", "#bbf7d0", "#15803d", "#86efac"),
                ("#2563eb", "#eff6ff", "#172554", "#1e3a5f", "#bfdbfe", "#1d4ed8", "#93c5fd"),
            ]
            top2 = top10[:2]
            for i, d in enumerate(top2):
                pct      = getattr(d, metric_key) or 0
                ref_val  = getattr(d, ref_col)
                diff_val = getattr(d, diff_key)
                brd, bg_l, bg_d, txt_l, txt_d, lbl_l, lbl_d = highlight_colors[i]
                bg   = bg_d  if dark else bg_l
                txt  = txt_d if dark else txt_l
                lbl  = lbl_d if dark else lbl_l
                medal_lbl = "🥇 BEST VALUE PICK #1" if i == 0 else "🥈 BEST VALUE PICK #2"
                pct_color = "#dc2626" if pct < 0 else "#166534"
                diff_str  = f"{diff_val:+.2f}" if diff_val is not None else "—"
                ref_str   = f"₹{ref_val:,.2f}" if ref_val else "—"
                ts_str    = d.nse_live_time if d.price_source == "NSE Live" else (d.ref_date or "—")
                st.markdown(f"""
<div style="background:{bg};border:2px solid {brd};border-radius:12px;
            padding:1rem 1.4rem;margin-bottom:0.8rem;">
  <div style="font-size:1.1rem;font-weight:800;color:{brd};margin-bottom:0.5rem;">
    {medal_lbl} — Most Below {ref_name}
  </div>
  <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:0.8rem;">
    <div>
      <div style="font-size:1.5rem;font-weight:800;color:{brd};">{d.symbol}</div>
      <div style="font-size:0.9rem;color:{txt};">{d.company_name or d.symbol}</div>
    </div>
    <div style="text-align:right;">
      <div style="font-size:1.6rem;font-weight:800;color:{pct_color};">{pct:+.2f}%</div>
      <div style="font-size:0.85rem;color:{lbl};">
        Price ₹{d.current_price:,.2f} &nbsp;·&nbsp; {ref_name} {ref_str}
        &nbsp;·&nbsp; Diff {diff_str} &nbsp;·&nbsp; {ts_str}
      </div>
    </div>
  </div>
  <div style="display:flex;gap:0.5rem;margin-top:0.75rem;padding-top:0.65rem;border-top:1px solid {brd}40;">
    <a href="https://www.tradingview.com/chart/?symbol=NSE:{d.symbol}" target="_blank"
       style="display:inline-flex;align-items:center;gap:0.3rem;font-size:0.78rem;font-weight:600;
              color:{brd};background:rgba(255,255,255,0.4);border:1px solid {brd};
              border-radius:6px;padding:0.25rem 0.7rem;text-decoration:none;">
      📈 TradingView
    </a>
    <a href="https://www.nseindia.com/get-quotes/equity?symbol={d.symbol}" target="_blank"
       style="display:inline-flex;align-items:center;gap:0.3rem;font-size:0.78rem;font-weight:600;
              color:{brd};background:rgba(255,255,255,0.4);border:1px solid {brd};
              border-radius:6px;padding:0.25rem 0.7rem;text-decoration:none;">
      🏛️ NSE
    </a>
  </div>
</div>
""", unsafe_allow_html=True)

            st.markdown("<br>", unsafe_allow_html=True)

            # ── Summary table ─────────────────────────────────────────────────
            sug_rows = [
                {
                    "Rank":           i + 1,
                    pct_col:          getattr(top10[i], metric_key),
                    "Symbol":         top10[i].symbol,
                    "Company Name":   top10[i].company_name or top10[i].symbol,
                    ref_label:        getattr(top10[i], ref_col),
                    "Current Price":  top10[i].current_price,
                }
                for i in range(len(top10))
            ]
            sdf = pd.DataFrame(sug_rows)
            st.dataframe(
                sdf, hide_index=True, width='stretch',
                column_config={
                    "Rank":          st.column_config.NumberColumn("Rank",         format="%d",       width="small"),
                    pct_col:         st.column_config.NumberColumn(pct_col,        format="%+.2f%%",  width="small"),
                    "Symbol":        st.column_config.TextColumn("Symbol",         width="small"),
                    "Company Name":  st.column_config.TextColumn("Company Name",   width="large"),
                    ref_label:       st.column_config.NumberColumn(ref_label,      format="₹%.2f"),
                    "Current Price": st.column_config.NumberColumn("Current Price (₹)", format="₹%.2f"),
                },
            )

    # ── Section 1: Ranked by % vs 52W Low ────────────────────────────────────
    _rank_section(
        title="💡 Top 10 Suggestions — Best Value Picks (Most Below 52-Week Low)",
        metric_key="pct_vs_52w_low",
        diff_key="diff_52w_low",
        pct_col="% vs 52W Low",
        ref_col="week52_low",
        ref_label="52W Low (₹)",
        ref_name="52-Week Low",
        label_fn=lambda d: (
            f"Price ₹{d.current_price:,.2f}  ·  52W Low ₹{d.week52_low:,.2f}"
            if d.current_price and d.week52_low else "—"
        ),
        detail_fn=lambda d: (
            f"Diff {d.diff_52w_low:+.2f}  ·  {d.ref_date or '—'}"
            if d.diff_52w_low is not None else "—"
        ),
        expanded=True,
    )

    # ── Section 2: Ranked by % vs 3Y Avg ─────────────────────────────────────
    _rank_section(
        title="💡 Top 10 Suggestions — Best Value Picks (Most Below 3-Year Average)",
        metric_key="pct_vs_3y",
        diff_key="diff_3y",
        pct_col="% vs 3Y Avg",
        ref_col="avg_price_3y",
        ref_label="3Y Avg (₹)",
        ref_name="3-Year Average",
        label_fn=lambda d: (
            f"Price ₹{d.current_price:,.2f}  ·  3Y Avg ₹{d.avg_price_3y:,.2f}"
            if d.current_price and d.avg_price_3y else "—"
        ),
        detail_fn=lambda d: (
            f"Diff {d.diff_3y:+.2f}  ·  {d.ref_date or '—'}"
            if d.diff_3y is not None else "—"
        ),
        expanded=False,
    )

    # ── Section 3: Ranked by % vs 5Y Avg ─────────────────────────────────────
    _rank_section(
        title="💡 Top 10 Suggestions — Best Value Picks (Most Below 5-Year Average)",
        metric_key="pct_vs_5y",
        diff_key="diff_5y",
        pct_col="% vs 5Y Avg",
        ref_col="avg_price_5y",
        ref_label="5Y Avg (₹)",
        ref_name="5-Year Average",
        label_fn=lambda d: (
            f"Price ₹{d.current_price:,.2f}  ·  5Y Avg ₹{d.avg_price_5y:,.2f}"
            if d.current_price and d.avg_price_5y else "—"
        ),
        detail_fn=lambda d: (
            f"Diff {d.diff_5y:+.2f}  ·  {d.ref_date or '—'}"
            if d.diff_5y is not None else "—"
        ),
        expanded=False,
    )


# ── Eshwarana Fruit Forest ─────────────────────────────────────────────────────

def _render_eff():
    from datetime import date as _date
    _auto_restore_scan("eff_data", _load_eff_session, "eff")

    st.markdown(
        "<p style='color:gray;font-size:0.9rem;margin-bottom:0.5rem;'>"
        "Long-term analysis — 52W Low, 3-Year &amp; 5-Year average prices vs Current Price · Data via NSE India (nsepython) · 5-Year history via Yahoo Finance</p>",
        unsafe_allow_html=True,
    )

    # ── Reference date ────────────────────────────────────────────────────────
    today = _date.today()
    if st.session_state.pop("_live_pending_eff", False):
        st.session_state["eff_date_picker"] = today
    dcol1, dcol2 = st.columns([2, 5])
    with dcol1:
        saved_date   = st.session_state.get("eff_ref_date") or today
        ref_date_val = st.date_input(
            "Reference Date",
            value=saved_date,
            max_value=today,
            help="Prices and averages are calculated as of this date. Weekends / market holidays automatically fall back to the previous trading day.",
            key="eff_date_picker",
        )
        st.session_state.eff_ref_date = ref_date_val
    with dcol2:
        st.markdown(
            "<p style='color:gray;font-size:0.85rem;margin-top:1.9rem;'>"
            "If the selected date falls on a weekend or NSE holiday, the previous available trading day is used automatically.</p>",
            unsafe_allow_html=True,
        )

    st.markdown("---")

    # ── Input tabs ────────────────────────────────────────────────────────────
    tab_manual, tab_file = st.tabs(["📝 Enter Symbols", "📁 Upload File"])
    symbols: list = []

    with tab_manual:
        txt = st.text_area(
            "ETF/Stock symbols",
            placeholder="Single symbol:  NIFTYBEES\nMultiple:  NIFTYBEES, BANKBEES, GOLDBEES\nOne per line also works",
            height=100,
            label_visibility="collapsed",
            key="eff_manual_input",
        )
        c1, c2 = st.columns([1, 5])
        with c1:
            scan_btn = st.button("🔍 Scan", type="primary", width='stretch', key="eff_scan_m")
        with c2:
            st.caption("Use NSE symbols. Current price via NSE India · 5-year history via Yahoo Finance.")
        if scan_btn and txt.strip():
            st.session_state["_eff_pending_scan_name"] = f"Eshwarana Fruit Forest — {_now_ist().strftime('%d %b %H:%M')}"
            st.session_state["_eff_is_file_upload"]    = False
            symbols = _parser.parse_text_input(txt)

    with tab_file:
        up = st.file_uploader(
            "Upload CSV / Excel / TXT", type=["csv", "xlsx", "xls", "txt"],
            label_visibility="collapsed", key="eff_file_uploader",
        )
        c1, c2, c3 = st.columns([1, 1, 4])
        with c1:
            scan_f = st.button("🔍 Scan File", type="primary", width='stretch', key="eff_scan_f")
        with c2:
            sample = pd.DataFrame({"symbol": ["NIFTYBEES", "BANKBEES", "GOLDBEES", "JUNIORBEES", "ITBEES"]})
            st.download_button("📥 Sample", sample.to_csv(index=False).encode(),
                               "sample_eff.csv", "text/csv", key="eff_sample_dl")
        with c3:
            st.caption("CSV/Excel: 'symbol' column or first column. TXT: one per line.")
        if scan_f and up:
            st.session_state["_eff_pending_scan_name"] = up.name
            st.session_state["_eff_is_file_upload"]    = True
            symbols = _parser.parse_file(up)
            if not symbols:
                st.warning("⚠️ No valid symbols found in file.")

    # ── Fetch ─────────────────────────────────────────────────────────────────
    if symbols:
        scan_name = st.session_state.pop("_eff_pending_scan_name", "Eshwarana Fruit Forest")
        is_file   = st.session_state.pop("_eff_is_file_upload", False)
        st.markdown(
            f"**Fetching Eshwarana Fruit Forest data for {len(symbols)} symbol(s):** "
            f"{', '.join(symbols[:15])}{'…' if len(symbols) > 15 else ''}"
        )
        _do_eff_fetch(symbols, scan_name, ref_date=ref_date_val, save_to_history=True)

    # ── Refresh ───────────────────────────────────────────────────────────────
    if st.session_state.get("eff_do_refresh"):
        st.session_state.eff_do_refresh = False
        syms = st.session_state.get("eff_last_scan_symbols", [])
        if not syms:
            syms = [d.symbol for d in st.session_state.get("eff_data", [])]
        if syms:
            scan_name = st.session_state.get("eff_active_scan_name") or "Eshwarana Fruit Forest Refresh"
            st.markdown(f"**Refreshing Eshwarana Fruit Forest data for {len(syms)} symbol(s)…**")
            _do_eff_fetch(syms, scan_name, ref_date=ref_date_val, save_to_history=True)

    # ── Render results ────────────────────────────────────────────────────────
    data = st.session_state.get("eff_data", [])
    if not data:
        return

    valid      = [d for d in data if not d.error]
    fetched_at = st.session_state.get("eff_last_fetched_at")
    all_dates  = [d.ref_date for d in valid if d.ref_date]
    if all_dates:
        latest_ref = max(all_dates)
    else:
        latest_ref = "—"

    # Summary metrics
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Symbols Scanned", len(data))
    c2.metric("Trading Day",     latest_ref, help=f"Trading day used for analysis: {latest_ref}")
    c3.metric("Loaded OK",       len(valid))
    if fetched_at:
        c4.metric("Last Fetched", (_fmt_ts(fetched_at) if fetched_at else "—"))

    # Refresh + Live buttons
    rb1, rb2, rb3 = st.columns([1, 1, 4])
    with rb1:
        if st.button("🔄 Refresh", width='stretch', key="eff_refresh_btn"):
            st.session_state.eff_do_refresh = True
            st.rerun()
    with rb2:
        _live_btn("eff", "eff_do_refresh", "eff_live_btn")
    with rb3:
        if _is_market_open():
            st.success("🟢 **MARKET OPEN** — Live fetches real-time NSE prices.")
        else:
            from datetime import timezone, timedelta
            IST = timezone(timedelta(hours=5, minutes=30))
            ist_now = datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")
            st.info(
                f"⏸ Market closed ({ist_now}). "
                "Live / Refresh fetches the latest official NSE close price."
            )

    st.markdown("---")
    _render_eff_table(data)


def _render_eff_table(data: list) -> None:
    """Main Eshwarana Fruit Forest table + 3 collapsible ranked sections."""
    dark    = st.session_state.get("dark_mode", False)
    valid   = [d for d in data if not d.error]
    errors  = [d for d in data if d.error]

    # ── Main data table ───────────────────────────────────────────────────────
    rows = []
    for d in valid:
        price_as_of = d.nse_live_time if d.price_source == "NSE Live" else (d.ref_date or "—")
        rows.append({
            "Symbol":                     d.symbol,
            "Company Name":               d.company_name or d.symbol,
            "Current Price (₹)":          d.current_price,
            "Price As of Date":           price_as_of,
            "Price on the Date (₹)":      d.price_on_date,
            "52W Low (₹)":                d.week52_low,
            "Price − 52W Low":            d.diff_52w_low,
            "% vs 52W Low":               d.pct_vs_52w_low,
            "3Y Avg Price (₹)":           d.avg_price_3y,
            "5Y Avg Price (₹)":           d.avg_price_5y,
            "Price − 3Y Avg":             d.diff_3y,
            "% vs 3Y Avg":                d.pct_vs_3y,
            "Price − 5Y Avg":             d.diff_5y,
            "% vs 5Y Avg":                d.pct_vs_5y,
        })

    if rows:
        df = pd.DataFrame(rows)
        df = _df_filter_sort(df, "eff")
        num_col = st.column_config.NumberColumn
        txt_col = st.column_config.TextColumn

        # Export buttons
        ec1, ec2, *_ = st.columns([1, 1, 4])
        with ec1:
            st.download_button(
                "📥 CSV", df.to_csv(index=False).encode(),
                f"eff_{_now_ist().strftime('%Y%m%d_%H%M')}.csv", "text/csv",
                key="eff_csv_dl",
            )
        with ec2:
            try:
                import io
                buf = io.BytesIO()
                df.to_excel(buf, index=False, engine="openpyxl")
                st.download_button("📥 Excel", buf.getvalue(),
                                   f"eff_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx",
                                   key="eff_xls_dl")
            except Exception:
                pass

        st.dataframe(
            df, hide_index=True, width='stretch',
            column_config={
                "Symbol":                txt_col("Symbol",              width="small"),
                "Company Name":          txt_col("Company Name",        width="large"),
                "Current Price (₹)":     num_col("Current Price (₹)",   format="₹%.2f"),
                "Price As of Date":      txt_col("Price As of Date",    width="medium"),
                "Price on the Date (₹)": num_col("Price on the Date",   format="₹%.2f"),
                "52W Low (₹)":           num_col("52W Low (₹)",         format="₹%.2f"),
                "Price − 52W Low":       num_col("Price − 52W Low",     format="%+.2f"),
                "% vs 52W Low":          num_col("% vs 52W Low",        format="%+.2f%%"),
                "3Y Avg Price (₹)":      num_col("3Y Avg Price (₹)",    format="₹%.2f"),
                "5Y Avg Price (₹)":      num_col("5Y Avg Price (₹)",    format="₹%.2f"),
                "Price − 3Y Avg":        num_col("Price − 3Y Avg",      format="%+.2f"),
                "% vs 3Y Avg":           num_col("% vs 3Y Avg",         format="%+.2f%%"),
                "Price − 5Y Avg":        num_col("Price − 5Y Avg",      format="%+.2f"),
                "% vs 5Y Avg":           num_col("% vs 5Y Avg",         format="%+.2f%%"),
            },
        )
    else:
        st.info("No data to display. Scan some symbols first.")

    if errors:
        with st.expander(f"⚠️ {len(errors)} symbol(s) could not be fetched"):
            for d in errors:
                st.markdown(f"- **{d.symbol}**: {d.error}")

    if not valid:
        return

    st.markdown("---")

    # ── Helper: render top-10 ranked section (highlight cards + table) ──────
    def _rank_section(title: str, metric_key: str, diff_key: str,
                      pct_col: str, ref_col: str, ref_label: str,
                      ref_name: str, label_fn, detail_fn,
                      expanded: bool = True) -> None:
        ranked_all = sorted(
            [d for d in valid if getattr(d, metric_key) is not None],
            key=lambda x: getattr(x, metric_key),
        )
        if len(ranked_all) < 2:
            return

        top10 = ranked_all[:10]

        with st.expander(title, expanded=expanded):
            st.markdown(
                "<p style='color:gray;font-size:0.85rem;margin-bottom:0.8rem;'>"
                "Top 10 symbols trading furthest <b>below</b> their reference price — "
                "ranked by largest negative % gap (Rank 1 = best value pick). "
                "Negative % = below reference · Positive % = above reference.</p>",
                unsafe_allow_html=True,
            )

            # ── Top-2 highlight cards (MUST BUY style) ────────────────────────
            highlight_colors = [
                ("#16a34a", "#f0fdf4", "#052e16", "#14532d", "#bbf7d0", "#15803d", "#86efac"),
                ("#2563eb", "#eff6ff", "#172554", "#1e3a5f", "#bfdbfe", "#1d4ed8", "#93c5fd"),
            ]
            top2 = top10[:2]
            for i, d in enumerate(top2):
                pct      = getattr(d, metric_key) or 0
                ref_val  = getattr(d, ref_col)
                diff_val = getattr(d, diff_key)
                brd, bg_l, bg_d, txt_l, txt_d, lbl_l, lbl_d = highlight_colors[i]
                bg   = bg_d  if dark else bg_l
                txt  = txt_d if dark else txt_l
                lbl  = lbl_d if dark else lbl_l
                medal_lbl = "🥇 BEST VALUE PICK #1" if i == 0 else "🥈 BEST VALUE PICK #2"
                pct_color = "#dc2626" if pct < 0 else "#166534"
                diff_str  = f"{diff_val:+.2f}" if diff_val is not None else "—"
                ref_str   = f"₹{ref_val:,.2f}" if ref_val else "—"
                ts_str    = d.nse_live_time if d.price_source == "NSE Live" else (d.ref_date or "—")
                st.markdown(f"""
<div style="background:{bg};border:2px solid {brd};border-radius:12px;
            padding:1rem 1.4rem;margin-bottom:0.8rem;">
  <div style="font-size:1.1rem;font-weight:800;color:{brd};margin-bottom:0.5rem;">
    {medal_lbl} — Most Below {ref_name}
  </div>
  <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:0.8rem;">
    <div>
      <div style="font-size:1.5rem;font-weight:800;color:{brd};">{d.symbol}</div>
      <div style="font-size:0.9rem;color:{txt};">{d.company_name or d.symbol}</div>
    </div>
    <div style="text-align:right;">
      <div style="font-size:1.6rem;font-weight:800;color:{pct_color};">{pct:+.2f}%</div>
      <div style="font-size:0.85rem;color:{lbl};">
        Price ₹{d.current_price:,.2f} &nbsp;·&nbsp; {ref_name} {ref_str}
        &nbsp;·&nbsp; Diff {diff_str} &nbsp;·&nbsp; {ts_str}
      </div>
    </div>
  </div>
  <div style="display:flex;gap:0.5rem;margin-top:0.75rem;padding-top:0.65rem;border-top:1px solid {brd}40;">
    <a href="https://www.tradingview.com/chart/?symbol=NSE:{d.symbol}" target="_blank"
       style="display:inline-flex;align-items:center;gap:0.3rem;font-size:0.78rem;font-weight:600;
              color:{brd};background:rgba(255,255,255,0.4);border:1px solid {brd};
              border-radius:6px;padding:0.25rem 0.7rem;text-decoration:none;">
      📈 TradingView
    </a>
    <a href="https://www.nseindia.com/get-quotes/equity?symbol={d.symbol}" target="_blank"
       style="display:inline-flex;align-items:center;gap:0.3rem;font-size:0.78rem;font-weight:600;
              color:{brd};background:rgba(255,255,255,0.4);border:1px solid {brd};
              border-radius:6px;padding:0.25rem 0.7rem;text-decoration:none;">
      🏛️ NSE
    </a>
  </div>
</div>
""", unsafe_allow_html=True)

            st.markdown("<br>", unsafe_allow_html=True)

            # ── Summary table ─────────────────────────────────────────────────
            sug_rows = [
                {
                    "Rank":           i + 1,
                    pct_col:          getattr(top10[i], metric_key),
                    "Symbol":         top10[i].symbol,
                    "Company Name":   top10[i].company_name or top10[i].symbol,
                    ref_label:        getattr(top10[i], ref_col),
                    "Current Price":  top10[i].current_price,
                }
                for i in range(len(top10))
            ]
            sdf = pd.DataFrame(sug_rows)
            st.dataframe(
                sdf, hide_index=True, width='stretch',
                column_config={
                    "Rank":          st.column_config.NumberColumn("Rank",         format="%d",       width="small"),
                    pct_col:         st.column_config.NumberColumn(pct_col,        format="%+.2f%%",  width="small"),
                    "Symbol":        st.column_config.TextColumn("Symbol",         width="small"),
                    "Company Name":  st.column_config.TextColumn("Company Name",   width="large"),
                    ref_label:       st.column_config.NumberColumn(ref_label,      format="₹%.2f"),
                    "Current Price": st.column_config.NumberColumn("Current Price (₹)", format="₹%.2f"),
                },
            )

    # ── Section 1: Ranked by % vs 52W Low ────────────────────────────────────
    _rank_section(
        title="💡 Top 10 Suggestions — Best Value Picks (Most Below 52-Week Low)",
        metric_key="pct_vs_52w_low",
        diff_key="diff_52w_low",
        pct_col="% vs 52W Low",
        ref_col="week52_low",
        ref_label="52W Low (₹)",
        ref_name="52-Week Low",
        label_fn=lambda d: (
            f"Price ₹{d.current_price:,.2f}  ·  52W Low ₹{d.week52_low:,.2f}"
            if d.current_price and d.week52_low else "—"
        ),
        detail_fn=lambda d: (
            f"Diff {d.diff_52w_low:+.2f}  ·  {d.ref_date or '—'}"
            if d.diff_52w_low is not None else "—"
        ),
        expanded=True,
    )

    # ── Section 2: Ranked by % vs 3Y Avg ─────────────────────────────────────
    _rank_section(
        title="💡 Top 10 Suggestions — Best Value Picks (Most Below 3-Year Average)",
        metric_key="pct_vs_3y",
        diff_key="diff_3y",
        pct_col="% vs 3Y Avg",
        ref_col="avg_price_3y",
        ref_label="3Y Avg (₹)",
        ref_name="3-Year Average",
        label_fn=lambda d: (
            f"Price ₹{d.current_price:,.2f}  ·  3Y Avg ₹{d.avg_price_3y:,.2f}"
            if d.current_price and d.avg_price_3y else "—"
        ),
        detail_fn=lambda d: (
            f"Diff {d.diff_3y:+.2f}  ·  {d.ref_date or '—'}"
            if d.diff_3y is not None else "—"
        ),
        expanded=False,
    )

    # ── Section 3: Ranked by % vs 5Y Avg ─────────────────────────────────────
    _rank_section(
        title="💡 Top 10 Suggestions — Best Value Picks (Most Below 5-Year Average)",
        metric_key="pct_vs_5y",
        diff_key="diff_5y",
        pct_col="% vs 5Y Avg",
        ref_col="avg_price_5y",
        ref_label="5Y Avg (₹)",
        ref_name="5-Year Average",
        label_fn=lambda d: (
            f"Price ₹{d.current_price:,.2f}  ·  5Y Avg ₹{d.avg_price_5y:,.2f}"
            if d.current_price and d.avg_price_5y else "—"
        ),
        detail_fn=lambda d: (
            f"Diff {d.diff_5y:+.2f}  ·  {d.ref_date or '—'}"
            if d.diff_5y is not None else "—"
        ),
        expanded=False,
    )


# ── Admin Panel ───────────────────────────────────────────────────────────────

def render_admin():
    st.markdown("## ⚙️ Admin Panel")
    t_users, t_cache = st.tabs(["👥 User Management", "🗄️ Cache"])

    with t_users:
        st.markdown("### ➕ Add User")
        with st.form("add_user_form"):
            new_phone = st.text_input("Mobile Number", placeholder="+91XXXXXXXXXX")
            is_admin  = st.checkbox("Grant admin access")
            if st.form_submit_button("Add User", type="primary"):
                if not new_phone.strip():
                    st.error("Phone number is required.")
                else:
                    try:
                        db.add_user(new_phone.strip(), is_admin=is_admin)
                        st.success(f"✅ User {new_phone.strip()} added.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"❌ {exc}")

        st.markdown("### 👥 Existing Users")
        users = db.list_users()
        if users:
            for u in users:
                col_info, col_del = st.columns([5, 1])
                with col_info:
                    admin_badge = " 🔑 Admin" if u.get("is_admin") else ""
                    st.markdown(f"**{u['phone_number']}**{admin_badge}")
                with col_del:
                    if st.button("🗑", key=f"adm_del_{u['phone_number']}", help="Delete user"):
                        db.delete_user(u["phone_number"])
                        st.rerun()
        else:
            st.info("No users registered yet.")

    with t_cache:
        st.markdown("### 🗄️ Cache Status")
        cache_size = cache_manager.size()
        st.metric("Cached Symbols", cache_size)
        col1, col2 = st.columns(2)
        with col1:
            if st.button("🗑 Clear Memory Cache", width='stretch'):
                cache_manager.clear_all()
                st.success("Memory cache cleared.")
        with col2:
            if st.button("🗑 Clear DB Cache", width='stretch'):
                cleared = db.clear_cache()
                st.success(f"Cleared {cleared} cached entries from database.")


# ── Entry point ───────────────────────────────────────────────────────────────

_init()
st.markdown(get_theme_css(st.session_state.dark_mode), unsafe_allow_html=True)

if not st.session_state.authenticated:
    render_login()
else:
    render_sidebar()
    section = st.session_state.section
    if section == "fundamentals":
        render_dashboard()
    elif section == "technicals":
        render_technicals()
    elif section == "guru_smart_business":
        render_guru_smart_business()
    elif section == "rsi_screener":
        render_rsi_screener()
    elif section == "base_price_screener":
        render_base_price_screener()
    elif section == "turtle_trading":
        render_turtle_trading()
    elif section == "my_holdings":
        render_my_holdings()
    elif section == "paiso_ka_jungle":
        render_paiso_ka_jungle()
    elif section == "admin" and st.session_state.user and st.session_state.user.get("is_admin"):
        render_admin()
