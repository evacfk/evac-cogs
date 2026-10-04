"""Config defaults and fixed values for wonderevents."""
from zoneinfo import ZoneInfo

COG_VERSION = "1.3.1"
CONFIG_IDENTIFIER = 0x3E7E27A1
HOME_TZ_NAME = "America/Los_Angeles"
HOME_TZ = ZoneInfo(HOME_TZ_NAME)
DEFAULT_MOD_ROLE_ID = 426696709780013066

TICK_SECONDS = 60
SAMPLE_MINUTES = 5          # voice attendance is sampled this often while an event runs
ATTEND_MIN_MINUTES = 20     # this long in the voice channel counts as "came"
DEFAULT_DURATION_MIN = 180
EVENT_LOCATION = "Wonderland voice channels"  # Discord requires a place on non-voice events
ROLE_COLOR = 0x9B59B6

GENERIC_PING_ROLE_ID = 1556157565262364754   # pinged for any kind that isn't a movie or a game
BUILTIN_KINDS = {                            # work out of the box; no `.event kind add` needed
    "movie": {"label": "Movie Night", "emoji": "\N{CLAPPER BOARD}", "ping_role_id": 1536593335966367784,
              "duration": DEFAULT_DURATION_MIN},
    "game": {"label": "Game Night", "emoji": "\N{VIDEO GAME}", "ping_role_id": 1536590356492320768,
             "duration": DEFAULT_DURATION_MIN},
    "event": {"label": "Event", "emoji": "\N{CALENDAR}", "ping_role_id": GENERIC_PING_ROLE_ID,
              "duration": DEFAULT_DURATION_MIN},
}
DEFAULT_REMIND_MIN = 60
POLL_CLOSE_BEFORE_HOURS = 2  # a vote closes this long before the event starts
POLL_MAX_HOURS = 168
POLL_MAX_OPTIONS = 10
LIST_MENTIONS_MAX = 30

COLOR_EVENT = 0x9B59B6
COLOR_LIVE = 0xE74C3C
COLOR_DONE = 0x95A5A6

BTN_GOING = "wonderevents:going"
BTN_MAYBE = "wonderevents:maybe"
BTN_NO = "wonderevents:no"

DEFAULT_GUILD = {
    "channel_id": None,           # where event announcements are posted (e.g. #server-updates)
    "host_role_ids": [DEFAULT_MOD_ROLE_ID],  # besides admins/mods, these roles may create/edit events
    "remind_minutes": DEFAULT_REMIND_MIN,
    "kinds": {},                  # key -> {label, emoji, ping_role_id, duration}
    "events": {},                 # str(id) -> event record
    "next_id": 1,
}

DEFAULT_MEMBER = {
    "tz": None,                   # staff member's own timezone for typing event times
}
