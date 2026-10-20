"""Behaviour tests for The Knave's Trial (Medium #1): the rhyme-breaker poem, witness buttons with private generated
testimony, answers by DM with a lockout (round 1) and a cooldown (round 2), the hint ladder, and the Hall's hint counts.

Run against fakes: they prove the logic and clean imports under the stub, not live Discord behaviour (persistent buttons,
ephemeral replies, pins in forum posts, relative timestamps rendering)."""
import importlib
import sys
import time
import types

import pytest

from secrethunt import _testkit as tk
from secrethunt import engine, trial
from secrethunt.constants import HINT_MAX
from secrethunt.puzzles import PUZZLES
from secrethunt.settings_panel import PanelError

PID = "knaves_trial"
TRIAL = PUZZLES[PID]
NAMES = ["Gaming", "Music", "Memes", "Movies", "Cuddle"]
SALT = "fixed-test-salt"
HOUR = 3600
T0 = 1_800_000_000.0  # an arbitrary "now"; the clock fixture owns time
MODULES = ("secrethunt.secrethunt", "secrethunt.embeds", "secrethunt.views")


@pytest.fixture
def mod(monkeypatch):
    tk.install(monkeypatch)
    for name in MODULES:
        monkeypatch.delitem(sys.modules, name, raising=False)
    cog = importlib.import_module("secrethunt.secrethunt")
    yield cog
    for name in MODULES:
        sys.modules.pop(name, None)


@pytest.fixture
def clock(monkeypatch):
    c = types.SimpleNamespace(now=T0)
    monkeypatch.setattr(time, "time", lambda: c.now)
    return c


@pytest.fixture
async def env(mod, clock):
    g = tk.FakeGuild()
    posts = [g.add_thread(100 + i, n) for i, n in enumerate(NAMES)]
    hall = g.add_channel(200, "hall-of-secrets")
    announce = g.add_channel(201, "cuddle")
    bot = tk.FakeBot(g)
    cog = mod.SecretHunt(bot)
    conf = cog.config.guild(g)
    await conf.pool.set([c.id for c in posts])
    await conf.published.set([PID])
    await conf.active.set(PID)
    await conf.salt.set(SALT)
    await conf.hall_channel_id.set(hall.id)
    await conf.announce_channel_id.set(announce.id)
    await conf.enabled.set(True)
    await cog._setup_objects(g, PID)
    return types.SimpleNamespace(mod=mod, cog=cog, g=g, chans=posts, hall=hall, announce=announce, conf=conf, bot=bot)


class Ctx:
    def __init__(self, env, author):
        self.guild, self.out, self.author, self.channel = env.g, [], author, env.chans[0]

    async def send(self, content=None, **kw):
        self.out.append(content if content is not None else kw.get("embed"))


def member(env, uid, **kw):
    return tk.FakeMember(env.g, uid, **kw)


def chat(env, author, text):
    msg = tk.FakeMessage(env.chans[0], content=text, author=author)
    msg.guild = env.g
    return msg


def dm(author, text):
    msg = tk.FakeMessage(None, content=text, author=author)
    msg.guild = None
    return msg


async def state(env, m):
    return (await env.cog.config.member(m).puzzles()).get(PID)


def rooms(env):
    return [f"<#{c.id}>" for c in env.chans]


def case_for(uid, rnd, roll=0):
    return trial.generate(trial.case_seed(SALT, PID, uid, rnd, roll), rnd)


def right(uid, rnd, roll=0):
    _s, thief, hour, _l = case_for(uid, rnd, roll)
    return trial.SUSPECTS[thief] + (f" {hour}" if rnd == 2 else "")


def wrong(uid, rnd, roll=0):
    _s, thief, hour, _l = case_for(uid, rnd, roll)
    return trial.SUSPECTS[(thief + 1) % 5] + (f" {hour}" if rnd == 2 else "")


async def start(env, m, text=".mock turtle soup"):
    await env.cog._on_guild_message(chat(env, m, text))
    return await state(env, m)


def witness_message(env, room):
    return next(m for m in env.chans[room].sent if m.kw.get("view") is not None)


async def press(env, m, room):
    inter = tk.FakeInteraction(env.g, m, witness_message(env, room))
    await env.cog.handle_witness(inter)
    assert len(inter.replies) == 1 and inter.replies[0][1] == {"ephemeral": True}  # always exactly one private reply
    return inter.replies[0][0]


