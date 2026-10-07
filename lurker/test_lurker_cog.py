"""Behavior + import-smoke tests for the Lurker cog class.

redbot is faked (and discord.py topped up with any missing names) *inside a
fixture*, so these run the same with or without the real libraries installed
and never touch the shared root conftest.py.

What this proves: the decision logic (flag/unflag ordering, idempotency, race
guards, sweep, circuit breaker, report buffers) and clean imports. It does NOT
prove live Discord behaviour (real role edits, history scans, embeds rendering).
"""
import asyncio
import copy
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

NOW = datetime.now(timezone.utc)
DAY = 86400


# ------------------------------------------------------------------ fakes

class _Call:
    """What Red's Value() returns: awaitable AND an async context manager."""

    def __init__(self, val):
        self.val = val

    def __await__(self):
        async def _get():
            return copy.deepcopy(self.val.current())
        return _get().__await__()

    async def __aenter__(self):
        self.obj = copy.deepcopy(self.val.current())
        return self.obj

    async def __aexit__(self, *exc):
        if exc[0] is None:
            self.val.store(self.obj)


class _Val:
    def __init__(self, bucket, key, default):
        self.bucket, self.key, self.default = bucket, key, default

    def current(self):
        return self.bucket.get(self.key, copy.deepcopy(self.default))

    def store(self, value):
        self.bucket[self.key] = copy.deepcopy(value)

    def __call__(self):
        return _Call(self)

    async def set(self, value):
        self.store(value)


class _Group:
    def __init__(self, bucket, defaults):
        self._bucket, self._defaults = bucket, defaults

    def __getattr__(self, name):
        if name not in self._defaults:
            raise AttributeError(name)
        return _Val(self._bucket, name, self._defaults[name])

    def all(self):
        async def _all():
            return {k: _Val(self._bucket, k, d).current() for k, d in self._defaults.items()}
        return _all()

    def clear(self):
        async def _clear():
            self._bucket.clear()
        return _clear()


class FakeConfig:
    def __init__(self):
        self.data = {}
        self.gd, self.md = {}, {}

    @classmethod
    def get_conf(cls, *a, **k):
        return cls()

    def register_guild(self, **d):
        self.gd = d

    def register_member(self, **d):
        self.md = d

    def guild_from_id(self, gid):
        return _Group(self.data.setdefault(("g", gid), {}), self.gd)

    def guild(self, guild):
        return self.guild_from_id(guild.id)

    def member(self, m):
        return _Group(self.data.setdefault(("m", m.guild.id, m.id), {}), self.md)


class FakeRole:
    def __init__(self, rid, name, position, managed=False):
        self.id, self.name, self.position, self.managed = rid, name, position, managed

    def __eq__(self, o):
        return isinstance(o, FakeRole) and o.id == self.id

    def __hash__(self):
        return hash(self.id)

    def __lt__(self, o):
        return self.position < o.position

    def __le__(self, o):
        return self.position <= o.position

    def __gt__(self, o):
        return self.position > o.position

    def __ge__(self, o):
        return self.position >= o.position

    @property
    def mention(self):
        return f"<@&{self.id}>"


class FakeGuild:
    def __init__(self):
        self.id = 1
        self.default_role = FakeRole(1, "@everyone", 0)
        self.me = types.SimpleNamespace(top_role=FakeRole(900, "Bot", 100))
        self.roles = {}
        self.members = []
        self.channels = {}

    def add_role(self, role):
        self.roles[role.id] = role
        return role

    def get_role(self, rid):
        return self.roles.get(rid)

    def get_channel(self, cid):
        return self.channels.get(cid)

    def get_member(self, uid):
        return next((m for m in self.members if m.id == uid), None)


