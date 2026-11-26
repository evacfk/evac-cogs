"""All defaults and lookup tables for afterdark -- no discord/redbot imports."""

VERSION = "1.9.0"
CHANNEL_PREFIX = "🤖・"   # every bot-made channel starts like this, matching the other bot channels
BUILD = "afterdark build: permanent-rabbit-v1 (one permanent pinnable rabbit message, drops retired, level 5)"

CONFIG_IDENTIFIER = 7316482950
DAY = 86400
LA_TZ = "America/Los_Angeles"

# Custom IDs of persistent components. Never change these once messages with
# the buttons exist, or the old buttons stop working.
RABBIT_CLAIM_ID = "afterdark:rabbit:claim"
# The rabbit message: (description, footer). Posted once with `.afterdark post`, never deleted by the bot.
RABBIT_TEXT = (
    "*I'm late, I'm late.*\n\nThere is a door that only regulars ever find. Tap the rabbit and see whether it opens for you.",
    "Curiouser and curiouser.",
)
MIN_LEVEL = 5
SCHEMA_VERSION = 1       # bump when a one-time per-guild migration is added
INVITE_ACCEPT_ID = "afterdark:invite:accept"
INVITE_DECLINE_ID = "afterdark:invite:decline"
REVIEW_YES_ID = "afterdark:review:yes"
REVIEW_NO_ID = "afterdark:review:no"
REVIEW_STOP_ID = "afterdark:review:stop"
INTEREST_ID_PREFIX = "afterdark:interest:"
LEAVE_ID = "afterdark:rabbit:leave"

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
RULES_TEXT = """**Welcome to the Rabbit Hole.** This is an adults-only space inside Wonderland. Staying here is a privilege, so please read all of this.

**Who's here**
- You must be 18+. If you're not, or you're unsure, leave now.
- Access can be removed at any time at staff discretion.

**Respect**
- Don't pester members. No unsolicited DMs, repeated requests or pressure. "No" means no.
- No harassment, hate speech or doxxing.
- Keep content in the right channel and use spoilers where a channel asks for them.
- No selling, trading, advertising or self-promotion unless a channel says so.
- Only members with the **Age Verified** role, and with a moderator's permission, may post photos of themselves with nudity.
- The Age Verified role is optional and only for people who want to share photos of themselves. We don't collect IDs from everyone, to avoid the security risk.

**Privacy**
- What's posted here stays here. Don't screenshot, re-post or share content or members outside this space.
- Don't ask other members for personal info.

**Staying in**
- Each interest channel you join is tracked separately. If you stop posting or reacting in one for a while, you'll get a warning and then lose access to that channel. You can rejoin from the buttons below.
- Losing your Adult Chat or age role removes you from the Rabbit Hole.
- If you've lost access and think it's a mistake, contact ModMail."""

ACCESS_MODES = ("roles", "overwrites")
# Discord allows 100 permission overwrites per channel; refuse before the cap.
OVERWRITE_SOFT_CAP = 95
HIDE_PREFIX = "🤖"      # channels whose name starts with this can be hidden in one go
DEFAULT_HIDE_CATEGORY_ID = 633773912030838805   # the NSFW category: the only place the hide looks
HIDE_PREVIEW_TTL = 900          # seconds a preview stays valid for the hide that follows it

GUILD_DEFAULTS = dict(
    adult_role_id=DEFAULT_ADULT_ROLE_ID,
    verified_role_id=0,          # "Age Verified": invited first when set (0 = no priority)
    rabbit_role_id=DEFAULT_RABBIT_ROLE_ID,
    underage_role_ids=list(DEFAULT_UNDERAGE_ROLE_IDS),
    adult_age_role_ids=list(DEFAULT_ADULT_AGE_ROLE_IDS),
    exempt_role_ids=list(DEFAULT_EXEMPT_ROLE_IDS),
    skip_role_ids=[],            # extra "paused" roles; members holding one are never touched
    clue_channel_id=DEFAULT_CLUE_CHANNEL_ID,
    schema_version=0,            # per-guild one-time migrations already applied (see AfterDark._migrate_guild)
    drop_state={},               # legacy: only read once to delete a leftover timed drop, then cleared
    log_channel_id=DEFAULT_LOG_CHANNEL_ID,
    min_level=MIN_LEVEL,
    access_mode="overwrites",    # "overwrites" (per-user, no role) | "roles" -- how interest access is granted
    interests={},                # key -> Interest.to_dict()
    interest_category_id=0,      # category new interest channels are created in (0 = not set)
    # ---- inactivity (interest channels only) ----
    warn_days=7,
    remove_days=14,
    # ---- sweep ----
    dry_run=True,                # sweep only REPORTS until this is turned off
    sweep_max=25,                # circuit breaker: abort a live sweep that would act on more
    sweep_interval_minutes=60,
    last_sweep={},
    digest_last=0.0,             # unix time of the last sweep digest posted (sweeps report at most once a day)
    digest_pending={},           # {"dry": bool, "revoke": [...], "remove": [...], "warn": [...]} waiting for the next digest
    digest_enabled=True,         # False mutes the daily sweep digest (aborts and errors still post)
    digest_sig="",               # fingerprint of the last DRY-RUN digest posted: an identical one is not posted again
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
    hide_category_id=DEFAULT_HIDE_CATEGORY_ID,   # hide only touches text channels in this category
    hidden_channels={},          # str(channel id) -> {"flipped": [target ids], "default_view": bool|None}; how to undo a hide
    hidden_feeds=[],             # feed ids ("r:name"/"t:id") that a hide paused, so unhide resumes only those
    hide_preview={},             # {"ts", "ids"}: what the last preview listed; a hide only acts on this
)

# Activity timestamps are only rewritten when this old, so a chatty member does
# not mark the state dirty on every message.
TOUCH_RESOLUTION = 6 * 3600
STATE_FLUSH_SECONDS = 900
LOG_LINE_CAP = 25
DIGEST_SECONDS = 24 * 3600       # sweeps run hourly but report once a day
DIGEST_LINE_CAP = 10             # names listed per kind in one digest (the rest become "...and N more")