async def say(env, m, text):
    """DM the bot and return what it answered (None when it stayed silent)."""
    before = len(m.dms)
    await env.cog._on_dm(dm(m, text))
    return m.dms[-1] if len(m.dms) > before else None


async def deliver(env):
    await env.cog._deliver_due(env.g, T0 + 10**9)


async def through_round_one(env, clock, uid=11):
    m = member(env, uid)
    await start(env, m)
    assert "adjourns" in await say(env, m, right(uid, 1))
    clock.now += 2 * 24 * HOUR
    await env.cog._deliver_due(env.g, clock.now)
    return m


# ---------------------------------------------------------------- the poem and the data

def test_the_poem_hides_the_trigger_in_the_end_words_that_break_the_rhyme():
    poem = TRIAL["poem"]
    assert len(poem) == 12 and TRIAL["trigger_kind"] == "breaker"
    breakers = [engine.normalize(poem[i].split()[-1]) for i in (3, 7, 11)]  # the fourth line of each verse
    assert breakers == TRIAL["breakers"] == ["mock", "turtle", "soup"]
    assert "".join(breakers) == engine.normalize(TRIAL["trigger"])
    text = engine.normalize(" ".join(poem) + TRIAL["poem_title"])
    assert engine.normalize(TRIAL["trigger"]) not in text
    assert engine.acrostic(poem) != engine.normalize(TRIAL["trigger"])  # not an acrostic: members know to look for one
    assert "".join(engine.normalize(line)[-1:] for line in poem) != engine.normalize(TRIAL["trigger"])


def test_the_poem_does_not_tell_players_what_to_do_and_gives_no_answer_away():
    body = " ".join(TRIAL["poem"]).lower() + TRIAL["poem_title"].lower()
    assert not any(w in body for w in ("type", "typing", "react", "click", "press", "command", "prefix", "say it"))
    for name in trial.SUSPECTS:  # the suspects appear in the story but the poem never says who did it
        assert "stole" not in body and "thief" not in body


def test_the_nudge_under_the_poem_says_what_to_do_without_giving_the_words_away():
    from secrethunt import embeds
    nudge = TRIAL["nudge"]
    assert "dot" in nudge.lower() and "rhyme" not in nudge.lower()  # the format is hinted; the mechanic stays a riddle
    assert not any(w in engine.normalize(nudge) for w in TRIAL["breakers"])
    assert nudge in embeds.poem_embed(TRIAL).description  # it is really printed under the poem
    rabbit = PUZZLES["late_rabbit"]
    assert embeds.poem_embed(rabbit).description == "*" + "\n".join(rabbit["poem"]) + "*"  # Easy poems are unchanged


def test_every_trial_text_formats_cleanly():
    keys = dict(rooms="<#1> <#2>", until="<t:1:R>", when="<t:1:R>", room="<#1>", text="t", hint="h", n=1,
                max=HINT_MAX, rank=" #1")
    for field in ("summons", "summons2", "round1_done", "waiting", "idle", "closed", "testimony", "recess", "wrong1",
                  "wrong2", "malformed_suspect", "malformed_hour", "hint", "hint_early", "hint_max", "solved", "already"):
        assert "{" not in TRIAL[field].format_map(keys), field


def test_the_summonses_state_the_rules_the_court_runs_by():
    one, two = TRIAL["summons"], TRIAL["summons2"]
    assert "Exactly two" in one and "tell me the name" in one and "hint" in one
    for name in ("Hatter", "Hare", "Dormouse", "Duchess", "Cook"):
        assert name in one
    assert "exactly three" in two and "the hour" in two and "1 to 12" in two  # the format is stated, not hidden
    assert "hint" in two


def test_the_trial_is_medium_and_needs_five_rooms():
    assert TRIAL["tier"] == "medium" and TRIAL["kind"] == "trial" and TRIAL["needs_pool"] == trial.WITNESSES == 5


# ---------------------------------------------------------------- setup

async def test_setup_pins_one_witness_in_every_room(env):
    for ch in env.chans:
        witness = witness_message(env, env.chans.index(ch))
        assert witness.pinned and witness.embed.title == "A witness waits here"
    assert len((await env.conf.objects())[PID]) == 5


