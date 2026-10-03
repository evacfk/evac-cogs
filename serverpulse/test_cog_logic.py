"""Behaviour tests for the cog class against fakes (no Discord, no Red).

Proves decision logic: what counts as a message, flush/restart durability, the
mod gate, digest scheduling, and the backfill pipeline end to end. It does NOT
prove live Discord behaviour (real history pagination, embeds rendering, buttons).
"""
import asyncio
import copy
from datetime import datetime, timedelta
from types import SimpleNamespace

import discord
import pytest

from serverpulse import models, storage
from serverpulse import serverpulse as sp
from serverpulse._testdata import local_ts
from serverpulse.constants import DEFAULT_GUILD, DEFAULT_IGNORED_CHANNEL_IDS, DEFAULT_MOD_CHANNEL_ID, DEFAULT_MOD_ROLE_ID, TIMEZONE

NOW = local_ts(2026, 9, 29, 19, 59)  # live_since becomes 20:00
GUILD_ID = 42


# ------------------------------------------------------------------ fakes of Red's Config

class _Call:
    def __init__(self, value):
        self.value = value

    def __await__(self):
        async def _get():
            return copy.deepcopy(self.value.current())
        return _get().__await__()

    async def __aenter__(self):
        self.obj = copy.deepcopy(self.value.current())
        return self.obj

    async def __aexit__(self, *exc):
        if exc[0] is None:
            self.value.store(self.obj)


class _Value:
    def __init__(self, bucket, key):
        self.bucket, self.key = bucket, key

    def current(self):
        return self.bucket[self.key]

    def store(self, v):
        self.bucket[self.key] = v

    def __call__(self):
        return _Call(self)

    async def set(self, v):
        self.bucket[self.key] = copy.deepcopy(v)


class _Group:
    def __init__(self, bucket):
        self._bucket = bucket

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if name not in self._bucket:
            raise AttributeError(name)  # force_registration: unregistered keys blow up
        return _Value(self._bucket, name)

    async def all(self):
        return copy.deepcopy(self._bucket)


class FakeConfig:
    def __init__(self):
        self._guilds = {}
        self._global = {"allowed_guild_ids": None}

    def __getattr__(self, name):  # global keys, like Red's Config
        if name.startswith("_") or name not in self._global:
            raise AttributeError(name)
        return _Value(self._global, name)

    def guild(self, guild):
        return _Group(self._guilds.setdefault(guild.id, copy.deepcopy(DEFAULT_GUILD)))


# ------------------------------------------------------------------ fixtures

class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = Clock(NOW)
    monkeypatch.setattr(sp.time, "time", c)
    return c


def make_bot():
    async def prefixes(guild):
        return ["."]

    async def false(*a, **k):
        return False

    return SimpleNamespace(get_valid_prefixes=prefixes, is_mod=false, is_owner=false, guilds=[])


def make_cog(tmp_path, bot=None):
    cog = object.__new__(sp.ServerPulse)
    cog.bot = bot or make_bot()
    cog.config = FakeConfig()
    cog.data_dir = tmp_path
    cog._trackers, cog._cache, cog._flushed = {}, {}, {}
    cog._locks, cog._init_locks, cog._backfill_tasks, cog._cancel = {}, {}, {}, set()
    cog._allowed = None
    return cog


def make_guild(**kw):
    return SimpleNamespace(id=GUILD_ID, name="Wonderland", text_channels=[], voice_channels=[], stage_channels=[], forums=[], **kw)


GUILD = make_guild()


def msg(content="hello", uid=5, channel_id=100, parent_id=None, ts=None, bot=False, mtype="default", webhook=None):
    return SimpleNamespace(
        guild=GUILD, author=SimpleNamespace(bot=bot, id=uid), webhook_id=webhook,
        created_at=datetime.fromtimestamp(ts if ts is not None else NOW + 600, TIMEZONE), content=content,
        type=SimpleNamespace(name=mtype), channel=SimpleNamespace(id=channel_id, parent_id=parent_id),
    )


async def counted(cog, **kw) -> int:
    before = sum(a.msgs for a in cog._trackers[GUILD_ID].accs.values())
    await cog.on_message(msg(**kw))
    return sum(a.msgs for a in cog._trackers[GUILD_ID].accs.values()) - before


# ------------------------------------------------------------------ init

async def test_first_load_sets_live_since_to_the_next_full_hour(tmp_path, clock):
    cog = make_cog(tmp_path)
    st = await cog._ensure_guild(GUILD)
    assert st["live_since"] == int(local_ts(2026, 9, 29, 20)) and st["coverage_start"] == st["live_since"]
    assert st["ignored"] == set(DEFAULT_IGNORED_CHANNEL_IDS)
    assert st["mod_channel"] == DEFAULT_MOD_CHANNEL_ID
    assert GUILD_ID in cog._trackers


