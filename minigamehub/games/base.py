"""Game type registration (design doc: "Game type plugin shape").

Adding a new game type is a code change (one module implementing `spawn`,
plus a line registering it here); the scheduler itself never needs to
change. `key` must match the Config `games.<key>` block in config_schema.py.

Every `spawn(cog, channel, game_conf, dry_run=False)` implementation is
responsible for:
  - Setting `cog.active_game[guild_id] = key` when it starts and clearing it
    (in a `finally`) when it resolves -- this is what enforces the "one
    active minigame at a time" concurrency rule (locked decision #7). The
    scheduler checks this before picking a game and won't schedule the next
    spawn until it's cleared.
  - Paying rewards through `pacing.settle_reward` / penalties through
    `pacing.settle_penalty` rather than touching `bank` directly, so the
    per-user daily cap/taper applies uniformly across every game type.
  - Accepting a `dry_run` kwarg (default False) and threading it into every
    `pacing.settle_reward` / `pacing.settle_penalty` call, and skipping any
    `stats.record_result` / `stats.add_boss_damage` call, when set. This is
    what backs `.minigamehub test` -- the mechanic plays out for real (embeds,
    buttons, timing) but no currency or stats actually move. A game whose
    payout happens inside a button callback (not the main `spawn` coroutine)
    needs to stash `dry_run` on its View so the callback can see it.
"""
from typing import Awaitable, Callable, Dict

GAME_REGISTRY: Dict[str, Callable[..., Awaitable[None]]] = {}


def register(key: str):
    def decorator(fn):
        GAME_REGISTRY[key] = fn
        return fn
    return decorator