async def test_the_trial_refuses_a_pool_that_is_not_exactly_five(env):
    await env.conf.pool.set([c.id for c in env.chans[:4]])
    for action in (env.cog._apply_setup(env.g, member(env, 1), PID), env.cog._apply_start(env.g, member(env, 1), PID)):
        with pytest.raises(PanelError, match="exactly 5"):
            await action


async def test_going_live_with_five_rooms_works(env):
    await env.conf.published.set([])
    assert "is live" in await env.cog._apply_start(env.g, member(env, 1), PID)
    assert await env.conf.published() == [PID]


# ---------------------------------------------------------------- the trigger

async def test_the_rhyme_breakers_start_the_trial_and_the_chat_message_vanishes(env):
    m = member(env, 11)
    msg = chat(env, m, ".mock turtle soup")
    await env.cog._on_guild_message(msg)
    assert msg.deleted and (await state(env, m))["stage"] == engine.STAGE_RIDDLE
    assert len(m.dms) == 1 and "gavel" in m.dms[0] and "Exactly two" in m.dms[0]
    assert all(r in m.dms[0] for r in rooms(env))  # the summons names every room a witness waits in


@pytest.mark.parametrize("text", [".mockturtlesoup", ".Mock Turtle Soup", ".mock turtle soup."])
async def test_other_spellings_of_the_trigger_work_too(env, text):
    m = member(env, 11)
    assert (await start(env, m, text))["stage"] == engine.STAGE_RIDDLE


async def test_a_dm_trigger_works_and_resending_it_repeats_the_current_round(env):
    m = member(env, 11)
    await env.cog._on_dm(dm(m, "mock turtle soup"))
    assert (await state(env, m))["stage"] == engine.STAGE_RIDDLE
    await env.cog._on_dm(dm(m, "mockturtlesoup"))
    assert len(m.dms) == 2 and m.dms[0] == m.dms[1]


@pytest.mark.parametrize("text", ["mock turtle soup", ".mock turtle", ".mock turtle soup please", ".turtle soup", "hello there"])
async def test_near_misses_do_nothing_and_are_never_deleted(env, text):
    m = member(env, 11)
    msg = chat(env, m, text)
    await env.cog._on_guild_message(msg)
    assert not msg.deleted and m.dms == [] and await state(env, m) is None  # no prefix, or not the words in order


async def test_adding_spaces_to_triggers_did_not_change_the_easy_puzzles(env):
    await env.conf.published.set([PID, "late_rabbit"])
    m = member(env, 11)
    odd = chat(env, m, ".tick tock")
    await env.cog._on_guild_message(odd)
    assert not odd.deleted and m.dms == []
    good = chat(env, m, ".tick")
    await env.cog._on_guild_message(good)
    assert good.deleted and (await env.cog.config.member(m).puzzles()).get("late_rabbit")["stage"] == engine.STAGE_RIDDLE


async def test_nothing_happens_while_the_hunt_is_disabled(env):
    await env.conf.enabled.set(False)
    m = member(env, 11)
    msg = chat(env, m, ".mock turtle soup")
    await env.cog._on_guild_message(msg)
    assert not msg.deleted and await state(env, m) is None


# ---------------------------------------------------------------- the witnesses

async def test_a_witness_ignores_someone_who_was_not_called_to_the_jury(env):
    m = member(env, 11)
    assert await press(env, m, 0) == TRIAL["idle"] and await state(env, m) is None


async def test_each_room_gives_that_rooms_witnesss_private_statement(env):
    m = member(env, 11)
    await start(env, m)
    statements, *_ = case_for(11, 1)
    for room in range(5):
        text = await press(env, m, room)
        assert text == TRIAL["testimony"].format(room=rooms(env)[room], text=trial.testimony(statements[room], rooms(env)))
        assert await press(env, m, room) == text  # asking again repeats the same testimony


async def test_two_jurors_hear_different_testimony(env):
    a, b = member(env, 11), member(env, 12)
    await start(env, a)
    await start(env, b)
    assert [await press(env, a, r) for r in range(5)] != [await press(env, b, r) for r in range(5)]


async def test_a_witness_says_nothing_while_the_court_is_adjourned_or_after_the_trial(env, clock):
    m = member(env, 11)
    await start(env, m)
    await say(env, m, right(11, 1))
    assert await press(env, m, 2) == TRIAL["closed"]
    clock.now += 2 * 24 * HOUR
    await env.cog._deliver_due(env.g, clock.now)
    await say(env, m, right(11, 2))
    assert await press(env, m, 2) == TRIAL["already"]


