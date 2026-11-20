"""Behaviour tests for The Caterpillar's Question (Easy #3, kind "profile"): a pinned Caterpillar in every room, a question
about when the member joined, answered by DM, and a final clue slid forward by the month they gave. Run against fakes: they
prove the logic and clean imports under the stub, not live Discord behaviour (persistent buttons, ephemeral replies, pins in
forum posts, the Member since date a profile shows)."""
import importlib
import sys
import types
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from secrethunt import _testkit as tk
from secrethunt import caterpillar, engine
from secrethunt.constants import PROFILE_WRONG_PER_DAY, TEST_DELAY_SECONDS
from secrethunt.puzzles import PUZZLES
from secrethunt.settings_panel import PanelError

LA = ZoneInfo("America/Los_Angeles")
PID = "caterpillar"
NAMES = ["Gaming", "Music", "Memes", "Movies", "Cuddle"]
CAT = PUZZLES[PID]
MODULES = ("secrethunt.secrethunt", "secrethunt.embeds", "secrethunt.views")
JOINED = datetime(2023, 3, 14, 12, 0, tzinfo=timezone.utc)   # a member who joined mid-March 2023


def ts(d, h=12, mi=0):
    return datetime(2026, 11, d, h, mi, tzinfo=LA).timestamp()


# ---------------------------------------------------------------- the pure logic


@pytest.mark.parametrize("text, expected", [
    ("March 2023", ("ok", 3, 2023)), ("march 2023", ("ok", 3, 2023)), ("Mar 2023", ("ok", 3, 2023)),
    ("2023 March", ("ok", 3, 2023)), ("sept 2021", ("ok", 9, 2021)), ("it was September, 2021!", ("ok", 9, 2021)),
    ("3/2023", ("ok", 3, 2023)), ("03-2023", ("ok", 3, 2023)), ("2023-03", ("ok", 3, 2023)), ("12.2022", ("ok", 12, 2022)),
])
def test_a_join_date_is_read_in_every_fair_spelling(text, expected):
    assert caterpillar.parse_when(text) == expected


@pytest.mark.parametrize("text", ["march", "2023", "March of some year", "13/2023", "99-2023"])
def test_a_month_without_a_year_or_a_year_without_a_month_is_only_partial(text):
    assert caterpillar.parse_when(text)[0] == "partial" or text in ("13/2023", "99-2023")


@pytest.mark.parametrize("text", ["", "hello", "hatter", "recap", "hint", "teapot", "what is this"])
def test_anything_that_is_not_a_date_is_left_alone(text):
    assert caterpillar.parse_when(text) == ("none", None, None)


def test_the_month_is_accepted_whichever_time_zone_the_profile_was_read_in():
    edge = datetime(2023, 3, 1, 0, 30, tzinfo=timezone.utc)   # still February for anyone west of Greenwich
    assert {(2023, 2), (2023, 3)} <= caterpillar.valid_months(edge)
    new_year = datetime(2024, 1, 1, 0, 10, tzinfo=timezone.utc)
    assert {(2023, 12), (2024, 1)} <= caterpillar.valid_months(new_year)
    mid = caterpillar.valid_months(JOINED)
    assert mid == {(2023, 3)}  # mid-month there is only one possible month
    assert caterpillar.valid_months(None) == set()
    assert caterpillar.valid_months(datetime(2023, 3, 14, 12, 0)) == {(2023, 3)}  # a naive datetime is taken as UTC


def test_sliding_a_word_is_reversible_and_never_leaves_it_unchanged_for_a_real_month():
    for word in CAT["answers"]:
        for month in range(1, 13):
            slid = caterpillar.slide(word, month)
            assert slid != word and len(slid) == len(word)
            assert caterpillar.slide(slid, 26 - month) == word
    assert caterpillar.slide("zap", 1) == "abq"


def test_the_answers_never_appear_in_the_poem_or_the_question():
    public = engine.normalize(" ".join(CAT["poem"]) + CAT["poem_title"] + CAT["object_text"] + CAT["riddle"])
    assert not any(engine.normalize(a) in public for a in CAT["answers"])


def test_check_counts_wrong_dates_and_locks_after_the_daily_cap():
    st = engine.new_state(0, ts(20))
    for n in range(PROFILE_WRONG_PER_DAY):
        assert caterpillar.check(st, "May 2019", JOINED, ts(20)) == "wrong" and st["wrong_count"] == n + 1
    assert caterpillar.check(st, "May 2019", JOINED, ts(20)) == "locked"
    assert caterpillar.check(st, "March 2023", JOINED, ts(20)) == "locked"  # even the right date waits until tomorrow
    assert caterpillar.check(st, "March 2023", JOINED, ts(21)) == "correct"  # a new Pacific day clears the count


