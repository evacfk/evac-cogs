"""Shared constants for MinigameHub.

Values here are the evac.dev-specific defaults called out in the design doc
(claude/minigamehub-cog-design.md). Everything is still Config-editable per
guild via `.minigamehub` commands -- these are just the seed defaults.
"""
from zoneinfo import ZoneInfo

# Default fixed spawn channel (general chat). Editable via `.minigamehub channel`.
DEFAULT_SPAWN_CHANNEL_ID = 93284396739604480

# Mod role gate for everything except the two wipe commands (bot-owner only).
MOD_ROLE_ID = 426696709780013066

# Payout-cap daily reset boundary -- corrected in design review from UTC to the
# bot's actual configured timezone so "daily" resets overnight, not mid-afternoon.
RESET_TIMEZONE = ZoneInfo("America/Los_Angeles")

# Scheduler tick interval, matching calamari/lootdrop's own tasks.loop cadence.
SCHEDULER_TICK_SECONDS = 30

# Registry order also doubles as the default enable order / UI ordering.
# "trivia" intentionally omitted -- paused per design review, adding it back
# later is additive (one new key, no scheduler changes).
GAME_KEYS = ["pet", "mathdrop", "hunt", "lootdrop", "reacttowin", "boss"]

CONFIG_IDENTIFIER = 0xE7ACF3E5  # arbitrary, stable, evac/minigamehub
