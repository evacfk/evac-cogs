"""Behaviour tests for SecretHunt against fakes. They prove the logic and clean imports under the stub, not live
Discord behaviour (real DMs, pins, reaction events, role icons, the pin notice, archived forum posts)."""
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
NAMES = ["Gaming", "Music", "Memes", "Movies", "Artwork"]  # the forum posts the objects hide in


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
    posts = [g.add_thread(100 + i, n) for i, n in enumerate(NAMES)]
    hall = g.add_channel(200, "hall-of-secrets")
    role = g.add_role(300, "Secret Finder")
    bot = tk.FakeBot(g)
    cog = mod.SecretHunt(bot)
    conf = cog.config.guild(g)
    await conf.pool.set([c.id for c in posts])
    await conf.published.set([PID])
    await conf.active.set(PID)
    await conf.salt.set("fixed-test-salt")
    await conf.hall_channel_id.set(hall.id)
    await conf.enabled.set(True)
    await cog._setup_objects(g, PID)
    return types.SimpleNamespace(mod=mod, cog=cog, g=g, chans=posts, hall=hall, role=role, conf=conf, bot=bot)


def member(env, uid, **kw):
    return tk.FakeMember(env.g, uid, **kw)


def chat(env, author, text, channel=None):
    msg = tk.FakeMessage(channel or env.chans[0], content=text, author=author)
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
    return env.chans[(await state(env, m))["variant"]]


async def wrong_channel(env, m):
    return env.chans[((await state(env, m))["variant"] + 1) % len(env.chans)]


async def start(env, m, channel=None):
    await env.cog._on_guild_message(chat(env, m, ".tick", channel))
    return await state(env, m)


async def find_object(env, m, own=True):
    ch = await (own_channel(env, m) if own else wrong_channel(env, m))
    mid = (await objects(env))[str(ch.id)]
    await env.cog._on_reaction(react(env, m, mid, ch.id))
    return ch, mid


async def at_final(env, uid=11, name=None):
    m = member(env, uid, name=name)
    await start(env, m)
    await find_object(env, m)
    await env.cog._deliver_due(env.g, (await state(env, m))["due_ts"] + 1)
    return m, PUZZLES[PID]["answers"][(await state(env, m))["variant"]]


async def solve(env, uid, name=None):
    m, answer = await at_final(env, uid, name)
    await env.cog._on_dm(dm(m, answer))
    return m


def hall_description(env):
    return env.hall.sent[-1].embed.description


# ---------------------------------------------------------------- setup in forum posts


async def test_setup_pins_one_identical_object_per_post_and_is_idempotent(env):
    objs = await objects(env)
    assert set(objs) == {str(c.id) for c in env.chans}
    for c in env.chans:
        msg = c.sent[0]
        assert msg.pinned and msg.id == objs[str(c.id)]
    assert len({c.sent[0].embed.description for c in env.chans}) == 1  # decoys look exactly like the real thing
    report = await env.cog._setup_objects(env.g, PID)
    assert all("already set up" in line for line in report) and all(len(c.sent) == 1 for c in env.chans)


async def test_the_pinned_a_message_notice_is_deleted_so_it_cannot_point_at_the_object(env):
    import discord
    if not hasattr(discord, "MessageType"):
        pytest.skip("the repo's minimal discord stub has no MessageType")
    for c in env.chans:
        assert len(c.sent) == 1 and c.sent[0].pinned  # the fake posted a pin notice at pin time; the cog removed it


async def test_setup_unarchives_a_quiet_post_and_reports_a_locked_one(env):
    await env.conf.objects.set({})
    env.chans[1].archived = True
    env.chans[2].archived = env.chans[2].locked = True  # the bot cannot reopen this one
    report = await env.cog._setup_objects(env.g, PID)
    assert not env.chans[1].archived and env.chans[1].sent[-1].pinned
    assert any("failed" in line and env.chans[2].mention in line for line in report)
    assert str(env.chans[2].id) not in await objects(env) and str(env.chans[1].id) in await objects(env)


async def test_setup_reports_a_failed_pin_and_does_not_record_it(env):
    await env.conf.objects.set({})
    env.chans[1].fail_pin = True
    report = await env.cog._setup_objects(env.g, PID)
    assert any("failed" in line and env.chans[1].mention in line for line in report)
    assert str(env.chans[1].id) not in await objects(env) and str(env.chans[0].id) in await objects(env)


async def test_setup_reports_a_deleted_post(env):
    await env.conf.objects.set({})
    del env.g.threads[env.chans[4].id]
    report = await env.cog._setup_objects(env.g, PID)
    assert any("not found" in line for line in report)


# ---------------------------------------------------------------- the full chain