class FakeMember:
    def __init__(self, guild, uid, roles=(), joined_days_ago=400, bot=False):
        self.guild, self.id, self.bot = guild, uid, bot
        self.display_name = f"user{uid}"
        self.roles = [guild.default_role] + list(roles)
        self.joined_at = NOW - timedelta(days=joined_days_ago)
        self.deny = set()  # role ids whose add_roles must raise Forbidden
        guild.members.append(self)

    def __str__(self):
        return self.display_name

    async def add_roles(self, *roles, reason=None):
        await asyncio.sleep(0)
        if any(r.id in self.deny for r in roles):
            raise self._forbidden()
        for r in roles:
            if r not in self.roles:
                self.roles.append(r)

    async def remove_roles(self, *roles, reason=None):
        await asyncio.sleep(0)
        self.roles = [r for r in self.roles if r not in roles]

    def _forbidden(self):
        import discord

        class _Denied(discord.Forbidden):
            def __init__(self):
                Exception.__init__(self, "denied")
        return _Denied()

    def role_ids(self):
        return {r.id for r in self.roles if r != self.guild.default_role}


class FakeBot:
    def __init__(self, guild):
        self.guilds = [guild]
        self.loop = types.SimpleNamespace(create_task=self._create_task)

    @staticmethod
    def _create_task(coro):
        coro.close()  # never run the background loops in unit tests
        return types.SimpleNamespace(cancel=lambda: None)


# --------------------------------------------------------------- fixture

@pytest.fixture
def mod(monkeypatch):
    import discord

    # top up whatever the installed/stubbed discord is missing (names used at def time)
    for name in ("Message", "Member", "Guild", "Role", "Interaction", "TextChannel",
                 "RawReactionActionEvent", "Reaction", "Thread", "ForumChannel", "User"):
        if not hasattr(discord, name):
            monkeypatch.setattr(discord, name, type(name, (), {}), raising=False)
    for name in ("Forbidden", "HTTPException", "NotFound"):
        if not hasattr(discord, name):
            monkeypatch.setattr(discord, name, type(name, (Exception,), {}), raising=False)
    if not hasattr(discord, "abc"):
        monkeypatch.setattr(discord, "abc", types.SimpleNamespace(Messageable=object), raising=False)

    class _Cmd:
        def __init__(self, f, **kw):
            self.callback, self.kw = f, kw

        def command(self, *a, **kw):
            return lambda f: _Cmd(f, **kw)

    def _deco(*a, **kw):
        return lambda f: _Cmd(f, **kw)

    class _Cog:
        @staticmethod
        def listener(*a, **kw):
            return lambda f: f

    commands_ns = types.SimpleNamespace(Cog=_Cog, group=_deco, command=_deco)
    checks_ns = types.SimpleNamespace(
        admin_or_permissions=lambda **kw: (lambda f: f),
        mod_or_permissions=lambda **kw: (lambda f: f),
    )
    core = types.ModuleType("redbot.core")
    core.Config, core.checks, core.commands = FakeConfig, checks_ns, commands_ns
    utils = types.ModuleType("redbot.core.utils")
    fmt = types.ModuleType("redbot.core.utils.chat_formatting")
    fmt.humanize_list = lambda items: ", ".join(items)
    root = types.ModuleType("redbot")
    for name, m in (("redbot", root), ("redbot.core", core),
                    ("redbot.core.utils", utils), ("redbot.core.utils.chat_formatting", fmt)):
        monkeypatch.setitem(sys.modules, name, m)
    monkeypatch.delitem(sys.modules, "lurker.lurker", raising=False)

    import importlib
    module = importlib.import_module("lurker.lurker")
    yield module
    sys.modules.pop("lurker.lurker", None)


@pytest.fixture
def env(mod):
    guild = FakeGuild()
    lurker_role = guild.add_role(FakeRole(500, "lurker", 10))
    staff = guild.add_role(FakeRole(501, "Mod", 50))
    a = guild.add_role(FakeRole(601, "RoleA", 20))
    b = guild.add_role(FakeRole(602, "RoleB", 21))
    booster = guild.add_role(FakeRole(603, "Booster", 22, managed=True))
    cog = mod.Lurker(FakeBot(guild))
    return types.SimpleNamespace(
        mod=mod, cog=cog, guild=guild, lurker=lurker_role, staff=staff, a=a, b=b, booster=booster,
        cutoff=NOW.timestamp() - 30 * DAY,
    )


