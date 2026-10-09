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


LIB_DIRS = ("/data/cogs/Downloader/lib",)   # where Red's Downloader puts cog requirements


def ensure_module(name: str, extra_dirs=LIB_DIRS) -> bool:
    """Red installs cog requirements into its own lib dir and only adds it to sys.path
    inside the bot process, so a script run by `docker exec` has to look there itself."""
    import importlib
    import importlib.util

    if importlib.util.find_spec(name) is not None:
        return True
    extra = [os.environ.get("TG_LIB_DIR", "")] + list(extra_dirs)
    for directory in extra:
        if directory and os.path.isdir(directory) and directory not in sys.path:
            sys.path.append(directory)
            importlib.invalidate_caches()
            if importlib.util.find_spec(name) is not None:
                return True
    return importlib.util.find_spec(name) is not None


async def main() -> int:
    api_id = os.environ.get("TG_API_ID", "").strip()
    api_hash = os.environ.get("TG_API_HASH", "").strip()
    if not api_id.isdigit() or not api_hash:
        print("TG_API_ID / TG_API_HASH are not set in this container's environment. Add them to the red service in docker-compose.yml and recreate the container.")
        return 1
    data_dir = os.environ.get("TG_DATA_DIR", "/data/tgfeed")
    os.makedirs(data_dir, mode=0o700, exist_ok=True)
    session = os.path.join(data_dir, "tgfeed")

    if not ensure_module("telethon"):
        print("telethon isn't installed in this container yet. In Discord run `.cog install evac-cogs tgfeed` first "
              "(it installs telethon), or set TG_LIB_DIR to the folder that holds it.")
        return 1
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
