"""Default Config schema for MinigameHub.

Mirrors the schema in claude/minigamehub-cog-design.md. Kept in its own module
so the shape is easy to diff against the design doc, and so games/migration
code can import DEFAULT_GUILD without pulling in the whole cog module.
"""
from .constants import DEFAULT_SPAWN_CHANNEL_ID

# lootdrop's 54 scenarios and boss's dedicated pool are seeded in from
# scenarios.py at cog_load time (see minigamehub.py:_seed_scenarios), not
# baked into this static default, so a guild that customizes/deletes some
# doesn't get them silently re-added on every Config.register_guild call.

DEFAULT_GUILD = {
    "enabled": False,
    "spawn_channel": DEFAULT_SPAWN_CHANNEL_ID,
    "activity_window": 300,  # seconds of required recent activity in spawn_channel

    "games": {
        "pet": {
            "enabled": True,
            # Busy-tier baseline (evac tuning, 2026-09-24): the number used
            # as-is when the channel's busy, and stretched by
            # adaptive_pacing's quiet_multiplier as it gets quieter -- see
            # "adaptive_pacing" below and MinigameHub._pacing_multiplier.
            "min_frequency": 1500,
            "max_frequency": 4500,
            "next_spawn": 0,
            "adaptive_pacing": True,  # scale with how busy the channel is (see top-level "adaptive_pacing")
            "reward_range": [100, 500],
            "spawn_message": "Hi! Can someone pet me?",
            "pet_reaction": "\U0001F44B",  # 👋
            "goodbye_message": "borf! (thanks h00man, have a doggocoin)",
            "window_seconds": 60,
        },
        "mathdrop": {
            "enabled": True,
            "min_frequency": 1500,
            "max_frequency": 4500,
            "next_spawn": 0,
            "adaptive_pacing": True,
            "reward_range": [50, 550],
            "operators": ["+", "-", "*", "/"],
            "response_timeout": 10,
            "timeout_message": "Too slow!",
        },
        "hunt": {
            "enabled": True,
            "min_frequency": 1500,
            "max_frequency": 4500,
            "next_spawn": 0,
            "adaptive_pacing": True,
            "reward_range": [50, 400],
            "response_timeout": 20,
            "trigger_mode": "both",  # "both" | "word" | "reaction"
            "shoot_word": "bang",
            "safe_word": "salute",
            "shoot_reaction": "\U0001F4A5",  # 💥
            "safe_reaction": "\U0001FAE1",   # 🫡
            "animals": {
                "dove": {"emoji": "\U0001F54A️", "text": "**_Coo!_**"},
                "penguin": {"emoji": "\U0001F427", "text": "**_Noot!_**"},
                "chicken": {"emoji": "\U0001F414", "text": "**_Bah-gawk!_**"},
                "duck": {"emoji": "\U0001F986", "text": "**_Quack!_**"},
                "turkey": {"emoji": "\U0001F983", "text": "**_Gobble-Gobble!_**"},
                "owl": {"emoji": "\U0001F989", "text": "**_Hoo-Hooo!_**"},
                "eagle": {"emoji": "\U0001F985", "text": "**_Caw!_**"},
                "dodo": {"emoji": "\U0001F9A4", "text": "**_Squak!_**"},
            },
            "safe_animals": {
                # key -> per-animal economy override. "safe": True means
                # shooting it costs penalty_pct of the shooter's balance and
                # reward_range is what saluting it pays instead; "safe":
                # False means it's a normal shoot target but reward_range
                # overrides the game-wide reward_range just for this animal
                # (penalty_pct is stored either way but only ever applied
                # while "safe" is True). An animal key with no entry here at
                # all just uses the game-wide reward_range, unaffected.
                # Optional "safe_word" overrides the game-wide safe_word for
                # just this animal (e.g. "caw" for a crow) -- unset means it
                # uses the game-wide word. Only meaningful while "safe": True.
                "eagle": {"safe": True, "penalty_pct": 8.0, "reward_range": [50, 200]},
            },
        },
        "lootdrop": {
            "enabled": True,
            "min_frequency": 750,
            "max_frequency": 3750,
            "next_spawn": 0,
            "adaptive_pacing": True,
            "reward_range": [100, 1000],
            "bad_outcome_chance": 30,
            "streak_bonus": 10,
            "streak_max": 5,
            "streak_timeout": 24,
            "party_drop_chance": 5,
            "party_drop_min": 50,
            "party_drop_max": 200,
            "party_drop_timeout": 30,
            "claim_timeout": 60,
            "scenarios": [],  # seeded from scenarios.SEED_LOOTDROP_SCENARIOS on first load
        },
        "reacttowin": {
            "enabled": True,
            "min_frequency": 750,
            "max_frequency": 2250,
            "next_spawn": 0,
            "adaptive_pacing": True,
            "reward_range": [100, 400],
            "spawn_message": "\U0001F3C3 First to click wins!",
            "response_timeout": 20,
            "streak_bonus_pct": 10,
        },
        "boss": {
            "enabled": True,
            "min_frequency": 3600,
            "max_frequency": 10800,
            "next_spawn": 0,
            # Not adaptive-pacing-scaled -- boss is a longer-form "event"
            # (multi-minute HP-bar fight), not a quick filler minigame, so it
            # stays on its own fixed cadence regardless of how busy chat is.
            "adaptive_pacing": False,
            "fight_duration": 300,
            "hp_update_interval": 3,
            "attack_cooldown": 1.5,
            "hit_chance": 70,
            "damage_per_hit": [5, 15],
            "tiers": {
                "weak":   {"hp": [50, 100],  "reward": [50, 200],   "penalty": [50, 150],  "weight": 60},
                "medium": {"hp": [150, 300], "reward": [150, 500],  "penalty": [150, 400], "weight": 30},
                "strong": {"hp": [400, 700], "reward": [400, 1200], "penalty": [400, 900], "weight": 10},
            },
            "scenarios": [],  # seeded from scenarios.SEED_BOSS_SCENARIOS on first load
        },
    },

    "payout_pacing": {
        "daily_cap_per_user": 5000,
        "taper_start_pct": 70,
        "taper_floor_pct": 15,
        "reset_timezone": "America/Los_Angeles",
    },

    # Adaptive spawn pacing (evac request, 2026-09-24) -- separate from
    # payout_pacing above (that's the per-user daily reward cap; this is how
    # often games spawn at all). Scales every game's min/max_frequency (that
    # has "adaptive_pacing": True in its own config -- see "games" above) up
    # as chat gets quieter, so spawns thin out during dead stretches instead
    # of firing at the same rate as a busy afternoon. See
    # MinigameHub._pacing_multiplier for the exact math.
    "adaptive_pacing": {
        "enabled": True,
        # "How busy" is read as distinct people who've talked in the spawn
        # channel in the last 5 minutes (activity.py's concurrency window --
        # the same number diagnostics reports as avg_concurrency).
        "busy_threshold": 5,   # >= this many talkers = fully busy (games' min/max_frequency used as-is)
        "quiet_threshold": 3,  # <= this many = fully quiet (frequencies stretched by quiet_multiplier)
        "quiet_multiplier": 3.0,
        # After every process restart/cog reload, the live "how busy" reading
        # is blank for a while (see ActivityTracker.current_concurrency) --
        # for this many seconds after startup, pacing uses a fixed
        # in-between value instead of trusting that blank reading as "dead
        # quiet". minigamehub reloads on every code deploy, so this matters
        # more here than it would for a cog that's rarely restarted.
        "startup_grace_seconds": 600,
    },

    "activity_tracking": {
        # hour-of-day (str "0".."23", America/Los_Angeles) -> stats dict
        "hourly_buckets": {},
        "sampling_since": 0,
    },

    # Observed spawn cadence (evac request, 2026-09-24: "this needs to be
    # continuous improvement") -- same hour-of-day bucket shape as
    # activity_tracking above, but for what actually spawned rather than
    # chat activity, so `.mgh diagnostics` can show whether adaptive pacing
    # is actually landing in the 5-10 min busy / 15-30 min quiet target
    # ranges, not just chat volume. Logged once per real scheduled spawn (see
    # MinigameHub._log_spawn) -- .mgh test spawns don't count. Starts empty
    # after this deploy since it's brand new; `.mgh diagnostics reset` clears
    # both this and activity_tracking to start a fresh sampling window after
    # a future tuning change.
    "spawn_pacing_tracking": {
        "hourly_buckets": {},
        "sampling_since": 0,
        "last_spawn_at": 0,  # epoch seconds of the most recent logged spawn (any game), for computing the next gap
    },

    "last_game": None,

    # Minimum seconds between any two games' scheduled times. Used only to
    # nudge colliding timers apart -- each game still runs on its own
    # random frequency.
    "min_separation": 120,
}

DEFAULT_MEMBER = {
    "games": {},          # per-game-key -> {"good": int, "bad": int, "streak": int, "highest_streak": int}
    "boss_damage": 0,     # lifetime, for the unified leaderboard
    "payout_today": 0,
    "payout_day": "",     # ISO date string (America/Los_Angeles) payout_today applies to
    "lootdrop_streak": {"streak": 0, "highest_streak": 0, "last_claim": 0},  # timeout-based, ported from calamari/lootdrop
}