async def test_buttons_do_nothing_for_bots_when_disabled_or_on_unknown_messages(env):
    m = member(env, 11)
    await start(env, m)
    await env.conf.enabled.set(False)
    assert await press(env, m, 0) == "Nothing happens."
    await env.conf.enabled.set(True)
    stray = tk.FakeMessage(env.chans[0], content="not an object")
    inter = tk.FakeInteraction(env.g, m, stray)
    await env.cog.handle_witness(inter)
    assert inter.replies[0][0] == "Nothing happens."
    bot_inter = tk.FakeInteraction(env.g, member(env, 99, bot=True), witness_message(env, 0))
    await env.cog.handle_witness(bot_inter)
    assert bot_inter.replies[0][0] == "Nothing happens."


# ---------------------------------------------------------------- the whole trial

async def test_full_trial_from_poem_to_the_hall_with_no_role_for_medium(env, clock):
    async with env.conf.roles() as roles:
        roles["hard"] = 5  # a leftover role for ANOTHER tier must never leak into a Medium solve
    m = await through_round_one(env, clock)
    st = await state(env, m)
    assert st["stage"] == engine.STAGE_FINAL and st["roll"] == 0
    assert "reconvenes" in m.dms[-1] and "the hour" in m.dms[-1] and all(r in m.dms[-1] for r in rooms(env))
    statements2, *_ = case_for(11, 2)
    assert trial.testimony(statements2[1], rooms(env)) in await press(env, m, 1)  # round 2 testimony, not round 1's
    reply = await say(env, m, right(11, 2))
    assert "Hall of Secrets" in reply and "finder **#1**" in reply and "role" not in reply.lower()
    assert m.roles == []
    assert (await state(env, m))["stage"] == engine.STAGE_SOLVED
    assert env.hall.sent[-1].embed.title == "Medium \N{MIDDLE DOT} The Knave's Trial"
    assert "1. user11 \N{MIDDLE DOT} no hints" in env.hall.sent[-1].embed.description
    assert "The Knave's Trial" in env.announce.sent[-1].content and "finder #1" in env.announce.sent[-1].content


async def test_the_second_round_waits_for_morning_not_for_days(env, clock):
    m = member(env, 11)
    await start(env, m)
    await say(env, m, right(11, 1))
    st = await state(env, m)
    assert st["due_ts"] - clock.now <= 28 * HOUR  # next 9:00 Pacific, at least four hours on: never a long wait
    await env.cog._deliver_due(env.g, clock.now + HOUR)
    assert (await state(env, m))["stage"] == engine.STAGE_FOUND  # not yet
    await env.cog._deliver_due(env.g, st["due_ts"] + 1)
    assert (await state(env, m))["stage"] == engine.STAGE_FINAL


async def test_a_closed_dm_retries_the_round_two_summons_later(env, clock):
    m = member(env, 11)
    await start(env, m)
    await say(env, m, right(11, 1))
    m.dm_closed = True
    due = (await state(env, m))["due_ts"]
    await env.cog._deliver_due(env.g, due + 1)
    assert (await state(env, m))["stage"] == engine.STAGE_FOUND  # put back, not lost
    m.dm_closed = False
    await env.cog._deliver_due(env.g, due + 2 * HOUR)
    assert (await state(env, m))["stage"] == engine.STAGE_FINAL and "reconvenes" in m.dms[-1]


async def test_the_trigger_replays_whatever_step_the_member_is_on(env, clock):
    m = member(env, 11)
    await start(env, m)
    await say(env, m, right(11, 1))
    await say(env, m, "mock turtle soup")
    assert "adjourned" in m.dms[-1]
    clock.now += 2 * 24 * HOUR
    await env.cog._deliver_due(env.g, clock.now)
    await say(env, m, "mock turtle soup")
    assert "reconvenes" in m.dms[-1]
    await say(env, m, right(11, 2))
    await say(env, m, "mock turtle soup")
    assert "walks free" in m.dms[-1]


# ---------------------------------------------------------------- wrong answers

