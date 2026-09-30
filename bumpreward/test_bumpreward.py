"""Handler-level tests with real discord.py objects (Embed/AllowedMentions) and fake guild/channel/message."""
import time
from types import SimpleNamespace

import discord
import pytest

from . import bumpreward as br
from . import engine

CHANNEL_ID = 111
ROLE_ID = 999


class FakeChannel:
    def __init__(self, cid):
        self.id = cid
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)


class FakeGuild:
    def __init__(self):
        self.id = 1
        self.channel = FakeChannel(CHANNEL_ID)
        self.other_channel = FakeChannel(222)
        self.members = {}

    def get_channel(self, cid):
        return {CHANNEL_ID: self.channel, 222: self.other_channel}.get(cid)

    def get_member(self, uid):
        return self.members.get(uid)


def make_member(uid):
    return SimpleNamespace(id=uid, mention=f"<@{uid}>", display_name=f"user{uid}")


def make_msg(guild, *, mid, text="Bump done! :thumbsup:", uid=42, channel=None, author=engine.DISBOARD_ID, legacy=False):
    embeds = [discord.Embed(description=text)] if text else []
    ns = SimpleNamespace(
        id=mid, guild=guild, author=SimpleNamespace(id=author), content="", embeds=embeds,
        channel=channel or guild.channel,
    )
    user = SimpleNamespace(id=uid) if uid else None
    if legacy:
        ns.interaction = SimpleNamespace(user=user)
    else:
        ns.interaction_metadata = SimpleNamespace(user=user)
    return ns


@pytest.fixture
async def env(monkeypatch):
    from redbot.core import bank
    bank.deposits.clear()
    bank.raise_on_deposit = None
    bot = SimpleNamespace(guilds=[])
    cog = br.BumpReward(bot)
    guild = FakeGuild()
    guild.members[42] = make_member(42)
    guild.members[43] = make_member(43)
    bot.guilds.append(guild)
    conf = cog.config.guild(guild)
    await conf.enabled.set(True)
    await conf.channel_id.set(CHANNEL_ID)
    await conf.role_id.set(ROLE_ID)
    await conf.min_reward.set(100)
    await conf.max_reward.set(100)  # deterministic
    return SimpleNamespace(cog=cog, guild=guild, conf=conf, bank=bank, monkeypatch=monkeypatch)


async def test_successful_bump_pays_posts_and_schedules(env):
    await env.cog._handle(make_msg(env.guild, mid=1))
    assert env.bank.deposits == [(42, 100)]
    g = await env.conf.all()
    assert g["reminded"] is False and g["warned"] is False
    assert abs(g["next_bump_at"] - (time.time() + 7200)) < 5
    assert g["last_bumper"] == 42
    assert g["daily"]["counts"] == {"42": 1}
    sent = env.guild.channel.sent
    assert len(sent) == 1
    desc = sent[0]["embed"].description
    assert "<@42>" in desc and "100" in desc and "wondercoin" in desc and "Bump #1" in desc
    assert sent[0]["allowed_mentions"].users is False


async def test_ignored_outside_bump_channel(env):
    await env.cog._handle(make_msg(env.guild, mid=1, channel=env.guild.other_channel))
    assert env.bank.deposits == [] and env.guild.channel.sent == []
    assert (await env.conf.all())["reminded"] is True


async def test_ignored_when_disabled(env):
    await env.conf.enabled.set(False)
    await env.cog._handle(make_msg(env.guild, mid=1))
    assert env.bank.deposits == []


async def test_ignores_other_bots_and_unrelated_disboard_messages(env):
    await env.cog._handle(make_msg(env.guild, mid=1, author=12345))
    await env.cog._handle(make_msg(env.guild, mid=2, text="Server stats blah"))
    assert env.bank.deposits == [] and env.guild.channel.sent == []


async def test_same_message_seen_via_create_and_edit_pays_once(env):
    m = make_msg(env.guild, mid=7)
    await env.cog._handle(m)
    await env.cog._handle(m)
    assert env.bank.deposits == [(42, 100)]


async def test_deferred_reply_loading_then_edit(env):
    await env.cog._handle(make_msg(env.guild, mid=8, text=""))   # "thinking..." placeholder
    assert env.bank.deposits == []
    await env.cog._handle(make_msg(env.guild, mid=8))            # edited into the real result
    assert env.bank.deposits == [(42, 100)]


async def test_legacy_interaction_attribute_fallback(env):
    await env.cog._handle(make_msg(env.guild, mid=9, legacy=True))
    assert env.bank.deposits == [(42, 100)]


async def test_unresolvable_bumper_still_starts_reminder_but_no_payout(env):
    await env.cog._handle(make_msg(env.guild, mid=10, uid=None))
    assert env.bank.deposits == []
    assert (await env.conf.all())["reminded"] is False


