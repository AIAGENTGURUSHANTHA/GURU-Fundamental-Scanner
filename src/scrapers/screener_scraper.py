import re
from typing import Any, Dict, Optional

from bs4 import BeautifulSoup

from .base_scraper import BaseScraper


class ScreenerScraper(BaseScraper):
    """
    Sole data source — scrapes screener.in company pages.
    Pre-fetches the homepage to obtain session cookies (required by screener.in).
    """

    HOME_URL = "https://www.screener.in"
    COMPANY_URL = "https://www.screener.in/company/{symbol}/"

    def __init__(self) -> None:
        super().__init__()
        # Extra headers that screener.in expects
        self.session.headers.update({
            "Referer": "https://www.screener.in/",
            "Host": "www.screener.in",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Upgrade-Insecure-Requests": "1",
        })
        self._session_ready = False

    # ── Session bootstrap ─────────────────────────────────────────────────────

    def _ensure_session(self) -> None:
        """Fetch the screener.in homepage once to establish cookies."""
        if self._session_ready:
            return
        try:
            self.session.get(self.HOME_URL, timeout=15)
            self._session_ready = True
        except Exception as exc:
            self._log_error(f"Session init failed: {exc}")

    # ── Public API ────────────────────────────────────────────────────────────

    def fetch_company_data(self, symbol: str) -> Optional[Dict[str, Any]]:
        self._ensure_session()
        sym = symbol.strip().upper()
        # Always use the consolidated URL — e.g. /company/BAJAJ-AUTO/
        return self._fetch_page(sym, "")

    # ── Page fetch ────────────────────────────────────────────────────────────

    def _fetch_page(self, symbol: str, path: str = "") -> Optional[Dict[str, Any]]:
        url = f"{self.COMPANY_URL.format(symbol=symbol)}{path}"
        try:
            resp = self._make_request(url)
            if resp is None:
                return None

            soup = BeautifulSoup(resp.content, "lxml")
            data: Dict[str, Any] = {"symbol": symbol}

            # Bail early if screener returned a 'not found' or login page
            if soup.find("div", class_="login-form") or \
               soup.find("h1", string=re.compile(r"not found", re.I)):
                return None

            self._extract_name(soup, data)
            self._extract_company_links(soup, data)
            self._extract_top_ratios(soup, data)
            self._extract_price_meta(soup, data)
            self._extract_ratios_table(soup, data)
            self._extract_pl_table(soup, data)
            self._calculate_derived(data)

            return data if data.get("current_price") or data.get("market_cap_cr") else None

        except Exception as exc:
            self._log_error(f"_fetch_page({symbol}/{path}): {exc}")
            return None

    # ── Extractors ────────────────────────────────────────────────────────────

    def _extract_name(self, soup: BeautifulSoup, data: dict) -> None:
        for selector in [
            {"class_": "margin-0"},
            {"class_": "company-name"},
            {"id": "company-name"},
        ]:
            tag = soup.find("h1", **selector)
            if tag:
                data["company_name"] = tag.get_text(strip=True)
                return
        h1 = soup.find("h1")
        if h1:
            data["company_name"] = h1.get_text(strip=True)

    def _extract_company_links(self, soup: BeautifulSoup, data: dict) -> None:
        # Collect all text from the top of the page
        top_section = (
            soup.find("section", id="company-top")
            or soup.find("div", id="company-top")
            or soup.find("div", class_=re.compile(r"company[-_]", re.I))
        )
        raw = (top_section or soup).get_text(" ", strip=True)[:2000]

        m_bse = re.search(r'BSE[:\s]+(\d{5,6})', raw)
        m_nse = re.search(r'NSE[:\s]+([A-Z0-9&\-]{1,20})', raw)
        if m_bse:
            data["bse_code"] = m_bse.group(1)
        if m_nse:
            data["nse_code"] = m_nse.group(1)

    def _extract_top_ratios(self, soup: BeautifulSoup, data: dict) -> None:
        # Scan every container that holds name/number span pairs —
        # covers both #top-ratios (quick summary) and the custom company-ratios grid
        containers = []
        ul = soup.find("ul", {"id": "top-ratios"}) or soup.find("ul", class_="ranges")
        if ul:
            containers.append(ul)
        for sec in soup.find_all(["section", "div"], id=re.compile(r"company.ratio", re.I)):
            containers.append(sec)
        for sec in soup.find_all(["section", "div"], class_=re.compile(r"company.ratio", re.I)):
            if sec not in containers:
                containers.append(sec)

        seen: set = set()
        for container in containers:
            for item in container.find_all(["li", "div", "td"], recursive=True):
                name_s = item.find("span", class_="name")
                val_s  = item.find("span", class_="number")
                if not name_s or not val_s:
                    continue
                name = name_s.get_text(strip=True).lower()
                if name in seen:
                    continue
                seen.add(name)
                raw  = val_s.get_text(strip=True)
                num  = self._num(raw)

                if "market cap" in name:
                    data["market_cap_str"] = raw
                    data["market_cap_cr"]  = self._parse_cr(raw)
                elif "current price" in name:
                    data["current_price"] = num
                elif "high" in name and "low" in name:
                    cleaned = re.sub(r"[₹,\s]", "", raw)
                    parts = cleaned.split("/")
                    if len(parts) == 2:
                        h, l = self._num(parts[0]), self._num(parts[1])
                        if h: data["high_52w"] = h
                        if l: data["low_52w"]  = l
                elif "stock p/e" in name or name.strip() == "p/e":
                    data["stock_pe"] = num
                elif "book value" in name:
                    data["book_value"] = num
                elif "dividend yield" in name:
                    data["dividend_yield"] = num
                elif "roce" in name:
                    data["roce"] = num
                elif name.strip() == "roe" or ("return on equity" in name and "net" not in name):
                    data["roe"] = num
                elif "face value" in name:
                    data["face_value"] = num
                elif "p/b" in name or "price to book" in name:
                    data["pb_ratio"] = num
                elif "eps" in name and "growth" not in name:
                    data["eps"] = num
                elif "peg" in name:
                    data["peg_ratio"] = num
                elif "ev/ebitda" in name or "evebitda" in name or ("ev" in name and "ebitda" in name):
                    data["ev_ebitda"] = num

    def _extract_price_meta(self, soup: BeautifulSoup, data: dict) -> None:
        # Price change %
        for sel in [{"class_": re.compile(r"change", re.I)}, {"id": re.compile(r"change", re.I)}]:
            el = soup.find("span", **sel)
            if el:
                n = self._num(el.get_text(strip=True).replace("%", ""))
                if n is not None:
                    data["price_change_pct"] = n
                    break

        # Timestamp
        time_tag = soup.find("time")
        if time_tag:
            data["data_timestamp"] = time_tag.get_text(strip=True)
            return

        top = soup.find("section", id="company-top") or soup.body
        if top:
            text = top.get_text(" ", strip=True)[:3000]
            m = re.search(
                r'\b(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}:\d{2}',
                text, re.I,
            )
            if m:
                data["data_timestamp"] = m.group(0)

    def _extract_ratios_table(self, soup: BeautifulSoup, data: dict) -> None:
        section = soup.find(id="ratios")
        if not section:
            return
        table = section.find("table")
        if not table:
            return

        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if not cells:
                continue
            label = cells[0].get_text(strip=True).lower()
            vals = [c.get_text(strip=True) for c in cells[1:]]
            vals = [v for v in vals if v and v not in ("-", "—")]
            if not vals:
                continue
            latest = vals[-1]

            if "ronw" in label or "return on net worth" in label:
                data["ronw"] = self._num(latest)
            elif ("roe" in label or "return on equity" in label) and "ronw" not in data:
                data["_ronw_fallback"] = self._num(latest)
            elif "roce" in label and "roce" not in data:
                data["roce"] = self._num(latest)

        # Use ratios-table ROE as RONW if no dedicated RONW row found
        if "ronw" not in data:
            data["ronw"] = data.pop("_ronw_fallback", None)
        else:
            data.pop("_ronw_fallback", None)

    def _extract_pl_table(self, soup: BeautifulSoup, data: dict) -> None:
        section = soup.find(id="profit-loss")
        if not section:
            return
        table = section.find("table")
        if not table:
            return

        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if not cells:
                continue
            label = cells[0].get_text(strip=True).lower()
            vals = [c.get_text(strip=True) for c in cells[1:]]
            vals = [v for v in vals if v and v not in ("-", "—")]
            if not vals:
                continue
            latest = vals[-1]

            if ("sales" in label or "revenue" in label) and "growth" not in label and "revenue_cr" not in data:
                data["revenue_cr"] = self._num(latest)
            elif "operating profit" in label and "%" not in label and "opm" not in label:
                data["operating_profit_cr"] = self._num(latest)
            elif label.strip() == "eps" or ("eps" in label and "growth" not in label and "eps_ttm" not in data):
                data["eps_ttm"] = self._num(latest)

    # ── Derived calculations ──────────────────────────────────────────────────

    def _calculate_derived(self, data: dict) -> None:
        price  = data.get("current_price")
        bv     = data.get("book_value")
        roe    = data.get("roe")
        ronw   = data.get("ronw") or roe
        mcap   = data.get("market_cap_cr")
        rev    = data.get("revenue_cr")

        # P/B
        if price and bv and bv > 0 and "pb_ratio" not in data:
            data["pb_ratio"] = round(price / bv, 2)

        # RONW defaults to ROE
        if not data.get("ronw") and roe:
            data["ronw"] = roe
            ronw = roe

        # PRONW = Price / RONW_pct
        if price and ronw and ronw > 0:
            data["pronw"] = round(price / ronw, 2)

        # P/S via Market Cap / Revenue
        if mcap and rev and rev > 0:
            data["price_to_sales"] = round(mcap / rev, 2)

        # NSPS = Revenue_Cr × Price / Market Cap_Cr  → ₹ per share
        if price and mcap and rev and mcap > 0:
            data["nsps"] = round(rev * price / mcap, 2)

        # PNSPS = Price / NSPS
        nsps = data.get("nsps")
        if price and nsps and nsps > 0:
            data["pnsps"] = round(price / nsps, 2)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _num(self, text: str) -> Optional[float]:
        if not text:
            return None
        try:
            c = re.sub(r"[₹,\s]", "", str(text))
            c = re.sub(r"(?i)(cr\.?|l\.?|lakh|crore|%|\+)", "", c)
            if "/" in c:
                c = c.split("/")[0]
            c = c.strip()
            if not c or c in ("-", "—", "N/A", "NA"):
                return None
            return float(c)
        except (ValueError, TypeError):
            return None

    def _parse_cr(self, text: str) -> Optional[float]:
        if not text:
            return None
        try:
            c = re.sub(r"[₹,\s]", "", text)
            c = re.sub(r"(?i)cr\.?", "", c).strip()
            return float(c) if c else None
        except (ValueError, TypeError):
            return None
