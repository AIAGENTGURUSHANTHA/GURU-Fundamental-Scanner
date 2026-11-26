import json
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    create_engine,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    phone_number = Column(String(20), unique=True, nullable=False, index=True)
    is_active = Column(Boolean, default=False, nullable=False)
    is_admin = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_login = Column(DateTime, nullable=True)
    added_by = Column(String(20), nullable=True)


class OTPSession(Base):
    __tablename__ = "otp_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    phone_number = Column(String(20), nullable=False, index=True)
    otp_code = Column(String(10), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    is_used = Column(Boolean, default=False, nullable=False)
    attempts = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class StockCache(Base):
    __tablename__ = "stock_cache"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(20), unique=True, nullable=False, index=True)
    data = Column(Text, nullable=False)
    fetched_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class ScanSession(Base):
    __tablename__ = "scan_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_phone = Column(String(20), nullable=False, index=True)
    scan_name = Column(String(200), nullable=False)
    scan_type = Column(String(20), nullable=False, server_default="fundamentals")
    symbol_count = Column(Integer, default=0, nullable=False)
    results_json = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Holding(Base):
    __tablename__ = "holdings"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    user_phone       = Column(String(20), nullable=False, index=True)
    asset_type       = Column(String(30), nullable=False, index=True)
    symbol           = Column(String(50), nullable=False)
    company_name     = Column(String(200), default="", nullable=False)
    buy_date         = Column(String(10), nullable=False)   # YYYY-MM-DD
    buy_price        = Column(Float, nullable=False)
    buy_qty          = Column(Float, nullable=False)
    sell_date        = Column(String(10), nullable=True)
    sell_price       = Column(Float, nullable=True)
    sell_qty         = Column(Float, nullable=True)
    notes            = Column(Text, default="", nullable=False)
    created_at       = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at       = Column(DateTime, default=datetime.utcnow, nullable=False)
    # Transaction tracking (added in v2)
    transaction_type = Column(String(10), default="BUY", nullable=False)
    remaining_qty    = Column(Float, nullable=True)   # BUY rows: how much is still open
    parent_buy_id    = Column(Integer, nullable=True)  # SELL rows: ID of the originating BUY


class UserSession(Base):
    """Persistent session tokens — survive server restarts and redeployments."""
    __tablename__ = "user_sessions"

    token        = Column(String(64), primary_key=True)
    phone_number = Column(String(20), nullable=False, index=True)
    created_at   = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at   = Column(DateTime, nullable=False)


