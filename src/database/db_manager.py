import json
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
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
            from config import config
            self.engine = create_engine(
                f"sqlite:///{config.DB_PATH}",
                connect_args={"check_same_thread": False},
                echo=config.DEBUG,
            )
            Base.metadata.create_all(self.engine)
            # Migrate: add scan_type column if the table was created before this change
            try:
                with self.engine.connect() as conn:
                    conn.execute(text(
                        "ALTER TABLE scan_sessions ADD COLUMN scan_type TEXT NOT NULL DEFAULT 'fundamentals'"
                    ))
                    conn.commit()
            except Exception:
                pass  # Column already exists
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

    # ── Stock cache operations ────────────────────────────────────────────────

    def cache_stock_data(self, symbol: str, data: Dict[str, Any]) -> None:
        with self.get_session() as session:
            cache = (
                session.query(StockCache)
                .filter(StockCache.symbol == symbol)
                .first()
            )
            if cache:
                cache.data = json.dumps(data, default=str)
                cache.fetched_at = datetime.utcnow()
            else:
                cache = StockCache(
                    symbol=symbol,
                    data=json.dumps(data, default=str),
                )
                session.add(cache)

    def get_cached_stock(self, symbol: str) -> Optional[Dict[str, Any]]:
        with self.get_session() as session:
            cache = (
                session.query(StockCache)
                .filter(StockCache.symbol == symbol)
                .first()
            )
            if cache:
                return {
                    "data": json.loads(cache.data),
                    "fetched_at": cache.fetched_at,
                }
        return None

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


# Singleton instance
db = DatabaseManager()