async def test_a_wrong_round_one_answer_locks_the_member_out_for_four_hours_then_rerolls(env, clock):
    m = member(env, 11)
    await start(env, m)
    first = [await press(env, m, r) for r in range(5)]
    reply = await say(env, m, wrong(11, 1))
    assert "recess" in reply and "<t:" in reply
    st = await state(env, m)
    assert st["roll"] == 1 and st["locked_until"] == clock.now + 4 * HOUR
    assert await press(env, m, 0) == TRIAL["recess"].format(until=f"<t:{int(st['locked_until'])}:R>")
    clock.now += HOUR
    assert "recess" in await say(env, m, right(11, 1, 0)) and (await state(env, m))["stage"] == engine.STAGE_RIDDLE
    clock.now += 4 * HOUR  # the recess is over: the witnesses remember it differently now
    second = [await press(env, m, r) for r in range(5)]
    assert second != first
    assert "adjourns" in await say(env, m, right(11, 1, 1))


async def test_a_wrong_round_two_answer_is_a_four_hour_cooldown_and_round_one_stays_solved(env, clock):
    m = await through_round_one(env, clock)
    reply = await say(env, m, wrong(11, 2))
    assert "<t:" in reply and "recess" in reply
    st = await state(env, m)
    assert st["stage"] == engine.STAGE_FINAL and st["locked_until"] == clock.now + 4 * HOUR and st["roll"] == 1
    clock.now += 3 * HOUR
    assert "recess" in await say(env, m, right(11, 2, 1))
    clock.now += 2 * HOUR
    assert "Hall of Secrets" in await say(env, m, right(11, 2, 1))


async def test_a_right_thief_at_the_wrong_hour_is_wrong(env, clock):
    m = await through_round_one(env, clock)
    _s, thief, hour, _l = case_for(11, 2)
    await say(env, m, f"{trial.SUSPECTS[thief]} {hour % 12 + 1}")
    assert (await state(env, m))["roll"] == 1


async def test_incomplete_answers_cost_nothing(env, clock):
    m = await through_round_one(env, clock)
    thief = trial.SUSPECTS[case_for(11, 2)[1]]
    assert "thief" in await say(env, m, thief) and "hour" in m.dms[-1]
    assert "one" in await say(env, m, "hatter hare 4")
    st = await state(env, m)
    assert st["roll"] == 0 and st["wrongs"] == 0 and not st["locked_until"]


async def test_chatter_that_names_no_suspect_gets_no_reply_and_costs_nothing(env, clock):
    m = member(env, 11)
    await start(env, m)
    assert await say(env, m, "I have no idea what is going on") is None
    assert (await state(env, m))["wrongs"] == 0


async def test_dms_from_members_with_no_open_round_are_ignored(env, clock):
    stranger = member(env, 12)
    assert await say(env, stranger, "hint") is None and await say(env, stranger, "cook") is None
    m = member(env, 11)
    await start(env, m)
    await say(env, m, right(11, 1))  # adjourned: nothing to answer or hint until the court reconvenes
    assert await say(env, m, "hint") is None and await say(env, m, "cook 4") is None


# ---------------------------------------------------------------- hints

async def test_hints_come_after_two_days_then_a_day_apart_up_to_three(env, clock):
    m = member(env, 11)
    await start(env, m)
    _s, _t, _h, liars = case_for(11, 1)
    assert "Not yet" in await say(env, m, "hint") and "<t:" in m.dms[-1]
    clock.now += 47 * HOUR
    assert "Not yet" in await say(env, m, "hint")
    clock.now += HOUR
    first = await say(env, m, "hint")
    assert "Hint 1 of 3" in first and "Hall of Secrets" in first and "telling the truth" in first
    clock.now += 23 * HOUR
    assert "Not yet" in await say(env, m, "hint")
    clock.now += HOUR
    second = await say(env, m, "hint")
    assert "Hint 2 of 3" in second and "is lying" in second
    liar_room = next(r for r in rooms(env) if r in second)
    assert rooms(env).index(liar_room) in liars
    clock.now += 24 * HOUR
    assert "Hint 3 of 3" in await say(env, m, "hint")
    clock.now += 99 * HOUR
    assert "all the help" in await say(env, m, "hint")
    assert (await state(env, m))["hints"] == 3


