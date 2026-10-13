"""Behaviour tests for SecretHunt against fakes. They prove the logic and clean imports under the stub, not live
Discord behaviour (real DMs, pins, reaction events, role icons, the pin notice)."""
import asyncio
import importlib
import sys
import types
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from secrethunt import _testkit as tk
from secrethunt import engine
from secrethunt.constants import MAX_WRONG_PER_DAY, WATCH_EMOJI
from secrethunt.puzzles import PUZZLES

LA = ZoneInfo("America/Los_Angeles")
PID = "late_rabbit"
NAMES = ["cuddle", "gaming", "artwork", "music", "memes"]


def ts(d, h=12, mi=0):
    return datetime(2026, 10, d, h, mi, tzinfo=LA).timestamp()


@pytest.fixture
def mod(monkeypatch):
    tk.install(monkeypatch)
    for name in ("secrethunt.secrethunt", "secrethunt.embeds"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    cog = importlib.import_module("secrethunt.secrethunt")
    yield cog
    for name in ("secrethunt.secrethunt", "secrethunt.embeds"):
        sys.modules.pop(name, None)


@pytest.fixture
async def env(mod):
    g = tk.FakeGuild()
    chans = [g.add_channel(100 + i, n) for i, n in enumerate(NAMES)]
    hall = g.add_channel(200, "hall-of-finders")
    role = g.add_role(300, "Secret Finder")
    bot = tk.FakeBot(g)
    cog = mod.SecretHunt(bot)
    conf = cog.config.guild(g)
    await conf.pool.set([c.id for c in chans])
    await conf.published.set([PID])
    await conf.active.set(PID)
    await conf.salt.set("fixed-test-salt")
    await conf.role_id.set(role.id)
    await conf.hall_channel_id.set(hall.id)
    await conf.enabled.set(True)
    await cog._setup_objects(g, PID)
    return types.SimpleNamespace(mod=mod, cog=cog, g=g, chans=chans, hall=hall, role=role, conf=conf, bot=bot)


def member(env, uid, **kw):
    return tk.FakeMember(env.g, uid, **kw)


def chat(env, author, text, channel=None):
    ch = channel or env.chans[0]
    msg = tk.FakeMessage(ch, content=text, author=author)
    msg.guild = env.g
    return msg


def dm(author, text):
    msg = tk.FakeMessage(None, content=text, author=author)
    msg.guild = None
    return msg


def react(env, member_, message_id, channel_id, emoji=WATCH_EMOJI):
    return types.SimpleNamespace(guild_id=env.g.id, user_id=member_.id, message_id=message_id,
                                 channel_id=channel_id, emoji=emoji)


async def state(env, m):
    return (await env.cog.config.member(m).puzzles()).get(PID)


async def objects(env):
    return (await env.conf.objects())[PID]


async def own_channel(env, m):
    st = await state(env, m)
    return env.chans[st["variant"]]


async def wrong_channel(env, m):
    st = await state(env, m)
    return env.chans[(st["variant"] + 1) % len(env.chans)]


async def start(env, m, now=None):
    await env.cog._on_guild_message(chat(env, m, ".tick"))
    return await state(env, m)


async def find_object(env, m, own=True):
    ch = await (own_channel(env, m) if own else wrong_channel(env, m))
    mid = (await objects(env))[str(ch.id)]
    await env.cog._on_reaction(react(env, m, mid, ch.id))
    return ch, mid


# ---------------------------------------------------------------- setup


async def test_setup_pins_one_identical_object_per_channel_and_is_idempotent(env):
    objs = await objects(env)
    assert set(objs) == {str(c.id) for c in env.chans}
    for c in env.chans:
        msg = c.sent[0]
        assert msg.pinned and msg.id == objs[str(c.id)]
    assert len({c.sent[0].embed.description for c in env.chans}) == 1  # decoys look exactly like the real thing
    report = await env.cog._setup_objects(env.g, PID)
    assert all("already set up" in line for line in report) and all(len(c.sent) == 1 for c in env.chans)


async def test_setup_reports_a_failed_pin_and_does_not_record_it(env):
    await env.conf.objects.set({})
    env.chans[1].fail_pin = True
    report = await env.cog._setup_objects(env.g, PID)
    assert any("failed" in line and env.chans[1].mention in line for line in report)
    assert str(env.chans[1].id) not in await objects(env)
    assert str(env.chans[0].id) in await objects(env)


# ---------------------------------------------------------------- the full chain


async def test_full_chain_from_poem_to_role(env):
    a = member(env, 11)
    msg = chat(env, a, ".tick")
    await env.cog._on_guild_message(msg)
    assert msg.deleted  # the trigger never stays visible in chat
    st = await state(env, a)
    assert st["stage"] == engine.STAGE_RIDDLE
    riddle = a.dms[-1]
    ch = await own_channel(env, a)
    hint = engine.channel_hint(ch.name, f"{PID}:{ch.id}")
    assert hint in riddle and f"({len(hint)} letters)" in riddle and ch.name not in riddle.lower().replace(hint, "")

    _, mid = await find_object(env, a)
    assert (WATCH_EMOJI, a.id) in env.g.get_channel(ch.id).get_partial_message(mid).removed
    assert (await state(env, a))["stage"] == engine.STAGE_FOUND
    assert a.dms[-1] == PUZZLES[PID]["found"]

    due = (await state(env, a))["due_ts"]
    await env.cog._deliver_due(env.g, due - 60)
    assert (await state(env, a))["stage"] == engine.STAGE_FOUND and len(a.dms) == 2  # not before the morning
    await env.cog._deliver_due(env.g, due + 1)
    assert (await state(env, a))["stage"] == engine.STAGE_FINAL
    answer = PUZZLES[PID]["answers"][st["variant"]]
    assert sorted(a.dms[-1].split("**")[1]) == sorted(answer)  # the DM carries the jumbled answer
    await env.cog._deliver_due(env.g, due + 120)
    assert len(a.dms) == 3  # delivered once, not every minute

    await env.cog._on_dm(dm(a, answer.upper()))
    assert (await state(env, a))["stage"] == engine.STAGE_SOLVED
    assert env.role in a.roles
    assert env.hall.sent[-1].content.count(a.mention) == 1 and "#1" in env.hall.sent[-1].content
    assert "#1" in a.dms[-1]


async def test_the_answer_can_be_sent_with_the_command_prefix_too(env):
    a = member(env, 11)
    await start(env, a)
    await find_object(env, a)
    await env.cog._deliver_due(env.g, (await state(env, a))["due_ts"] + 1)
    answer = PUZZLES[PID]["answers"][(await state(env, a))["variant"]]
    await env.cog._on_dm(dm(a, f".{answer}"))
    assert (await state(env, a))["stage"] == engine.STAGE_SOLVED


async def test_finishers_are_ranked_in_order(env):
    solved = []
    for uid in (21, 22):
        m = member(env, uid)
        await start(env, m)
        await find_object(env, m)
        await env.cog._deliver_due(env.g, (await state(env, m))["due_ts"] + 1)
        await env.cog._on_dm(dm(m, PUZZLES[PID]["answers"][(await state(env, m))["variant"]]))
        solved.append((await state(env, m))["rank"])
    assert solved == [1, 2] and (await env.conf.finishers())[PID] == [21, 22]


async def test_members_get_different_variants(env):
    seen = set()
    for uid in range(100, 140):
        m = member(env, uid)
        seen.add((await start(env, m))["variant"])
    assert len(seen) >= 4


# ---------------------------------------------------------------- objects and decoys


async def test_a_decoy_object_gets_a_decoy_reply_and_no_progress(env):
    a = member(env, 11)
    await start(env, a)
    ch, mid = await find_object(env, a, own=False)
    assert a.dms[-1] == PUZZLES[PID]["decoy"]
    assert (await state(env, a))["stage"] == engine.STAGE_RIDDLE
    assert (WATCH_EMOJI, a.id) in ch.get_partial_message(mid).removed


async def test_a_bystander_reacting_gets_nothing_but_the_reaction_vanishes(env):
    b = member(env, 12)
    ch = env.chans[2]
    mid = (await objects(env))[str(ch.id)]
    await env.cog._on_reaction(react(env, b, mid, ch.id))
    assert b.dms == [] and await state(env, b) is None
    assert (WATCH_EMOJI, b.id) in ch.get_partial_message(mid).removed


async def test_other_emoji_and_other_messages_are_ignored(env):
    a = member(env, 11)
    await start(env, a)
    ch = await own_channel(env, a)
    mid = (await objects(env))[str(ch.id)]
    await env.cog._on_reaction(react(env, a, mid, ch.id, emoji="\N{THUMBS UP SIGN}"))
    await env.cog._on_reaction(react(env, a, 987654, ch.id))
    assert (await state(env, a))["stage"] == engine.STAGE_RIDDLE and len(a.dms) == 1
    assert ch.get_partial_message(mid).removed == []


async def test_the_variation_selector_on_the_emoji_still_counts(env):
    a = member(env, 11)
    await start(env, a)
    ch = await own_channel(env, a)
    await env.cog._on_reaction(react(env, a, (await objects(env))[str(ch.id)], ch.id, emoji=WATCH_EMOJI + "️"))
    assert (await state(env, a))["stage"] == engine.STAGE_FOUND


# ---------------------------------------------------------------- triggers


async def test_retriggering_replays_whatever_step_you_are_on(env):
    a = member(env, 11)
    await start(env, a)
    await env.cog._on_guild_message(chat(env, a, ".tick"))
    assert a.dms[0] == a.dms[1]  # same riddle again
    await find_object(env, a)
    await env.cog._on_dm(dm(a, "tick"))
    assert a.dms[-1] == PUZZLES[PID]["waiting"]
    await env.cog._deliver_due(env.g, (await state(env, a))["due_ts"] + 1)
    await env.cog._on_dm(dm(a, "tick"))
    assert a.dms[-1] == a.dms[-2]  # the jumbled word again


async def test_it_works_by_dm_with_or_without_the_prefix(env):
    a, b = member(env, 11), member(env, 12)
    await env.cog._on_dm(dm(a, "tick"))
    await env.cog._on_dm(dm(b, ".Tick"))
    assert (await state(env, a)) and (await state(env, b))


async def test_nothing_happens_for_unknown_words_bots_a_disabled_hunt_or_prefix_less_chat(env):
    a = member(env, 11)
    await env.cog._on_guild_message(chat(env, a, ".tock"))
    await env.cog._on_guild_message(chat(env, a, "tick"))  # no prefix in a public channel
    await env.cog._on_guild_message(chat(env, a, ".tick please"))
    await env.cog.on_message(chat(env, member(env, 13, bot=True), ".tick"))
    await env.conf.enabled.set(False)
    await env.cog._on_guild_message(chat(env, a, ".tick"))
    assert await state(env, a) is None and a.dms == []


async def test_a_member_with_closed_dms_is_told_in_channel_and_can_retry(env):
    a = member(env, 11)
    a.dm_closed = True
    await env.cog._on_guild_message(chat(env, a, ".tick"))
    notice = env.chans[0].sent[-1]
    assert a.mention in notice.content and notice.kw["delete_after"] == 15
    a.dm_closed = False
    await env.cog._on_guild_message(chat(env, a, ".tick"))
    assert len(a.dms) == 1


async def test_two_simultaneous_triggers_start_one_chain(env):
    a = member(env, 11)
    # different timestamps, so a lost update (second writer overwrites the first) is visible
    await asyncio.gather(env.cog._run_trigger(env.g, a, PID, 1.0), env.cog._run_trigger(env.g, a, PID, 2.0))
    st = await state(env, a)
    assert st["stage"] == engine.STAGE_RIDDLE and st["started_ts"] == 1.0


async def test_nothing_is_sent_before_the_pool_is_configured(env):
    await env.conf.pool.set([])
    a = member(env, 11)
    await env.cog._on_guild_message(chat(env, a, ".tick"))
    assert a.dms == [] and await state(env, a) is None


# ---------------------------------------------------------------- the delayed clue


async def test_a_closed_dm_on_the_final_clue_is_retried_later(env):
    a = member(env, 11)
    await start(env, a)
    await find_object(env, a)
    due = (await state(env, a))["due_ts"]
    a.dm_closed = True
    await env.cog._deliver_due(env.g, due + 1)
    st = await state(env, a)
    assert st["stage"] == engine.STAGE_FOUND and st["dm_fails"] == 1 and st["due_ts"] > due + 1
    a.dm_closed = False
    await env.cog._deliver_due(env.g, st["due_ts"] + 1)
    assert (await state(env, a))["stage"] == engine.STAGE_FINAL


async def test_test_mode_shortens_the_wait(env):
    await env.conf.test_mode.set(True)
    a = member(env, 11)
    await start(env, a)
    await find_object(env, a)
    st = await state(env, a)
    assert st["due_ts"] - st["found_ts"] == 30


async def test_a_member_who_left_is_skipped_by_the_delivery_loop(env):
    a = member(env, 11)
    await start(env, a)
    await find_object(env, a)
    env.g.members.remove(a)
    await env.cog._deliver_due(env.g, (await state_from_config(env, a))["due_ts"] + 1)  # must not raise


async def state_from_config(env, m):
    return (await env.cog.config.member_from_ids(env.g.id, m.id).puzzles())[PID]


# ---------------------------------------------------------------- guessing


async def at_final(env, uid=11):
    m = member(env, uid)
    await start(env, m)
    await find_object(env, m)
    await env.cog._deliver_due(env.g, (await state(env, m))["due_ts"] + 1)
    return m, PUZZLES[PID]["answers"][(await state(env, m))["variant"]]


async def test_wrong_answers_get_a_vague_reply_then_a_daily_lockout(env):
    a, answer = await at_final(env)
    for i in range(MAX_WRONG_PER_DAY):
        await env.cog._on_dm(dm(a, "teapot"))
        assert a.dms[-1] in PUZZLES[PID]["wrong"]
    await env.cog._on_dm(dm(a, answer))  # right word, but you are locked out today
    assert a.dms[-1] == PUZZLES[PID]["locked"]
    assert (await state(env, a))["stage"] == engine.STAGE_FINAL


async def test_dms_from_people_who_are_not_at_the_final_stage_are_ignored(env):
    a = member(env, 11)
    await start(env, a)
    before = len(a.dms)
    await env.cog._on_dm(dm(a, "hatter"))
    await env.cog._on_dm(dm(member(env, 12), "hello there"))
    await env.cog._on_dm(dm(a, "x" * 200))
    assert len(a.dms) == before and (await state(env, a))["stage"] == engine.STAGE_RIDDLE


async def test_solving_twice_does_not_double_count_or_regrant(env):
    a, answer = await at_final(env)
    await env.cog._on_dm(dm(a, answer))
    await env.cog._on_dm(dm(a, answer))
    assert (await env.conf.finishers())[PID] == [a.id] and len(env.hall.sent) == 1


async def test_a_missing_role_or_hall_does_not_stop_the_solve(env):
    await env.conf.role_id.set(None)
    await env.conf.hall_channel_id.set(None)
    a, answer = await at_final(env)
    await env.cog._on_dm(dm(a, answer))
    assert (await state(env, a))["stage"] == engine.STAGE_SOLVED and a.roles == []


# ---------------------------------------------------------------- admin commands and housekeeping


class Ctx:
    def __init__(self, env, author=None):
        self.guild, self.out = env.g, []
        self.author = author or member(env, 1)
        self.channel = env.chans[0]
        self.message = types.SimpleNamespace(attachments=[])

    async def send(self, content=None, **kw):
        self.out.append(content if content is not None else kw.get("embed"))

    async def tick(self):
        self.out.append("tick")


async def test_role_command_creates_the_default_role_hoisted(env):
    await env.conf.role_id.set(None)
    ctx = Ctx(env)
    await env.cog.secrethunt_role.callback(env.cog, ctx, None)
    role = env.g.created_roles[-1]
    assert role.name == "Secret Finder" and role.kw["hoist"] is True
    assert await env.conf.role_id() == role.id


async def test_enable_refuses_until_pool_and_puzzle_exist(env):
    await env.conf.enabled.set(False)
    await env.conf.published.set([])
    ctx = Ctx(env)
    await env.cog.secrethunt_enable.callback(env.cog, ctx)
    assert "no puzzle started" in ctx.out[-1] and not await env.conf.enabled()
    await env.cog.secrethunt_start.callback(env.cog, ctx, PID)
    await env.cog.secrethunt_enable.callback(env.cog, ctx)
    assert await env.conf.enabled()


async def test_channels_command_validates_and_resets_objects(env):
    ctx = Ctx(env)
    await env.cog.secrethunt_channels.callback(env.cog, ctx, env.chans[0])
    assert "2 to 5" in ctx.out[-1]
    await env.cog.secrethunt_channels.callback(env.cog, ctx, env.chans[0], env.chans[0])
    assert "different" in ctx.out[-1]
    await env.cog.secrethunt_channels.callback(env.cog, ctx, *env.chans[:3])
    assert await env.conf.pool() == [c.id for c in env.chans[:3]] and await env.conf.objects() == {}


async def test_reset_wipes_progress_and_the_finisher_entry(env):
    a, answer = await at_final(env)
    await env.cog._on_dm(dm(a, answer))
    ctx = Ctx(env)
    await env.cog.secrethunt_reset.callback(env.cog, ctx, a, PID)
    assert await state(env, a) is None and (await env.conf.finishers())[PID] == []
    await env.cog._on_guild_message(chat(env, a, ".tick"))
    assert (await state(env, a))["stage"] == engine.STAGE_RIDDLE


async def test_status_and_version_commands_respond(env):
    ctx = Ctx(env)
    await env.cog.secrethunt_version.callback(env.cog, ctx)
    assert ctx.out[-1].startswith("SecretHunt v")
    a = member(env, 11)
    await start(env, a)
    embed = await env.cog._status_embed(env.g)
    assert "1 started, 0 solved" in embed.description


async def test_preview_dms_the_admin_every_variant_with_answers(env):
    ctx = Ctx(env)
    await env.cog.secrethunt_preview.callback(env.cog, ctx, PID)
    text = ctx.author.dms[-1]
    assert all(a in text for a in PUZZLES[PID]["answers"]) and text.count("Variant") == 5


async def test_the_data_deletion_request_clears_progress_and_finisher_entries(env):
    a, answer = await at_final(env)
    await env.cog._on_dm(dm(a, answer))
    await env.cog.red_delete_data_for_user(requester="user", user_id=a.id)
    assert await state(env, a) is None and (await env.conf.finishers())[PID] == []


async def test_the_poem_command_posts_the_acrostic(env):
    ctx = Ctx(env)
    await env.cog.secrethunt_poem.callback(env.cog, ctx, env.chans[1], "")
    poem = env.chans[1].sent[-1].embed.description
    assert engine.acrostic(poem.strip("*").split("\n")) == "tick"