def _stale(env, member):
    """Pretend the member was last seen long ago."""
    env.cog._cache.setdefault(env.guild.id, {})[member.id] = NOW.timestamp() - 90 * DAY
    env.cog._loaded_guilds.add(env.guild.id)


# ----------------------------------------------------------------- tests

def test_module_imports_and_defines_cog(mod):
    assert hasattr(mod, "Lurker")
    assert mod.VERSION == "2.3.0"
    for name in ("lurker_version", "lurker_backfill", "lurker_backfill_confirm",
                 "lurker_sweep_preview", "lurker_sweep_run", "lurker_report",
                 "lurker_report_send", "lurker_exempt_audit"):
        assert hasattr(mod.Lurker, name), name


async def test_flag_skips_member_who_is_recently_active(env):
    """REGRESSION (flip-flop): a stale backfill/sweep list must never re-flag someone
    whose live activity is recent."""
    m = FakeMember(env.guild, 10, roles=[env.a])
    env.cog._loaded_guilds.add(env.guild.id)
    env.cog._touch(env.guild.id, m.id)  # they just posted

    result = await env.cog._flag_member(m, env.lurker, set(), cutoff=env.cutoff, source="backfill")

    assert result == "active"
    assert m.role_ids() == {601}  # untouched
    assert await env.cog.config.member(m).flagged() is False


async def test_flag_unflag_roundtrip_then_stale_list_cannot_reflag(env):
    """REGRESSION (flip-flop): flag -> member posts in #lurkers (unflag) -> the
    still-running loop reaches them with its old list -> must be skipped."""
    m = FakeMember(env.guild, 11, roles=[env.a, env.b, env.booster])
    _stale(env, m)

    assert await env.cog._flag_member(m, env.lurker, set(), cutoff=env.cutoff, source="backfill") == "flagged"
    assert m.role_ids() == {500, 603}  # lurker + managed role kept
    assert await env.cog.config.member(m).stored_roles() == [601, 602]

    restored = await env.cog._unflag_member(m, env.lurker, source="post")
    assert restored == 2
    assert m.role_ids() == {601, 602, 603}
    assert await env.cog.config.member(m).flagged() is False

    again = await env.cog._flag_member(m, env.lurker, set(), cutoff=env.cutoff, source="backfill")
    assert again == "active"
    assert 500 not in m.role_ids()


async def test_flag_failure_does_not_strand_member_without_roles(env):
    """Adding Lurker fails -> member must keep every role (old order stripped first)."""
    m = FakeMember(env.guild, 12, roles=[env.a, env.b])
    _stale(env, m)
    m.deny = {500}

    with pytest.raises(env.mod.discord.Forbidden):
        await env.cog._flag_member(m, env.lurker, set(), cutoff=env.cutoff)

    assert m.role_ids() == {601, 602}
    assert await env.cog.config.member(m).flagged() is False

    m.deny = set()  # fixed (e.g. bot role moved) -> retry works and stored roles intact
    assert await env.cog._flag_member(m, env.lurker, set(), cutoff=env.cutoff) == "flagged"
    assert await env.cog.config.member(m).stored_roles() == [601, 602]


async def test_reflagging_flagged_member_never_overwrites_stored_roles(env):
    m = FakeMember(env.guild, 13, roles=[env.a, env.b])
    _stale(env, m)
    await env.cog._flag_member(m, env.lurker, set(), cutoff=env.cutoff)
    before = await env.cog.config.member(m).stored_roles()

    assert await env.cog._flag_member(m, env.lurker, set(), cutoff=env.cutoff) == "already"
    assert await env.cog.config.member(m).stored_roles() == before == [601, 602]


async def test_exempt_role_checked_against_current_roles(env):
    m = FakeMember(env.guild, 14, roles=[env.staff, env.a])
    _stale(env, m)
    assert await env.cog._flag_member(m, env.lurker, {501}, cutoff=env.cutoff) == "exempt"
    assert m.role_ids() == {501, 601}