async def test_balance_too_high_does_not_crash_and_is_reported(env):
    from redbot.core import errors
    env.bank.raise_on_deposit = errors.BalanceTooHigh()
    await env.cog._handle(make_msg(env.guild, mid=11))
    assert "maximum" in env.guild.channel.sent[0]["embed"].description


async def test_cooldown_notice_syncs_timer(env):
    await env.cog._handle(make_msg(env.guild, mid=12, text="Please wait another 37 minutes until the server can be bumped"))
    g = await env.conf.all()
    assert g["reminded"] is False
    assert abs(g["next_bump_at"] - (time.time() + 37 * 60)) < 5
    assert env.bank.deposits == []


async def test_cooldown_notice_does_not_disturb_accurate_timer(env):
    await env.cog._handle(make_msg(env.guild, mid=13))
    before = (await env.conf.all())["next_bump_at"]
    await env.cog._handle(make_msg(env.guild, mid=14, text="Please wait another 120 minutes until the server can be bumped"))
    assert (await env.conf.all())["next_bump_at"] == before


async def test_bumps_in_a_row_grow_bonus_and_reset_when_someone_else_bumps(env):
    order = [42, 42, 42, 43, 42, 42, 42, 42, 42, 42, 42]
    for i, uid in enumerate(order):
        await env.cog._handle(make_msg(env.guild, mid=100 + i, uid=uid))
    amounts = [a for _, a in env.bank.deposits]
    # 42: 100,110,120 | 43 breaks the run: 100 | 42 restarts: 100,110,120,130,140,150, then capped 150
    assert amounts == [100, 110, 120, 100, 100, 110, 120, 130, 140, 150, 150]
    g = await env.conf.all()
    assert g["last_bumper"] == 42 and g["run_count"] == 7
    assert "7 bumps in a row" in env.guild.channel.sent[-1]["embed"].description
    assert "+50%" in env.guild.channel.sent[-1]["embed"].description


async def test_first_bump_has_no_streak_text(env):
    await env.cog._handle(make_msg(env.guild, mid=200))
    assert "in a row" not in env.guild.channel.sent[0]["embed"].description


async def test_unidentified_bumper_breaks_the_run(env):
    await env.cog._handle(make_msg(env.guild, mid=210, uid=42))
    await env.cog._handle(make_msg(env.guild, mid=211, uid=None))    # unknown person bumps
    await env.cog._handle(make_msg(env.guild, mid=212, uid=42))
    assert [a for _, a in env.bank.deposits] == [100, 100]
    assert (await env.conf.all())["run_count"] == 1


async def test_total_week_and_month_counts_recorded(env):
    for i, uid in enumerate([42, 42, 43, 42]):
        await env.cog._handle(make_msg(env.guild, mid=300 + i, uid=uid))
    g = await env.conf.all()
    assert g["weekly"] == {"key": engine.week_key(), "counts": {"42": 3, "43": 1}}
    assert g["monthly"] == {"key": engine.month_key(), "counts": {"42": 3, "43": 1}}
    assert g["daily"]["counts"] == {"42": 3, "43": 1}
    assert (await env.cog.config.member(env.guild.members[42]).all())["total_bumps"] == 3


async def test_stale_week_and_month_buckets_reset_on_next_bump(env):
    await env.conf.weekly.set({"key": "2000-W01", "counts": {"42": 50}})
    await env.conf.monthly.set({"key": "2000-01", "counts": {"42": 90}})
    await env.cog._handle(make_msg(env.guild, mid=310))
    g = await env.conf.all()
    assert g["weekly"] == {"key": engine.week_key(), "counts": {"42": 1}}
    assert g["monthly"] == {"key": engine.month_key(), "counts": {"42": 1}}


def make_ctx(env):
    sent = []

    async def send(*a, **k):
        sent.append((a, k))

    return SimpleNamespace(guild=env.guild, send=send, sent=sent)


async def test_top_all_week_month(env):
    ctx = make_ctx(env)
    await env.cog.br_top(ctx)                                        # nothing yet
    assert ctx.sent[-1][0] == ("Nobody has bumped yet.",)
    for i, uid in enumerate([43, 42, 42]):
        await env.cog._handle(make_msg(env.guild, mid=400 + i, uid=uid))
    for period, title in [("all", "All-time"), ("week", "this week"), ("weekly", "this week"),
                          ("month", "this month"), ("monthly", "this month")]:
        await env.cog.br_top(ctx, period)
        emb = ctx.sent[-1][1]["embed"]
        assert title in emb.title
        assert emb.description.index("<@42>") < emb.description.index("<@43>")
        assert "**2** bumps" in emb.description and "**1** bump" in emb.description
        assert ctx.sent[-1][1]["allowed_mentions"].users is False