async def test_a_hint_only_ever_tells_the_truth(env, clock):
    m = member(env, 11)
    await start(env, m)
    _s, thief, _h, liars = case_for(11, 1)
    clock.now += 48 * HOUR
    first = await say(env, m, "hint")
    honest_room = next(r for r in rooms(env) if r in first)
    assert rooms(env).index(honest_room) not in liars
    clock.now += 24 * HOUR
    await say(env, m, "hint")
    clock.now += 24 * HOUR
    third = await say(env, m, "hint")
    assert f"the {trial.SUSPECTS[thief]}" not in third  # round 1's last hint clears someone else, never the thief


async def test_hints_are_for_the_current_testimony_and_a_reroll_does_not_refund_them(env, clock):
    m = member(env, 11)
    await start(env, m)
    clock.now += 48 * HOUR
    await say(env, m, "hint")
    await say(env, m, wrong(11, 1))
    clock.now += 13 * HOUR
    st = await state(env, m)
    assert st["hints"] == 1 and st["roll"] == 1
    assert "Not yet" in await say(env, m, "hint")  # the ladder carries on from the last hint


async def test_no_hint_during_a_recess(env, clock):
    m = member(env, 11)
    await start(env, m)
    clock.now += 60 * HOUR
    await say(env, m, wrong(11, 1))
    assert "recess" in await say(env, m, "hint")
    assert (await state(env, m))["hints"] == 0


async def test_round_two_has_its_own_hint_ladder_and_the_hall_shows_the_total(env, clock):
    m = member(env, 11)
    await start(env, m)
    clock.now += 48 * HOUR
    await say(env, m, "hint")  # one hint in round 1
    assert "adjourns" in await say(env, m, right(11, 1))
    clock.now += 2 * 24 * HOUR
    await env.cog._deliver_due(env.g, clock.now)
    assert "Not yet" in await say(env, m, "hint")  # round 2's clock started when the court reconvened
    clock.now += 48 * HOUR
    hint = await say(env, m, "hint")
    assert "Hint 1 of 3" in hint
    await say(env, m, right(11, 2))
    assert "1. user11 \N{MIDDLE DOT} 2 hints" in env.hall.sent[-1].embed.description


async def test_the_hall_tells_apart_solvers_who_needed_hints(env, clock):
    a = await through_round_one(env, clock, 11)
    await say(env, a, right(11, 2))
    b = member(env, 12)
    await start(env, b)
    clock.now += 48 * HOUR
    await say(env, b, "hint")
    await say(env, b, right(12, 1))
    clock.now += 2 * 24 * HOUR
    await env.cog._deliver_due(env.g, clock.now)
    await say(env, b, right(12, 2))
    text = env.hall.sent[-1].embed.description
    assert "1. user11 \N{MIDDLE DOT} no hints" in text and "2. user12 \N{MIDDLE DOT} 1 hint" in text


async def test_secrets_command_shows_hints_for_a_trial(env, clock):
    m = await through_round_one(env, clock)
    await say(env, m, right(11, 2))
    ctx = Ctx(env, m)
    await env.cog.secrets_cmd.callback(env.cog, ctx, m)
    text = ctx.out[-1].description
    assert "**Medium** \N{MIDDLE DOT} The Knave's Trial \N{MIDDLE DOT} finder #1 \N{MIDDLE DOT} no hints" in text


# ---------------------------------------------------------------- test mode, other puzzles, admin

async def test_test_mode_shrinks_the_waits(env, clock):
    await env.conf.test_mode.set(True)
    m = member(env, 11)
    await start(env, m)
    await say(env, m, wrong(11, 1))
    assert (await state(env, m))["locked_until"] == clock.now + 30
    clock.now += 31
    assert "Hint 1 of 3" in await say(env, m, "hint")


async def test_an_exact_easy_answer_never_costs_a_trial_lockout(env, clock):
    """Two Late Rabbit answers (hatter, dormouse) are also suspects. A member playing both, who types their Rabbit answer,
    must solve the Rabbit, not burn a wrong trial answer."""
    await env.conf.published.set([PID, "late_rabbit"])
    uid = next(u for u in range(1, 500) if engine.variant_for(SALT, "late_rabbit", u, 5) in (0, 1))
    variant = engine.variant_for(SALT, "late_rabbit", uid, 5)
    answer = PUZZLES["late_rabbit"]["answers"][variant]
    assert answer in ("hatter", "dormouse")
    m = member(env, uid)
    await start(env, m)  # the trial, round 1
    async with env.cog.config.member(m).puzzles() as states:
        st = engine.new_state(variant, clock.now)
        st["stage"] = engine.STAGE_FINAL
        states["late_rabbit"] = st
    await say(env, m, answer)
    states = await env.cog.config.member(m).puzzles()
    assert states["late_rabbit"]["stage"] == engine.STAGE_SOLVED
    assert states[PID]["roll"] == 0 and states[PID]["wrongs"] == 0 and not states[PID]["locked_until"]