async def test_full_chain_from_poem_to_the_hall_with_no_role_for_easy(env):
    async with env.conf.roles() as roles:
        roles["medium"] = env.role.id  # a role for ANOTHER tier must never leak into Easy
    a = member(env, 11)
    msg = chat(env, a, ".tick")
    await env.cog._on_guild_message(msg)
    assert msg.deleted  # the trigger never stays visible in chat
    st = await state(env, a)
    assert st["stage"] == engine.STAGE_RIDDLE
    riddle = a.dms[-1]
    ch = await own_channel(env, a)
    hint, shape = engine.title_hint(ch.name, f"{PID}:{ch.id}")
    assert hint in riddle and f"({shape} letters)" in riddle and ch.name.lower() not in riddle.lower()

    _, mid = await find_object(env, a)
    assert (WATCH_EMOJI, a.id) in ch.get_partial_message(mid).removed  # forum posts are not plain channels
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
    assert a.roles == []  # Easy: no role, no badge
    assert "role" not in a.dms[-1].lower() and "#1" in a.dms[-1] and "Hall of Secrets" in a.dms[-1]
    post = env.hall.sent[-1]
    assert post.embed.title == "Easy · The Late Rabbit" and post.embed.description == "1. user11"


async def test_the_trigger_works_in_an_ordinary_channel_too(env):
    a = member(env, 11)
    await start(env, a, channel=env.hall)
    assert (await state(env, a))["stage"] == engine.STAGE_RIDDLE


async def test_the_answer_can_be_sent_with_the_command_prefix_too(env):
    a, answer = await at_final(env)
    await env.cog._on_dm(dm(a, f".{answer}"))
    assert (await state(env, a))["stage"] == engine.STAGE_SOLVED


async def test_members_get_different_variants(env):
    seen = set()
    for uid in range(100, 140):
        seen.add((await start(env, member(env, uid)))["variant"])
    assert len(seen) >= 4


async def test_a_post_with_a_tiny_title_or_one_that_vanished_still_gets_a_clue(env):
    for c in env.chans:
        c.name = "ab"
    a = member(env, 11)
    await start(env, a)
    assert (await own_channel(env, a)).mention in a.dms[-1]  # no scramble possible: it points straight at the post
    b = member(env, 12)
    await start(env, b)
    del env.g.threads[(await own_channel(env, b)).id]
    await env.cog._on_guild_message(chat(env, b, ".tick"))
    assert "a room I cannot name" in b.dms[-1]


# ---------------------------------------------------------------- objects and decoys


async def test_a_decoy_object_gets_a_warning_and_no_progress(env):
    a = member(env, 11)
    await start(env, a)
    ch, mid = await find_object(env, a, own=False)
    assert a.dms[-1] == PUZZLES[PID]["decoy"] and "One more wrong watch" in a.dms[-1]
    assert (await state(env, a))["stage"] == engine.STAGE_RIDDLE
    assert (WATCH_EMOJI, a.id) in ch.get_partial_message(mid).removed


async def test_the_second_wrong_object_locks_them_out_even_from_their_own(env):
    a = member(env, 11)
    await start(env, a)
    await find_object(env, a, own=False)
    await find_object(env, a, own=False)
    assert a.dms[-1] == PUZZLES[PID]["lockout"]
    n = len(a.dms)
    ch, mid = await find_object(env, a, own=True)  # their real object, but they are locked out
    assert (await state(env, a))["stage"] == engine.STAGE_RIDDLE and len(a.dms) == n
    assert (WATCH_EMOJI, a.id) in ch.get_partial_message(mid).removed  # still tidied away
    st = await state(env, a)
    st_until = st["locked_until"]
    async with env.cog.config.member(a).puzzles() as states:
        states[PID]["locked_until"] = 1.0  # a day later
    await find_object(env, a, own=True)
    assert (await state(env, a))["stage"] == engine.STAGE_FOUND and st_until > 1.0


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
    due = (await env.cog.config.member_from_ids(env.g.id, a.id).puzzles())[PID]["due_ts"]
    await env.cog._deliver_due(env.g, due + 1)  # must not raise


# ---------------------------------------------------------------- guessing


async def test_wrong_answers_get_a_vague_reply_then_a_daily_lockout(env):
    a, answer = await at_final(env)
    for _ in range(MAX_WRONG_PER_DAY):
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


async def test_solving_twice_does_not_double_count(env):
    a, answer = await at_final(env)
    await env.cog._on_dm(dm(a, answer))
    await env.cog._on_dm(dm(a, answer))
    assert (await env.conf.finishers())[PID] == [a.id] and len(env.hall.sent) == 1


async def test_a_missing_hall_channel_does_not_stop_the_solve(env):
    await env.conf.hall_channel_id.set(None)
    a, answer = await at_final(env)
    await env.cog._on_dm(dm(a, answer))
    assert (await state(env, a))["stage"] == engine.STAGE_SOLVED and env.hall.sent == []


