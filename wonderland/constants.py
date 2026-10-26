"""Fixed values for the Wonderland guide. No discord/redbot imports here."""

COG_VERSION = "1.5.1"
CONFIG_ID = 0x574C44  # "WLD"

VIEW_TIMEOUT = 120          # seconds the topic buttons stay alive
STUB_TTL = 60               # seconds the one-line "tap to open" stub stays in the channel
STATS_COOLDOWN = 15
SEARCH_MAX_RESULTS = 15
SEARCH_MIN_CHARS = 2

EMBED_COLOR = 0xB388FF
FOOTER = "Wonderland Guide · .wld"
PUBLISH_FOOTER = "Run .wld in any channel for the interactive guide."

# Room owner permissions + guard (roomperms.py). IDs over names; verify against the live server.
MOD_LOG_CHANNEL_ID = 397513667052175360  # #mod-log
GUARD_DELAY = 1.5   # seconds to let a burst of edits settle (and the audit log catch up) before checking a room
GUARD_PASSES = 3    # re-check after a fix, in case the owner edited again while we worked
GUARD_LOG_COOLDOWN = 60  # seconds between mod-log reports for the same room (fixes still happen every time)
GUARD_ROUNDS = 5  # most times one room is re-checked in a row because it kept being edited
