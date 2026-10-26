"""Pure rules for owner Manage Permissions and the room guard (no discord, no redbot)."""
import copy

import pytest

from wonderland import roomperms as rp

EVERYONE = 1
MEMBER_ROLE = 10   # allowed on the source: the normal members
MUTED = 20         # explicitly denied on the source (connect + view)
QUARANTINE = 21    # explicitly denied connect only
OWNER, GUEST, STAFFER = 100, 101, 102


def source():
    return {
        ("role", EVERYONE): {"connect": False, "view_channel": False},
        ("role", MEMBER_ROLE): {"connect": True, "view_channel": True},
        ("role", MUTED): {"connect": False, "view_channel": False},
        ("role", QUARANTINE): {"connect": False, "view_channel": None},
    }


def fresh_room():
    """What AutoRoom builds from that source, plus its owner."""
    room = copy.deepcopy(source())
    room[("member", OWNER)] = {"connect": True, "view_channel": True}
    return room


def cfg(owner_switch=True):
    allow = {"view_channel": True, "connect": True, "send_messages": True}
    return {
        "perm_owner_manage_channels": owner_switch,
        "perms": {
            "allow": allow,
            "lock": {"view_channel": True, "connect": False, "send_messages": False},
            "deny": {"view_channel": False, "connect": False, "send_messages": False},
            "owner": {**allow, "manage_channels": True, "manage_messages": True},
            "access": allow,
        },
    }


# ---------------------------------------------------------------- the grant

def test_grant_adds_exactly_one_flag_to_the_owner_set_and_nothing_else():
    config = cfg()
    before = copy.deepcopy(config)
    rp.grant_owner_manage_permissions(config)
    assert config["perms"]["owner"]["manage_roles"] is True
    assert {k: v for k, v in config["perms"]["owner"].items() if k != "manage_roles"} == before["perms"]["owner"]
    for shared in ("allow", "lock", "deny", "access"):
        assert config["perms"][shared] == before["perms"][shared]
        assert "manage_roles" not in config["perms"][shared]


def test_grant_does_not_leak_into_the_shared_allow_set_even_though_owner_was_built_from_it():
    config = cfg()
    rp.grant_owner_manage_permissions(config)
    assert config["perms"]["owner"] is not config["perms"]["allow"]


def test_grant_twice_is_the_same_as_once():
    once, twice = cfg(), cfg()
    rp.grant_owner_manage_permissions(once)
    rp.grant_owner_manage_permissions(twice)
    rp.grant_owner_manage_permissions(twice)
    assert once == twice


@pytest.mark.parametrize("config, enabled, bot_ok", [
    (None, True, True),                 # no such AutoRoom source
    ({}, True, True),
    (cfg(), False, True),               # switched off with `.wld roomperms off`
    (cfg(), True, False),               # bot lacks Manage Roles: Discord would refuse to create rooms at all
    (cfg(owner_switch=False), True, True),  # AutoRoom's own `ownermodify` is off: owners get no extra rights
])
def test_nothing_is_granted_unless_every_condition_holds(config, enabled, bot_ok):
    assert rp.should_grant_owner_manage_permissions(config, enabled, bot_ok) is False


def test_grant_when_everything_holds():
    assert rp.should_grant_owner_manage_permissions(cfg(), True, True) is True


def test_a_config_without_an_owner_set_is_never_granted_and_never_crashes():
    broken = {"perm_owner_manage_channels": True, "perms": {"allow": {}}}
    assert rp.should_grant_owner_manage_permissions(broken, True, True) is False
    assert rp.should_grant_owner_manage_permissions({"perm_owner_manage_channels": True}, True, True) is False


# ---------------------------------------------------------------- which roles are protected

def test_protected_roles_are_the_sources_explicit_connect_denies_not_everyone():
    assert rp.protected_roles(source(), EVERYONE) == {MUTED: ("connect", "view_channel"), QUARANTINE: ("connect",)}


def test_a_role_allowed_on_the_source_is_never_protected():
    assert MEMBER_ROLE not in rp.protected_roles(source(), EVERYONE)


# ---------------------------------------------------------------- the guard

def test_an_untouched_room_needs_no_fixes():
    assert rp.plan_guard(fresh_room(), source(), EVERYONE, {}) == {}