# ---------------------------------------------------------------- the Hall of Secrets


async def test_starting_a_puzzle_posts_its_empty_hall_entry_so_the_order_is_release_order(env):
    ctx = Ctx(env)
    await env.cog.secrethunt_start.callback(env.cog, ctx, PID)
    assert len(env.hall.sent) == 1 and hall_description(env) == "*Nobody yet.*"
    assert env.hall.sent[0].embed.title == "Easy \u00b7 The Late Rabbit"
    await env.cog.secrethunt_start.callback(env.cog, ctx, PID)  # starting again must not duplicate it
    assert len(env.hall.sent) == 1


async def test_the_hall_is_one_post_per_puzzle_edited_in_place_in_finishing_order(env):
    await solve(env, 21)
    await solve(env, 22)
    assert len(env.hall.sent) == 1
    assert hall_description(env) == "1. user21\n2. user22"
    assert (await env.conf.hall_messages())[PID] == env.hall.sent[0].id
    assert (await env.conf.finishers())[PID] == [21, 22]


async def test_a_deleted_hall_post_is_rewritten_not_lost(env):
    await solve(env, 21)
    env.hall.sent.clear()  # a mod deleted the post
    await solve(env, 22)
    assert len(env.hall.sent) == 1 and hall_description(env) == "1. user21\n2. user22"
    assert (await env.conf.hall_messages())[PID] == env.hall.sent[0].id


async def test_people_who_left_drop_off_the_hall_but_nobody_is_renumbered(env):
    a = await solve(env, 21)
    await solve(env, 22)
    await solve(env, 23)
    env.g.members.remove(a)
    await env.cog._refresh_hall(env.g, PID)
    assert hall_description(env) == "2. user22\n3. user23"


async def test_hall_names_cannot_restyle_the_post(env):
    await solve(env, 21, name="a_b*c")
    assert hall_description(env) == "1. a\\_b\\*c"


async def test_a_tier_with_a_role_grants_it_and_titles_the_hall_by_tier(env, monkeypatch):
    monkeypatch.setitem(env.mod.PUZZLES, PID, {**PUZZLES[PID], "tier": "medium"})
    async with env.conf.roles() as roles:
        roles["medium"] = env.role.id
    a = await solve(env, 21)
    assert env.role in a.roles and "Secret Finder" in a.dms[-1]
    assert env.hall.sent[-1].embed.title.startswith("Medium ·")


async def test_changing_the_hall_channel_starts_fresh_posts(env):
    await solve(env, 21)
    new_hall = env.g.add_channel(201, "new-hall")
    ctx = Ctx(env)
    await env.cog.secrethunt_hall.callback(env.cog, ctx, new_hall)
    assert await env.conf.hall_messages() == {}
    await env.cog.secrethunt_hallrefresh.callback(env.cog, ctx)
    assert len(new_hall.sent) == 1 and new_hall.sent[0].embed.description == "1. user21"


async def test_reset_and_data_deletion_take_people_off_the_hall(env):
    a = await solve(env, 21)
    b = await solve(env, 22)
    await env.cog.secrethunt_reset.callback(env.cog, Ctx(env), a, PID)
    assert hall_description(env) == "1. user22"  # reset removes them from the list, so it renumbers
    await env.cog.red_delete_data_for_user(requester="user", user_id=b.id)
    assert hall_description(env) == "*Nobody yet.*" and await state(env, b) is None
    assert (await env.conf.finishers())[PID] == []


# ---------------------------------------------------------------- admin commands


class Attachment:
    def __init__(self, size=100, data=b"png"):
        self.size, self._data = size, data

    async def read(self):
        return self._data


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


async def test_easy_can_never_be_given_a_role_or_badge(env):
    ctx = Ctx(env)
    await env.cog.secrethunt_role.callback(env.cog, ctx, "easy", None)
    assert "no role or badge" in ctx.out[-1] and await env.conf.roles() == {}
    await env.cog.secrethunt_roleicon.callback(env.cog, ctx, "Easy")
    assert "no role or badge" in ctx.out[-1]
    await env.cog.secrethunt_role.callback(env.cog, ctx, "legendary", None)
    assert "Tier must be one of" in ctx.out[-1]


async def test_role_command_creates_the_tiers_default_role_hoisted(env):
    ctx = Ctx(env)
    await env.cog.secrethunt_role.callback(env.cog, ctx, "hard", None)
    role = env.g.created_roles[-1]
    assert role.name == "Timekeeper" and role.kw["hoist"] is True and "colour" not in role.kw  # members keep their own colour
    assert (await env.conf.roles())["hard"] == role.id
    await env.cog.secrethunt_role.callback(env.cog, ctx, "medium", env.role)  # or point it at an existing role
    assert (await env.conf.roles())["medium"] == env.role.id