async def test_first_load_marks_digests_as_already_sent_so_no_stale_backlog_fires(tmp_path, clock):
    cog = make_cog(tmp_path)
    await cog._ensure_guild(GUILD)
    gconf = cog.config.guild(GUILD)
    assert (await gconf.digest_weekly())["last"] == "2026-09-21"  # Tue 9/29: this week's Monday digest already 'went out'
    assert (await gconf.digest_monthly())["last"] == "2026-08"


async def test_second_load_does_not_move_live_since(tmp_path, clock):
    cog = make_cog(tmp_path)
    await cog._ensure_guild(GUILD)
    clock.t = NOW + 7 * 86400
    cog2 = make_cog(tmp_path)
    cog2.config = cog.config
    st2 = await cog2._ensure_guild(GUILD)
    assert st2["live_since"] == int(local_ts(2026, 9, 29, 20))


# ------------------------------------------------------------------ what counts

async def test_ordinary_message_counts_and_is_attributed_to_its_channel(tmp_path, clock):
    cog = make_cog(tmp_path)
    await cog._ensure_guild(GUILD)
    assert await counted(cog, channel_id=100) == 1
    acc = next(iter(cog._trackers[GUILD_ID].accs.values()))
    assert acc.channels["100"].msgs == 1


@pytest.mark.parametrize(
    "kw",
    [
        dict(bot=True),
        dict(webhook=777),
        dict(mtype="new_member"),
        dict(channel_id=DEFAULT_IGNORED_CHANNEL_IDS[0]),
        dict(channel_id=9999, parent_id=DEFAULT_IGNORED_CHANNEL_IDS[1]),  # thread under an ignored channel
        dict(content=".gamble 100"),
        dict(ts=NOW - 60),  # before live tracking began (backfill's territory)
    ],
)
async def test_uncounted_messages(tmp_path, clock, kw):
    cog = make_cog(tmp_path)
    await cog._ensure_guild(GUILD)
    assert await counted(cog, **kw) == 0


async def test_thread_messages_count_toward_the_parent_channel(tmp_path, clock):
    cog = make_cog(tmp_path)
    await cog._ensure_guild(GUILD)
    assert await counted(cog, channel_id=9999, parent_id=555) == 1
    acc = next(iter(cog._trackers[GUILD_ID].accs.values()))
    assert list(acc.channels) == ["555"]


async def test_ellipsis_chat_counts_even_though_it_starts_with_a_dot(tmp_path, clock):
    cog = make_cog(tmp_path)
    await cog._ensure_guild(GUILD)
    assert await counted(cog, content="...ok then") == 1


async def test_commands_count_when_the_toggle_is_off(tmp_path, clock):
    cog = make_cog(tmp_path)
    await cog._ensure_guild(GUILD)
    await cog.config.guild(GUILD).exclude_commands.set(False)
    await cog._refresh_settings(GUILD)
    assert await counted(cog, content=".gamble 100") == 1


async def test_ignore_list_changes_apply_immediately(tmp_path, clock):
    cog = make_cog(tmp_path)
    await cog._ensure_guild(GUILD)
    assert await counted(cog, channel_id=100) == 1
    async with cog.config.guild(GUILD).ignored_channels() as ignored:
        ignored.append(100)
    await cog._refresh_settings(GUILD)
    assert await counted(cog, channel_id=100) == 0


async def test_dm_messages_are_ignored(tmp_path, clock):
    cog = make_cog(tmp_path)
    m = msg()
    m.guild = None
    await cog.on_message(m)  # must not raise
    assert not cog._trackers


# ------------------------------------------------------------------ flush & restart

async def test_flush_writes_day_files_and_survives_a_restart_mid_hour(tmp_path, clock):
    cog = make_cog(tmp_path)
    await cog._ensure_guild(GUILD)
    for i, uid in enumerate([1, 2, 3]):
        await cog.on_message(msg(uid=uid, ts=NOW + 600 + i))
    clock.t = NOW + 700
    await cog._flush_guild(GUILD_ID)
    day = storage.load_day(tmp_path, GUILD_ID, "2026-09-29")
    assert day["h"]["20"]["m"] == 3 and day["h"]["20"]["u"] == 3 and day["u"] == {"1": 1, "2": 1, "3": 1}

    # reload the cog (same data dir, same config) -- the hour must continue, not restart
    cog2 = make_cog(tmp_path)
    cog2.config = cog.config
    await cog2._ensure_guild(GUILD)
    await cog2.on_message(msg(uid=2, ts=NOW + 800))  # returning chatter
    await cog2.on_message(msg(uid=9, ts=NOW + 810))  # new chatter
    clock.t = NOW + 900
    await cog2._flush_guild(GUILD_ID)
    day = storage.load_day(tmp_path, GUILD_ID, "2026-09-29")
    assert day["h"]["20"]["m"] == 5 and day["h"]["20"]["u"] == 4
    assert day["u"]["2"] == 2