async def test_concurrent_unflag_records_a_single_restore_event(env):
    m = FakeMember(env.guild, 15, roles=[env.a])
    _stale(env, m)
    await env.cog._flag_member(m, env.lurker, set(), cutoff=env.cutoff, source="manual")
    env.cog._events.clear()

    await asyncio.gather(
        env.cog._unflag_member(m, env.lurker, source="post"),
        env.cog._unflag_member(m, env.lurker, source="post"),
    )
    events = env.cog._events[env.guild.id]
    assert [e["kind"] for e in events] == ["unflag"]
    assert m.role_ids() == {601}


async def test_unflag_tolerates_deleted_role_and_unrestorable_role(env):
    m = FakeMember(env.guild, 16, roles=[env.a])
    _stale(env, m)
    await env.cog._flag_member(m, env.lurker, set(), cutoff=env.cutoff)
    # one stored role gets deleted, one ends up above the bot
    await env.cog.config.member(m).stored_roles.set([601, 9999, 888])
    env.guild.add_role(FakeRole(888, "TooHigh", 500))

    restored = await env.cog._unflag_member(m, env.lurker, source="mod")
    assert restored == 1
    assert m.role_ids() == {601}


async def test_report_events_flush_and_bulk_counts(env):
    m = FakeMember(env.guild, 17, roles=[env.a])
    env.cog._record_event(env.guild.id, "flag", m, "sweep")
    env.cog._record_event(env.guild.id, "flag", m, "backfill")
    env.cog._record_event(env.guild.id, "flag", m, "backfill")

    await env.cog._flush_events(env.guild.id)

    cfg = env.cog.config.guild(env.guild)
    events = await cfg.report_events()
    assert [e["src"] for e in events] == ["sweep"]  # bulk never listed individually
    assert (await cfg.report_counts())["backfill"] == 2
    assert env.cog._events[env.guild.id] == []


async def test_unload_flushes_activity_so_reload_loses_nothing(env):
    env.cog._loaded_guilds.add(env.guild.id)
    env.cog._touch(env.guild.id, 42)
    await env.cog.cog_unload()
    stored = await env.cog.config.guild(env.guild).last_active()
    assert "42" in stored


async def test_partial_cache_is_never_flushed_over_stored_data(env):
    """A touch before the guild's cache is loaded must not wipe stored activity."""
    await env.cog.config.guild(env.guild).last_active.set({"7": 123.0})
    env.cog._touch(env.guild.id, 42)  # NOT loaded yet
    await env.cog._flush_all()
    assert await env.cog.config.guild(env.guild).last_active() == {"7": 123.0}

    await env.cog._ensure_loaded(env.guild.id)  # now merges instead of replacing
    assert 7 in env.cog._cache[env.guild.id] and 42 in env.cog._cache[env.guild.id]


async def _prep_sweep(env, monkeypatch, n_stale):
    real_sleep = asyncio.sleep

    async def fast(_s):
        await real_sleep(0)
    monkeypatch.setattr(asyncio, "sleep", fast)

    cfg = env.cog.config.guild(env.guild)
    await cfg.enabled.set(True)
    await cfg.lurker_role_id.set(500)
    await cfg.exempt_role_ids.set([501])
    stale = [FakeMember(env.guild, 100 + i, roles=[env.a]) for i in range(n_stale)]
    active = FakeMember(env.guild, 200, roles=[env.a])
    mod_user = FakeMember(env.guild, 201, roles=[env.staff])
    newbie = FakeMember(env.guild, 202, roles=[env.a], joined_days_ago=3)
    bot = FakeMember(env.guild, 203, bot=True)
    env.cog._loaded_guilds.add(env.guild.id)
    for m in stale:
        env.cog._cache.setdefault(env.guild.id, {})[m.id] = NOW.timestamp() - 90 * DAY
    env.cog._touch(env.guild.id, active.id)
    return stale, active, mod_user, newbie, bot