def test_deleting_a_protected_roles_overwrite_puts_the_deny_back():
    room = fresh_room()
    del room[("role", MUTED)]
    assert rp.plan_guard(room, source(), EVERYONE, {}) == {("role", MUTED): {"connect": False, "view_channel": False}}


@pytest.mark.parametrize("weakened", [None, True])
def test_neutralising_or_allowing_a_protected_role_is_put_back(weakened):
    room = fresh_room()
    room[("role", QUARANTINE)] = {"connect": weakened, "view_channel": None}
    assert rp.plan_guard(room, source(), EVERYONE, {}) == {("role", QUARANTINE): {"connect": False}}


def test_only_the_weakened_flag_is_restored():
    room = fresh_room()
    room[("role", MUTED)] = {"connect": False, "view_channel": None}
    assert rp.plan_guard(room, source(), EVERYONE, {}) == {("role", MUTED): {"view_channel": False}}


def test_a_personal_allow_for_someone_holding_a_protected_role_is_reset_to_neutral():
    room = fresh_room()
    room[("member", GUEST)] = {"connect": True, "view_channel": True}
    fixes = rp.plan_guard(room, source(), EVERYONE, {GUEST: [MEMBER_ROLE, MUTED]})
    assert fixes == {("member", GUEST): {"connect": None, "view_channel": None}}


def test_a_personal_allow_for_an_ordinary_member_is_left_alone():
    room = fresh_room()
    room[("member", GUEST)] = {"connect": True, "view_channel": True}
    assert rp.plan_guard(room, source(), EVERYONE, {GUEST: [MEMBER_ROLE]}) == {}


def test_a_personal_allow_is_only_reset_for_flags_that_role_is_denied_at_the_source():
    room = fresh_room()
    room[("member", GUEST)] = {"connect": True, "view_channel": True}
    # Quarantine is denied Connect on the source but not View, so View stays.
    assert rp.plan_guard(room, source(), EVERYONE, {GUEST: [QUARANTINE]}) == {("member", GUEST): {"connect": None}}


def test_exempt_members_are_never_touched_even_with_a_protected_role():
    room = fresh_room()
    room[("member", STAFFER)] = {"connect": True, "view_channel": True}
    assert rp.plan_guard(room, source(), EVERYONE, {STAFFER: [MUTED]}, exempt_members=[STAFFER]) == {}


def test_a_personal_deny_for_a_protected_member_is_not_a_violation():
    room = fresh_room()
    room[("member", GUEST)] = {"connect": False, "view_channel": None}
    assert rp.plan_guard(room, source(), EVERYONE, {GUEST: [MUTED]}) == {}


def test_changing_everyone_or_the_member_roles_is_the_owners_right_and_not_guarded():
    room = fresh_room()
    room[("role", EVERYONE)] = {"connect": True, "view_channel": True}      # `.autoroom public`
    room[("role", MEMBER_ROLE)] = {"connect": False, "view_channel": False}  # `.autoroom private`
    assert rp.plan_guard(room, source(), EVERYONE, {}) == {}


def test_the_plan_is_idempotent_once_applied():
    room = fresh_room()
    del room[("role", MUTED)]
    room[("role", QUARANTINE)] = {"connect": True, "view_channel": True}
    room[("member", GUEST)] = {"connect": True, "view_channel": True}
    roles = {GUEST: [MUTED]}
    fixes = rp.plan_guard(room, source(), EVERYONE, roles)
    assert fixes
    for target, changes in fixes.items():
        room.setdefault(target, {}).update(changes)
    assert rp.plan_guard(room, source(), EVERYONE, roles) == {}


def test_plan_does_not_modify_its_inputs():
    room, src = fresh_room(), source()
    del room[("role", MUTED)]
    snapshot = (copy.deepcopy(room), copy.deepcopy(src))
    rp.plan_guard(room, src, EVERYONE, {})
    assert (room, src) == snapshot


# ---------------------------------------------------------------- mod-log wording

def test_describe_fixes_names_roles_and_members_and_caps_the_list():
    fixes = {("role", MUTED): {"connect": False, "view_channel": False}, ("member", GUEST): {"connect": None}}
    lines = rp.describe_fixes(fixes)
    assert lines == [f"<@&{MUTED}>: join and see denied again", f"<@{GUEST}>: personal allow to join removed"]
    many = {("role", n): {"connect": False} for n in range(30, 45)}
    capped = rp.describe_fixes(many, limit=8)
    assert len(capped) == 9 and capped[-1] == "...and 7 more"