async def test_unflushed_messages_are_saved_on_unload(tmp_path, clock):
    cog = make_cog(tmp_path)
    await cog._ensure_guild(GUILD)
    await cog.on_message(msg(uid=1, ts=NOW + 600))
    clock.t = NOW + 700
    cog._flush_all_sync()
    assert storage.load_day(tmp_path, GUILD_ID, "2026-09-29")["h"]["20"]["m"] == 1


async def test_flush_with_nothing_new_writes_nothing(tmp_path, clock):
    cog = make_cog(tmp_path)
    await cog._ensure_guild(GUILD)
    await cog._flush_guild(GUILD_ID)
    assert storage.list_dates(tmp_path, GUILD_ID) == []


async def test_an_idle_flush_does_not_rewrite_unchanged_data(tmp_path, clock):
    cog = make_cog(tmp_path)
    await cog._ensure_guild(GUILD)
    writes = []
    real = cog._write_snap
    cog._write_snap = lambda gid, snap: (writes.append(1), real(gid, snap))[1]
    await cog.on_message(msg(uid=1, ts=NOW + 600))
    clock.t = NOW + 700
    await cog._flush_guild(GUILD_ID)
    await cog._flush_guild(GUILD_ID)
    await cog._flush_guild(GUILD_ID)
    assert len(writes) == 1


async def test_a_closed_hour_is_released_even_if_nobody_talks_afterwards(tmp_path, clock):
    cog = make_cog(tmp_path)
    await cog._ensure_guild(GUILD)
    await cog.on_message(msg(uid=1, ts=NOW + 600))  # 8:10pm
    clock.t = NOW + 700
    await cog._flush_guild(GUILD_ID)
    assert len(cog._trackers[GUILD_ID].accs) == 1  # still open
    clock.t = NOW + 2 * 3600  # 9:59pm: the 8pm hour is long over, and the server went quiet
    await cog._flush_guild(GUILD_ID)
    assert not cog._trackers[GUILD_ID].accs


async def test_a_closed_hour_is_finalised_and_the_next_hour_continues(tmp_path, clock):
    cog = make_cog(tmp_path)
    await cog._ensure_guild(GUILD)
    await cog.on_message(msg(uid=1, ts=NOW + 600))  # 8:10pm
    await cog.on_message(msg(uid=1, ts=NOW + 4000))  # 9:05pm
    clock.t = NOW + 4100
    await cog._flush_guild(GUILD_ID)
    assert set(storage.load_day(tmp_path, GUILD_ID, "2026-09-29")["h"]) == {"20", "21"}
    assert [a.key for a in cog._trackers[GUILD_ID].accs.values()] == ["21"]  # 8pm released from memory


async def test_join_and_leave_are_counted_per_day(tmp_path, clock):
    cog = make_cog(tmp_path)
    member = SimpleNamespace(bot=False, guild=GUILD)
    await cog.on_member_join(member)
    await cog.on_member_remove(member)
    await cog.on_member_join(SimpleNamespace(bot=True, guild=GUILD))  # bots ignored
    clock.t = NOW + 60
    await cog._flush_guild(GUILD_ID)
    day = storage.load_day(tmp_path, GUILD_ID, "2026-09-29")
    assert (day["joins"], day["leaves"]) == (1, 1)


# ------------------------------------------------------------------ access control

def ctx_for(*, channel_id=DEFAULT_MOD_CHANNEL_ID, parent_id=None, roles=(), admin=False, cog="self"):
    sent = []

    async def send(text=None, **kw):
        sent.append(text)

    author = SimpleNamespace(
        guild=GUILD, get_role=lambda rid: object() if rid in roles else None,
        guild_permissions=SimpleNamespace(administrator=admin),
    )
    return SimpleNamespace(guild=GUILD, author=author, cog=cog, channel=SimpleNamespace(id=channel_id, parent_id=parent_id), send=send), sent


async def test_mod_in_the_mod_channel_is_allowed(tmp_path, clock):
    cog = make_cog(tmp_path)
    ctx, sent = ctx_for(roles={DEFAULT_MOD_ROLE_ID})
    assert await cog.cog_check(ctx) is True and not sent


async def test_threads_under_the_mod_channel_are_allowed(tmp_path, clock):
    cog = make_cog(tmp_path)
    ctx, _ = ctx_for(channel_id=1, parent_id=DEFAULT_MOD_CHANNEL_ID, roles={DEFAULT_MOD_ROLE_ID})
    assert await cog.cog_check(ctx) is True


async def test_non_mod_is_silently_refused(tmp_path, clock):
    cog = make_cog(tmp_path)
    ctx, sent = ctx_for()
    assert await cog.cog_check(ctx) is False and not sent