async def test_sweep_flags_only_truly_inactive_members(env, monkeypatch):
    stale, active, mod_user, newbie, bot = await _prep_sweep(env, monkeypatch, n_stale=3)

    stats = await env.cog._sweep_guild(env.guild, NOW.timestamp())

    assert stats["flagged"] == 3 and stats["aborted"] is False
    assert all(500 in m.role_ids() for m in stale)
    for safe in (active, mod_user, newbie, bot):
        assert 500 not in safe.role_ids()
    sweeps = [e for e in env.cog._events[env.guild.id] if e["src"] == "sweep"]
    assert len(sweeps) == 3
    cfg = env.cog.config.guild(env.guild)
    assert (await cfg.last_sweep())["flagged"] == 3
    assert await cfg.last_sweep_ts() == pytest.approx(NOW.timestamp())


async def test_sweep_is_gated_to_once_per_24h(env, monkeypatch):
    await _prep_sweep(env, monkeypatch, n_stale=1)
    cfg = env.cog.config.guild(env.guild)
    await cfg.last_sweep_ts.set(NOW.timestamp() - 3600)
    assert await env.cog._sweep_guild(env.guild, NOW.timestamp()) is None


async def test_sweep_circuit_breaker_aborts_mass_flag(env, monkeypatch):
    stale, *_ = await _prep_sweep(env, monkeypatch, n_stale=5)
    cfg = env.cog.config.guild(env.guild)
    await cfg.sweep_max.set(3)

    stats = await env.cog._sweep_guild(env.guild, NOW.timestamp())

    assert stats["aborted"] is True and stats["candidates"] == 5
    assert all(500 not in m.role_ids() for m in stale)  # nobody touched
    assert (await cfg.last_sweep())["aborted"] is True


async def test_digest_quiet_week_and_busy_week(env):
    chan = types.SimpleNamespace(id=700, mention="#lurkers")
    env.guild.channels[700] = chan
    await env.cog.config.guild(env.guild).lurker_role_id.set(500)
    await env.cog.config.guild(env.guild).lurker_channel_id.set(700)
    now = NOW.timestamp()

    quiet, csv_none = await env.cog._build_digest(env.guild, [], {}, now - 7 * DAY, now)
    assert "No lurker activity" in quiet.description
    assert csv_none is None

    events = [
        {"uid": i, "name": f"u{i}", "ts": now, "kind": "flag", "src": "sweep"} for i in range(20)
    ] + [{"uid": 99, "name": "u99", "ts": now, "kind": "unflag", "src": "post"}]
    busy, csv_text = await env.cog._build_digest(env.guild, events, {"backfill": 5}, now - 7 * DAY, now)
    names = [f.name for f in busy.fields]
    assert any(n.startswith("Flagged by daily sweep (20)") for n in names)
    assert "Restored" in names and "Backfill" in names
    assert csv_text and csv_text.count("\n") == 22  # header + 21 events + trailing newline


# ------------------------------------------------- write-reduction (v2.1.0)

async def test_touch_skips_a_fresh_timestamp_and_does_not_mark_dirty(env):
    """REGRESSION: every message used to mark the guild dirty, so the whole
    1.5 MB Config file was rewritten on each flush. A fresh stamp needs no write."""
    gid, uid = env.guild.id, 42
    env.cog._loaded_guilds.add(gid)
    env.cog._cache[gid] = {uid: NOW.timestamp() - 60}  # active a minute ago
    env.cog._dirty.clear()

    env.cog._touch(gid, uid)

    assert gid not in env.cog._dirty
    assert env.cog._cache[gid][uid] == pytest.approx(NOW.timestamp() - 60)  # unchanged


async def test_touch_refreshes_a_stale_timestamp_and_marks_dirty(env):
    gid, uid = env.guild.id, 42
    env.cog._loaded_guilds.add(gid)
    env.cog._cache[gid] = {uid: NOW.timestamp() - 2 * DAY}
    env.cog._dirty.clear()

    env.cog._touch(gid, uid)

    assert gid in env.cog._dirty
    assert env.cog._cache[gid][uid] > NOW.timestamp() - 60


async def test_first_ever_touch_is_recorded(env):
    env.cog._touch(env.guild.id, 7)
    assert 7 in env.cog._cache[env.guild.id]
    assert env.guild.id in env.cog._dirty


