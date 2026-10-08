"""All defaults and lookup tables for afterdark -- no discord/redbot imports."""

VERSION = "1.3.0"
BUILD = "afterdark build: multi-v1 (interests open several channels, rules panel)"

CONFIG_IDENTIFIER = 7316482950
DAY = 86400
LA_TZ = "America/Los_Angeles"

# Custom IDs of persistent components. Never change these once messages with
# the buttons exist, or the old buttons stop working.
RABBIT_CLAIM_ID = "afterdark:rabbit:claim"
INVITE_ACCEPT_ID = "afterdark:invite:accept"
INVITE_DECLINE_ID = "afterdark:invite:decline"
REVIEW_YES_ID = "afterdark:review:yes"
REVIEW_NO_ID = "afterdark:review:no"
INTEREST_ID_PREFIX = "afterdark:interest:"

# ---- Wonderland IDs (pre-filled so setup is just interests + posting) ----
DEFAULT_ADULT_ROLE_ID = 1400989955794276452   # "Adult Chat" (the old NSFW role)
DEFAULT_RABBIT_ROLE_ID = 1557705475091472445  # the white-rabbit-emoji role
DEFAULT_UNDERAGE_ROLE_IDS = [554840521617440778]  # 13-17
DEFAULT_ADULT_AGE_ROLE_IDS = [
    554840801079984128,   # 18-21
    554840950049079306,   # 22-25
    1400940701994320074,  # 26-29
    554841909412102169,   # 30+
]
DEFAULT_CLUE_CHANNEL_ID = 1160321156579082290  # #pillow-talk
DEFAULT_LOG_CHANNEL_ID = 416660303741452299    # #mod-commands
# Staff are exempt from the interest-channel inactivity rule: Mod role,
# superpowers, higher entity. (Admins / Manage Server are exempt too.)
DEFAULT_EXEMPT_ROLE_IDS = [426696709780013066, 723377047950590033, 458830622597840906]

CONTACT_TEXT = "Contact ModMail."

WHITE = 0xFFFFFF   # the white rabbit: border colour of the Rabbit Hole embeds
# The Rabbit Hole rules, shown in the panel embed. Plain text / Discord markdown,
# at most ~3800 characters. Empty = no rules section.
RULES_TEXT = ""

ACCESS_MODES = ("roles", "overwrites")
# Discord allows 100 permission overwrites per channel; refuse before the cap.
OVERWRITE_SOFT_CAP = 95

GUILD_DEFAULTS = dict(
    adult_role_id=DEFAULT_ADULT_ROLE_ID,
    rabbit_role_id=DEFAULT_RABBIT_ROLE_ID,
    underage_role_ids=list(DEFAULT_UNDERAGE_ROLE_IDS),
    adult_age_role_ids=list(DEFAULT_ADULT_AGE_ROLE_IDS),
    exempt_role_ids=list(DEFAULT_EXEMPT_ROLE_IDS),
    skip_role_ids=[],            # extra "paused" roles; members holding one are never touched
    clue_channel_id=DEFAULT_CLUE_CHANNEL_ID,
    log_channel_id=DEFAULT_LOG_CHANNEL_ID,
    min_level=3,
    access_mode="overwrites",    # "overwrites" (per-user, no role) | "roles" -- how interest access is granted
    interests={},                # key -> Interest.to_dict()
    # ---- inactivity (interest channels only) ----
    warn_days=7,
    remove_days=14,
    # ---- sweep ----
    dry_run=True,                # sweep only REPORTS until this is turned off
    sweep_max=25,                # circuit breaker: abort a live sweep that would act on more
    sweep_interval_minutes=60,
    last_sweep={},
    # ---- invitations ----
    invites_enabled=False,
    invite_min=3,
    invite_max=4,
    invite_ttl_days=30,
    invite_snooze_days=7,        # "Not now" keeps someone in the pool but skips them for this long
    invite_snoozed={},           # str(uid) -> unix time they may be suggested again
    invite_round={},             # today's review round: {"date","target","sent","done","prompt":{message_id,channel_id,user_id}}
    last_invite_date="",         # America/Los_Angeles date of the last daily batch
    invites={},                  # str(uid) -> Invite.to_dict()  (outstanding)
    declined=[],                 # uids that declined: never invited again
    # ---- state ----
    excluded={},                 # str(uid) -> {"by","reason","ts"}
    lapsed={},                   # interest key -> [uid]; removed for inactivity, ModMail to return
    granted={},                  # str(uid) -> {"ts","via","by"}
    interest_state={},           # key -> {str(uid): {"since","last","warned"}}
    rabbit_post={},              # {"channel_id","message_id"}
    panel_post={},               # {"channel_id","message_id"}
)

# Activity timestamps are only rewritten when this old, so a chatty member does
# not mark the state dirty on every message.
TOUCH_RESOLUTION = 6 * 3600
STATE_FLUSH_SECONDS = 900
LOG_LINE_CAP = 25