async def test_mod_in_the_wrong_channel_gets_a_pointer(tmp_path, clock):
    cog = make_cog(tmp_path)
    ctx, sent = ctx_for(channel_id=5, roles={DEFAULT_MOD_ROLE_ID})
    ctx.cog = cog
    assert await cog.cog_check(ctx) is False
    assert sent and str(DEFAULT_MOD_CHANNEL_ID) in sent[0]


async def test_help_menu_evaluating_checks_does_not_spam_the_channel(tmp_path, clock):
    """Regression: Red's help runs cog_check for every command -- that must stay silent."""
    cog = make_cog(tmp_path)
    ctx, sent = ctx_for(channel_id=5, roles={DEFAULT_MOD_ROLE_ID}, cog=object())  # invoked command belongs to another cog (help)
    assert await cog.cog_check(ctx) is False
    assert sent == []


async def test_admin_counts_as_mod_and_bot_owner_may_use_any_channel(tmp_path, clock):
    cog = make_cog(tmp_path)
    ctx, _ = ctx_for(admin=True)
    assert await cog.cog_check(ctx) is True

    async def yes(*a, **k):
        return True

    cog.bot.is_owner = yes
    ctx, _ = ctx_for(channel_id=5, admin=True)
    assert await cog.cog_check(ctx) is True


async def test_red_mod_status_also_counts(tmp_path, clock):
    cog = make_cog(tmp_path)

    async def yes(*a, **k):
        return True

    cog.bot.is_mod = yes
    ctx, _ = ctx_for()
    assert await cog.cog_check(ctx) is True


# ------------------------------------------------------------------ digests

async def test_digest_fires_once_per_due_period_and_marks_before_sending(tmp_path, clock):
    cog = make_cog(tmp_path)
    await cog._ensure_guild(GUILD)
    sent = []

    async def fake_send(guild, kind, start, end, channel=None):
        sent.append((kind, start.isoformat(), end.isoformat()))
        if kind == "week":
            assert (await cog.config.guild(guild).digest_weekly())["last"] == "2026-09-28"  # recorded BEFORE sending
        return True

    cog._send_digest = fake_send
    monday_9am = datetime(2026, 10, 5, 9, 0, tzinfo=TIMEZONE)
    await cog._run_digest_check(GUILD, monday_9am)
    assert [s for s in sent if s[0] == "week"] == [("week", "2026-09-28", "2026-10-04")]
    await cog._run_digest_check(GUILD, monday_9am + timedelta(minutes=5))
    assert len([s for s in sent if s[0] == "week"]) == 1  # no repeat


async def test_digest_waits_until_9am_and_respects_off_switch(tmp_path, clock):
    cog = make_cog(tmp_path)
    await cog._ensure_guild(GUILD)
    sent = []

    async def fake_send(*a, **k):
        sent.append(a[1])
        return True

    cog._send_digest = fake_send
    await cog._run_digest_check(GUILD, datetime(2026, 10, 5, 8, 59, tzinfo=TIMEZONE))
    assert "week" not in sent  # Monday 8:59: not yet
    cfg = await cog.config.guild(GUILD).digest_weekly()
    cfg["enabled"] = False
    await cog.config.guild(GUILD).digest_weekly.set(cfg)
    await cog._run_digest_check(GUILD, datetime(2026, 10, 5, 9, 30, tzinfo=TIMEZONE))
    assert "week" not in sent  # switched off


async def test_monthly_digest_fires_on_the_first(tmp_path, clock):
    cog = make_cog(tmp_path)
    await cog._ensure_guild(GUILD)
    sent = []

    async def fake_send(guild, kind, start, end, channel=None):
        sent.append((kind, start.isoformat(), end.isoformat()))
        return True

    cog._send_digest = fake_send
    await cog._run_digest_check(GUILD, datetime(2026, 10, 1, 9, 5, tzinfo=TIMEZONE))
    assert ("month", "2026-09-01", "2026-09-30") in sent


async def test_toggling_a_digest_on_skips_the_stale_backlog(tmp_path, clock):
    from serverpulse import engine

    cog = make_cog(tmp_path)
    await cog._ensure_guild(GUILD)
    async def _send(*a, **k):
        return None

    ctx = SimpleNamespace(guild=GUILD, send=_send)
    await cog._toggle_digest(ctx, "digest_weekly", engine.weekly_due, True)
    cfg = await cog.config.guild(GUILD).digest_weekly()
    assert cfg["enabled"] and cfg["last"] == engine.weekly_due(datetime.now(TIMEZONE))[0]


# ------------------------------------------------------------------ arg parsing