def test_flush_interval_is_hourly(mod):
    assert mod.Lurker.FLUSH_INTERVAL == 3600


def _spy_clear(env):
    calls = []
    orig = env.cog.config.member

    def member_spy(m):
        group = orig(m)
        real_clear = group.clear

        def clear():
            calls.append(m.id)
            return real_clear()
        group.clear = clear
        return group
    env.cog.config.member = member_spy
    return calls


async def test_member_leave_without_a_record_does_not_touch_config(env):
    """REGRESSION: on_member_remove used to clear() for every departure, rewriting
    the whole Config file even when the member had nothing stored."""
    m = FakeMember(env.guild, 10, roles=[env.a])
    calls = _spy_clear(env)

    await env.cog.on_member_remove(m)

    assert calls == []


async def test_member_leave_with_a_record_clears_it(env):
    """A flagged member who leaves must not carry stale state back on rejoin."""
    m = FakeMember(env.guild, 10, roles=[env.a])
    await env.cog.config.member(m).stored_roles.set([601, 602])
    await env.cog.config.member(m).flagged.set(True)
    calls = _spy_clear(env)

    await env.cog.on_member_remove(m)

    assert calls == [10]
    assert await env.cog.config.member(m).flagged() is False
    assert await env.cog.config.member(m).stored_roles() == []


async def test_unflag_clears_the_record_in_one_write(env):
    """Restoring a member must leave no default-valued entry behind, and must do it
    with a single clear() rather than two separate sets."""
    m = FakeMember(env.guild, 10, roles=[env.lurker])
    await env.cog.config.member(m).stored_roles.set([601, 602])
    await env.cog.config.member(m).flagged.set(True)
    calls = _spy_clear(env)

    restored = await env.cog._unflag_member(m, env.lurker, source="mod")

    assert restored == 2
    assert m.role_ids() == {601, 602}                    # roles back, Lurker gone
    assert calls == [10]
    assert env.cog.config.data[("m", env.guild.id, 10)] == {}  # no leftover entry


# ---------------------------------------------------------------- year club (v2.2)

def _years_ago(years, extra_days=0):
    """A join timestamp `years` calendar years ago (+extra_days earlier), safely away from today's boundary."""
    from lurker import engine
    now_local = datetime.now(engine.LOCAL_TZ)
    try:
        j = now_local.replace(year=now_local.year - years)
    except ValueError:  # Feb 29
        j = now_local.replace(year=now_local.year - years, day=28)
    return j - timedelta(days=extra_days)


def _club_env(env):
    club = {n: env.guild.add_role(FakeRole(700 + n, f"{n} year club!", 30 + n)) for n in range(1, 13)}
    env.cog.config.guild(env.guild).yearclub_roles.store({str(n): r.id for n, r in club.items()})
    env.cog.config.guild(env.guild).yearclub_enabled.store(True)
    env.cog.config.guild(env.guild).lurker_role_id.store(env.lurker.id)
    return club


def _member_joined(env, uid, joined_dt, roles=()):
    m = FakeMember(env.guild, uid, roles=roles)
    m.joined_at = joined_dt
    return m


def test_engine_years_completed_uses_local_calendar_date():
    from lurker import engine
    tz = engine.LOCAL_TZ
    joined = datetime(2020, 10, 3, 12, 0, tzinfo=tz).timestamp()
    assert engine.years_completed(joined, datetime(2026, 10, 2, 23, 59, tzinfo=tz).timestamp()) == 5
    assert engine.years_completed(joined, datetime(2026, 10, 3, 0, 1, tzinfo=tz).timestamp()) == 6
    leap = datetime(2024, 2, 29, 9, 0, tzinfo=tz).timestamp()
    assert engine.years_completed(leap, datetime(2025, 2, 28, 23, 0, tzinfo=tz).timestamp()) == 0
    assert engine.years_completed(leap, datetime(2025, 3, 1, 0, 30, tzinfo=tz).timestamp()) == 1
    assert engine.years_completed(leap, datetime(2020, 1, 1, tzinfo=tz).timestamp()) == 0  # never negative