class DatabaseManager:
    _instance: Optional["DatabaseManager"] = None
    _lock = threading.Lock()

    def __new__(cls) -> "DatabaseManager":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    instance = super().__new__(cls)
                    instance._initialized = False
                    cls._instance = instance
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            import logging
            from config import config
            db_url = config.DATABASE_URL or f"sqlite:///{config.DB_PATH}"
            _is_sqlite = db_url.startswith("sqlite")
            _db_label = "SQLite (local)" if _is_sqlite else "Neon PostgreSQL"
            logging.getLogger(__name__).info("Database: %s", _db_label)
            if _is_sqlite:
                self.engine = create_engine(
                    db_url,
                    connect_args={"check_same_thread": False},
                    echo=config.DEBUG,
                )
            else:
                self.engine = create_engine(db_url, echo=config.DEBUG)
            Base.metadata.create_all(self.engine)
            # Schema migrations — try/except silently skips if column already exists
            _migrations = [
                "ALTER TABLE scan_sessions ADD COLUMN scan_type TEXT NOT NULL DEFAULT 'fundamentals'",
                "ALTER TABLE holdings ADD COLUMN transaction_type TEXT NOT NULL DEFAULT 'BUY'",
                "ALTER TABLE holdings ADD COLUMN remaining_qty REAL",
                "ALTER TABLE holdings ADD COLUMN parent_buy_id INTEGER",
            ]
            for _ddl in _migrations:
                try:
                    with self.engine.connect() as conn:
                        conn.execute(text(_ddl))
                        conn.commit()
                except Exception:
                    pass
            # Back-fill remaining_qty for existing BUY rows
            try:
                with self.engine.connect() as conn:
                    conn.execute(text(
                        "UPDATE holdings SET remaining_qty = buy_qty "
                        "WHERE remaining_qty IS NULL AND (sell_date IS NULL OR sell_date = '')"
                    ))
                    conn.execute(text(
                        "UPDATE holdings SET remaining_qty = 0 "
                        "WHERE remaining_qty IS NULL AND sell_date IS NOT NULL AND sell_date != ''"
                    ))
                    conn.commit()
            except Exception:
                pass
            self.SessionLocal = sessionmaker(
                bind=self.engine, autocommit=False, autoflush=False
            )
            self._initialized = True

    @contextmanager
    def get_session(self):
        session: Session = self.SessionLocal()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # ── Persistent session tokens ─────────────────────────────────────────────

    def save_session_token(self, token: str, phone_number: str, days: int = 30) -> None:
        from datetime import timedelta
        now     = datetime.utcnow()
        expires = now + timedelta(days=days)
        with self.get_session() as session:
            existing = session.query(UserSession).filter_by(token=token).first()
            if existing:
                existing.expires_at = expires
            else:
                session.add(UserSession(
                    token=token, phone_number=phone_number,
                    created_at=now, expires_at=expires,
                ))

    def get_session_phone(self, token: str) -> Optional[str]:
        now = datetime.utcnow()
        with self.get_session() as session:
            row = session.query(UserSession).filter(
                UserSession.token == token,
                UserSession.expires_at > now,
            ).first()
        return row.phone_number if row else None

    def delete_session_token(self, token: str) -> None:
        with self.get_session() as session:
            session.query(UserSession).filter_by(token=token).delete()

    def cleanup_expired_sessions(self) -> None:
        now = datetime.utcnow()
        with self.get_session() as session:
            session.query(UserSession).filter(UserSession.expires_at <= now).delete()

    # ── User operations ──────────────────────────────────────────────────────

    def add_user(
        self,
        phone_number: str,
        is_active: bool = True,
        is_admin: bool = False,
        added_by: Optional[str] = None,
    ) -> bool:
        with self.get_session() as session:
            user = User(
                phone_number=phone_number,
                is_active=is_active,
                is_admin=is_admin,
                added_by=added_by,
            )
            session.add(user)
        return True

    def get_user(self, phone_number: str) -> Optional[Dict[str, Any]]:
        with self.get_session() as session:
            user = (
                session.query(User)
                .filter(User.phone_number == phone_number)
                .first()
            )
            if user:
                return {
                    "id": user.id,
                    "phone_number": user.phone_number,
                    "is_active": user.is_active,
                    "is_admin": user.is_admin,
                    "created_at": user.created_at,
                    "last_login": user.last_login,
                    "added_by": user.added_by,
                }
        return None

    def update_user(
        self,
        phone_number: str,
        is_active: Optional[bool] = None,
        is_admin: Optional[bool] = None,
        last_login: Optional[datetime] = None,
    ) -> bool:
        with self.get_session() as session:
            user = (
                session.query(User)
                .filter(User.phone_number == phone_number)
                .first()
            )
            if not user:
                return False
            if is_active is not None:
                user.is_active = is_active
            if is_admin is not None:
                user.is_admin = is_admin
            if last_login is not None:
                user.last_login = last_login
        return True

    def list_users(self) -> List[Dict[str, Any]]:
        with self.get_session() as session:
            users = session.query(User).order_by(User.created_at.desc()).all()
            return [
                {
                    "id": u.id,
                    "phone_number": u.phone_number,
                    "is_active": u.is_active,
                    "is_admin": u.is_admin,
                    "created_at": str(u.created_at) if u.created_at else None,
                    "last_login": str(u.last_login) if u.last_login else None,
                    "added_by": u.added_by,
                }
                for u in users
            ]

    def delete_user(self, phone_number: str) -> bool:
        with self.get_session() as session:
            user = (
                session.query(User)
                .filter(User.phone_number == phone_number)
                .first()
            )
            if user:
                session.delete(user)
                return True
        return False

    # ── OTP operations ───────────────────────────────────────────────────────

    def save_otp(
        self, phone_number: str, otp_code: str, expires_at: datetime
    ) -> None:
        with self.get_session() as session:
            # Invalidate all previous unused OTPs for this number
            session.query(OTPSession).filter(
                OTPSession.phone_number == phone_number,
                OTPSession.is_used == False,
            ).update({"is_used": True})
            otp = OTPSession(
                phone_number=phone_number,
                otp_code=otp_code,
                expires_at=expires_at,
            )
            session.add(otp)

    def get_latest_otp(self, phone_number: str) -> Optional[Dict[str, Any]]:
        with self.get_session() as session:
            otp = (
                session.query(OTPSession)
                .filter(
                    OTPSession.phone_number == phone_number,
                    OTPSession.is_used == False,
                )
                .order_by(OTPSession.created_at.desc())
                .first()
            )
            if otp:
                return {
                    "id": otp.id,
                    "otp_code": otp.otp_code,
                    "expires_at": otp.expires_at,
                    "attempts": otp.attempts,
                }
        return None

    def increment_otp_attempts(self, otp_id: int) -> None:
        with self.get_session() as session:
            otp = session.query(OTPSession).filter(OTPSession.id == otp_id).first()
            if otp:
                otp.attempts += 1

    def mark_otp_used(self, otp_id: int) -> None:
        with self.get_session() as session:
            otp = session.query(OTPSession).filter(OTPSession.id == otp_id).first()
            if otp:
                otp.is_used = True

    def clear_cache(self) -> int:
        with self.get_session() as session:
            count = session.query(StockCache).count()
            session.query(StockCache).delete()
        return count

    # ── Scan session operations ──────────────────────────────────────────────

    def save_scan_session(
        self, user_phone: str, scan_name: str, results_dicts: List[Dict[str, Any]],
        scan_type: str = "fundamentals",
    ) -> int:
        """Upsert: update existing entry if scan_name+scan_type already exists for this user."""
        with self.get_session() as session:
            existing = (
                session.query(ScanSession)
                .filter(
                    ScanSession.user_phone == user_phone,
                    ScanSession.scan_name == scan_name,
                    ScanSession.scan_type == scan_type,
                )
                .first()
            )
            if existing:
                existing.symbol_count = len(results_dicts)
                existing.results_json = json.dumps(results_dicts, default=str)
                existing.created_at = datetime.utcnow()
                return existing.id
            else:
                row = ScanSession(
                    user_phone=user_phone,
                    scan_name=scan_name,
                    scan_type=scan_type,
                    symbol_count=len(results_dicts),
                    results_json=json.dumps(results_dicts, default=str),
                )
                session.add(row)
                session.flush()
                return row.id

    def deduplicate_scan_sessions(self, user_phone: str) -> int:
        """Remove duplicate entries keeping only the latest per (scan_name, scan_type). Returns count removed."""
        removed = 0
        with self.get_session() as session:
            rows = (
                session.query(ScanSession)
                .filter(ScanSession.user_phone == user_phone)
                .order_by(ScanSession.scan_name, ScanSession.scan_type, ScanSession.created_at.desc())
                .all()
            )
            seen: set = set()
            for row in rows:
                key = (row.scan_name, getattr(row, "scan_type", "fundamentals"))
                if key in seen:
                    session.delete(row)
                    removed += 1
                else:
                    seen.add(key)
        return removed

    def get_scan_sessions(
        self, user_phone: str, limit: int = 20, scan_type: str = "fundamentals"
    ) -> List[Dict[str, Any]]:
        with self.get_session() as session:
            rows = (
                session.query(ScanSession)
                .filter(
                    ScanSession.user_phone == user_phone,
                    ScanSession.scan_type == scan_type,
                )
                .order_by(ScanSession.created_at.desc())
                .limit(limit)
                .all()
            )
            return [
                {
                    "id": r.id,
                    "scan_name": r.scan_name,
                    "scan_type": r.scan_type,
                    "symbol_count": r.symbol_count,
                    "results_json": r.results_json,
                    "created_at": str(r.created_at)[:16],
                }
                for r in rows
            ]

    def delete_scan_session(self, session_id: int, user_phone: str) -> bool:
        with self.get_session() as session:
            row = (
                session.query(ScanSession)
                .filter(
                    ScanSession.id == session_id,
                    ScanSession.user_phone == user_phone,
                )
                .first()
            )
            if row:
                session.delete(row)
                return True
        return False

    # ── Holdings operations ──────────────────────────────────────────────────

    def add_holding(self, user_phone: str, asset_type: str, data: Dict[str, Any]) -> int:
        with self.get_session() as session:
            row = Holding(
                user_phone       = user_phone,
                asset_type       = asset_type,
                symbol           = data["symbol"].strip().upper(),
                company_name     = data.get("company_name", ""),
                buy_date         = data["buy_date"],
                buy_price        = float(data["buy_price"]),
                buy_qty          = float(data["buy_qty"]),
                sell_date        = data.get("sell_date") or None,
                sell_price       = float(data["sell_price"]) if data.get("sell_price") else None,
                sell_qty         = float(data["sell_qty"])   if data.get("sell_qty")   else None,
                notes            = data.get("notes", ""),
                transaction_type = "BUY",
                remaining_qty    = float(data["buy_qty"]),
                parent_buy_id    = None,
            )
            session.add(row)
            session.flush()
            return row.id

    def update_holding(self, holding_id: int, user_phone: str,
                       data: Dict[str, Any]) -> bool:
        with self.get_session() as session:
            row = (
                session.query(Holding)
                .filter(Holding.id == holding_id, Holding.user_phone == user_phone)
                .first()
            )
            if not row:
                return False
            row.symbol       = data["symbol"].strip().upper()
            row.company_name = data.get("company_name", row.company_name)
            row.buy_date     = data["buy_date"]
            row.buy_price    = float(data["buy_price"])
            row.buy_qty      = float(data["buy_qty"])
            row.sell_date    = data.get("sell_date") or None
            row.sell_price   = float(data["sell_price"]) if data.get("sell_price") else None
            row.sell_qty     = float(data["sell_qty"])   if data.get("sell_qty")   else None
            row.notes        = data.get("notes", row.notes)
            row.updated_at   = datetime.utcnow()
        return True

    def add_sell_transaction(self, user_phone: str, buy_id: int,
                              sell_data: Dict[str, Any]) -> tuple:
        """Record a partial or full sell against a BUY row.
        Returns (success: bool, error_msg: str, new_id: int|None).
        """
        with self.get_session() as session:
            buy_row = (
                session.query(Holding)
                .filter(
                    Holding.id == buy_id,
                    Holding.user_phone == user_phone,
                    Holding.transaction_type == "BUY",
                )
                .first()
            )
            if not buy_row:
                return False, "Buy record not found.", None
            remaining = buy_row.remaining_qty if buy_row.remaining_qty is not None else buy_row.buy_qty
            sell_qty  = float(sell_data["sell_qty"])
            if sell_qty <= 0:
                return False, "Sell qty must be greater than zero.", None
            if sell_qty > remaining + 1e-6:
                return False, f"Sell qty {sell_qty} exceeds remaining {remaining:.4f}.", None
            sell_row = Holding(
                user_phone       = user_phone,
                asset_type       = buy_row.asset_type,
                symbol           = buy_row.symbol,
                company_name     = buy_row.company_name,
                buy_date         = buy_row.buy_date,
                buy_price        = buy_row.buy_price,
                buy_qty          = sell_qty,
                sell_date        = sell_data["sell_date"],
                sell_price       = float(sell_data["sell_price"]),
                sell_qty         = sell_qty,
                notes            = sell_data.get("notes", ""),
                transaction_type = "SELL",
                remaining_qty    = None,
                parent_buy_id    = buy_id,
            )
            session.add(sell_row)
            buy_row.remaining_qty = round(remaining - sell_qty, 8)
            buy_row.updated_at    = datetime.utcnow()
            session.flush()
            return True, "", sell_row.id

    def update_sell_transaction(self, sell_id: int, user_phone: str,
                                 data: Dict[str, Any]) -> tuple:
        """Edit an existing SELL row; re-adjusts parent BUY remaining_qty.
        Returns (success: bool, error_msg: str).
        """
        with self.get_session() as session:
            sell_row = (
                session.query(Holding)
                .filter(
                    Holding.id == sell_id,
                    Holding.user_phone == user_phone,
                    Holding.transaction_type == "SELL",
                )
                .first()
            )
            if not sell_row:
                return False, "SELL record not found."
            old_qty  = float(sell_row.buy_qty or 0)
            new_qty  = float(data["sell_qty"])
            new_price = float(data["sell_price"])
            if new_qty <= 0:
                return False, "Sell qty must be greater than zero."
            if new_price <= 0:
                return False, "Sell price must be greater than zero."
            # Adjust parent BUY remaining
            if sell_row.parent_buy_id:
                parent = (
                    session.query(Holding)
                    .filter(
                        Holding.id == sell_row.parent_buy_id,
                        Holding.user_phone == user_phone,
                    )
                    .first()
                )
                if parent:
                    avail = round((parent.remaining_qty or 0) + old_qty, 8)
                    if new_qty > avail + 1e-6:
                        return False, f"New qty {new_qty} exceeds available {avail:.4f}."
                    parent.remaining_qty = round(avail - new_qty, 8)
                    parent.updated_at    = datetime.utcnow()
            # Update the SELL row
            sell_row.buy_qty    = new_qty
            sell_row.sell_qty   = new_qty
            sell_row.sell_price = new_price
            sell_row.sell_date  = data["sell_date"]
            sell_row.notes      = data.get("notes", sell_row.notes or "")
            sell_row.updated_at = datetime.utcnow()
        return True, ""

    def get_holdings(self, user_phone: str,
                     asset_type: Optional[str] = None) -> List[Dict[str, Any]]:
        with self.get_session() as session:
            q = session.query(Holding).filter(Holding.user_phone == user_phone)
            if asset_type:
                q = q.filter(Holding.asset_type == asset_type)
            rows = q.order_by(Holding.buy_date.desc(), Holding.created_at.desc()).all()
            return [
                {
                    "id":           r.id,
                    "user_phone":   r.user_phone,
                    "asset_type":   r.asset_type,
                    "symbol":       r.symbol,
                    "company_name":     r.company_name,
                    "buy_date":         r.buy_date,
                    "buy_price":        r.buy_price,
                    "buy_qty":          r.buy_qty,
                    "sell_date":        r.sell_date,
                    "sell_price":       r.sell_price,
                    "sell_qty":         r.sell_qty,
                    "notes":            r.notes,
                    "created_at":       str(r.created_at)[:16],
                    "updated_at":       str(r.updated_at)[:16],
                    "transaction_type": r.transaction_type or "BUY",
                    "remaining_qty":    r.remaining_qty,
                    "parent_buy_id":    r.parent_buy_id,
                }
                for r in rows
            ]


# Singleton instance
db = DatabaseManager()
