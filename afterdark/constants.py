"""All defaults and lookup tables for afterdark -- no discord/redbot imports."""

VERSION = "1.6.0"
BUILD = "afterdark build: multi-v1 (random rabbit drops, 4 slots, members report, leave button, final, drop next, verified first, stop invites)"

CONFIG_IDENTIFIER = 7316482950
DAY = 86400
LA_TZ = "America/Los_Angeles"

# Custom IDs of persistent components. Never change these once messages with
# the buttons exist, or the old buttons stop working.
RABBIT_CLAIM_ID = "afterdark:rabbit:claim"
DROP_TICK_SECONDS = 30
DROP_MAX_GAP_MINUTES = 24 * 60
# (description, footer) for each rabbit drop. The first is the original clue.
DROP_VARIANTS = (
    ("*I'm late, I'm late.*\n\nThere is a door that only regulars ever find. Tap the rabbit and see whether it opens for you.",
     "Curiouser and curiouser."),
    ("*Follow the white rabbit.*\n\nIt won't wait long. Tap it if you dare.", "It never stays for long."),
    ("The rabbit just ran past with a pocket watch.\n\nNo time to explain. Tap and find out if it was for you.", "Late, late, late."),
    ("*Down the hole we go.*\n\nSomeone left the door ajar. It might open for you.", "Mind the fall."),
    ("A white blur, a flash of a waistcoat.\n\nOnly a few ever catch it. Tap the rabbit.", "Quick now."),
    ("*Oh my ears and whiskers!*\n\nThe rabbit has a door to show someone. Maybe you.", "Not everyone gets in."),
    ("There's a keyhole the size of a mouse and a rabbit with the key.\n\nTap it and see.", "Drink me. Or tap me."),
    ("*Who are you?* said the caterpillar.\n\nTap the rabbit and let the door decide.", "Off with their heads. Or not."),
    ("The rabbit paused. Just for a moment.\n\nIf you've been around long enough, it might let you through.", "Tick, tock."),
    ("*Curiouser.*\n\nA little white door appeared where there wasn't one. Tap the rabbit to try the handle.", "Handle with care."),
    ("You weren't supposed to see this.\n\nBut since you did, tap the rabbit.", "Shh."),
    ("*Every rabbit hole starts somewhere.*\n\nThis one starts here, for a few minutes only.", "Gone soon."),
)
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

GUILD_DEFAULTS = dict(
    adult_role_id=DEFAULT_ADULT_ROLE_ID,
    verified_role_id=0,          # "Age Verified": invited first when set (0 = no priority)
    rabbit_role_id=DEFAULT_RABBIT_ROLE_ID,
    underage_role_ids=list(DEFAULT_UNDERAGE_ROLE_IDS),
    adult_age_role_ids=list(DEFAULT_ADULT_AGE_ROLE_IDS),
    exempt_role_ids=list(DEFAULT_EXEMPT_ROLE_IDS),
    skip_role_ids=[],            # extra "paused" roles; members holding one are never touched
    clue_channel_id=DEFAULT_CLUE_CHANNEL_ID,
    # ---- random rabbit drops (replace the permanent clue message) ----
    drop_enabled=False,
    drop_channel_id=0,           # 0 = use clue_channel_id
    drop_min_minutes=60,         # gap between drops, picked at random in [min, max]
    drop_max_minutes=180,
    drop_max_claims=4,           # people who can get in per drop (0 = unlimited)
    drop_life_minutes=10,        # how long a drop stays before the bot deletes it
    drop_state={},               # {"next_ts","message_id","channel_id","delete_ts","last_variant","claims"}
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