def test_parse_args_is_forgiving():
    parse = sp.ServerPulse._parse_args
    assert parse(("fri", "60")) == (60, 4, set(), [])
    assert parse(("all",)) == (None, None, {"all"}, [])
    assert parse(("peak", "7")) == (7, None, set(), ["peak"])
    assert parse(("99999",))[0] == sp.MAX_DAYS_ARG
    assert parse(("0",))[0] == 1


# ------------------------------------------------------------------ backfill end to end

class FakeChannel:
    def __init__(self, cid, name, messages=(), threads=(), forbidden=False, gone=False):
        self.id, self.name, self.messages, self.threads, self.forbidden = cid, name, list(messages), list(threads), forbidden
        self.gone = gone

    async def history(self, limit=None, after=None, before=None, oldest_first=True):
        if self.forbidden:
            raise discord.Forbidden()
        if self.gone:
            raise discord.NotFound.__new__(discord.NotFound)
        for m in sorted(self.messages, key=lambda m: m.created_at):
            if after <= m.created_at < before:
                yield m


def bf_msg(ts, uid=1, content="hi", bot=False, mtype="default"):
    return SimpleNamespace(created_at=datetime.fromtimestamp(ts, TIMEZONE), author=SimpleNamespace(bot=bot, id=uid),
                           webhook_id=None, content=content, type=SimpleNamespace(name=mtype))


class FakeForum:
    """Forum channels have no history() of their own -- only posts (threads)."""

    def __init__(self, cid, name, threads):
        self.id, self.name, self.threads = cid, name, threads


def history_world():
    ignored = DEFAULT_IGNORED_CHANNEL_IDS[0]
    thread = FakeChannel(7001, "thread", [bf_msg(local_ts(2026, 9, 28, 20, 40), uid=3)])
    general = FakeChannel(
        100, "general",
        [
            bf_msg(local_ts(2026, 9, 28, 20, 10), uid=1),
            bf_msg(local_ts(2026, 9, 28, 20, 20), uid=2),
            bf_msg(local_ts(2026, 9, 28, 20, 21), uid=2, bot=True),  # bot: no
            bf_msg(local_ts(2026, 9, 28, 20, 22), uid=1, content=".bj"),  # command: no
            bf_msg(local_ts(2026, 9, 29, 15, 0, 30), uid=1),
            bf_msg(local_ts(2026, 9, 29, 15, 5), uid=1, mtype="new_member"),  # system: no
            bf_msg(local_ts(2026, 9, 29, 20, 30), uid=1),  # after live_since (8pm): live tracking's territory, not backfill's
        ],
        threads=[thread],
    )
    bots = FakeChannel(ignored, "bots", [bf_msg(local_ts(2026, 9, 28, 20, 11), uid=9)])
    secret = FakeChannel(300, "secret", forbidden=True)
    return general, bots, secret


async def run_backfill(cog, days=3):
    reports = []

    async def send(embed=None, **kw):
        reports.append(embed)

    report_channel = SimpleNamespace(send=send)
    stage_meta = {"days": days, "start_ts": int(local_ts(2026, 9, 27)), "end_ts": cog._cache[GUILD_ID]["live_since"], "started": NOW}
    stage = sp.bf.Stage(tmp_data(cog), GUILD_ID)
    stage.start(stage_meta)
    await cog._backfill_run(GUILD, report_channel, stage_meta)
    return reports


def tmp_data(cog):
    return cog.data_dir


async def test_backfill_end_to_end(tmp_path, clock):
    general, bots, secret = history_world()
    guild = make_guild()
    guild.text_channels = [general, bots, secret]
    global GUILD
    saved, GUILD = GUILD, guild
    try:
        cog = make_cog(tmp_path)
        await cog._ensure_guild(guild)
        clock.t = NOW + 3 * 3600  # live tracking is well underway
        reports = await run_backfill(cog)

        state = await cog.config.guild(guild).backfill()
        assert state["status"] == "done" and state["messages"] == 4
        assert state["skipped"] == ["#secret"]
        assert state["channels_done"] == 2 and state["channels_total"] == 2  # the ignored channel is not even a source

        sep28 = storage.load_day(tmp_path, GUILD_ID, "2026-09-28")
        assert sep28["h"]["20"]["m"] == 3  # uid1, uid2, and the thread message (uid3) -> parent channel
        assert sep28["h"]["20"]["c"] == {"100": [3, 3]}
        assert sep28["ub"] == {"1": 1, "2": 1, "3": 1}
        assert storage.load_day(tmp_path, GUILD_ID, "2026-09-29")["h"]["15"]["m"] == 1
        assert "20" not in storage.load_day(tmp_path, GUILD_ID, "2026-09-29")["h"]  # live hour untouched
        # the ignored channel's message and every excluded kind never appear
        assert "9" not in sep28["ub"]

        # coverage was pulled back to the start of the backfill window
        assert (await cog.config.guild(guild).coverage_start()) == int(local_ts(2026, 9, 27))
        assert cog._cache[GUILD_ID]["coverage_start"] == int(local_ts(2026, 9, 27))
        assert reports and "done" in reports[-1].title
        assert not sp.bf.Stage(tmp_path, GUILD_ID).done_channels()  # stage cleaned up

        # re-running replaces rather than double counts
        await run_backfill(cog)
        assert storage.load_day(tmp_path, GUILD_ID, "2026-09-28")["h"]["20"]["m"] == 3
    finally:
        GUILD = saved