async def test_roleicon_edits_the_right_tiers_role(env):
    ctx = Ctx(env)
    await env.cog.secrethunt_roleicon.callback(env.cog, ctx, "medium")
    assert "Set the role first" in ctx.out[-1]
    await env.cog.secrethunt_role.callback(env.cog, ctx, "medium", env.role)
    await env.cog.secrethunt_roleicon.callback(env.cog, ctx, "medium")
    assert "Attach" in ctx.out[-1]
    ctx.message.attachments = [Attachment(size=300 * 1024)]
    await env.cog.secrethunt_roleicon.callback(env.cog, ctx, "medium")
    assert "256 KB" in ctx.out[-1] and env.role.edits == []
    ctx.message.attachments = [Attachment()]
    await env.cog.secrethunt_roleicon.callback(env.cog, ctx, "medium")
    assert ctx.out[-1] == "Icon set." and env.role.edits[-1]["display_icon"] == b"png"
    env.role.fail_edit = True
    await env.cog.secrethunt_roleicon.callback(env.cog, ctx, "medium")
    assert "boost level" in ctx.out[-1]
    env.role.fail_edit = False


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


async def test_reset_wipes_progress_and_lets_them_start_again(env):
    a, answer = await at_final(env)
    await env.cog._on_dm(dm(a, answer))
    await env.cog.secrethunt_reset.callback(env.cog, Ctx(env), a, PID)
    assert await state(env, a) is None and (await env.conf.finishers())[PID] == []
    await env.cog._on_guild_message(chat(env, a, ".tick"))
    assert (await state(env, a))["stage"] == engine.STAGE_RIDDLE


async def test_status_shows_tier_roles_counts_and_flags_a_deleted_post(env):
    ctx = Ctx(env)
    await env.cog.secrethunt_version.callback(env.cog, ctx)
    assert ctx.out[-1].startswith("SecretHunt v")
    await start(env, member(env, 11))
    text = (await env.cog._status_embed(env.g)).description
    assert "1 started, 0 solved" in text and "easy: none (by design)" in text and "medium: not set" in text
    assert "MISSING" not in text
    del env.g.threads[env.chans[3].id]
    assert "MISSING" in (await env.cog._status_embed(env.g)).description


async def test_preview_dms_the_admin_every_variant_with_answers(env):
    ctx = Ctx(env)
    await env.cog.secrethunt_preview.callback(env.cog, ctx, PID)
    text = ctx.author.dms[-1]
    assert all(a in text for a in PUZZLES[PID]["answers"]) and text.count("Variant") == 5


async def test_the_poem_is_posted_and_pinned_and_the_old_one_comes_down(env):
    ctx = Ctx(env)
    ch = env.chans[1]
    await env.cog.secrethunt_poem.callback(env.cog, ctx, ch, "")
    first = next(m for m in ch.sent if m.embed is not None and m.pinned and "Hatter" in (m.embed.title or ""))
    poem = first.embed.description
    assert engine.acrostic(poem.strip("*").split("\n")) == "tick"
    await env.cog.secrethunt_poem.callback(env.cog, ctx, ch, "")
    pinned_poems = [m for m in ch.sent if m.embed is not None and m.pinned and "Hatter" in (m.embed.title or "")]
    assert len(pinned_poems) == 1 and pinned_poems[0] is not first and not first.pinned
    assert (await env.conf.poems())[str(ch.id)] == pinned_poems[0].id


async def test_a_failed_pin_is_reported_and_forgotten(env):
    ctx = Ctx(env)
    ch = env.chans[2]
    ch.fail_pin = True
    await env.cog.secrethunt_poem.callback(env.cog, ctx, ch, "")
    assert "could not pin" in ctx.out[-1] and str(ch.id) not in await env.conf.poems()


async def test_a_deleted_old_poem_does_not_block_the_new_one(env):
    ctx = Ctx(env)
    ch = env.chans[3]
    async with env.conf.poems() as poems:
        poems[str(ch.id)] = 999999  # a poem a mod deleted long ago
    await env.cog.secrethunt_poem.callback(env.cog, ctx, ch, "")
    assert (await env.conf.poems())[str(ch.id)] != 999999


async def test_disable_silences_triggers_reactions_and_delivery(env):
    a = member(env, 11)
    await start(env, a)
    await env.conf.enabled.set(False)
    await env.cog._on_guild_message(chat(env, member(env, 12), ".tick"))
    assert await state(env, member(env, 12)) is None
    await find_object(env, a)
    assert (await state(env, a))["stage"] == engine.STAGE_RIDDLE
