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
            "min_frequency": 1800,
            "max_frequency": 5400,
            "reward_range": [100, 500],
            "spawn_message": "Hi! Can someone pet me?",
            "pet_reaction": "\U0001F44B",  # 👋
            "goodbye_message": "borf! (thanks h00man, have a doggocoin)",
            "window_seconds": 60,
        },
        "mathdrop": {
            "enabled": True,
            "min_frequency": 900,
            "max_frequency": 2700,
            "reward_range": [50, 550],
            "operators": ["+", "-", "*", "/"],
            "response_timeout": 10,
            "timeout_message": "Too slow!",
        },
        "hunt": {
            "enabled": True,
            "min_frequency": 900,
            "max_frequency": 3600,
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
                # key -> percent of the shooter's balance taken as a penalty
                "eagle": {"penalty_pct": 8.0},
            },
        },
        "lootdrop": {
            "enabled": True,
            "min_frequency": 300,
            "max_frequency": 1800,
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
            "min_frequency": 1200,
            "max_frequency": 3600,
            "reward_range": [100, 400],
            "spawn_message": "\U0001F3C3 First to click wins!",
            "response_timeout": 20,
            "streak_bonus_pct": 10,
        },
        "boss": {
            "enabled": True,
            "min_frequency": 3600,
            "max_frequency": 10800,
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

    "activity_tracking": {
        # hour-of-day (str "0".."23", America/Los_Angeles) -> stats dict
        "hourly_buckets": {},
        "sampling_since": 0,
    },

    "next_spawn": 0,
    "last_game": None,
}

DEFAULT_MEMBER = {
    "games": {},          # per-game-key -> {"good": int, "bad": int, "streak": int, "highest_streak": int}
    "boss_damage": 0,     # lifetime, for the unified leaderboard
    "payout_today": 0,
    "payout_day": "",     # ISO date string (America/Los_Angeles) payout_today applies to
    "lootdrop_streak": {"streak": 0, "highest_streak": 0, "last_claim": 0},  # timeout-based, ported from calamari/lootdrop
}