async def test_a_trial_answer_is_not_a_wrong_easy_guess(env, clock):
    await env.conf.published.set([PID, "late_rabbit"])
    m = member(env, 11)
    await start(env, m)
    async with env.cog.config.member(m).puzzles() as states:
        st = engine.new_state(0, clock.now)
        st["stage"] = engine.STAGE_FINAL
        states["late_rabbit"] = st
    await say(env, m, right(11, 1))
    states = await env.cog.config.member(m).puzzles()
    assert states[PID]["stage"] == engine.STAGE_FOUND and states["late_rabbit"]["wrong_count"] == 0
    assert "walrus" not in (m.dms[-1] or "")


async def test_an_easy_wrong_guess_still_counts_when_it_is_not_a_suspect(env, clock):
    await env.conf.published.set([PID, "late_rabbit"])
    m = member(env, 11)
    await start(env, m)
    async with env.cog.config.member(m).puzzles() as states:
        st = engine.new_state(0, clock.now)
        st["stage"] = engine.STAGE_FINAL
        states["late_rabbit"] = st
    await say(env, m, "banana")
    assert (await env.cog.config.member(m).puzzles())["late_rabbit"]["wrong_count"] == 1


async def test_preview_dms_the_admin_their_own_cases_and_answers(env):
    admin = member(env, 1)
    note = await env.cog._apply_preview(env.g, admin, PID)
    assert "round 1 and round 2" in note and len(admin.dms) == 3
    assert all(f"{rooms(env)[w]}:" in admin.dms[1] for w in range(5))
    assert f"`{right(1, 1)}`" in admin.dms[1] and f"`{right(1, 2)}`" in admin.dms[2]
    assert "(exactly 2 lie)" in admin.dms[1] and "(exactly 3 lie)" in admin.dms[2]


async def test_who_shows_where_a_juror_is(env, clock):
    m = member(env, 11)
    await start(env, m)
    await say(env, m, wrong(11, 1))
    ctx = Ctx(env, member(env, 1))
    await env.cog.secrethunt_who.callback(env.cog, ctx, m)
    assert "round 1 open" in ctx.out[-1] and "re-rolls 1" in ctx.out[-1] and "wrong answers 1" in ctx.out[-1]


async def test_reset_lets_a_juror_start_over_with_fresh_state(env, clock):
    m = await through_round_one(env, clock)
    await say(env, m, right(11, 2))
    ctx = Ctx(env, member(env, 1))
    await env.cog.secrethunt_reset.callback(env.cog, ctx, m, PID)
    assert await state(env, m) is None and await env.conf.finishers() == {PID: []}
    assert ctx.out[-1] == "Reset."
    assert "user11" not in env.hall.sent[-1].embed.description
    assert (await start(env, m))["stage"] == engine.STAGE_RIDDLE


async def test_testimony_is_generated_off_the_event_loop_and_cached(env):
    trial.generate.cache_clear()
    m = member(env, 11)
    await start(env, m)
    await press(env, m, 0)
    await press(env, m, 1)
    assert trial.generate.cache_info().hits >= 1  # the second press reuses the first one's case


async def test_the_witness_button_is_persistent_with_its_own_fixed_id(env):
    """Only meaningful under real discord.py (the repo's stub View has no buttons); then it proves what the fakes cannot:
    a persistent view needs no timeout and a custom id on every button, and the id must not clash with the glass buttons."""
    await env.cog.cog_load()
    witness, glass = env.cog._witness_view, env.cog._view
    assert env.bot.views == [glass, witness]
    ids = [getattr(c, "custom_id", None) for c in getattr(witness, "children", [])]
    if not ids:
        pytest.skip("the repo's discord stub has no buttons")
    assert ids == ["secrethunt:witness"] and witness.is_persistent()
    assert not set(ids) & {getattr(c, "custom_id", None) for c in glass.children}
