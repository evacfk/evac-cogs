"""Defaults and tuning values for rejoinwatch."""

VERSION = "1.0.1"

# #mod-chat: the same channel serverpulse and lurker post to.
DEFAULT_MOD_CHANNEL_ID = 912520841613418596
# Mod role: its members may press the Ban / No buttons (so can anyone with Ban Members or Administrator).
DEFAULT_MOD_ROLE_ID = 426696709780013066

DEFAULT_RETENTION_DAYS = 180
MIN_RETENTION_DAYS = 7
MAX_RETENTION_DAYS = 730

# After a leave event, wait this long before judging it. Gives the ban event and the
# kick/ban audit-log entry time to land, so mod removals are not counted as leaves.
SETTLE_SECONDS = 3.0
# An audit-log entry this close to the leave counts as "staff removed them".
AUDIT_WINDOW_SECONDS = 30
# How long a ban event is remembered while waiting for its matching leave event.
BAN_NOTE_TTL_SECONDS = 300

PRUNE_INTERVAL_SECONDS = 6 * 3600
MAX_HISTORY_SHOWN = 10          # leave dates listed in a mod embed
MAX_HANDLED_MESSAGES = 1000     # button messages remembered, so a double click can't act twice

DEFAULT_WARNING = (
    "Welcome back to {server}! A heads-up: you've left the server before. "
    "If you leave again, you may be banned, at a moderator's discretion."
)

GUILD_DEFAULTS = {
    "enabled": True,
    "mod_channel_id": DEFAULT_MOD_CHANNEL_ID,
    "cuddle_channel_id": None,      # set once with `.rejoinwatch fallback #cuddle`
    "mod_role_id": DEFAULT_MOD_ROLE_ID,
    "retention_days": DEFAULT_RETENTION_DAYS,
    "warning_text": DEFAULT_WARNING,
    "leaves": {},                   # str(user_id) -> [unix timestamps of voluntary leaves]
}