async def test_backfill_resume_skips_channels_that_are_already_staged(tmp_path, clock):
    general, bots, secret = history_world()
    guild = make_guild()
    guild.text_channels = [general]
    global GUILD
    saved, GUILD = GUILD, guild
    try:
        cog = make_cog(tmp_path)
        await cog._ensure_guild(guild)
        clock.t = NOW + 3 * 3600
        meta = {"days": 3, "start_ts": int(local_ts(2026, 9, 27)), "end_ts": cog._cache[GUILD_ID]["live_since"], "started": NOW}
        stage = sp.bf.Stage(tmp_path, GUILD_ID)
        stage.start(meta)
        stage.write_channel("100", [(local_ts(2026, 9, 28, 20, 10), 77)])  # pre-staged from an interrupted run
        general.messages = []  # if it were re-read, nothing would be found
        async def send(embed=None, **kw):
            pass
        await cog._backfill_run(guild, SimpleNamespace(send=send), meta)
        assert storage.load_day(tmp_path, GUILD_ID, "2026-09-28")["ub"] == {"77": 1}
    finally:
        GUILD = saved


async def test_backfill_cancel_keeps_progress_and_reports_cancelled(tmp_path, clock):
    general, bots, secret = history_world()
    guild = make_guild()
    guild.text_channels = [general]
    global GUILD
    saved, GUILD = GUILD, guild
    try:
        cog = make_cog(tmp_path)
        await cog._ensure_guild(guild)
        clock.t = NOW + 3 * 3600
        cog._cancel.add(GUILD_ID)
        reports = await run_backfill(cog)
        state = await cog.config.guild(guild).backfill()
        assert state["status"] == "cancelled" and "cancelled" in reports[-1].title
        assert storage.list_dates(tmp_path, GUILD_ID) == []  # nothing half-applied
    finally:
        GUILD = saved


async def test_backfill_error_is_reported_not_swallowed(tmp_path, clock):
    general, bots, secret = history_world()
    guild = make_guild()
    guild.text_channels = [general]
    global GUILD
    saved, GUILD = GUILD, guild
    try:
        cog = make_cog(tmp_path)
        await cog._ensure_guild(guild)
        clock.t = NOW + 3 * 3600

        async def boom(*a, **k):
            raise RuntimeError("discord exploded")

        cog.bot.get_valid_prefixes = boom
        reports = await run_backfill(cog)
        state = await cog.config.guild(guild).backfill()
        assert state["status"] == "error" and "discord exploded" in state["error"]
        assert "error" in reports[-1].title
    finally:
        GUILD = saved


async def test_interrupted_backfill_is_reported_as_resumable(tmp_path, clock):
    cog = make_cog(tmp_path)
    await cog._ensure_guild(GUILD)
    await cog.config.guild(GUILD).backfill.set({"status": "running", "days": 30})
    state = await cog._backfill_state(GUILD)
    assert state["status"] == "cancelled" and "interrupted" in state["error"]
    assert (await cog._backfill_state(make_guild()))["status"] in ("cancelled", "none")


async def test_forum_channels_are_read_through_their_posts_only(tmp_path, clock):
    post = FakeChannel(8001, "a post", [bf_msg(local_ts(2026, 9, 28, 20, 10), uid=4)])
    forum = FakeForum(900, "ideas", [post])
    guild = make_guild()
    guild.forums = [forum]
    global GUILD
    saved, GUILD = GUILD, guild
    try:
        cog = make_cog(tmp_path)
        await cog._ensure_guild(guild)
        clock.t = NOW + 3 * 3600
        await run_backfill(cog)
        state = await cog.config.guild(guild).backfill()
        assert state["status"] == "done" and state["messages"] == 1
        assert storage.load_day(tmp_path, GUILD_ID, "2026-09-28")["h"]["20"]["c"] == {"900": [1, 1]}
    finally:
        GUILD = saved


# ------------------------------------------------------------------ guild allowlist (v1.1.0)

OTHER_GUILD_ID = 99


def _guild(gid, has_mod_channel):
    return SimpleNamespace(
        id=gid, name=f"g{gid}", text_channels=[], voice_channels=[], stage_channels=[], forums=[],
        get_channel_or_thread=lambda cid: object() if has_mod_channel and cid == DEFAULT_MOD_CHANNEL_ID else None,
    )