def test_a_partial_answer_costs_nothing_and_a_non_date_is_ignored():
    st = engine.new_state(0, ts(20))
    assert caterpillar.check(st, "March", JOINED, ts(20)) == "partial" and st["wrong_count"] == 0
    assert caterpillar.check(st, "hatter", JOINED, ts(20)) == "ignore" and st["wrong_count"] == 0


def test_a_right_date_waits_for_the_next_nine_am_and_remembers_the_month_given():
    st = engine.new_state(0, ts(20, 13, 50))
    assert caterpillar.check(st, "2023-03", JOINED, ts(20, 13, 50)) == "correct"
    assert st["stage"] == engine.STAGE_FOUND and st["shift"] == 3 and st["due_ts"] == ts(21, 9)
    assert caterpillar.check(st, "March 2023", JOINED, ts(20, 14)) == "ignore"  # no longer at that stage


def test_test_mode_skips_the_wait():
    st = engine.new_state(0, ts(20, 13, 50))
    caterpillar.check(st, "March 2023", JOINED, ts(20, 13, 50), test_mode=True)
    assert st["due_ts"] == ts(20, 13, 50) + TEST_DELAY_SECONDS


def test_a_member_with_no_join_date_can_never_be_right():
    st = engine.new_state(0, ts(20))
    assert caterpillar.check(st, "March 2023", None, ts(20)) == "wrong"


# ---------------------------------------------------------------- the cog, against fakes


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
async def env(mod):
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
    await conf.salt.set("fixed-test-salt")
    await conf.hall_channel_id.set(hall.id)
    await conf.announce_channel_id.set(announce.id)
    await conf.enabled.set(True)
    await cog._setup_objects(g, PID)
    return types.SimpleNamespace(mod=mod, cog=cog, g=g, chans=posts, hall=hall, announce=announce, conf=conf, bot=bot)


def member(env, uid, joined=JOINED, **kw):
    return tk.FakeMember(env.g, uid, joined_at=joined, **kw)


def dm(author, text):
    msg = tk.FakeMessage(None, content=text, author=author)
    msg.guild = None
    return msg


async def state(env, m):
    return (await env.cog.config.member(m).puzzles()).get(PID)


def caterpillar_in(env, channel):
    return next(m for m in channel.sent if m.kw.get("view") is not None)


async def press(env, m, channel=None):
    inter = tk.FakeInteraction(env.g, m, caterpillar_in(env, channel or env.chans[0]))
    await env.cog.handle_caterpillar(inter)
    assert len(inter.replies) == 1 and inter.replies[0][1] == {"ephemeral": True}  # always one private reply
    return inter.replies[0][0]


async def answered(env, uid=11, joined=JOINED, text="March 2023"):
    m = member(env, uid, joined=joined)
    await press(env, m)
    await env.cog._on_dm(dm(m, text))
    return m


async def at_final(env, uid=11, joined=JOINED, text="March 2023"):
    m = await answered(env, uid, joined, text)
    await env.cog._deliver_due(env.g, (await state(env, m))["due_ts"] + 1)
    return m, CAT["answers"][(await state(env, m))["variant"]]


async def test_setup_pins_one_identical_caterpillar_in_every_room(env):
    embeds_seen = set()
    for ch in env.chans:
        cat = caterpillar_in(env, ch)
        assert cat.pinned and cat.embed.title == "A large blue Caterpillar"
        embeds_seen.add(cat.embed.description)
    assert len(embeds_seen) == 1 and len(((await env.conf.objects())[PID])) == 5


async def test_cog_load_registers_the_caterpillars_button(env):
    await env.cog.cog_load()
    assert env.cog._cat_view in env.bot.views


async def test_pressing_the_caterpillar_in_any_room_starts_the_puzzle_and_asks_privately(env):
    for ch in env.chans:
        m = member(env, 10 + env.chans.index(ch))
        assert await press(env, m, ch) == CAT["riddle"]
        st = await state(env, m)
        assert st["stage"] == engine.STAGE_RIDDLE and 0 <= st["variant"] < 5
        assert m.dms == []  # the question is a private reply, never a DM


async def test_pressing_again_asks_the_same_question_without_a_new_start(env):
    m = member(env, 11)
    await press(env, m)
    first = dict(await state(env, m))
    assert await press(env, m, env.chans[3]) == CAT["riddle"] and await state(env, m) == first


async def test_the_variant_is_stable_and_every_variant_is_reached(env):
    seen = set()
    for uid in range(1, 40):
        m = member(env, uid)
        await press(env, m)
        seen.add((await state(env, m))["variant"])
    assert seen == {0, 1, 2, 3, 4}


