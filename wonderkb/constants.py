"""Fixed values for wonderkb. No discord/redbot imports here."""

COG_VERSION = "1.0.1"
CONFIG_ID = 0x574B42  # "WKB"

AUTO_PREFIX = "kb:"       # entries this cog owns and may rewrite or delete
MANUAL_PREFIX = "kbm:"    # entries a mod added with `.kb add`; sync never touches these

MAX_CHUNK_CHARS = 1100
MAX_CHANNEL_MESSAGES = 100
SYNC_CHECK_MINUTES = 30
DEFAULT_INTERVAL_HOURS = 24

DEFAULT_SOURCES = ("announcements", "server-guide", "rules", "server-updates", "shop")
DEFAULT_MOD_CHANNEL = "mod-chat"
CURRENCY = "wondercoins"
BOT_CHANNEL = "bot-commands"   # where members are told to run commands

# The assistant's refusal line starts like this (see the system prompt). Matched case-insensitively.
UNSURE_MARKER = "I'm not sure, try"
SEEN_TTL_SECONDS = 7 * 24 * 3600
SEEN_MAX = 300
QUESTION_MAX_CHARS = 600
ANSWER_MAX_CHARS = 1500

EMBED_COLOR = 0xB388FF
