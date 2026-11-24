import random
import string
from datetime import datetime, timedelta
from typing import Optional, Tuple

from config import config
from src.database.db_manager import db


class OTPService:
    """Generates, sends, and verifies OTPs."""

    def generate_otp(self) -> str:
        return "".join(random.choices(string.digits, k=config.OTP_LENGTH))

    def send_otp(
        self, phone_number: str
    ) -> Tuple[bool, str, Optional[str]]:
        """
        Returns (success, message, dev_otp).
        dev_otp is set only when SMS is not configured (dev mode).
        """
        otp = self.generate_otp()
        expires_at = datetime.utcnow() + timedelta(minutes=config.OTP_EXPIRY_MINUTES)
        db.save_otp(phone_number, otp, expires_at)

        if config.is_sms_configured():
            success = self._send_sms(phone_number, otp)
            if success:
                return True, f"OTP sent to {phone_number}", None
            return False, "Failed to send OTP via SMS. Please try again.", None
        else:
            # Dev mode: return OTP for display in UI
            return True, "OTP generated (dev mode — SMS not configured)", otp

    def verify_otp(self, phone_number: str, otp: str) -> Tuple[bool, str]:
        """Returns (success, message)."""
        record = db.get_latest_otp(phone_number)

        if not record:
            return False, "No active OTP found. Please request a new one."

        if record["attempts"] >= config.OTP_MAX_ATTEMPTS:
            return (
                False,
                "Too many failed attempts. Please request a new OTP.",
            )

        if datetime.utcnow() > record["expires_at"]:
            return False, "OTP has expired. Please request a new one."

        if record["otp_code"] != otp.strip():
            db.increment_otp_attempts(record["id"])
            remaining = config.OTP_MAX_ATTEMPTS - record["attempts"] - 1
            if remaining <= 0:
                return False, "Invalid OTP. No attempts remaining — request a new OTP."
            return False, f"Invalid OTP. {remaining} attempt(s) remaining."

        db.mark_otp_used(record["id"])
        return True, "OTP verified successfully."

    def _send_sms(self, phone_number: str, otp: str) -> bool:
        try:
            from twilio.rest import Client

            client = Client(config.TWILIO_SID, config.TWILIO_TOKEN)
            message = client.messages.create(
                body=(
                    f"Your GURU Fundamental Scanner OTP is: {otp}. "
                    f"Valid for {config.OTP_EXPIRY_MINUTES} minutes. "
                    "Do not share this code."
                ),
                from_=config.TWILIO_FROM,
                to=phone_number,
            )
            return bool(message.sid)
        except Exception as exc:
            print(f"[OTPService] SMS send failed: {exc}")
            return False


otp_service = OTPService()