async def test_top_ignores_stale_period_buckets_but_keeps_all_time(env):
    await env.cog._handle(make_msg(env.guild, mid=410))
    await env.conf.weekly.set({"key": "2000-W01", "counts": {"42": 50}})
    await env.conf.monthly.set({"key": "2000-01", "counts": {"42": 90}})
    ctx = make_ctx(env)
    await env.cog.br_top(ctx, "week")
    assert ctx.sent[-1][0] == ("No bumps yet this week.",)
    await env.cog.br_top(ctx, "month")
    assert ctx.sent[-1][0] == ("No bumps yet this month.",)
    await env.cog.br_top(ctx, "all")
    assert "<@42>" in ctx.sent[-1][1]["embed"].description


async def test_top_rejects_unknown_period_and_stats_command_is_gone(env):
    ctx = make_ctx(env)
    await env.cog.br_top(ctx, "decade")
    assert "top week" in ctx.sent[-1][0][0]
    assert not hasattr(br.BumpReward, "br_stats")


async def test_reminders_warning_then_now_with_role_ping(env):
    now = time.time()
    await env.conf.next_bump_at.set(now + 30)
    await env.conf.warned.set(False)
    await env.conf.reminded.set(False)
    await env.cog._run_reminders(env.guild, now)           # 30s before due -> warning
    await env.cog._run_reminders(env.guild, now)           # no duplicate
    assert len(env.guild.channel.sent) == 1
    assert f"<@&{ROLE_ID}>" in env.guild.channel.sent[0]["content"] and "1 minute" in env.guild.channel.sent[0]["content"]
    await env.cog._run_reminders(env.guild, now + 31)      # due -> NOW
    await env.cog._run_reminders(env.guild, now + 60)      # no duplicate
    assert len(env.guild.channel.sent) == 2
    assert "Time to bump" in env.guild.channel.sent[1]["content"]
    assert (await env.conf.all())["reminded"] is True


async def test_warning_can_be_disabled_and_not_early(env):
    now = time.time()
    await env.conf.next_bump_at.set(now + 500)
    await env.conf.warned.set(False)
    await env.conf.reminded.set(False)
    await env.cog._run_reminders(env.guild, now)           # too early: nothing
    assert env.guild.channel.sent == []
    await env.conf.warning_enabled.set(False)
    await env.cog._run_reminders(env.guild, now + 470)     # in warning window but disabled
    assert env.guild.channel.sent == []
    await env.cog._run_reminders(env.guild, now + 501)
    assert len(env.guild.channel.sent) == 1


async def test_overdue_reminder_after_downtime_fires_once(env):
    now = time.time()
    await env.conf.next_bump_at.set(now - 3600)
    await env.conf.warned.set(False)
    await env.conf.reminded.set(False)
    await env.cog._tick_guild(env.guild)
    await env.cog._tick_guild(env.guild)
    assert len(env.guild.channel.sent) == 1


async def test_day_rollover_posts_board_then_resets(env):
    await env.conf.daily.set({"date": "2000-01-01", "counts": {"42": 3, "43": 1}})
    await env.cog._tick_guild(env.guild)
    sent = env.guild.channel.sent
    assert len(sent) == 1
    emb = sent[0]["embed"]
    assert "2000-01-01" in emb.title
    assert emb.description.index("<@42>") < emb.description.index("<@43>")
    assert "4 bumps" in emb.footer.text
    assert (await env.conf.daily())["counts"] == {}
    assert (await env.conf.daily())["date"] == engine.day_key()
    await env.cog._tick_guild(env.guild)                   # second tick: nothing more
    assert len(env.guild.channel.sent) == 1


async def test_empty_day_rollover_posts_nothing(env):
    await env.conf.daily.set({"date": "2000-01-01", "counts": {}})
    await env.cog._tick_guild(env.guild)
    assert env.guild.channel.sent == []


async def test_reward_message_shows_next_reminder_time(env):
    await env.cog._handle(make_msg(env.guild, mid=500))
    desc = env.guild.channel.sent[0]["embed"].description
    g = await env.conf.all()
    assert f"⏰ Next bump reminder <t:{int(g['next_bump_at'])}:R>" in desc


async def test_free_mode_is_two_hours_and_still_records_bumps(env):
    await env.cog._handle(make_msg(env.guild, mid=510))
    g = await env.conf.all()
    assert abs(g["next_bump_at"] - (time.time() + 7200)) < 5
    assert len(g["recent_bumps"]) == 1
    assert "⚡" not in env.guild.channel.sent[0]["embed"].description