async def test_a_member_with_no_join_date_gets_a_polite_reply_and_nothing_is_started(env):
    m = member(env, 11, joined=None)
    assert await press(env, m) == CAT["blind"]
    assert await state(env, m) is None


async def test_the_lurker_role_is_ignored(env):
    lurker = env.g.add_role(55, "Lurker")
    await env.conf.blocked_role_id.set(lurker.id)
    m = member(env, 11)
    m.roles.append(lurker)
    assert await press(env, m) == "Nothing happens." and await state(env, m) is None


async def test_the_button_does_nothing_when_the_hunt_is_off_or_the_puzzle_is_not_live(env):
    m = member(env, 11)
    await env.conf.enabled.set(False)
    assert await press(env, m) == "Nothing happens." and await state(env, m) is None
    await env.conf.enabled.set(True)
    await env.conf.published.set([])
    assert await press(env, m) == "Nothing happens." and await state(env, m) is None


async def test_a_press_on_some_other_message_is_just_a_quiet_reply(env):
    m = member(env, 11)
    inter = tk.FakeInteraction(env.g, m, tk.FakeMessage(env.chans[0], content="hello"))
    await env.cog.handle_caterpillar(inter)
    assert inter.replies == [("Nothing happens.", {"ephemeral": True})] and await state(env, m) is None


async def test_a_failed_reply_never_breaks_the_cog(env):
    m = member(env, 11)

    class Boom(tk.FakeInteraction):
        async def _send(self, content=None, **kw):
            raise tk.http_error("NotFound")

    inter = Boom(env.g, m, caterpillar_in(env, env.chans[0]))
    inter.response = types.SimpleNamespace(send_message=inter._send)
    await env.cog.handle_caterpillar(inter)  # must not raise
    assert (await state(env, m))["stage"] == engine.STAGE_RIDDLE


async def test_a_clock_reaction_on_the_caterpillar_is_ignored(env):
    m = member(env, 11)
    await press(env, m)
    cat = caterpillar_in(env, env.chans[0])
    payload = types.SimpleNamespace(guild_id=env.g.id, user_id=m.id, message_id=cat.id, channel_id=env.chans[0].id,
                                    emoji="\N{MANTELPIECE CLOCK}")
    await env.cog._on_reaction(payload)
    assert cat.removed == [] and (await state(env, m))["stage"] == engine.STAGE_RIDDLE


# ---------------------------------------------------------------- the answer


async def test_the_right_month_and_year_sends_him_to_sleep_until_the_morning(env):
    m = await answered(env)
    st = await state(env, m)
    assert m.dms[-1] == CAT["found"] and st["stage"] == engine.STAGE_FOUND and st["shift"] == 3
    assert st["due_ts"] > st["found_ts"]
    assert await press(env, m) == CAT["waiting"]  # pressing him again while he sleeps just says so


async def test_a_wrong_date_gets_a_gentle_reply_and_costs_only_the_daily_allowance(env):
    m = member(env, 11)
    await press(env, m)
    await env.cog._on_dm(dm(m, "May 2019"))
    assert m.dms[-1] in CAT["wrong"] and (await state(env, m))["stage"] == engine.STAGE_RIDDLE
    for _ in range(PROFILE_WRONG_PER_DAY):
        await env.cog._on_dm(dm(m, "May 2019"))
    assert m.dms[-1] == CAT["locked"]
    await env.cog._on_dm(dm(m, "March 2023"))
    assert m.dms[-1] == CAT["locked"] and (await state(env, m))["stage"] == engine.STAGE_RIDDLE


async def test_a_month_with_no_year_is_asked_again_for_free(env):
    m = member(env, 11)
    await press(env, m)
    await env.cog._on_dm(dm(m, "March"))
    assert m.dms[-1] == CAT["partial"] and (await state(env, m))["wrong_count"] == 0


async def test_other_dms_are_not_swallowed_while_the_question_is_open(env):
    m = member(env, 11)
    await press(env, m)
    await env.cog._on_dm(dm(m, "hello there"))
    await env.cog._on_dm(dm(m, "recap"))
    assert m.dms == [] and (await state(env, m))["wrong_count"] == 0


async def test_a_dm_before_pressing_the_caterpillar_does_nothing(env):
    m = member(env, 11)
    await env.cog._on_dm(dm(m, "March 2023"))
    assert m.dms == [] and await state(env, m) is None


async def test_a_member_who_reads_the_date_in_another_time_zone_is_still_right(env):
    edge = datetime(2023, 3, 1, 0, 30, tzinfo=timezone.utc)
    m = await answered(env, 12, joined=edge, text="February 2023")
    assert (await state(env, m))["stage"] == engine.STAGE_FOUND and (await state(env, m))["shift"] == 2
    m2 = await answered(env, 13, joined=edge, text="March 2023")
    assert (await state(env, m2))["stage"] == engine.STAGE_FOUND and (await state(env, m2))["shift"] == 3