def test_engine_club_names_targets_and_plans():
    from lurker import engine
    assert engine.parse_club_role_name("7 year club!") == 7
    assert engine.parse_club_role_name("12 Year Club") == 12
    assert engine.parse_club_role_name("1 yr club") == 1
    assert engine.parse_club_role_name("year club fan") is None
    assert engine.parse_club_role_name("Movie Night") is None
    mapping = {1: 11, 2: 12, 3: 13, 12: 112}
    assert engine.club_target(0, mapping) is None
    assert engine.club_target(2, mapping) == 12
    assert engine.club_target(7, mapping) == 13  # gap: keeps the highest configured <= years
    assert engine.club_target(14, mapping) == 112
    clubs = set(mapping.values())
    assert engine.club_plan({11, 12, 13, 99}, 13, clubs) == (None, {11, 12})
    assert engine.club_plan({99}, 12, clubs) == (12, set())
    assert engine.club_plan({11, 99}, None, clubs) == (None, {11})
    assert engine.club_mapping({"3": "13", "x": 1, "4": None}) == {3: 13}


async def test_yearclub_sync_leaves_exactly_one_current_role(env):
    club = _club_env(env)
    stacked = _member_joined(env, 20, _years_ago(3, 10), roles=[club[1], club[2], club[3], env.a])
    due = _member_joined(env, 21, _years_ago(2, 1), roles=[club[1]])  # anniversary passed: 1 -> 2
    fresh = _member_joined(env, 22, _years_ago(0, 30), roles=[club[1]])  # under a year: stale role goes
    veteran = _member_joined(env, 23, _years_ago(14, 5))  # past the top role: keeps 12
    lurker = _member_joined(env, 24, _years_ago(5, 5), roles=[env.lurker, club[1]])
    correct = _member_joined(env, 25, _years_ago(4, 5), roles=[club[4]])

    stats = await env.cog._yearclub_pass(env.guild, auto=False)

    assert stacked.role_ids() == {club[3].id, env.a.id}
    assert due.role_ids() == {club[2].id}
    assert fresh.role_ids() == set()
    assert veteran.role_ids() == {club[12].id}
    assert lurker.role_ids() == {env.lurker.id, club[1].id}  # lurkers are never touched
    assert correct.role_ids() == {club[4].id}
    assert stats["changed"] == 4 and stats["skipped_lurkers"] == 1 and stats["errors"] == 0


async def test_yearclub_auto_pass_aborts_on_mass_change(env, monkeypatch):
    club = _club_env(env)
    monkeypatch.setattr(env.mod.engine, "YEARCLUB_AUTO_MAX", 2)
    alerts = []

    async def fake_alert(guild, text):
        alerts.append(text)

    env.cog._send_alert = fake_alert
    members = [_member_joined(env, 30 + i, _years_ago(3, 3), roles=[club[1]]) for i in range(3)]

    stats = await env.cog._yearclub_pass(env.guild, auto=True)

    assert stats["aborted"] is True and alerts
    assert all(m.role_ids() == {club[1].id} for m in members)  # nothing changed


async def test_flag_never_stores_club_roles_and_unflag_restores_current_year(env):
    """REGRESSION: lurker used to store the year role at flag time and restore that
    stale role later, e.g. a member flagged in year 2 came back holding '2 year club!'
    in year 3 (or stacked with the right one)."""
    club = _club_env(env)
    m = _member_joined(env, 40, _years_ago(3, 2), roles=[env.a, club[2]])
    _stale(env, m)

    assert await env.cog._flag_member(m, env.lurker, set(), cutoff=env.cutoff, source="sweep") == "flagged"
    assert await env.cog.config.member(m).stored_roles() == [env.a.id]

    await env.cog._unflag_member(m, env.lurker, source="post")
    assert m.role_ids() == {env.a.id, club[3].id}