async def test_pro_mode_fast_cooldown_while_under_the_limit(env):
    await env.conf.mode.set("pro")
    await env.cog._handle(make_msg(env.guild, mid=520))
    g = await env.conf.all()
    assert abs(g["next_bump_at"] - (time.time() + 1800)) < 5
    desc = env.guild.channel.sent[0]["embed"].description
    assert "⚡ 1/12 bumps in the last 24h" in desc
    assert f"<t:{int(g['next_bump_at'])}:R>" in desc


async def test_pro_mode_twelfth_bump_falls_back_to_two_hours(env):
    await env.conf.mode.set("pro")
    now = time.time()
    await env.conf.recent_bumps.set([now - 600 * i for i in range(1, 12)])   # 11 bumps already today
    await env.cog._handle(make_msg(env.guild, mid=530))                      # this is #12
    g = await env.conf.all()
    assert len(g["recent_bumps"]) == 12
    assert abs(g["next_bump_at"] - (time.time() + 7200)) < 5
    assert "⚡ 12/12" in env.guild.channel.sent[0]["embed"].description


async def test_pro_mode_eleventh_bump_is_still_fast(env):
    await env.conf.mode.set("pro")
    now = time.time()
    await env.conf.recent_bumps.set([now - 600 * i for i in range(1, 11)])   # 10 already
    await env.cog._handle(make_msg(env.guild, mid=540))                      # this is #11
    assert abs((await env.conf.all())["next_bump_at"] - (time.time() + 1800)) < 5


async def test_pro_mode_old_bumps_age_out_of_the_window(env):
    await env.conf.mode.set("pro")
    now = time.time()
    await env.conf.recent_bumps.set([now - 86400 - 60 * i for i in range(1, 20)])   # all older than 24h
    await env.cog._handle(make_msg(env.guild, mid=550))
    g = await env.conf.all()
    assert len(g["recent_bumps"]) == 1
    assert abs(g["next_bump_at"] - (time.time() + 1800)) < 5


async def test_switching_to_pro_uses_bumps_recorded_while_free(env):
    for i in range(11):
        await env.cog._handle(make_msg(env.guild, mid=560 + i, uid=42 + (i % 2)))   # 11 bumps in free mode
    await env.conf.mode.set("pro")
    await env.cog._handle(make_msg(env.guild, mid=600))                              # #12 -> slow
    assert abs((await env.conf.all())["next_bump_at"] - (time.time() + 7200)) < 5


async def test_mode_command(env):
    ctx = make_ctx(env)
    await env.cog.br_mode(ctx)
    assert "Free" in ctx.sent[-1][0][0] and "2h" in ctx.sent[-1][0][0]
    await env.cog.br_mode(ctx, "pro")
    assert (await env.conf.all())["mode"] == "pro"
    assert "Pro" in ctx.sent[-1][0][0] and "30m" in ctx.sent[-1][0][0] and "12" in ctx.sent[-1][0][0]
    await env.cog.br_mode(ctx)
    assert "Pro" in ctx.sent[-1][0][0]
    await env.cog.br_mode(ctx, "FREE")
    assert (await env.conf.all())["mode"] == "free"
    await env.cog.br_mode(ctx, "gold")
    assert "Use `mode" in ctx.sent[-1][0][0]
    assert (await env.conf.all())["mode"] == "free"


async def test_handler_uses_settings_as_of_when_it_gets_the_lock_not_before(env):
    """A settings change made while a handler is queued behind the lock must be honoured."""
    import asyncio
    await env.cog._locks[env.guild.id].acquire()
    task = asyncio.create_task(env.cog._handle(make_msg(env.guild, mid=700)))
    await asyncio.sleep(0.05)                      # handler is now waiting for the lock
    await env.conf.enabled.set(False)              # mod turns the cog off in the meantime
    env.cog._locks[env.guild.id].release()
    await task
    assert env.bank.deposits == []
    assert env.guild.channel.sent == []


async def test_status_does_not_show_a_fake_user_when_bumper_unknown(env):
    await env.cog._handle(make_msg(env.guild, mid=710, uid=None))     # bump seen, bumper unidentified
    ctx = make_ctx(env)
    await env.cog.br_status(ctx)
    text = ctx.sent[-1][1]["embed"].fields[0].value
    assert "<@0>" not in text and "unidentified" in text


async def test_generic_payout_failure_is_not_reported_as_max_balance(env):
    env.bank.raise_on_deposit = RuntimeError("bank exploded")
    await env.cog._handle(make_msg(env.guild, mid=720))
    desc = env.guild.channel.sent[0]["embed"].description
    assert "maximum" not in desc and "+100" not in desc and "couldn't pay" in desc.lower()
    assert "bumped the server" in desc
