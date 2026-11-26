import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from src.auth.otp_service import otp_service
from src.database.db_manager import db


class AuthManager:
    """Handles user authentication and admin user management."""

    PHONE_RE = re.compile(r"^\+?[0-9\s\-]{7,15}$")

    # ── OTP flow ──────────────────────────────────────────────────────────────

    def request_otp(
        self, phone_number: str
    ) -> Tuple[bool, str, Optional[str]]:
        """
        Validate phone, check user, send OTP.
        Returns (success, message, dev_otp).
        """
        phone = self.normalize_phone(phone_number)
        if not phone:
            return (
                False,
                "Invalid mobile number. Use format: +919876543210 or 9876543210.",
                None,
            )

        user = db.get_user(phone)
        if not user:
            return (
                False,
                "Mobile number not registered. Please contact the admin.",
                None,
            )
        if not user["is_active"]:
            return (
                False,
                "Your account has been deactivated. Please contact the admin.",
                None,
            )

        success, message, dev_otp = otp_service.send_otp(phone)
        return success, message, dev_otp

    def verify_login(
        self, phone_number: str, otp: str
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """
        Verify OTP and log in user.
        Returns (success, message, user_dict or None).
        """
        phone = self.normalize_phone(phone_number)
        if not phone:
            return False, "Invalid phone number.", None

        success, message = otp_service.verify_otp(phone, otp)
        if not success:
            return False, message, None

        db.update_user(phone, last_login=datetime.utcnow())
        user = db.get_user(phone)
        return True, "Login successful.", user

    # ── Admin operations ──────────────────────────────────────────────────────

    def add_user(
        self,
        phone_number: str,
        is_admin: bool = False,
        added_by: Optional[str] = None,
    ) -> Tuple[bool, str]:
        phone = self.normalize_phone(phone_number)
        if not phone:
            return False, "Invalid mobile number format."

        existing = db.get_user(phone)
        if existing:
            return False, f"User {phone} already exists."

        db.add_user(phone, is_active=True, is_admin=is_admin, added_by=added_by)
        role = "admin" if is_admin else "user"
        return True, f"User {phone} added as {role}."

    def list_users(self) -> List[Dict[str, Any]]:
        return db.list_users()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def normalize_phone(self, phone: str) -> Optional[str]:
        """
        Normalize Indian mobile to E.164 (+91XXXXXXXXXX).
        Returns None if invalid.
        """
        if not phone:
            return None

        # Strip whitespace, dashes, parens
        cleaned = re.sub(r"[\s\-\(\)]", "", phone.strip())

        # Already in E.164 format with country code
        if cleaned.startswith("+91") and len(cleaned) == 13:
            digits = cleaned[3:]
            if re.match(r"^[6-9]\d{9}$", digits):
                return cleaned

        # With country code but no plus
        if cleaned.startswith("91") and len(cleaned) == 12:
            digits = cleaned[2:]
            if re.match(r"^[6-9]\d{9}$", digits):
                return f"+91{digits}"

        # 10-digit Indian mobile
        if len(cleaned) == 10 and re.match(r"^[6-9]\d{9}$", cleaned):
            return f"+91{cleaned}"

        # For non-Indian numbers (allow if well-formed E.164)
        if cleaned.startswith("+") and 8 <= len(cleaned) <= 16:
            return cleaned

        return None


auth_manager = AuthManager()
