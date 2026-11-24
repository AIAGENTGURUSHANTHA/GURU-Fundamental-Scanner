from typing import Optional, Tuple


# ── Formatters ────────────────────────────────────────────────────────────────

def fmt_crores(value_cr: Optional[float]) -> str:
    if value_cr is None:
        return "—"
    if value_cr >= 100_000:
        return f"₹{value_cr / 100_000:.2f}L Cr"
    if value_cr >= 1_000:
        return f"₹{value_cr / 1_000:.2f}K Cr"
    return f"₹{value_cr:,.0f} Cr"


def fmt_pct(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{value:.2f}%"


def fmt_ratio(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{value:.2f}x"


def fmt_currency(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"₹{value:,.2f}"


def metric_color(metric: str, value: Optional[float]) -> str:
    """Return 'green', 'red', or '' based on a simple good/bad heuristic."""
    if value is None:
        return ""
    thresholds = {
        "roce":     (15, 10),
        "roe":      (15, 10),
        "stock_pe": (20, 40),
        "peg_ratio": (1, 2),
        "pb_ratio":  (1, 4),
    }
    if metric not in thresholds:
        return ""
    good, bad = thresholds[metric]
    if metric in ("stock_pe", "peg_ratio", "pb_ratio"):
        return "green" if value <= good else ("red" if value >= bad else "")
    return "green" if value >= good else ("red" if value < bad else "")


# ── Aliases (long-form names used in app.py imports) ─────────────────────────

def format_market_cap(value_cr: Optional[float]) -> str:
    return fmt_crores(value_cr)


def format_percentage(value: Optional[float]) -> str:
    return fmt_pct(value)


def format_ratio(value: Optional[float]) -> str:
    return fmt_ratio(value)


def format_currency(value: Optional[float]) -> str:
    return fmt_currency(value)


def format_crores(value: Optional[float]) -> str:
    return fmt_crores(value)


def highlight_good_bad(
    value: Optional[float], metric: str
) -> Tuple[Optional[float], str]:
    """
    Returns (value, color_hex).
    color_hex: green for good, red for bad, gray for neutral.
    """
    if value is None:
        return value, "#888888"

    color_name = metric_color(metric.lower(), value)
    color_map = {
        "green": "#2ea043",
        "red":   "#f85149",
        "":      "#888888",
    }
    return value, color_map.get(color_name, "#888888")