def _msg_in(guild):
    m = msg()
    m.guild = guild
    return m


async def test_allowlist_is_seeded_from_the_guild_containing_the_mod_channel(tmp_path, clock):
    wonderland, other = _guild(GUILD_ID, True), _guild(OTHER_GUILD_ID, False)
    cog = make_cog(tmp_path, SimpleNamespace(**{**vars(make_bot()), "guilds": [wonderland, other]}))

    await cog._load_allowed_guilds()

    assert cog._allowed == {GUILD_ID}
    assert await cog.config.allowed_guild_ids() == [GUILD_ID]  # persisted for next boot


async def test_unseedable_allowlist_stays_open_instead_of_silencing_tracking(tmp_path, clock):
    cog = make_cog(tmp_path, SimpleNamespace(**{**vars(make_bot()), "guilds": [_guild(OTHER_GUILD_ID, False)]}))

    await cog._load_allowed_guilds()

    assert cog._allowed is None
    assert cog._guild_allowed(OTHER_GUILD_ID) is True  # fail open


async def test_stored_allowlist_wins_over_reseeding(tmp_path, clock):
    cog = make_cog(tmp_path, SimpleNamespace(**{**vars(make_bot()), "guilds": [_guild(GUILD_ID, True)]}))
    await cog.config.allowed_guild_ids.set([GUILD_ID, OTHER_GUILD_ID])

    await cog._load_allowed_guilds()

    assert cog._allowed == {GUILD_ID, OTHER_GUILD_ID}


async def test_messages_in_a_non_allowed_guild_are_ignored_and_create_no_state(tmp_path, clock):
    """REGRESSION: serverpulse tracked every server the bot is in and logged
    'mod channel not found' digest warnings for the ones without a mod channel."""
    cog = make_cog(tmp_path)
    cog._apply_allowed({GUILD_ID})
    other = _guild(OTHER_GUILD_ID, False)

    await cog.on_message(_msg_in(other))

    assert OTHER_GUILD_ID not in cog._trackers
    assert OTHER_GUILD_ID not in cog._cache
    assert OTHER_GUILD_ID not in cog.config._guilds  # not even a Config entry written


async def test_joins_and_leaves_in_a_non_allowed_guild_are_ignored(tmp_path, clock):
    cog = make_cog(tmp_path)
    cog._apply_allowed({GUILD_ID})
    member = SimpleNamespace(bot=False, guild=_guild(OTHER_GUILD_ID, False))

    await cog.on_member_join(member)
    await cog.on_member_remove(member)

    assert OTHER_GUILD_ID not in cog._trackers


async def test_commands_are_silently_refused_in_a_non_allowed_guild(tmp_path, clock):
    cog = make_cog(tmp_path)
    cog._apply_allowed({OTHER_GUILD_ID})  # Wonderland (GUILD_ID) is NOT allowed
    ctx, sent = ctx_for(roles={DEFAULT_MOD_ROLE_ID})

    assert await cog.cog_check(ctx) is False
    assert sent == []  # silent, no pointer message


async def test_loops_skip_non_allowed_guilds(tmp_path, clock):
    if sp.tasks is None:
        pytest.skip("discord.ext.tasks is not available in this test environment (stubbed redbot)")
    other = _guild(OTHER_GUILD_ID, False)
    bot = SimpleNamespace(**{**vars(make_bot()), "guilds": [GUILD, other]})
    cog = make_cog(tmp_path, bot)
    cog._apply_allowed({GUILD_ID})
    checked = []

    async def fake_check(guild, nowl=None):
        checked.append(guild.id)

    cog._run_digest_check = fake_check
    await cog._digest_loop()
    await cog._flush_loop()

    assert checked == [GUILD_ID]
    assert OTHER_GUILD_ID not in cog._trackers  # flush loop didn't hydrate it either


async def test_applying_a_narrower_allowlist_drops_in_memory_state_for_removed_guilds(tmp_path, clock):
    cog = make_cog(tmp_path)
    await cog._ensure_guild(GUILD)
    assert GUILD_ID in cog._trackers

    cog._apply_allowed({OTHER_GUILD_ID})

    assert GUILD_ID not in cog._trackers and GUILD_ID not in cog._cache


def _fn(command):
    """The plain coroutine behind a command (real Red: .callback, dev stub: .func)."""
    return getattr(command, "callback", None) or command.func


def _owner_ctx(is_owner=True, guild=GUILD):
    sent, ticks = [], []

    async def send(text=None, **kw):
        sent.append(text)

    async def tick():
        ticks.append(1)

    return SimpleNamespace(guild=guild, author=SimpleNamespace(id=1), send=send, tick=tick), sent, ticks