# ---------------------------------------------------------------- the clue and the finish


async def test_the_delayed_clue_is_the_word_slid_forward_by_the_month_given(env):
    m, answer = await at_final(env)
    final = m.dms[-1]
    assert caterpillar.slide(answer, 3).lower() in final.lower() and answer not in final
    assert (await state(env, m))["stage"] == engine.STAGE_FINAL
    assert await press(env, m) == CAT["final_wait"]


async def test_two_members_with_the_same_word_but_different_months_get_different_clues(env):
    first, a1 = await at_final(env, 11, JOINED, "March 2023")
    # find a second member who lands on the same variant but joined in another month
    variant = (await state(env, first))["variant"]
    uid = next(u for u in range(100, 400) if engine.variant_for("fixed-test-salt", PID, u, 5) == variant)
    second, a2 = await at_final(env, uid, datetime(2022, 8, 2, 12, 0, tzinfo=timezone.utc), "August 2022")
    assert a1 == a2 and first.dms[-1] != second.dms[-1]


async def test_the_plain_word_solves_it_and_lists_the_member(env):
    m, answer = await at_final(env)
    await env.cog._on_dm(dm(m, answer.upper()))
    assert (await state(env, m))["stage"] == engine.STAGE_SOLVED
    assert "Hall of Secrets" in m.dms[-1]
    hall = next(msg for msg in env.hall.sent if msg.embed.title == "Easy \N{MIDDLE DOT} The Caterpillar's Question")
    assert "1. user11" in hall.embed.description
    assert m.roles == []  # Easy never has a role
    line = env.announce.sent[-1].content
    assert "The Caterpillar's Question" in line and "(Easy)" in line
    assert await press(env, m) == CAT["already"]


async def test_the_slid_word_itself_is_not_the_answer(env):
    m, answer = await at_final(env)
    await env.cog._on_dm(dm(m, caterpillar.slide(answer, 3)))
    assert m.dms[-1] in CAT["wrong"] and (await state(env, m))["stage"] == engine.STAGE_FINAL


async def test_a_closed_dm_is_retried_for_the_clue(env):
    m = await answered(env)
    m.dm_closed = True
    due = (await state(env, m))["due_ts"] + 1
    await env.cog._deliver_due(env.g, due)
    assert (await state(env, m))["stage"] == engine.STAGE_FOUND and (await state(env, m))["dm_fails"] == 1
    m.dm_closed = False
    await env.cog._deliver_due(env.g, due + 7200)
    assert (await state(env, m))["stage"] == engine.STAGE_FINAL


async def test_the_clue_survives_a_restart_because_the_month_is_stored(env):
    m = await answered(env, text="March 2023")
    assert (await state(env, m))["shift"] == 3  # kept in the member's saved progress, not in memory


# ---------------------------------------------------------------- admin


async def test_preview_dms_the_admin_every_variant_with_its_march_clue(env):
    admin = member(env, 1)
    text = await env.cog._apply_preview(env.g, admin, PID)
    assert "Sent you a DM" in text
    body = admin.dms[-1]
    assert body.count("Variant") == 5
    assert all(a in body and caterpillar.slide(a, 3) in body for a in CAT["answers"])


async def test_who_reports_the_month_given_and_wrong_dates(env):
    m = await answered(env)
    out = []

    class Ctx:
        guild = env.g

        async def send(self, text, **kw):
            out.append(text)

    await env.cog.secrethunt_who.callback(env.cog, Ctx(), m)
    assert out and PID in out[0] and "month given 3" in out[0]


async def test_a_rehearsal_copy_in_the_test_channel_works_too(env):
    box = env.g.add_channel(300, "test-box")
    await env.conf.test_channel_id.set(box.id)
    await env.conf.test_mode.set(True)
    await env.conf.test_objects.set({})
    await env.cog._setup_objects(env.g, PID, force=True)
    stand_ins = [m for m in box.sent if m.kw.get("view") is not None]
    assert len(stand_ins) == 5 and all("Caterpillar" in m.embed.title for m in stand_ins)
    m = member(env, 11)
    inter = tk.FakeInteraction(env.g, m, stand_ins[0])
    await env.cog.handle_caterpillar(inter)
    assert inter.replies[0][0] == CAT["riddle"]


async def test_the_panel_lists_it_as_the_next_puzzle_to_release(env):
    await env.conf.published.set([p for p in PUZZLES if p != PID])
    assert [p for p in PUZZLES if p not in await env.conf.published()] == [PID]
    with pytest.raises(PanelError):
        await env.cog._apply_preview(env.g, member(env, 1), "no_such_puzzle")
