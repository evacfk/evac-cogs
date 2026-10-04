"""Config defaults and fixed values for celebrations."""
from zoneinfo import ZoneInfo

COG_VERSION = "1.0.1"
TZ = ZoneInfo("America/Los_Angeles")
CONFIG_IDENTIFIER = 0xCE1EB8A7

TICK_MINUTES = 10
RECENT_CELEBRATION_DAYS = 300  # never celebrate (or gift) the same member twice inside this window
MIN_AGE = 13  # Discord's minimum; a birth year implying less is rejected
ANNIVERSARY_LIST_MAX = 40  # names listed in one anniversary embed before "+N more"

COLOR_BIRTHDAY = 0xFF7EB6
COLOR_ANNIVERSARY = 0xF1C40F

DEFAULT_GUILD = {
    "channel_id": None,             # #cuddle: birthday + anniversary posts (these stay up)
    "announce_channel_id": None,    # #announcements: birthday post, deleted when the day ends
    "role_id": None,                # hoisted birthday role, held for the (Pacific) day
    "gift_min": 20000,
    "gift_max": 30000,              # 0 = no gift; min == max = fixed amount
    "gift_min_member_days": 14,     # anti-farming: no gift for someone who joined in the last N days
    "announce_hour": 9,             # local hour posts go out (role + gift start at midnight)
    "star_emoji": "\N{WHITE MEDIUM STAR}",  # added to the #cuddle post so people can star it; "" = off
    "anniversaries": True,
    "anniversary_gift": 0,
    "state": {},                    # {"date": iso, "birthdays": {uid: {...}}, "anniv_posted": bool}
}

DEFAULT_MEMBER = {
    "month": None,
    "day": None,
    "year": None,                   # optional; when set, the post says how old they're turning
    "last_celebrated": None,        # iso date
}
