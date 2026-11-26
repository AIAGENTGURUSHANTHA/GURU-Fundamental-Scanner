import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


def _secret(key: str, default: str = "") -> str:
    """Return env var; fall back to st.secrets on Streamlit Community Cloud."""
    val = os.getenv(key, "")
    if not val:
        try:
            import streamlit as st  # lazy — avoids import-order issues
            val = str(st.secrets.get(key, default))
        except Exception:
            val = default
    return val


class Config:
    BASE_DIR = Path(__file__).parent
    APP_NAME = "GURU Fundamental Scanner"
    VERSION = "1.0.0"
    DEBUG = os.getenv("DEBUG", "False").lower() == "true"

    # Database — Neon PostgreSQL on cloud, SQLite locally
    # On Streamlit Cloud: add NEON_DATABASE_URL to App Settings → Secrets
    _neon_url: str = ""   # filled by bootstrap()
    DATABASE_URL: str = ""  # filled by bootstrap()
    DB_PATH = str(BASE_DIR / "guru_scanner.db")  # kept for reference only

    # Admin
    ADMIN_PHONE: str = ""   # filled by _bootstrap() at app startup

    # Twilio SMS
    TWILIO_SID: str = ""
    TWILIO_TOKEN: str = ""
    TWILIO_FROM: str = ""

    # OTP Settings
    OTP_EXPIRY_MINUTES = 10
    OTP_LENGTH = 6
    OTP_MAX_ATTEMPTS = 3

    # Scraper
    REQUEST_DELAY = 1.0
    REQUEST_TIMEOUT = 30
    MAX_RETRIES = 3

    # Cache TTL seconds
    CACHE_TTL = 300

    @classmethod
    def bootstrap(cls) -> None:
        """Call once at app startup to load secrets (env or Streamlit Cloud)."""
        cls.ADMIN_PHONE  = _secret("ADMIN_PHONE")
        cls.TWILIO_SID   = _secret("TWILIO_ACCOUNT_SID")
        cls.TWILIO_TOKEN = _secret("TWILIO_AUTH_TOKEN")
        cls.TWILIO_FROM  = _secret("TWILIO_PHONE_NUMBER")
        # Neon PostgreSQL — set NEON_DATABASE_URL in Streamlit Cloud secrets
        raw_neon = _secret("NEON_DATABASE_URL")
        if raw_neon:
            # Normalize: Neon gives postgres:// or postgresql://, SQLAlchemy needs postgresql+psycopg2://
            if raw_neon.startswith("postgres://"):
                raw_neon = "postgresql+psycopg2://" + raw_neon[len("postgres://"):]
            elif raw_neon.startswith("postgresql://"):
                raw_neon = "postgresql+psycopg2://" + raw_neon[len("postgresql://"):]
        cls._neon_url   = raw_neon
        cls.DATABASE_URL = (
            cls._neon_url
            if cls._neon_url
            else f"sqlite:///{cls.DB_PATH}"
        )

    @classmethod
    def is_sms_configured(cls) -> bool:
        return all([cls.TWILIO_SID, cls.TWILIO_TOKEN, cls.TWILIO_FROM])


config = Config()
