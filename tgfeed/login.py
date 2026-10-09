#!/usr/bin/env python3
"""One-time Telegram login for tgfeed. Standalone: needs only telethon.

Run it inside the red container so the session lands in the persisted /data mount:

    docker exec -it red python /data/cogs/CogManager/cogs/tgfeed/login.py

It asks for your phone number, then the code Telegram sends you (and your 2FA
password if you have one). It tries exactly once and never loops. Nothing you type
is stored anywhere except Telegram's own session file, which is locked to the bot's
user. Delete tgfeed.session in the data dir to log out locally; terminate the session
under Telegram -> Settings -> Devices to revoke it everywhere.
"""
import asyncio
import getpass
import os
import sys


async def main() -> int:
    api_id = os.environ.get("TG_API_ID", "").strip()
    api_hash = os.environ.get("TG_API_HASH", "").strip()
    if not api_id.isdigit() or not api_hash:
        print("TG_API_ID / TG_API_HASH are not set in this container's environment. Add them to the red service in docker-compose.yml and recreate the container.")
        return 1
    data_dir = os.environ.get("TG_DATA_DIR", "/data/tgfeed")
    os.makedirs(data_dir, mode=0o700, exist_ok=True)
    session = os.path.join(data_dir, "tgfeed")

    from telethon import TelegramClient
    from telethon.errors import PhoneCodeExpiredError, PhoneCodeInvalidError, SessionPasswordNeededError

    client = TelegramClient(session, int(api_id), api_hash)
    await client.connect()
    try:
        if await client.is_user_authorized():
            print("Already logged in. Nothing to do.")
        else:
            phone = input("Phone number with country code (e.g. +15551234567): ").strip()
            sent = await client.send_code_request(phone)
            code = input("Code Telegram just sent you: ").strip()
            try:
                await client.sign_in(phone, code, phone_code_hash=sent.phone_code_hash)
            except SessionPasswordNeededError:
                await client.sign_in(password=getpass.getpass("Two-step verification password: "))
            except (PhoneCodeInvalidError, PhoneCodeExpiredError):
                print("That code was wrong or expired. Run the script again for a fresh one (wait a few minutes first).")
                return 1
        print("Logged in. Session saved.")
    finally:
        await client.disconnect()

    path = session + ".session"
    try:
        os.chmod(path, 0o600)
        os.chmod(data_dir, 0o700)
        if os.geteuid() == 0:   # running as root via docker exec: hand the files to the bot's user
            st = os.stat(os.path.dirname(data_dir.rstrip("/")) or "/")
            for target in (data_dir, path):
                os.chown(target, st.st_uid, st.st_gid)
    except OSError as exc:
        print(f"Note: could not tighten permissions on the session file: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
