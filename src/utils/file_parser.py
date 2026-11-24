import io
import re
from typing import List, Optional

import pandas as pd


class FileParser:
    """Parse uploaded files (CSV / Excel / TXT) into a list of NSE stock symbols."""

    SYMBOL_COL_NAMES = {"symbol", "ticker", "stock", "scrip", "code", "nse", "bse", "script"}

    # ── Public ────────────────────────────────────────────────────────────────

    def parse_file(self, uploaded_file) -> List[str]:
        """Accept a Streamlit UploadedFile object. Return clean symbol list."""
        name = uploaded_file.name.lower()
        raw = uploaded_file.read()

        if name.endswith(".csv"):
            return self._parse_csv(raw)
        if name.endswith((".xlsx", ".xls")):
            return self._parse_excel(raw)
        if name.endswith(".txt"):
            return self._parse_txt(raw.decode("utf-8", errors="ignore"))
        # Fallback: try CSV
        return self._parse_csv(raw)

    def parse_text_input(self, text: str) -> List[str]:
        """Split raw text (newlines / commas / spaces) into symbols."""
        parts = re.split(r"[,\n\r\t ]+", text)
        return self._clean_symbols(parts)

    # ── Parsers ───────────────────────────────────────────────────────────────

    def _parse_csv(self, raw: bytes) -> List[str]:
        try:
            df = pd.read_csv(io.BytesIO(raw), dtype=str)
            return self._extract_from_df(df)
        except Exception:
            # Fallback: treat as plain text
            return self._parse_txt(raw.decode("utf-8", errors="ignore"))

    def _parse_excel(self, raw: bytes) -> List[str]:
        try:
            df = pd.read_excel(io.BytesIO(raw), dtype=str)
            return self._extract_from_df(df)
        except Exception as exc:
            print(f"[FileParser] Excel parse error: {exc}")
            return []

    def _parse_txt(self, text: str) -> List[str]:
        return self._clean_symbols(text.splitlines())

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _extract_from_df(self, df: pd.DataFrame) -> List[str]:
        """Find the symbol column, fall back to first column."""
        col = None
        for c in df.columns:
            if str(c).strip().lower() in self.SYMBOL_COL_NAMES:
                col = c
                break
        if col is None and len(df.columns) > 0:
            col = df.columns[0]
        if col is None:
            return []
        return self._clean_symbols(df[col].tolist())

    def _clean_symbols(self, tokens: list) -> List[str]:
        seen = set()
        result = []
        for t in tokens:
            s = str(t).strip().upper()
            # Remove common suffixes people paste with the symbol
            s = re.sub(r"\.(NS|BO|NSE|BSE)$", "", s)
            # Keep only alphanumeric + hyphen (NSE symbols like M&M need & too)
            s = re.sub(r"[^A-Z0-9\-&]", "", s)
            if 2 <= len(s) <= 20 and s not in seen:
                seen.add(s)
                result.append(s)
        return result
