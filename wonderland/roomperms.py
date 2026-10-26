"""AutoRoom owner permissions and the room guard. Pure logic: no discord/redbot imports.

Two jobs, both about rooms made by PhasecoreX's AutoRoom cog:

1. `grant_owner_manage_permissions` adds Discord's channel-level "Manage Permissions" (the
   `manage_roles` overwrite flag) to the permission set AutoRoom gives a room's owner, so the owner can
   use the native Edit Channel > Permissions tab on their own room.
2. `plan_guard` is the safety net that goes with it. AutoRoom's own `.autoroom allow` refuses anyone who
   couldn't Connect on the Room Creator source; the native Permissions tab has no such rule. The guard
   compares a room to its source and says what must be put back: roles the source denies must stay
   denied, and a member holding such a role must not hold a personal allow.

An overwrite is a dict of flag -> True / False / None. A target is ("role", id) or ("member", id).
"""
from __future__ import annotations

from typing import Dict, Iterable, Mapping, Optional, Set, Tuple

Target = Tuple[str, int]
Overwrite = Mapping[str, Optional[bool]]

# The flags the guard cares about: whether someone can see the room and whether they can join it.
GUARDED_FLAGS = ("connect", "view_channel")

OWNER_EXTRA_FLAG = "manage_roles"  # discord.py's name for the channel "Manage Permissions" overwrite


def should_grant_owner_manage_permissions(cfg: Optional[Mapping], enabled: bool, bot_can_manage_roles: bool) -> bool:
    """True when this source config should hand its room owners Manage Permissions.

    Fails closed: no config, the feature switched off, the bot unable to set the flag (Discord would
    reject room creation outright), or AutoRoom's own owner-rights switch (`ownermodify`) turned off all
    mean "grant nothing"."""
    if not cfg or not enabled or not bot_can_manage_roles:
        return False
    if not cfg.get("perm_owner_manage_channels"):
        return False
    perms = cfg.get("perms")
    return isinstance(perms, Mapping) and isinstance(perms.get("owner"), Mapping)


def grant_owner_manage_permissions(cfg: dict) -> dict:
    """Add Manage Permissions to the owner's permission set in an AutoRoom source config.

    Replaces `cfg["perms"]["owner"]` with a copy that has exactly one extra flag, so the shared
    allow / lock / deny / access sets are never touched. Safe to call twice."""
    cfg["perms"]["owner"] = {**cfg["perms"]["owner"], OWNER_EXTRA_FLAG: True}
    return cfg


def protected_roles(source: Mapping[Target, Overwrite], everyone_id: int) -> Dict[int, Tuple[str, ...]]:
    """Roles the Room Creator source explicitly denies Connect, with the guarded flags it denies them.

    @everyone is left out: AutoRoom itself flips it between public, locked and private."""
    result: Dict[int, Tuple[str, ...]] = {}
    for (kind, target_id), overwrite in source.items():
        if kind != "role" or target_id == everyone_id:
            continue
        if overwrite.get("connect") is False:
            result[target_id] = tuple(flag for flag in GUARDED_FLAGS if overwrite.get(flag) is False)
    return result


def plan_guard(
    room: Mapping[Target, Overwrite],
    source: Mapping[Target, Overwrite],
    everyone_id: int,
    member_roles: Mapping[int, Iterable[int]],
    exempt_members: Iterable[int] = (),
) -> Dict[Target, Dict[str, Optional[bool]]]:
    """What to change in `room` so the source's denies still hold. Empty dict = nothing to do.

    - A protected role must be denied every guarded flag the source denies it, in the room too.
      (Missing, neutral and allow all count as weakened and are set back to False.)
    - A member who holds a protected role must not have a personal allow for a flag that role is denied
      at the source; such an allow is reset to neutral (None), not to a deny, so the owner can still
      remove or re-add the person by command.
    - `exempt_members` (bots, staff, the room's owner, the server owner) are never touched.
    """
    protected = protected_roles(source, everyone_id)
    exempt: Set[int] = set(exempt_members)
    fixes: Dict[Target, Dict[str, Optional[bool]]] = {}

    for role_id, flags in protected.items():
        current = room.get(("role", role_id), {})
        for flag in flags:
            if current.get(flag) is not False:
                fixes.setdefault(("role", role_id), {})[flag] = False

    for (kind, member_id), overwrite in room.items():
        if kind != "member" or member_id in exempt:
            continue
        held = [role_id for role_id in member_roles.get(member_id, ()) if role_id in protected]
        if not held:
            continue
        for flag in GUARDED_FLAGS:
            if overwrite.get(flag) is True and any(flag in protected[role_id] for role_id in held):
                fixes.setdefault(("member", member_id), {})[flag] = None

    return fixes


def describe_fixes(fixes: Mapping[Target, Mapping[str, Optional[bool]]], limit: int = 8) -> list:
    """Short human lines for the mod log, e.g. '<@&5> can no longer join: denied again'."""
    lines = []
    for (kind, target_id), changes in fixes.items():
        mention = f"<@&{target_id}>" if kind == "role" else f"<@{target_id}>"
        flags = " and ".join("see" if flag == "view_channel" else "join" for flag in changes)
        if kind == "role":
            lines.append(f"{mention}: {flags} denied again")
        else:
            lines.append(f"{mention}: personal allow to {flags} removed")
    if len(lines) > limit:
        extra = len(lines) - limit
        lines = lines[:limit] + [f"...and {extra} more"]
    return lines