async def test_owner_can_add_and_remove_allowed_guilds(tmp_path, clock):
    other = _guild(OTHER_GUILD_ID, False)
    bot = SimpleNamespace(**{**vars(make_bot()), "get_guild": lambda gid: other if gid == OTHER_GUILD_ID else None})

    async def is_owner(user):
        return True

    bot.is_owner = is_owner
    cog = make_cog(tmp_path, bot)
    cog._apply_allowed({GUILD_ID})
    ctx, sent, ticks = _owner_ctx()

    await _fn(sp.ServerPulse.pulse_guilds_add)(cog, ctx, OTHER_GUILD_ID)
    assert cog._allowed == {GUILD_ID, OTHER_GUILD_ID}
    assert await cog.config.allowed_guild_ids() == [GUILD_ID, OTHER_GUILD_ID]

    await _fn(sp.ServerPulse.pulse_guilds_remove)(cog, ctx, OTHER_GUILD_ID)
    assert cog._allowed == {GUILD_ID}

    await _fn(sp.ServerPulse.pulse_guilds_remove)(cog, ctx, GUILD_ID)  # the guild it is run from
    assert cog._allowed == {GUILD_ID}  # refused, would lock itself out
    assert "different allowed server" in sent[-1]

    await _fn(sp.ServerPulse.pulse_guilds_add)(cog, ctx, 12345)  # bot isn't in it
    assert cog._allowed == {GUILD_ID}
    assert "isn't in a server" in sent[-1]


async def test_non_owner_cannot_change_the_allowlist(tmp_path, clock):
    cog = make_cog(tmp_path)  # make_bot().is_owner returns False
    cog._apply_allowed({GUILD_ID})
    ctx, sent, _ = _owner_ctx()

    await _fn(sp.ServerPulse.pulse_guilds_add)(cog, ctx, OTHER_GUILD_ID)

    assert cog._allowed == {GUILD_ID} and sent == []


# ------------------------------------------------------------------ command message cleanup (v1.2.0)

def _ctx(deleter, interaction=None, guild=True):
    return SimpleNamespace(
        guild=SimpleNamespace(id=GUILD_ID) if guild else None,
        interaction=interaction,
        message=SimpleNamespace(delete=deleter),
        sent=[],
    )


async def test_accepted_command_message_is_deleted(tmp_path, clock):
    cog = make_cog(tmp_path)
    deleted = []

    async def deleter():
        deleted.append(True)

    await cog.cog_before_invoke(_ctx(deleter))

    assert deleted == [True]


async def test_cleanup_never_raises_when_the_bot_cannot_delete(tmp_path, clock):
    cog = make_cog(tmp_path)

    async def forbidden():
        raise discord.Forbidden.__new__(discord.Forbidden)

    await cog.cog_before_invoke(_ctx(forbidden))  # must not raise


async def test_slash_invocations_and_dms_are_not_touched(tmp_path, clock):
    cog = make_cog(tmp_path)
    deleted = []

    async def deleter():
        deleted.append(True)

    await cog.cog_before_invoke(_ctx(deleter, interaction=object()))
    await cog.cog_before_invoke(_ctx(deleter, guild=False))

    assert deleted == []


async def test_ack_replaces_the_tick_reaction_and_self_deletes(tmp_path, clock):
    cog = make_cog(tmp_path)
    sent = []

    async def send(text, **kw):
        sent.append((text, kw))

    await cog._ack(SimpleNamespace(send=send))

    assert sent and sent[0][1].get("delete_after") == 8


async def test_backfill_survives_a_channel_or_thread_deleted_mid_run(tmp_path, clock):
    """Regression: 404 Unknown Channel (error 10003) used to abort the whole backfill at 49/57 channels."""
    live_thread = FakeChannel(7002, "ok-thread", [bf_msg(local_ts(2026, 9, 28, 20, 40), uid=3)])
    dead_thread = FakeChannel(7001, "deleted-thread", gone=True)
    general = FakeChannel(100, "general", [bf_msg(local_ts(2026, 9, 28, 20, 10), uid=1)], threads=[dead_thread, live_thread])
    vanished = FakeChannel(200, "vanished", gone=True)
    after = FakeChannel(400, "after", [bf_msg(local_ts(2026, 9, 28, 20, 15), uid=2)])
    guild = make_guild()
    guild.text_channels = [general, vanished, after]
    global GUILD
    saved, GUILD = GUILD, guild
    try:
        cog = make_cog(tmp_path)
        await cog._ensure_guild(guild)
        clock.t = NOW + 3 * 3600
        await run_backfill(cog)

        state = await cog.config.guild(guild).backfill()
        assert state["status"] == "done", state
        assert state["skipped"] == ["#vanished"]
        assert state["messages"] == 3  # general + the healthy thread after the dead one + 'after'
        assert state["channels_done"] == 3 and state["channels_total"] == 3
    finally:
        GUILD = saved
