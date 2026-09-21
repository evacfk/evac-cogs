"""Game type registration (design doc: "Game type plugin shape").

Adding a new game type is a code change (one module implementing `spawn`,
plus a line registering it here); the scheduler itself never needs to
change. `key` must match the Config `games.<key>` block in config_schema.py.

Every `spawn(cog, channel, game_conf)` implementation is responsible for:
  - Setting `cog.active_game[guild_id] = key` when it starts and clearing it
    (in a `finally`) when it resolves -- this is what enforces the "one
    active minigame at a time" concurrency rule (locked decision #7). The
    scheduler checks this before picking a game and won't schedule the next
    spawn until it's cleared.
  - Paying rewards through `pacing.apply_pacing` / `pacing.record_payout`
    rather than depositing the base reward directly, so the per-user daily
    cap/taper applies uniformly across every game type.
"""
from typing import Awaitable, Callable, Dict

GAME_REGISTRY: Dict[str, Callable[..., Awaitable[None]]] = {}


def register(key: str):
    def decorator(fn):
        GAME_REGISTRY[key] = fn
        return fn
    return decorator
