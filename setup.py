#!/usr/bin/env python3
"""First-time setup: initialise the database and create the admin user."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def main() -> None:
    print("=" * 55)
    print("  GURU Fundamental Scanner — First-Time Setup")
    print("=" * 55)

    from config import config
    from src.database.db_manager import db

    print("✅ Database initialised:", config.DB_PATH)

    admin_phone = config.ADMIN_PHONE
    if not admin_phone:
        print("\n📱 No ADMIN_PHONE found in .env")
        try:
            admin_phone = input("   Enter admin mobile number (+91XXXXXXXXXX or 10 digits): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n⚠️  No admin phone provided — skipping admin creation.")
            print("   Set ADMIN_PHONE in .env then run setup.py again.\n")
            return

    # Normalise
    cleaned = admin_phone.replace(" ", "").replace("-", "")
    if not cleaned.startswith("+"):
        if len(cleaned) == 10:
            cleaned = f"+91{cleaned}"
        elif len(cleaned) == 12 and cleaned.startswith("91"):
            cleaned = f"+{cleaned}"

    existing = db.get_user(cleaned)
    if existing:
        db.update_user(cleaned, is_admin=True, is_active=True)
        print(f"✅ Existing user promoted to admin: {cleaned}")
    else:
        db.add_user(cleaned, is_active=True, is_admin=True, added_by="setup")
        print(f"✅ Admin user created: {cleaned}")

    if config.is_sms_configured():
        print("✅ Twilio SMS configured — OTP will be sent via SMS.")
    else:
        print("⚠️  Twilio not configured — running in DEV MODE.")
        print("   OTP will be shown directly in the UI.")
        print("   Add TWILIO_* vars to .env for production SMS OTP.")

    print("\n✅ Setup complete!")
    print("   Start the app:  streamlit run app.py")
    print("   Or run:         bash run.sh\n")


if __name__ == "__main__":
    main()