async def test_unflag_filters_stale_club_role_stored_by_older_version(env):
    club = _club_env(env)
    m = _member_joined(env, 41, _years_ago(5, 2), roles=[env.lurker])
    await env.cog.config.member(m).stored_roles.set([env.b.id, club[1].id])
    await env.cog.config.member(m).flagged.set(True)

    await env.cog._unflag_member(m, env.lurker, source="post")
    assert m.role_ids() == {env.b.id, club[5].id}


async def test_club_roles_are_stored_normally_when_yearclub_is_off(env):
    club = _club_env(env)
    env.cog.config.guild(env.guild).yearclub_enabled.store(False)
    m = _member_joined(env, 42, _years_ago(3, 2), roles=[env.a, club[3]])
    _stale(env, m)
    await env.cog._flag_member(m, env.lurker, set(), cutoff=env.cutoff, source="sweep")
    assert await env.cog.config.member(m).stored_roles() == [env.a.id, club[3].id]


# ------------------------------------------------------------ dashboard page

def _dash_user(env, uid, *, manage_guild=False, manage_roles=False, is_mod=False, owner=False):
    m = FakeMember(env.guild, uid)
    m.guild_permissions = types.SimpleNamespace(manage_guild=manage_guild, manage_roles=manage_roles)

    async def is_owner(u):
        return owner

    async def is_mod_(member):
        return is_mod

    env.cog.bot.is_owner, env.cog.bot.is_mod = is_owner, is_mod_
    return m


def test_dashboard_page_registration_params(mod):
    """REGRESSION: name=None registers as 'None' on the dashboard; needs an explicit name."""
    args, kwargs = mod.Lurker.dashboard_lurker.__dashboard_decorator_params__
    assert kwargs["name"] == "overview"
    assert tuple(kwargs["methods"]) == ("GET",)
    assert mod.Lurker.on_dashboard_cog_add  # mixin present
    assert mod.Lurker.__mro__[1].__name__ == "DashboardIntegration"


async def test_dashboard_denies_non_mod_and_shows_no_data(env):
    env.guild.name = "Wonderland"
    user = _dash_user(env, 70)
    out = await env.cog.dashboard_lurker(user, env.guild)
    wc = out["web_content"]
    assert wc["denied"] is True and wc["rows"] == []


async def test_dashboard_overview_for_mod_has_only_settings_and_counts(env):
    env.guild.name = "Wonderland"
    env.lurker.members = [object(), object()]
    env.guild.channels[7] = types.SimpleNamespace(id=7, name="lurkers")
    cfg = env.cog.config.guild(env.guild)
    await cfg.lurker_role_id.set(500)
    await cfg.lurker_channel_id.set(7)
    await cfg.exempt_role_ids.set([501])
    await cfg.last_active.set({"1": 1.0, "2": 2.0, "3": 3.0})
    user = _dash_user(env, 71, is_mod=True)

    out = await env.cog.dashboard_lurker(user, env.guild)
    rows = dict(out["web_content"]["rows"])
    assert out["web_content"]["denied"] is False
    assert rows["Lurker role"] == "lurker"
    assert rows["Reactivation channel"] == "lurkers"
    assert rows["Members currently flagged"] == "2"
    assert rows["Members with tracked activity"] == "3"
    assert rows["Exempt roles"] == "Mod"
    assert rows["Last sweep"] == "never"
    # page view must not write Config or load the cache
    assert env.guild.id not in env.cog._loaded_guilds


async def test_dashboard_allows_manage_roles_and_owner(env):
    env.guild.name = "W"
    for uid, kw in ((72, {"manage_roles": True}), (73, {"manage_guild": True}), (74, {"owner": True})):
        user = _dash_user(env, uid, **kw)
        out = await env.cog.dashboard_lurker(user, env.guild)
        assert out["web_content"]["denied"] is False, kw


def test_dashboard_template_escapes_values():
    jinja2 = pytest.importorskip("jinja2")
    from lurker.dashboard_view import PAGE_TEMPLATE

    html = jinja2.Template(PAGE_TEMPLATE).render(
        denied=False, guild_name="<b>x</b>", rows=[("Lurker role", "<script>alert(1)</script>")]
    )
    assert "<script>" not in html and "&lt;script&gt;" in html and "<b>x</b>" not in html
