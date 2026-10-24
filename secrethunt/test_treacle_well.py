"""Behaviour tests for The Treacle Well (Medium #2), push 1: the poem reactions (stage 1), the three sisters (stage 2),
trail setup, the lurker block and the admins-only guard while the puzzle is unfinished.

Run against fakes: they prove the logic and clean imports under the stub, not live Discord behaviour (persistent buttons,
ephemeral replies, reaction removal, pins in forum posts, archived-thread behaviour, relative timestamps rendering)."""
import importlib
import sys
import time
import types

import pytest

from secrethunt import _testkit as tk
from secrethunt import engine, trail
from secrethunt.constants import TRAIL_REST_MINUTES, TRAIL_WRONG_LOOKS
from secrethunt.puzzles import PUZZLES
from secrethunt.settings_panel import PanelError

PID = "treacle_well"
WELL = PUZZLES[PID]
NAMES = ["gaming", "music", "memes", "movies", "anime", "books", "food", "pets", "tech", "sports", "travel", "photography"]
SALT = "fixed-test-salt"
T0 = 1_800_000_000.0
TEA, HONEY, SLEEP = "\N{HOT BEVERAGE}", "\N{HONEY POT}", "\N{SLEEPING FACE}"
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
async def env(mod, clock, monkeypatch):
    monkeypatch.setitem(mod.PUZZLES, PID, {**WELL, "wip": False})  # most tests play the finished-puzzle rules
    g = tk.FakeGuild()
    rooms = [g.add_thread(100 + i, n) for i, n in enumerate(NAMES)]
    pool = [g.add_thread(300 + i, n) for i, n in enumerate(["gaming", "music", "memes"])]
    poem = g.add_channel(400, "secrets")
    hall = g.add_channel(200, "hall-of-secrets")
    bot = tk.FakeBot(g)
    cog = mod.SecretHunt(bot)
    conf = cog.config.guild(g)
    await conf.pool.set([c.id for c in pool])
    await conf.trail.set([c.id for c in rooms])
    await conf.published.set([PID])
    await conf.active.set(PID)
    await conf.salt.set(SALT)
    await conf.hall_channel_id.set(hall.id)
    await conf.enabled.set(True)
    await cog._setup_objects(g, PID)
    admin = tk.FakeMember(g, 1, name="admin")
    admin.guild_permissions = types.SimpleNamespace(administrator=True, manage_guild=True)
    await cog._apply_poem(g, admin, poem, PID)
    return types.SimpleNamespace(mod=mod, cog=cog, g=g, rooms=rooms, pool=pool, poem=poem, hall=hall, conf=conf, bot=bot,
                                 admin=admin, clock=clock)


def member(env, uid, **kw):
    return tk.FakeMember(env.g, uid, **kw)


def dm(author, text):
    msg = tk.FakeMessage(None, content=text, author=author)
    msg.guild = None
    return msg


async def state(env, m):
    return (await env.cog.config.member(m).puzzles()).get(PID)


def poem_message(env):
    return next(m for m in env.poem.sent if m.pinned)


def plan_of(env, uid):
    return trail.plan(trail.seed(SALT, PID, uid), len(env.rooms))


async def react(env, m, emoji, mid=None):
    payload = types.SimpleNamespace(guild_id=env.g.id, user_id=m.id, message_id=mid or poem_message(env).id,
                                    channel_id=env.poem.id, emoji=emoji)
    await env.cog._on_reaction(payload)
    env.clock.now += 1


async def wake(env, m):
    for emoji in (TEA, HONEY, SLEEP):
        await react(env, m, emoji)


def table(env, room):
    return next(msg for msg in env.rooms[room].sent if msg.kw.get("view") is not None)


async def look(env, m, room, key="look"):
    inter = tk.FakeInteraction(env.g, m, table(env, room))
    await env.cog.handle_trail(inter, key)
    assert len(inter.replies) == 1 and inter.replies[0][1] == {"ephemeral": True}  # always exactly one private reply
    return inter.replies[0][0]


async def say(env, m, text):
    before = len(m.dms)
    await env.cog._on_dm(dm(m, text))
    return m.dms[-1] if len(m.dms) > before else None


# ---------------------------------------------------------------- setup

async def test_setup_pins_one_tea_table_with_buttons_in_each_trail_room_and_none_in_the_pool(env):
    for room in env.rooms:
        tables = [m for m in room.sent if m.kw.get("view") is not None]
        assert len(tables) == 1 and tables[0].pinned and tables[0].embed.title == "A tea table"
    assert all(not any(m.kw.get("view") is not None for m in c.sent) for c in env.pool)
    objects = (await env.conf.objects())[PID]
    assert sorted(objects) == sorted(str(r.id) for r in env.rooms)


async def test_the_tea_table_is_identical_in_every_room(env):
    assert len({(table(env, i).embed.title, table(env, i).embed.description) for i in range(12)}) == 1


async def test_setup_reopens_an_archived_trail_room_first(env):
    env.rooms[3].archived = True
    await env.cog._setup_objects(env.g, PID, force=True)
    assert env.rooms[3].archived is False and any(m.kw.get("view") for m in env.rooms[3].sent)


async def test_the_poem_is_pinned_and_remembered_as_a_trail_poem(env):
    poem = poem_message(env)
    assert (await env.conf.poem_ids()) == {str(poem.id): PID}
    assert "Dormouse" in poem.embed.title or "Dormouse" in poem.embed.description
    assert WELL["nudge"] in poem.embed.description


async def test_a_new_poem_in_the_same_channel_forgets_the_old_one(env):
    old = poem_message(env).id
    await env.cog._apply_poem(env.g, env.admin, env.poem, PID)
    ids = await env.conf.poem_ids()
    assert str(old) not in ids and list(ids.values()) == [PID]


# ---------------------------------------------------------------- stage 1

async def test_every_reaction_on_the_poem_is_removed_right_or_wrong(env):
    m = member(env, 11)
    await react(env, m, TEA)
    await react(env, m, "\N{FIRE}")
    await react(env, m, HONEY)
    removed = [e for e, _u in poem_message(env).removed]
    assert removed == [TEA, "\N{FIRE}", HONEY] and await state(env, m) is None


async def test_the_three_images_in_order_start_the_trail_and_dm_the_first_clue(env):
    m = member(env, 11)
    await wake(env, m)
    st = await state(env, m)
    assert st and st["stage"] == engine.STAGE_RIDDLE and st["step"] == 2 and st["found"] == 0
    assert len(m.dms) == 1 and "Elsie" in m.dms[0] and "recap" in m.dms[0]
    first = plan_of(env, 11)["chain"][0]
    assert env.rooms[first].name == NAMES[first]
    pointer = engine.pointer_for(WELL, NAMES[first], 0)
    assert pointer in m.dms[0]
    assert [e for e, _u in poem_message(env).removed] == [TEA, HONEY, SLEEP]  # even the winning reactions vanish


async def test_the_wrong_order_does_not_and_a_slow_player_does_not(env):
    a, b = member(env, 11), member(env, 12)
    for emoji in (HONEY, TEA, SLEEP):
        await react(env, a, emoji)
    await react(env, b, TEA)
    env.clock.now += 700
    await react(env, b, HONEY)
    await react(env, b, SLEEP)
    assert await state(env, a) is None and await state(env, b) is None and not a.dms and not b.dms


async def test_two_players_do_not_share_a_reaction_history(env):
    a, b = member(env, 11), member(env, 12)
    await react(env, a, TEA)
    await react(env, b, HONEY)
    await react(env, a, HONEY)
    await react(env, b, SLEEP)
    await react(env, a, SLEEP)
    assert await state(env, a) and await state(env, b) is None


async def test_waking_the_dormouse_twice_does_not_restart_or_resend(env):
    m = member(env, 11)
    await wake(env, m)
    first = await state(env, m)
    await wake(env, m)
    assert await state(env, m) == first and len(m.dms) == 1


async def test_reactions_on_other_messages_and_the_hunt_being_off_are_left_alone(env):
    m = member(env, 11)
    other = await env.poem.send("hello")
    for emoji in (TEA, HONEY, SLEEP):
        await react(env, m, emoji, mid=other.id)
    assert await state(env, m) is None and other.removed == []
    await env.conf.enabled.set(False)
    await wake(env, m)
    assert await state(env, m) is None and poem_message(env).removed == []


async def test_bots_and_the_bots_own_reactions_are_ignored(env):
    bot = member(env, 12, bot=True)
    await wake(env, bot)
    assert await state(env, bot) is None
    me = types.SimpleNamespace(guild_id=env.g.id, user_id=999, message_id=poem_message(env).id, channel_id=env.poem.id, emoji=TEA)
    await env.cog._on_reaction(me)
    assert poem_message(env).removed == []


async def test_a_reaction_flood_is_capped(env):
    m = member(env, 11)
    for _ in range(40):
        await react(env, m, "\N{FIRE}")
    env.clock.now += 5
    await wake(env, m)
    assert await state(env, m) is None  # the cap swallowed the winning three
    env.clock.now += 3700
    await wake(env, m)
    assert await state(env, m)


async def test_an_unset_trail_stays_silent(env):
    await env.conf.trail.set([])
    m = member(env, 11)
    await wake(env, m)
    assert await state(env, m) is None and not m.dms


# ---------------------------------------------------------------- stage 2

async def test_the_whole_chain_of_three_sisters(env):
    m = member(env, 11)
    await wake(env, m)
    p = plan_of(env, 11)
    lines = []
    for k, room in enumerate(p["chain"]):
        lines.append(await look(env, m, room))
        assert str(p["numbers"][k]) in lines[-1] and p["tokens"][k] in lines[-1] and WELL["wrong_look"][0] not in lines[-1]
    st = await state(env, m)
    assert st["found"] == 3 and st["step"] == 3 and st["stage"] == engine.STAGE_RIDDLE
    assert engine.pointer_for(WELL, NAMES[p["chain"][1]], 1) in lines[0]  # each line names the next room
    assert engine.pointer_for(WELL, NAMES[p["chain"][2]], 2) in lines[1]


async def test_recap_resends_the_current_clue(env):
    m = member(env, 11)
    assert await say(env, m, "recap") is None  # nothing to recap before the trail starts
    await wake(env, m)
    first = m.dms[-1]
    assert await say(env, m, "recap") == first
    p = plan_of(env, 11)
    line = await look(env, m, p["chain"][0])
    assert await say(env, m, ".recap") == line  # the newest clue, and the prefix is tolerated
    await look(env, m, p["chain"][1])
    await look(env, m, p["chain"][2])
    assert await say(env, m, "recap") == WELL["stage3_intro"]


async def test_a_wrong_table_says_so_privately_and_costs_nothing_until_the_third(env):
    m = member(env, 11)
    await wake(env, m)
    p = plan_of(env, 11)
    wrong = next(r for r in range(12) if r not in p["chain"])
    for i in range(TRAIL_WRONG_LOOKS - 1):
        assert await look(env, m, wrong) in WELL["wrong_look"]
    assert "quiet" in await look(env, m, wrong)
    assert "quiet" in await look(env, m, p["chain"][0])  # even the right table rests
    env.clock.now += TRAIL_REST_MINUTES * 60 + 1
    assert "Elsie" in await look(env, m, p["chain"][0]) or str(p["numbers"][0]) in await look(env, m, p["chain"][0])


async def test_test_mode_shortens_the_rest(env):
    await env.conf.test_mode.set(True)
    m = member(env, 11)
    await wake(env, m)
    p = plan_of(env, 11)
    wrong = next(r for r in range(12) if r not in p["chain"])
    for _ in range(TRAIL_WRONG_LOOKS):
        await look(env, m, wrong)
    env.clock.now += 31
    assert str(p["numbers"][0]) in await look(env, m, p["chain"][0])


async def test_looking_before_waking_the_dormouse_and_pressing_other_buttons(env):
    m = member(env, 11)
    assert await look(env, m, 0) == WELL["idle"]
    assert await state(env, m) is None
    await wake(env, m)
    for key in ("pour", "bow", "toast"):
        assert await look(env, m, 0, key) == WELL["quiet"]
    assert (await state(env, m))["found"] == 0


async def test_a_found_sister_replays_in_any_order(env):
    m = member(env, 11)
    await wake(env, m)
    p = plan_of(env, 11)
    first = await look(env, m, p["chain"][0])
    assert await look(env, m, p["chain"][0]) == first
    assert (await state(env, m))["found"] == 1


async def test_members_get_different_chains(env):
    chains = set()
    for uid in range(100, 140):
        chains.add(tuple(plan_of(env, uid)["chain"]))
    assert len(chains) > 25


async def test_a_press_on_a_message_that_is_not_a_tea_table_does_nothing(env):
    m = member(env, 11)
    await wake(env, m)
    stray = await env.rooms[0].send("hi")
    inter = tk.FakeInteraction(env.g, m, stray)
    await env.cog.handle_trail(inter, "look")
    assert inter.replies == [("Nothing happens.", {"ephemeral": True})]


async def test_a_broken_press_still_gets_a_private_answer(env, monkeypatch):
    m = member(env, 11)

    async def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(env.cog, "_trail_text", boom)
    inter = tk.FakeInteraction(env.g, m, table(env, 0))
    await env.cog.handle_trail(inter, "look")
    assert inter.replies == [("Nothing happens.", {"ephemeral": True})]


# ---------------------------------------------------------------- the lurker block

async def test_a_blocked_member_is_ignored_everywhere(env):
    lurker = env.g.add_role(55, "Lurker")
    await env.cog._apply_blocked_role(env.g, env.admin, lurker)
    m = member(env, 11)
    m.roles.append(lurker)
    await wake(env, m)
    assert await state(env, m) is None and not m.dms
    assert [e for e, _u in poem_message(env).removed] == [TEA, HONEY, SLEEP]  # the poem still looks untouched
    m.roles.remove(lurker)
    await wake(env, m)
    assert await state(env, m) and m.dms
    m.roles.append(lurker)
    before = len(m.dms)
    assert await say(env, m, "recap") is None and len(m.dms) == before
    assert await look(env, m, plan_of(env, 11)["chain"][0]) == "Nothing happens."
    assert (await state(env, m))["found"] == 0  # their progress just waits
    m.roles.remove(lurker)
    assert str(plan_of(env, 11)["numbers"][0]) in await look(env, m, plan_of(env, 11)["chain"][0])


async def test_a_blocked_member_is_ignored_by_the_older_puzzles_too(env):
    rabbit = PUZZLES["late_rabbit"]
    await env.conf.published.set([PID, "late_rabbit"])
    lurker = env.g.add_role(55, "Lurker")
    await env.conf.blocked_role_id.set(lurker.id)
    m = member(env, 11)
    m.roles.append(lurker)
    msg = tk.FakeMessage(env.rooms[0], content=f".{rabbit['trigger']}", author=m)
    msg.guild = env.g
    await env.cog._on_guild_message(msg)
    assert msg.deleted and not m.dms and await env.cog.config.member(m).puzzles() == {}  # the word still vanishes
    await env.cog._on_dm(dm(m, rabbit["trigger"]))
    assert not m.dms and await env.cog.config.member(m).puzzles() == {}
    m.roles.clear()
    await env.cog._on_dm(dm(m, rabbit["trigger"]))
    assert m.dms and (await env.cog.config.member(m).puzzles()).get("late_rabbit")


async def test_the_blocked_role_can_be_set_and_cleared(env):
    lurker = env.g.add_role(55, "Lurker")
    assert "Lurker" not in await env.cog._apply_blocked_role(env.g, env.admin, None)
    assert await env.conf.blocked_role_id() is None
    await env.cog._apply_blocked_role(env.g, env.admin, lurker)
    assert await env.conf.blocked_role_id() == 55
    await env.cog._apply_blocked_role(env.g, env.admin, None)
    assert await env.conf.blocked_role_id() is None


# ---------------------------------------------------------------- work in progress: admins only

async def test_while_unfinished_only_admins_can_play(env, monkeypatch):
    monkeypatch.setitem(env.mod.PUZZLES, PID, {**WELL, "wip": True})
    m = member(env, 11)
    await wake(env, m)
    assert await state(env, m) is None and not m.dms
    assert [e for e, _u in poem_message(env).removed] == [TEA, HONEY, SLEEP]
    await wake(env, env.admin)
    assert await state(env, env.admin) and env.admin.dms
    mgr = member(env, 12)
    mgr.guild_permissions = types.SimpleNamespace(administrator=False, manage_guild=True)
    await wake(env, mgr)
    assert await state(env, mgr)


# ---------------------------------------------------------------- panel and commands

async def test_trail_rooms_from_pasted_mentions_and_ids(env):
    ok = await env.cog._apply_trail_text(env.g, env.admin, {"rooms": " ".join(f"<#{r.id}>" for r in env.rooms[:8])})
    assert "(8)" in ok and (await env.conf.trail()) == [r.id for r in env.rooms[:8]]
    assert PID not in await env.conf.objects()  # the old tea tables no longer match
    await env.cog._apply_trail_text(env.g, env.admin, {"rooms": ", ".join(str(r.id) for r in env.rooms)})
    assert len(await env.conf.trail()) == 12


@pytest.mark.parametrize("text, message", [
    ("<#1> <#2>", "couldn't find"),
    ("", "different rooms"),
])
async def test_bad_trail_input_changes_nothing(env, text, message):
    before = await env.conf.trail()
    with pytest.raises(PanelError, match=message):
        await env.cog._apply_trail_text(env.g, env.admin, {"rooms": text})
    assert await env.conf.trail() == before


async def test_duplicate_rooms_count_once_and_too_many_are_refused(env):
    with pytest.raises(PanelError):
        await env.cog._apply_trail_text(env.g, env.admin, {"rooms": f"<#{env.rooms[0].id}> <#{env.rooms[0].id}>"})
    extra = [env.g.add_thread(900 + i, f"x{i}") for i in range(10)]
    with pytest.raises(PanelError):
        await env.cog._apply_trail_text(env.g, env.admin, {"rooms": " ".join(str(r.id) for r in env.rooms + extra)})


async def test_go_live_and_setup_need_enough_trail_rooms(env):
    await env.conf.trail.set([r.id for r in env.rooms[:3]])
    with pytest.raises(PanelError, match="at least"):
        await env.cog._apply_start(env.g, env.admin, PID)
    with pytest.raises(PanelError, match="at least"):
        await env.cog._apply_setup(env.g, env.admin, PID)
    await env.conf.trail.set([r.id for r in env.rooms])
    assert "live" in await env.cog._apply_start(env.g, env.admin, PID)


async def test_go_live_does_not_need_the_pool_for_a_trail_puzzle(env):
    await env.conf.pool.set([])
    await env.conf.published.set([])
    assert "live" in await env.cog._apply_start(env.g, env.admin, PID)


async def test_preview_dms_the_admin_their_own_trail(env):
    await env.cog._apply_preview(env.g, env.admin, PID)
    text = env.admin.dms[-1]
    p = plan_of(env, env.admin.id)
    for k, name in enumerate(trail.SISTERS):
        assert name in text and f"<#{env.rooms[p['chain'][k]].id}>" in text and p["tokens"][k] in text
    assert TEA in text and HONEY in text and SLEEP in text


async def test_who_reports_the_trail(env):
    m = member(env, 11)
    await wake(env, m)

    class Ctx:
        def __init__(self):
            self.guild, self.out = env.g, []

        async def send(self, content=None, **kw):
            self.out.append(content)
    ctx = Ctx()
    await env.cog.secrethunt_who.callback(env.cog, ctx, m)
    assert "stage 2" in ctx.out[0] and "0/3" in ctx.out[0]


async def test_status_lists_the_puzzle_and_counts_starts(env):
    m = member(env, 11)
    await wake(env, m)
    embed = await env.cog._status_embed(env.g)
    assert "The Treacle Well" in embed.description and "1 started, 0 solved" in embed.description


# ---------------------------------------------------------------- the delivery loop

async def test_the_loop_reopens_archived_trail_rooms(env):
    env.rooms[2].archived = env.rooms[7].archived = True
    await env.cog._deliver_due(env.g, env.clock.now)
    assert not env.rooms[2].archived and not env.rooms[7].archived


async def test_an_uncached_archived_room_is_fetched_and_reopened(env):
    room = env.rooms[4]
    room.archived = True
    del env.g.threads[room.id]  # discord.py does not always keep archived threads in its cache

    async def fetch_channel(cid):
        if cid == room.id:
            return room
        raise tk.http_error("NotFound")
    env.g.fetch_channel = fetch_channel
    await env.cog._deliver_due(env.g, env.clock.now)
    assert room.archived is False


async def test_the_loop_leaves_trail_members_alone(env):
    m = member(env, 11)
    await wake(env, m)
    before = list(m.dms)
    await env.cog._deliver_due(env.g, env.clock.now + 10 ** 9)
    assert m.dms == before and (await state(env, m))["stage"] == engine.STAGE_RIDDLE


async def test_a_trail_puzzle_does_not_disturb_other_puzzles_or_guesses(env):
    await env.conf.published.set(["late_rabbit", PID])
    m = member(env, 11)
    assert engine.match_trigger(env.mod.PUZZLES, ["late_rabbit", PID], "") is None
    await env.cog._on_dm(dm(m, "hatter"))  # nobody has started anything: no reply, no crash
    assert not m.dms
    assert await env.cog._try_guess(env.g, m, "hatter", env.clock.now) is False


# ---------------------------------------------------------------- nothing leaks

def test_no_answer_or_token_appears_in_public_text():
    public = " ".join(
        [WELL["poem_title"], " ".join(WELL["poem"]), WELL["nudge"], WELL["object_title"], WELL["object_text"]]
        + list(WELL["pointers"].values()))
    for word in trail.TOKEN_WORDS:
        assert word not in public.lower(), word
    for group in trail.EMOJI_SLOTS:
        for e in group:
            assert e not in public


def test_the_readme_and_info_json_describe_mechanics_only():
    import pathlib
    root = pathlib.Path(__file__).parent
    text = ((root / "README.md").read_text() + (root / "info.json").read_text()).lower()
    for word in trail.TOKEN_WORDS:
        assert word not in text, word
    for group in trail.EMOJI_SLOTS:
        for e in group:
            assert e not in text
    for line in WELL["poem"]:
        assert line.lower() not in text


# ---------------------------------------------------------------- the test channel (rehearse in one private channel)

async def rehearse(env):
    """Test mode on with a private test channel, then Set up objects on the Treacle Well."""
    box = env.g.add_channel(500, "owner-private")
    for room in env.rooms:  # forget the tables the fixture put in the real rooms
        room.sent.clear()
    env.poem.sent.clear()
    await env.conf.objects.set({})
    await env.conf.poem_ids.set({})
    env.cog._poem_ids.clear()
    await env.cog._apply_test_channel(env.g, env.admin, box)
    await env.cog._apply_test_mode(env.g, env.admin, True)
    report = (await env.cog._apply_setup(env.g, env.admin, PID)).split("\n")
    return box, report


def tables_in(box):
    return [m for m in box.sent if m.kw.get("view") is not None]


async def test_test_mode_posts_every_tea_table_in_one_labelled_channel_and_touches_no_room(env):
    box, report = await rehearse(env)
    tables = tables_in(box)
    assert len(tables) == 12 and sum("rehearsal" in line for line in report) == 13  # 12 tables and the poem
    assert sorted(m.embed.title for m in tables) == sorted(f"A tea table \N{EM DASH} {n}" for n in NAMES)
    assert all(not r.sent for r in env.rooms) and not env.poem.sent  # nothing is posted or pinned anywhere real
    assert PID not in await env.conf.objects()
    assert sorted((await env.conf.test_objects())[PID]) == sorted(str(r.id) for r in env.rooms)


async def test_the_test_channel_does_nothing_while_test_mode_is_off(env):
    box = env.g.add_channel(500, "owner-private")
    await env.cog._apply_test_channel(env.g, env.admin, box)
    for room in env.rooms:
        room.sent.clear()
    await env.conf.objects.set({})
    await env.cog._apply_setup(env.g, env.admin, PID)
    assert not box.sent and all(any(m.pinned for m in r.sent) for r in env.rooms)


async def test_the_whole_chain_can_be_played_inside_the_test_channel(env):
    box, _ = await rehearse(env)
    await wake_in(env, box, env.admin)
    p = plan_of(env, env.admin.id)
    for k, room in enumerate(p["chain"]):
        message = next(m for m in tables_in(box) if m.embed.title.endswith(NAMES[room]))
        inter = tk.FakeInteraction(env.g, env.admin, message)
        await env.cog.handle_trail(inter, "look")
        assert str(p["numbers"][k]) in inter.replies[0][0]
    assert (await state(env, env.admin))["found"] == 3


async def wake_in(env, box, m):
    poem = next(x for x in box.sent if x.pinned)
    for emoji in (TEA, HONEY, SLEEP):
        payload = types.SimpleNamespace(guild_id=env.g.id, user_id=m.id, message_id=poem.id, channel_id=box.id, emoji=emoji)
        await env.cog._on_reaction(payload)
        env.clock.now += 1
    return poem


async def test_the_rehearsal_poem_is_posted_in_the_same_channel_and_works_end_to_end(env):
    box, _ = await rehearse(env)
    poem = await wake_in(env, box, env.admin)
    assert poem.embed.title == WELL["poem_title"] and (await env.conf.poem_ids()) == {str(poem.id): PID}
    assert [e for e, _u in poem.removed] == [TEA, HONEY, SLEEP]
    assert (await state(env, env.admin))["step"] == 2 and "Elsie" in env.admin.dms[-1]


async def test_a_wrong_test_table_is_just_a_tea_table_like_a_wrong_room(env):
    box, _ = await rehearse(env)
    await wake_in(env, box, env.admin)
    p = plan_of(env, env.admin.id)
    wrong = next(r for r in range(12) if r not in p["chain"])
    message = next(m for m in tables_in(box) if m.embed.title.endswith(NAMES[wrong]))
    inter = tk.FakeInteraction(env.g, env.admin, message)
    await env.cog.handle_trail(inter, "look")
    assert inter.replies[0][0] in WELL["wrong_look"]


async def test_the_poem_command_goes_to_the_test_channel_but_remembers_the_real_one(env):
    box, _ = await rehearse(env)
    other = env.g.add_channel(600, "somewhere-public")
    out = await env.cog._apply_poem(env.g, env.admin, other, PID)
    assert "test channel" in out and not other.sent
    posts = [m for m in box.sent if m.embed is not None and m.embed.title == WELL["poem_title"]]
    assert len(posts) == 2 and [m.pinned for m in posts] == [False, True]  # the newest replaces the old
    assert (await env.conf.poem_targets())[PID] == other.id
    assert list((await env.conf.poem_ids()).values()) == [PID] and len(await env.conf.poem_ids()) == 1


async def test_the_private_test_channel_never_appears_in_the_public_intro(env):
    box, _ = await rehearse(env)
    assert str(box.id) not in await env.conf.poems()


async def test_setting_up_again_does_not_repost_the_poem_but_redo_does(env):
    box, _ = await rehearse(env)
    count = lambda: len([m for m in box.sent if m.embed is not None and m.embed.title == WELL["poem_title"]])
    assert count() == 1
    await env.cog._apply_setup(env.g, env.admin, PID)
    assert count() == 1
    await env.cog._apply_setup(env.g, env.admin, PID, force=True)
    assert count() == 2


async def test_turning_test_mode_off_posts_the_real_poem_and_objects_and_stops_the_rehearsal(env):
    box, _ = await rehearse(env)
    other = env.g.add_channel(600, "wonderland-secrets")
    await env.cog._apply_poem(env.g, env.admin, other, PID)  # rehearsed: lands in the box, target remembered
    rehearsal_poem = next(m for m in box.sent if m.pinned)
    assert not other.sent
    out = await env.cog._apply_test_mode(env.g, env.admin, False)
    assert out.startswith("Test mode off.") and "The Treacle Well: objects ready in the real rooms" in out
    assert any(m.pinned and m.embed.title == WELL["poem_title"] for m in other.sent)  # the real poem, in its channel
    assert all(any(m.kw.get("view") is not None and m.pinned for m in r.sent) for r in env.rooms)  # real tables
    assert await env.conf.test_objects() == {} and await env.conf.test_poems() == {}
    assert str(rehearsal_poem.id) not in await env.conf.poem_ids()
    real = next(m for m in other.sent if m.pinned)
    assert (await env.conf.poem_ids()).get(str(real.id)) == PID
    # reactions on the dead rehearsal poem no longer start anything
    payload = types.SimpleNamespace(guild_id=env.g.id, user_id=env.admin.id, message_id=rehearsal_poem.id,
                                    channel_id=box.id, emoji=TEA)
    await env.cog._on_reaction(payload)
    assert rehearsal_poem.removed == []


async def test_turning_test_mode_off_without_a_known_poem_channel_says_so(env):
    box, _ = await rehearse(env)  # the poem was never given a regular channel in this guild config
    await env.conf.poem_targets.set({})
    out = await env.cog._apply_test_mode(env.g, env.admin, False)
    assert "no regular poem channel known" in out and len(tables_in(box)) == 12
    assert all(any(m.kw.get("view") is not None for m in r.sent) for r in env.rooms)


async def test_turning_test_mode_off_with_nothing_rehearsed_posts_nothing(env):
    await env.cog._apply_test_mode(env.g, env.admin, True)
    before = [len(r.sent) for r in env.rooms] + [len(env.poem.sent)]
    assert await env.cog._apply_test_mode(env.g, env.admin, False) == "Test mode off."
    assert [len(r.sent) for r in env.rooms] + [len(env.poem.sent)] == before


async def test_turning_test_mode_off_reports_a_trail_that_is_not_ready_instead_of_crashing(env):
    box, _ = await rehearse(env)
    await env.conf.trail.set([r.id for r in env.rooms[:3]])  # fewer than the puzzle needs
    out = await env.cog._apply_test_mode(env.g, env.admin, False)
    assert "NOT set up" in out


async def test_the_loop_reopens_trail_rooms_only_once_real_tables_exist(env):
    await rehearse(env)
    env.rooms[2].archived = True
    await env.cog._deliver_due(env.g, env.clock.now)
    assert env.rooms[2].archived is True  # a rehearsal: the real rooms hold nothing to keep open
    await env.cog._apply_test_mode(env.g, env.admin, False)
    env.rooms[2].archived = True
    await env.cog._deliver_due(env.g, env.clock.now)
    assert env.rooms[2].archived is False


async def test_a_test_channel_that_cannot_be_posted_to_is_reported_per_room(env):
    box = env.g.add_channel(500, "owner-private")
    await env.cog._apply_test_channel(env.g, env.admin, box)
    await env.cog._apply_test_mode(env.g, env.admin, True)
    await env.conf.objects.set({})
    box.fail_send = True
    report = await env.cog._setup_objects(env.g, PID)
    assert len(report) == 12 and all("failed in the test channel" in line for line in report)


async def test_changing_the_trail_rooms_forgets_the_rehearsal_tables(env):
    await rehearse(env)
    await env.cog._apply_trail_text(env.g, env.admin, {"rooms": " ".join(str(r.id) for r in env.rooms[:8])})
    assert PID not in await env.conf.test_objects()


# ---- every other puzzle can be rehearsed too

async def test_every_kind_of_puzzle_rehearses_in_the_test_channel(env, monkeypatch):
    """Easy (reaction), Easy (buttons) and the Knave's Trial each get labelled copies of their object in the box."""
    five = [env.g.add_thread(700 + i, n) for i, n in enumerate(["gaming", "music", "memes", "movies", "cuddle"])]
    await env.conf.pool.set([c.id for c in five])
    box = env.g.add_channel(500, "owner-private")
    await env.cog._apply_test_channel(env.g, env.admin, box)
    await env.cog._apply_test_mode(env.g, env.admin, True)
    for pid in ("late_rabbit", "looking_glass", "knaves_trial"):
        out = await env.cog._apply_setup(env.g, env.admin, pid)
        assert out.count("(rehearsal)") == 6, pid  # five objects and the poem
    assert all(not c.sent for c in five)  # not one real room was touched
    labels = [m.embed.title for m in box.sent if m.embed is not None]
    assert "A pocket watch \N{EM DASH} gaming" in labels and "A looking glass \N{EM DASH} cuddle" in labels
    assert "A witness waits here \N{EM DASH} movies" in labels
    assert len([m for m in box.sent if m.kw.get("view") is not None]) == 10  # glass buttons + witness buttons
    assert set(await env.conf.test_objects()) == {"late_rabbit", "looking_glass", "knaves_trial"}


async def test_a_rehearsed_easy_puzzle_can_be_played_from_the_test_channel(env):
    five = [env.g.add_thread(700 + i, n) for i, n in enumerate(["gaming", "music", "memes", "movies", "cuddle"])]
    await env.conf.pool.set([c.id for c in five])
    await env.conf.published.set(["looking_glass"])
    box = env.g.add_channel(500, "owner-private")
    await env.cog._apply_test_channel(env.g, env.admin, box)
    await env.cog._apply_test_mode(env.g, env.admin, True)
    await env.cog._apply_setup(env.g, env.admin, "looking_glass")
    glass = PUZZLES["looking_glass"]
    await env.cog._run_trigger(env.g, env.admin, "looking_glass", env.clock.now)
    st = (await env.cog.config.member(env.admin).puzzles())["looking_glass"]
    room = five[st["variant"] % 5]
    key = glass["buttons"][st["variant"] % len(glass["buttons"])]
    message = next(m for m in box.sent if m.embed is not None and m.embed.title.endswith(f"\N{EM DASH} {room.name}"))
    inter = tk.FakeInteraction(env.g, env.admin, message)
    await env.cog.handle_click(inter, key)
    assert inter.replies[0][0] == glass["found"]  # the right button on the right (labelled) copy


async def test_turning_test_mode_off_launches_every_rehearsed_puzzle(env):
    five = [env.g.add_thread(700 + i, n) for i, n in enumerate(["gaming", "music", "memes", "movies", "cuddle"])]
    await env.conf.pool.set([c.id for c in five])
    box = env.g.add_channel(500, "owner-private")
    await env.cog._apply_test_channel(env.g, env.admin, box)
    await env.cog._apply_test_mode(env.g, env.admin, True)
    real_poem = env.g.add_channel(650, "secrets-poems")
    await env.cog._apply_setup(env.g, env.admin, "late_rabbit")
    await env.cog._apply_poem(env.g, env.admin, real_poem, "late_rabbit")
    assert not real_poem.sent
    out = await env.cog._apply_test_mode(env.g, env.admin, False)
    assert "The Late Rabbit" in out
    posted = [m for m in real_poem.sent if m.embed is not None]  # (the fake adds a pin notice under real discord.py)
    assert [m.embed.title for m in posted] == [PUZZLES["late_rabbit"]["poem_title"]] and posted[0].pinned
    assert all(any(m.pinned for m in c.sent) for c in five)


def test_cuddle_and_the_other_unusual_room_names_get_a_written_clue():
    for name in ("cuddle", "Witch Bunny", "animal of the day", "deep dives", "Cool Tech Stuff", "Food, Recipes, and Cooking",
                 "pretty cool books", "gaming", "memes", "music", "movies", "anime"):
        assert engine.pointer_for(WELL, name, 0) and "letters" not in engine.pointer_for(WELL, name, 0), name


# ---- pasting rooms

@pytest.mark.parametrize("token, expected", [
    ("<#123456789>", 123456789), ("123456789", 123456789),
    ("https://discord.com/channels/9328/123456789", 123456789),
    ("https://discord.com/channels/9328/123456789/555", 123456789),
    ("https://discord.com/channels/@me/55", None), ("nope", None), ("", None),
])
async def test_rooms_can_be_pasted_as_mentions_ids_or_links(env, token, expected):
    assert env.cog._channel_id_of(token) == expected


async def test_a_link_to_cuddle_can_replace_a_trail_room(env):
    cuddle = env.g.add_channel(777, "cuddle")
    rooms = [f"<#{r.id}>" for r in env.rooms[:11]] + [f"https://discord.com/channels/1/{cuddle.id}"]
    await env.cog._apply_trail_text(env.g, env.admin, {"rooms": " ".join(rooms)})
    assert (await env.conf.trail())[-1] == 777


async def test_the_test_channel_can_be_cleared(env):
    box = env.g.add_channel(500, "owner-private")
    await env.cog._apply_test_channel(env.g, env.admin, box)
    assert await env.conf.test_channel_id() == 500
    assert "cleared" in await env.cog._apply_test_channel(env.g, env.admin, None)
    assert await env.conf.test_channel_id() is None


# ---------------------------------------------------------------- stage 3: the Muchness (DM) and stage 4: the Long Pour

async def through_stage2(env, uid=11):
    m = member(env, uid)
    await wake(env, m)
    for room in plan_of(env, uid)["chain"]:
        await look(env, m, room)
    return m


def drawable(env, uid, word):
    return trail.holds(tuple(plan_of(env, uid)["rule"]), word)


async def pass_stage3(env, m):
    p = plan_of(env, m.id)
    await say(env, m, "ready")
    _w, yes = trail.make_test(p["seed"], (await state(env, m))["test_roll"], tuple(p["rule"]))
    return await say(env, m, ", ".join(yes))


async def test_the_third_sister_wakes_the_dormouse_and_stage_three_opens_by_dm(env):
    m = await through_stage2(env)
    assert m.dms[-1] == WELL["stage3_intro"]
    st = await state(env, m)
    assert st["step"] == 3 and st["clue"] == WELL["stage3_intro"]
    assert await say(env, m, "recap") == WELL["stage3_intro"]


async def test_words_asked_by_dm_get_true_verdicts_and_the_prefix_is_tolerated(env):
    m = await through_stage2(env)
    for word in ("apple", "cloud", "tulip", "jam", "fox"):
        reply = await say(env, m, word)
        want = WELL["probe_yes" if drawable(env, 11, word) else "probe_no"].format(word=word)
        assert reply == want, word
    assert await say(env, m, ".apple") == WELL["probe_yes" if drawable(env, 11, "apple") else "probe_no"].format(word="apple")


async def test_words_from_a_member_who_is_not_on_stage_three_get_no_answer(env):
    early = member(env, 12)
    assert await say(env, early, "apple") is None
    await wake(env, early)
    n = len(early.dms)
    assert await say(env, early, "apple") is None and len(early.dms) == n  # woke him, but stage 2 is not done


async def test_the_test_is_taken_in_one_long_message_and_passing_opens_stage_four(env):
    m = await through_stage2(env)
    p = plan_of(env, 11)
    intro = await say(env, m, "ready")
    words = (await state(env, m))["test_words"]
    assert len(words) == 8 and all(w in intro for w in words)
    _w, yes = trail.make_test(p["seed"], 0, tuple(p["rule"]))
    answer = ", ".join(yes)
    done = await say(env, m, answer)
    assert (await state(env, m))["step"] == 4 and done == (await state(env, m))["clue"]
    assert p["extra_tokens"][0] in done
    assert engine.pointer_for(WELL, NAMES[p["pour_room"]], 3) in done and engine.pointer_for(WELL, NAMES[p["toast_room"]], 4) in done
    assert await say(env, m, "recap") == done


async def test_a_long_test_answer_is_not_dropped_by_the_dm_length_guard(env):
    m = await through_stage2(env)
    await say(env, m, "ready")
    words = (await state(env, m))["test_words"]
    long_answer = ", ".join(words) + " and then some more words to pad this out"
    assert 60 < len(long_answer) <= 120
    assert await say(env, m, long_answer) is not None


async def test_a_wrong_test_rests_and_the_next_test_has_fresh_words(env):
    m = await through_stage2(env)
    await say(env, m, "ready")
    first = list((await state(env, m))["test_words"])
    assert "think of new" in await say(env, m, "none")
    assert "asleep" in await say(env, m, "ready")
    env.clock.now += 15 * 60 + 1
    await say(env, m, "ready")
    assert (await state(env, m))["test_words"] != first and (await state(env, m))["step"] == 3


async def test_a_malformed_test_answer_costs_nothing_and_a_test_word_is_not_judged(env):
    m = await through_stage2(env)
    await say(env, m, "ready")
    words = (await state(env, m))["test_words"]
    assert "does not count" in await say(env, m, "I really have no idea what to say")
    assert "saved for the test" in await say(env, m, words[0])
    assert (await state(env, m))["test_roll"] == 0 and (await state(env, m))["test_words"] == words


async def test_an_exact_easy_answer_still_goes_to_the_easy_puzzle(env):
    m = await through_stage2(env)
    await env.conf.published.set(["late_rabbit", PID])
    rabbit = PUZZLES["late_rabbit"]
    await env.cog._run_trigger(env.g, m, "late_rabbit", env.clock.now)
    async with env.cog.config.member(m).puzzles() as states:
        states["late_rabbit"]["stage"] = engine.STAGE_FINAL
        answer = rabbit["answers"][states["late_rabbit"]["variant"] % len(rabbit["answers"])]
    await say(env, m, answer)
    assert (await state(env, m))["probes"] == 0  # the trail never saw it
    assert (await env.cog.config.member(m).puzzles())["late_rabbit"]["stage"] == engine.STAGE_SOLVED


async def pour_n(env, m, n, room):
    out = None
    for _ in range(n):
        out = await look(env, m, room, "pour")
    return out


async def test_the_long_pour_counts_pours_and_the_right_toast_finishes_stage_four(env):
    m = await through_stage2(env)
    await pass_stage3(env, m)
    p = plan_of(env, 11)
    assert await look(env, m, p["pour_room"], "pour") == WELL["pour_ok"].format(n=1)
    assert await look(env, m, p["pour_room"], "pour") == WELL["pour_ok"].format(n=2)
    await pour_n(env, m, p["pour_total"] - 2, p["pour_room"])
    assert (await state(env, m))["pours"] == p["pour_total"] == sum(p["numbers"])
    out = await look(env, m, p["toast_room"], "toast")
    done = env.cog._stage4_done_text(env.g, WELL, [r.id for r in env.rooms], p)
    assert out == done and m.dms[-1] == done and p["extra_tokens"][1] in done
    st = await state(env, m)
    assert st["step"] == 5 and st["clue"] == done and await say(env, m, "recap") == done


async def test_a_wrong_toast_empties_the_cup_and_three_rest_the_cups(env):
    m = await through_stage2(env)
    await pass_stage3(env, m)
    p = plan_of(env, 11)
    for i in range(3):
        await pour_n(env, m, p["pour_total"] + 1, p["pour_room"])
        out = await look(env, m, p["toast_room"], "toast")
        assert (await state(env, m))["pours"] == 0
    assert "Enough tea" in out and "washed" in await look(env, m, p["pour_room"], "pour")
    env.clock.now += 3601
    assert await look(env, m, p["pour_room"], "pour") == WELL["pour_ok"].format(n=1)


async def test_pour_and_toast_in_the_wrong_room_or_too_early_do_nothing_much(env):
    m = await through_stage2(env)
    p = plan_of(env, 11)
    assert await look(env, m, p["pour_room"], "pour") == WELL["quiet"]  # still stage 3
    await pass_stage3(env, m)
    other = next(r for r in range(12) if r not in (p["pour_room"], p["toast_room"]))
    assert await look(env, m, other, "pour") == WELL["pour_wrong"]
    assert await look(env, m, other, "toast") == WELL["toast_wrong"]
    assert await look(env, m, p["toast_room"], "toast") == WELL["toast_empty"]
    assert (await state(env, m))["pours"] == 0


async def test_looking_in_any_room_after_stage_two_costs_nothing(env):
    m = await through_stage2(env)
    p = plan_of(env, 11)
    wrong = next(r for r in range(12) if r not in p["chain"])
    for _ in range(6):
        assert await look(env, m, wrong) in WELL["wrong_look"]
    assert str(p["numbers"][0]) in await look(env, m, p["chain"][0])  # the sisters still replay


async def test_a_blocked_member_on_stage_three_or_four_is_ignored(env):
    lurker = env.g.add_role(55, "Lurker")
    m = await through_stage2(env)
    await env.conf.blocked_role_id.set(lurker.id)
    m.roles.append(lurker)
    n = len(m.dms)
    assert await say(env, m, "apple") is None and len(m.dms) == n
    assert await look(env, m, 0, "pour") == "Nothing happens."


async def test_the_whole_trail_to_stage_four_runs_in_the_test_channel(env):
    box, _ = await rehearse(env)
    await wake_in(env, box, env.admin)
    uid = env.admin.id
    p = plan_of(env, uid)

    def tbl(room):
        return next(m for m in tables_in(box) if m.embed.title.endswith(NAMES[room]))

    async def press(room, key):
        inter = tk.FakeInteraction(env.g, env.admin, tbl(room))
        await env.cog.handle_trail(inter, key)
        return inter.replies[0][0]

    for room in p["chain"]:
        await press(room, "look")
    await pass_stage3(env, env.admin)
    for _ in range(p["pour_total"]):
        await press(p["pour_room"], "pour")
    out = await press(p["toast_room"], "toast")
    assert env.admin.dms[-1] == out  # the same words by DM and in the private reply
    assert p["extra_tokens"][1] in env.admin.dms[-1]
    assert (await state(env, env.admin))["step"] == 5


async def test_preview_shows_the_admin_their_rule_test_and_pour_count(env):
    await env.cog._apply_preview(env.g, env.admin, PID)
    text = env.admin.dms[-1]
    p = plan_of(env, env.admin.id)
    assert trail.rule_text(tuple(p["rule"])) in text and f"pour {p['pour_total']} times" in text
    assert p["extra_tokens"][0] in text and p["extra_tokens"][1] in text
    words, yes = trail.make_test(p["seed"], 0, tuple(p["rule"]))
    assert ", ".join(words) in text


async def test_who_shows_the_pours_and_failed_tests(env):
    m = await through_stage2(env)

    class Ctx:
        def __init__(self):
            self.guild, self.out = env.g, []

        async def send(self, content=None, **kw):
            self.out.append(content)
    ctx = Ctx()
    await env.cog.secrethunt_who.callback(env.cog, ctx, m)
    assert "stage 3" in ctx.out[0] and "failed word tests 0" in ctx.out[0] and "pours 0" in ctx.out[0]


def test_stage_three_and_four_texts_format_cleanly():
    keys = dict(word="w", words="a, b", until="<t:1:R>", token="t", pour_pointer="P", toast_pointer="T", n=3)
    for field in ("stage3_intro", "probe_yes", "probe_no", "probe_saving", "probe_tired", "test_intro", "test_again",
                  "test_rest", "test_failed", "test_malformed", "stage3_done", "pour_ok", "pour_wrong", "pour_full", "pour_rest",
                  "toast_wrong", "toast_empty", "toast_miscount", "toast_miscount_rest"):
        assert "{" not in WELL[field].format_map(keys), field


def test_the_new_public_and_dm_text_never_leaks_a_rule():
    body = " ".join(WELL[k] for k in ("stage3_intro", "test_intro", "test_failed", "probe_saving", "stage3_done")).lower()
    for text in ("contains the letter", "starts with", "ends with", "vowel", "even number", "double letter"):
        assert text not in body, text


# ---------------------------------------------------------------- closed DMs, scheduled launches, status

from datetime import datetime  # noqa: E402

from secrethunt.constants import HOME_TZ  # noqa: E402
from secrethunt.puzzles import DM_CLOSED_NOTE, DM_CLOSED_PUBLIC  # noqa: E402


def pacific(ts):
    return datetime.fromtimestamp(ts, HOME_TZ).strftime("%Y-%m-%d %H:%M")


async def test_closed_dms_on_the_third_sister_tell_the_member_privately_and_retry_on_the_next_press(env):
    m = member(env, 21)
    await wake(env, m)
    p = plan_of(env, 21)
    await look(env, m, p["chain"][0])
    await look(env, m, p["chain"][1])
    m.dm_closed = True
    out = await look(env, m, p["chain"][2])
    assert DM_CLOSED_NOTE in out and str(p["numbers"][2]) in out
    assert (await state(env, m)).get("dm_pending") is True
    m.dm_closed = False
    await look(env, m, p["chain"][2])  # any press: the bounced DM is sent again
    assert m.dms[-1] == WELL["stage3_intro"] and "dm_pending" not in await state(env, m)
    n = len(m.dms)
    await look(env, m, p["chain"][2])
    assert len(m.dms) == n  # and only once


async def test_a_still_closed_dm_keeps_the_note_coming(env):
    m = member(env, 22)
    await wake(env, m)
    p = plan_of(env, 22)
    m.dm_closed = True
    for room in p["chain"]:
        await look(env, m, room)
    assert DM_CLOSED_NOTE in await look(env, m, p["chain"][0]) and (await state(env, m))["dm_pending"] is True


async def test_closed_dms_at_stage_one_undo_the_start_and_say_so_in_the_channel(env):
    m = member(env, 23)
    m.dm_closed = True
    await wake(env, m)
    assert await state(env, m) is None
    notice = env.poem.sent[-1]
    assert notice.content == DM_CLOSED_PUBLIC.format(mention=m.mention) and notice.kw["delete_after"] == 15
    m.dm_closed = False
    await wake(env, m)
    assert (await state(env, m)) is not None and m.dms[-1] == (await state(env, m))["clue"]


async def test_the_dm_closed_texts_never_name_a_stage_or_answer():
    for text in (DM_CLOSED_NOTE, DM_CLOSED_PUBLIC):
        assert "tea" not in text.lower().replace("tea table", "") and "stage" not in text.lower()


async def schedule(env, when=None, channel=None, puzzle=PID, **kw):
    when = pacific(env.clock.now + 7200) if when is None else when
    return await env.cog._apply_launch(env.g, env.admin, {"puzzle": puzzle, "when": when,
                                                         "channel": channel if channel is not None else f"<#{env.poem.id}>", **kw})


@pytest.fixture
async def fresh(env, monkeypatch):
    """A finished puzzle that is not live yet, with its real objects placed and the hunt on."""
    await env.conf.published.set([])
    await env.conf.active.set("")
    return env


async def test_a_wip_puzzle_cannot_be_scheduled(env, monkeypatch):
    monkeypatch.setitem(env.mod.PUZZLES, PID, {**WELL, "wip": True})
    with pytest.raises(PanelError, match="still being built"):
        await schedule(env)


@pytest.mark.parametrize("when,why", [("tomorrow 9am", "couldn't read"), ("2020-01-01 09:00", "already passed")])
async def test_a_bad_launch_time_is_refused_and_nothing_is_saved(fresh, when, why):
    with pytest.raises(PanelError, match=why):
        await schedule(fresh, when=when)
    assert await fresh.conf.launches() == {}


async def test_a_launch_needs_a_known_puzzle_and_a_channel(fresh):
    with pytest.raises(PanelError, match="don't know"):
        await schedule(fresh, puzzle="nope")
    with pytest.raises(PanelError, match="couldn.t find a channel"):
        await schedule(fresh, channel="<#999999>")


async def test_scheduling_reads_pacific_time_in_both_clock_styles(fresh):
    text = await schedule(fresh, when="2099-10-24 9:00am")
    want = datetime(2099, 10, 24, 9, 0, tzinfo=HOME_TZ).timestamp()
    assert (await fresh.conf.launches())[PID]["at"] == want and f"<t:{int(want)}:F>" in text
    await schedule(fresh, when="2099-10-24 09:00")
    assert (await fresh.conf.launches())[PID]["at"] == want


async def test_a_launch_runs_once_at_its_time_going_live_and_posting_the_poem(fresh):
    await schedule(fresh)
    at = (await fresh.conf.launches())[PID]["at"]
    before = len(fresh.poem.sent)
    await fresh.cog._run_launches(fresh.g, at - 1)
    assert await fresh.conf.published() == [] and len(fresh.poem.sent) == before  # not yet
    fresh.admin.dms.clear()
    await fresh.cog._run_launches(fresh.g, at + 5)
    assert await fresh.conf.published() == [PID] and await fresh.conf.active() == PID
    poem = next(m for m in fresh.poem.sent if m.pinned and m.embed is not None and m.embed.title == WELL["poem_title"])
    assert (await fresh.conf.poem_ids())[str(poem.id)] == PID
    launch = (await fresh.conf.launches())[PID]
    assert launch["state"] == "done" and "launched on schedule" in fresh.admin.dms[-1]
    sent = len(fresh.poem.sent)
    await fresh.cog._run_launches(fresh.g, at + 600)
    assert len(fresh.poem.sent) == sent  # never twice


async def test_the_loop_tick_runs_the_launch_even_while_the_hunt_is_off(fresh):
    await fresh.conf.enabled.set(True)
    await schedule(fresh)
    at = (await fresh.conf.launches())[PID]["at"]
    await fresh.cog._deliver_due(fresh.g, at + 1)
    assert await fresh.conf.published() == [PID]


async def test_a_launch_that_is_not_ready_fails_loudly_and_changes_nothing_public(fresh):
    await fresh.conf.enabled.set(False)
    await fresh.conf.objects.set({})
    text = await schedule(fresh)
    assert "Not ready yet" in text and "switched off" in text and "objects are not set up" in text
    at = (await fresh.conf.launches())[PID]["at"]
    before = len(fresh.poem.sent)
    fresh.admin.dms.clear()
    await fresh.cog._run_launches(fresh.g, at + 1)
    assert await fresh.conf.published() == [] and len(fresh.poem.sent) == before
    assert (await fresh.conf.launches())[PID]["state"] == "failed" and "NOT launched" in fresh.admin.dms[-1]


async def test_test_mode_blocks_a_launch_until_it_is_turned_off(fresh):
    await fresh.conf.test_mode.set(True)
    assert "Test mode is still on" in await schedule(fresh)
    at = (await fresh.conf.launches())[PID]["at"]
    await fresh.cog._run_launches(fresh.g, at + 1)
    assert await fresh.conf.published() == []


async def test_an_hour_before_a_launch_that_is_not_ready_warns_once(fresh):
    await fresh.conf.enabled.set(False)
    await schedule(fresh)
    at = (await fresh.conf.launches())[PID]["at"]
    fresh.admin.dms.clear()
    await fresh.cog._run_launches(fresh.g, at - 7200 + 10)
    assert fresh.admin.dms == []  # still hours away
    await fresh.cog._run_launches(fresh.g, at - 3000)
    assert len(fresh.admin.dms) == 1 and "not ready" in fresh.admin.dms[0]
    await fresh.cog._run_launches(fresh.g, at - 2000)
    assert len(fresh.admin.dms) == 1
    assert (await fresh.conf.launches())[PID]["state"] == "pending"


async def test_a_ready_launch_sends_no_warning(fresh):
    await schedule(fresh)
    at = (await fresh.conf.launches())[PID]["at"]
    fresh.admin.dms.clear()
    await fresh.cog._run_launches(fresh.g, at - 3000)
    assert fresh.admin.dms == []


async def test_an_empty_time_cancels_and_cancelling_nothing_says_so(fresh):
    await schedule(fresh)
    assert "cancelled" in await schedule(fresh, when="")
    assert await fresh.conf.launches() == {}
    with pytest.raises(PanelError, match="No launch is scheduled"):
        await schedule(fresh, when="")


async def test_turning_test_mode_off_leaves_a_scheduled_puzzle_for_its_time(fresh):
    box = fresh.g.add_channel(777, "test")
    await fresh.conf.test_channel_id.set(box.id)
    await fresh.cog._apply_test_mode(fresh.g, fresh.admin, True)
    await fresh.cog._apply_setup(fresh.g, fresh.admin, PID, True)
    await schedule(fresh)
    before = len(fresh.poem.sent)
    out = await fresh.cog._apply_test_mode(fresh.g, fresh.admin, False)
    assert "scheduled for" in out and len(fresh.poem.sent) == before
    assert str(table(fresh, 0)) and (await fresh.conf.objects())[PID]  # but the real objects are in place


async def test_status_lists_every_trail_room_by_name_and_flags_a_missing_one(env):
    await env.conf.trail.set([r.id for r in env.rooms] + [987654321])
    embed = await env.cog._status_embed(env.g)
    text = embed.description
    assert "13 set" in text and "gaming" in text and "**MISSING** `987654321`" in text


async def test_status_shows_a_scheduled_launch(fresh):
    await schedule(fresh)
    assert "Scheduled launches" in (await fresh.cog._status_embed(fresh.g)).description


async def test_the_panel_offers_the_schedule_form_prefilled(fresh):
    from secrethunt import panel
    spec = next(s for s in panel.build_specs() if s.key == "launch")
    await fresh.conf.published.set([p for p in PUZZLES if p != PID])  # everything else is out: the next to release is the Well
    value = await spec.get(fresh.cog, fresh.g)
    assert spec.fields[0].value(value) == WELL["title"]  # the next puzzle to release, by name
    assert datetime.strptime(spec.fields[1].value(value), "%Y-%m-%d %H:%M")


async def test_the_launch_form_takes_a_title_or_part_of_one(fresh):
    for typed in ("The Treacle Well", "treacle well", "treacle", "TREACLE_WELL"):
        await schedule(fresh, puzzle=typed)
        assert PID in await fresh.conf.launches()
        await schedule(fresh, puzzle=typed, when="")
    with pytest.raises(PanelError, match="don't know"):
        await schedule(fresh, puzzle="zzz")
    with pytest.raises(PanelError, match="several"):
        await schedule(fresh, puzzle="the")


# ---------------------------------------------------------------- stages 5 and 6, hints, funnel, the Hall

async def through_stage4(env, uid=31):
    m = await through_stage2(env, uid)
    await pass_stage3(env, m)
    p = plan_of(env, uid)
    await pour_n(env, m, p["pour_total"], p["pour_room"])
    await look(env, m, p["toast_room"], "toast")
    assert (await state(env, m))["step"] == 5
    return m, p


async def pay_all(env, m, p):
    out = None
    for k in range(4):
        out = await look(env, m, p["courtesy_rooms"][k], p["courtesy_buttons"][k])
    return out


async def through_stage5(env, uid=41):
    m, p = await through_stage4(env, uid)
    await pay_all(env, m, p)
    assert (await state(env, m))["step"] == 6
    return m, p


async def test_stage_four_hands_over_four_verses_in_the_members_own_order_with_a_clue_for_each_room(env):
    m, p = await through_stage4(env)
    text = m.dms[-1]
    assert p["extra_tokens"][1] in text and (await state(env, m))["clue"] == text
    spots = [text.index(WELL["verses"][b]) for b in p["courtesy_buttons"]]
    assert spots == sorted(spots)  # the verses come in step order
    for k in range(4):
        pointer = trail.room_pointer(WELL, NAMES[p["courtesy_rooms"][k]], 5 + k)
        assert WELL["verse_place"].format(pointer=pointer) in text
    assert await say(env, m, "recap") == text


async def test_each_courtesy_paid_in_order_is_acknowledged_and_the_last_hands_over_the_tale(env):
    m, p = await through_stage4(env)
    for k in range(3):
        out = await look(env, m, p["courtesy_rooms"][k], p["courtesy_buttons"][k])
        assert out == WELL["courtesy_ok"][p["courtesy_buttons"][k]].format(done=k + 1)
    last = await look(env, m, p["courtesy_rooms"][3], p["courtesy_buttons"][3])
    assert m.dms[-1] == last and p["extra_tokens"][2] in last and trail.tale_text(p) in last
    assert not any(w in last for w in p["tokens"] + p["extra_tokens"][:2])  # the tale names sources, never the earlier words
    st = await state(env, m)
    assert st["step"] == 6 and st["clue"] == last and await say(env, m, "recap") == last


async def test_a_wrong_press_during_the_courtesies_starts_them_again_and_the_wrong_room_costs_nothing_extra(env):
    m, p = await through_stage4(env)
    await look(env, m, p["courtesy_rooms"][0], p["courtesy_buttons"][0])
    stray = next(r for r in range(12) if r not in p["courtesy_rooms"] and r not in p["chain"])
    assert await look(env, m, stray, "pour") == WELL["courtesy_lost"]
    assert (await state(env, m))["courtesy"] == 0
    assert await look(env, m, stray, "pour") in WELL["wrong_look"]  # nothing to lose now: just a tea table
    assert await look(env, m, p["courtesy_rooms"][0], p["courtesy_buttons"][0]) == WELL["courtesy_ok"][p["courtesy_buttons"][0]].format(done=1)


async def test_five_slips_close_the_tables_for_two_hours(env):
    m, p = await through_stage4(env)
    stray = next(r for r in range(12) if r not in p["courtesy_rooms"] and r not in p["chain"])
    for _ in range(4):
        await look(env, m, stray, "bow")
    out = await look(env, m, stray, "bow")
    assert "Too many slips" in out
    assert "closed for the evening" in await look(env, m, p["courtesy_rooms"][0], p["courtesy_buttons"][0])
    env.clock.now += 2 * 3600 + 1
    assert "A courtesy paid" in await look(env, m, p["courtesy_rooms"][0], p["courtesy_buttons"][0])


async def test_a_sister_can_still_be_heard_again_during_the_courtesies_at_no_cost(env):
    m, p = await through_stage4(env)
    room = p["chain"][0]
    if (room, "look") == (p["courtesy_rooms"][0], p["courtesy_buttons"][0]):
        pytest.skip("this member's first courtesy is looking in that very room")
    out = await look(env, m, room)
    assert str(p["numbers"][0]) in out and (await state(env, m))["slips"] == 0


async def test_closed_dms_at_stage_four_and_five_tell_the_member_and_resend_on_the_next_press(env):
    m = await through_stage2(env, 42)
    await pass_stage3(env, m)
    p = plan_of(env, 42)
    await pour_n(env, m, p["pour_total"], p["pour_room"])
    m.dm_closed = True
    assert DM_CLOSED_NOTE in await look(env, m, p["toast_room"], "toast")
    m.dm_closed = False
    await look(env, m, p["courtesy_rooms"][0], p["courtesy_buttons"][0])
    assert m.dms[-1] == env.cog._stage4_done_text(env.g, WELL, [r.id for r in env.rooms], p)  # the verses finally arrive
    assert "dm_pending" not in await state(env, m)


async def test_the_six_letters_by_dm_solve_the_trail_and_everything_that_follows_a_solve_happens(env):
    announce = env.g.add_channel(600, "announce")
    await env.conf.announce_channel_id.set(announce.id)
    m, p = await through_stage5(env)
    await env.cog._refresh_hall(env.g, PID, create=True)
    reply = await say(env, m, p["answer"].upper())
    assert reply == WELL["solved"].format(rank="\n\nYou were finder **#1**.")
    st = await state(env, m)
    assert st["stage"] == engine.STAGE_SOLVED and st["rank"] == 1 and await env.conf.finishers() == {PID: [m.id]}
    assert "solved" in announce.sent[-1].content and m.mention in announce.sent[-1].content
    hall = next(x for x in env.hall.sent if x.embed is not None and "Treacle" in x.embed.title)
    assert "user" in hall.embed.description and "no hints" in hall.embed.description and "solved in" in hall.embed.description
    assert await look(env, m, 0) == WELL["already"] and await look(env, m, 0, "pour") == WELL["already"]
    n = len(m.dms)
    assert await say(env, m, p["answer"]) is None and len(m.dms) == n  # solved: nothing more to answer


async def test_a_wrong_answer_costs_an_hour_and_other_dms_cost_nothing(env):
    m, p = await through_stage5(env)
    for text in ("hello", "something long that is not an answer", "abc"):
        assert await say(env, m, text) is None
    assert (await state(env, m))["answer_wrongs"] == 0
    wrong = "zzzzzz" if p["answer"] != "zzzzzz" else "yyyyyy"
    first = await say(env, m, wrong)
    assert first and "not the bottom of the well" in first
    assert "dozed off" in await say(env, m, p["answer"])
    env.clock.now += 3601
    assert await say(env, m, p["answer"]) == WELL["solved"].format(rank="\n\nYou were finder **#1**.")


async def test_an_exact_easy_answer_is_still_the_easy_puzzles_on_stage_six(env):
    m, p = await through_stage5(env)
    await env.conf.published.set(["late_rabbit", PID])
    async with env.cog.config.member(m).puzzles() as states:
        states["late_rabbit"] = {**engine.new_state(0, env.clock.now), "stage": engine.STAGE_FINAL}
    await say(env, m, "hatter")  # six letters, and the Late Rabbit's answer for variant 0
    assert (await env.cog.config.member(m).puzzles())["late_rabbit"]["stage"] == engine.STAGE_SOLVED
    assert (await state(env, m))["answer_wrongs"] == 0


async def test_solvers_are_ranked_in_finishing_order_and_the_hall_lists_them_all(env):
    a, pa = await through_stage5(env, 51)
    b, pb = await through_stage5(env, 52)
    await say(env, b, pb["answer"])
    await say(env, a, pa["answer"])
    assert await env.conf.finishers() == {PID: [b.id, a.id]}
    assert (await state(env, b))["rank"] == 1 and (await state(env, a))["rank"] == 2


async def test_hints_by_dm_wait_then_help_then_stop(env):
    m = await through_stage2(env, 61)
    p = plan_of(env, 61)
    await look(env, m, p["chain"][0])  # (this is stage 3 for him now: three sisters were found by through_stage2)
    first = await say(env, m, "hint")
    assert "not ready" in first
    env.clock.now += 86400
    h1 = await say(env, m, "hint")
    assert "hint 1 of 2" in h1 and "He can draw" in h1
    assert "not ready" in await say(env, m, "hint")
    env.clock.now += 12 * 3600
    assert "hint 2 of 2" in await say(env, m, "hint")
    assert "said all he will say" in await say(env, m, "hint")
    assert (await state(env, m))["hints"] == 2


async def test_a_stage_two_hint_names_the_letter_of_the_room_being_sought(env):
    m = member(env, 62)
    await wake(env, m)
    p = plan_of(env, 62)
    env.clock.now += 86400
    text = await say(env, m, "hint")
    assert f"letter {NAMES[p['chain'][0]][0].upper()}" in text


async def test_hints_belong_to_the_knaves_trial_while_one_of_its_rounds_is_open(env):
    m = await through_stage2(env, 63)
    env.clock.now += 86400
    open_trial = {"knaves_trial": {"stage": engine.STAGE_RIDDLE}}
    assert env.cog._trial_open(open_trial, ["knaves_trial"]) is True
    assert env.cog._trial_open({"knaves_trial": {"stage": engine.STAGE_SOLVED}}, ["knaves_trial"]) is False
    assert env.cog._trial_open({}, ["knaves_trial", PID]) is False


async def test_a_hint_from_someone_with_no_trail_is_not_answered_by_the_dormouse(env):
    stranger = member(env, 64)
    assert await say(env, stranger, "hint") is None


async def test_status_shows_the_funnel_and_who_shows_the_new_columns(env):
    a, _ = await through_stage2(env, 71), None
    b, pb = await through_stage5(env, 72)
    text = (await env.cog._status_embed(env.g)).description
    assert "funnel" in text and "stage 3: 1" in text and "stage 6: 1" in text and "solved 0" in text

    class Ctx:
        def __init__(self):
            self.guild, self.out = env.g, []

        async def send(self, content=None, **kw):
            self.out.append(content)
    ctx = Ctx()
    await env.cog.secrethunt_who.callback(env.cog, ctx, b)
    assert "stage 6" in ctx.out[0] and "courtesies paid 0/4" in ctx.out[0] and "hints 0" in ctx.out[0]
    await say(env, b, pb["answer"])
    ctx = Ctx()
    await env.cog.secrethunt_who.callback(env.cog, ctx, b)
    assert "solved" in ctx.out[0]


async def test_secrets_command_lists_a_solved_trail_with_its_hints_and_days(env):
    m, p = await through_stage5(env, 81)
    env.clock.now += 3 * 86400
    await say(env, m, p["answer"])

    class Ctx:
        def __init__(self):
            self.guild, self.author, self.out = env.g, m, []

        async def send(self, content=None, **kw):
            self.out.append(kw.get("embed") or content)
    ctx = Ctx()
    await env.cog.secrets_cmd.callback(env.cog, ctx, m)
    text = ctx.out[0].description
    assert "Treacle" in text and "no hints" in text and "solved in 3 days" in text


async def test_preview_shows_stages_five_and_six_and_the_answer_matches_the_plan(env):
    await env.cog._apply_preview(env.g, env.admin, PID)
    text = env.admin.dms[-1]
    p = plan_of(env, env.admin.id)
    assert f"`{p['answer']}`" in text and trail.tale_text(p) in text and "Stage 5:" in text
    for k in range(4):
        assert p["courtesy_buttons"][k].title() in text and f"<#{env.rooms[p['courtesy_rooms'][k]].id}>" in text


async def test_the_whole_trail_runs_in_the_test_channel_to_the_end(env):
    box, _ = await rehearse(env)
    await wake_in(env, box, env.admin)
    uid = env.admin.id
    p = plan_of(env, uid)

    def tbl(room):
        return next(m for m in tables_in(box) if m.embed.title.endswith(NAMES[room]))

    async def press(room, key):
        inter = tk.FakeInteraction(env.g, env.admin, tbl(room))
        await env.cog.handle_trail(inter, key)
        return inter.replies[0][0]

    for room in p["chain"]:
        await press(room, "look")
    await pass_stage3(env, env.admin)
    for _ in range(p["pour_total"]):
        await press(p["pour_room"], "pour")
    await press(p["toast_room"], "toast")
    for k in range(4):
        await press(p["courtesy_rooms"][k], p["courtesy_buttons"][k])
    assert (await state(env, env.admin))["step"] == 6
    assert await say(env, env.admin, p["answer"]) == WELL["solved"].format(rank="\n\nYou were finder **#1**.")


async def test_twenty_synthetic_members_play_the_whole_puzzle_through_the_cog(env):
    members = []
    for uid in range(200, 220):
        m, p = await through_stage5(env, uid)
        members.append((m, p))
    for m, p in members:
        await say(env, m, p["answer"])
    assert len((await env.conf.finishers())[PID]) == 20
    for m, _ in members:
        assert (await state(env, m))["stage"] == engine.STAGE_SOLVED
    assert len({p["answer"] for _, p in members}) > 15  # personal answers: not one shared word


async def test_a_member_cannot_use_another_members_answer(env):
    a, pa = await through_stage5(env, 91)
    b, pb = await through_stage5(env, 92)
    if pa["answer"] == pb["answer"]:
        pytest.skip("the two members happen to share an answer")
    assert "not the bottom" in await say(env, b, pa["answer"])


def test_the_new_stage_texts_format_cleanly():
    keys = dict(until="<t:1:R>", token="t", verses="v", tale="a, b", hint="h", n=1, max=2, when="<t:1:R>", done=2, pointer="p")
    for field in ("stage4_done", "stage5_done", "courtesy_lost", "courtesy_rest", "courtesy_rest_start", "answer_wrong",
                  "answer_rest", "hint", "hint_early", "hint_max", "verse_place"):
        assert "{" not in WELL[field].format_map(keys), field
    for text in WELL["courtesy_ok"].values():
        assert "{" not in text.format(done=1)


def test_the_readme_and_info_never_contain_a_token_word_an_answer_an_emoji_or_a_verse():
    import json
    import os
    here = os.path.dirname(__file__)
    readme = open(os.path.join(here, "README.md")).read().lower()
    info = open(os.path.join(here, "info.json")).read().lower()
    for text, name in ((readme, "README"), (info, "info.json")):
        for word in trail.TOKEN_WORDS:
            assert not re.search(rf"\b{word}\b", text), (name, word)
        for group in trail.EMOJI_SLOTS:
            assert not any(e.lower() in text for e in group), name
        for verse in WELL["verses"].values():
            assert verse.split("\n")[0].strip("*").lower() not in text, name
        for rule in trail.RULES:
            assert not re.search(r"\b" + re.escape(trail.rule_text(rule).lower()) + r"\b", text), (name, rule)
    json.loads(info)


import re  # noqa: E402


async def test_hint_goes_to_an_open_knaves_trial_round_not_the_dormouse(env):
    from secrethunt import trial
    m = await through_stage2(env, 66)
    extra = [env.g.add_thread(900 + i, f"witness{i}") for i in range(5)]
    await env.conf.pool.set([c.id for c in extra])
    await env.conf.published.set([PID, "knaves_trial"])
    async with env.cog.config.member(m).puzzles() as states:
        states["knaves_trial"] = trial.ensure(engine.new_state(0, env.clock.now))
    reply = await say(env, m, "hint")
    assert reply is not None and "Dormouse" not in reply  # the trial answered (too early for its first hint)
    assert (await state(env, m))["hints"] == 0
    async with env.cog.config.member(m).puzzles() as states:
        states["knaves_trial"]["stage"] = engine.STAGE_SOLVED  # round over: the Dormouse may answer again
    env.clock.now += 86400
    assert "Dormouse" in await say(env, m, "hint")


async def test_a_suspects_name_on_stage_six_is_not_taken_for_a_wrong_answer(env):
    m, p = await through_stage5(env, 67)
    assert await say(env, m, "Hatter") is None
    assert (await state(env, m))["answer_wrongs"] == 0


async def test_the_shipped_puzzle_is_open_to_ordinary_members_and_can_be_scheduled(env):
    assert "wip" not in WELL  # WELL is the shipped definition; the fixture swaps a copy into the shared dict
    ordinary = member(env, 300)
    ordinary.guild_permissions = types.SimpleNamespace(administrator=False, manage_guild=False)
    assert env.cog._can_play(ordinary, WELL) is True
    await env.conf.published.set([])
    when = pacific(env.clock.now + 7200)
    assert "will launch" in await env.cog._apply_launch(env.g, env.admin, {"puzzle": "treacle", "when": when, "channel": f"<#{env.poem.id}>"})


async def test_an_ordinary_member_plays_stage_one_through_the_real_flags(env, monkeypatch):
    monkeypatch.setitem(env.mod.PUZZLES, PID, dict(WELL))  # exactly as shipped, no test override
    ordinary = member(env, 301)
    ordinary.guild_permissions = types.SimpleNamespace(administrator=False, manage_guild=False)
    await wake(env, ordinary)
    assert (await state(env, ordinary)) is not None and ordinary.dms


async def test_the_launch_form_finds_a_channel_by_name_with_or_without_hash_and_emoji(fresh):
    news = fresh.g.add_channel(700, "\N{PUBLIC ADDRESS LOUDSPEAKER}\N{KATAKANA MIDDLE DOT}announcements")
    fresh.g.add_channel(701, "mod-chat")
    for typed in ("announcements", "#announcements", "#Announcements", "announce", f"<#{news.id}>", str(news.id)):
        await schedule(fresh, channel=typed)
        assert (await fresh.conf.launches())[PID]["channel"] == news.id, typed


async def test_an_unknown_or_ambiguous_channel_name_is_refused_with_a_helpful_message(fresh):
    fresh.g.add_channel(710, "general-chat")
    fresh.g.add_channel(711, "general-talk")
    with pytest.raises(PanelError, match="several channels"):
        await schedule(fresh, channel="general")
    with pytest.raises(PanelError, match="couldn't find a channel called"):
        await schedule(fresh, channel="nonexistent-room")
    assert await fresh.conf.launches() == {}


# ---------------------------------------------------------------- old tea tables after the rooms were re-pasted

async def repaste(env):
    """What happens when the trail rooms are pasted again: the cog forgets its tables (the old ones stay pinned)."""
    await env.cog._apply_trail_text(env.g, env.admin, {"rooms": " ".join(f"<#{r.id}>" for r in env.rooms)})
    assert PID not in await env.conf.objects()


async def test_an_old_pinned_table_keeps_working_after_the_rooms_are_pasted_again(env):
    m = member(env, 400)
    await wake(env, m)
    p = plan_of(env, 400)
    old = table(env, p["chain"][0])
    await repaste(env)
    inter = tk.FakeInteraction(env.g, m, old)
    await env.cog.handle_trail(inter, "look")
    assert str(p["numbers"][0]) in inter.replies[0][0] and inter.replies[0][0] != "Nothing happens."
    assert (await state(env, m))["found"] == 1


async def test_setting_up_again_replaces_the_old_tables_so_each_room_holds_one(env):
    old = [table(env, i) for i in range(12)]
    await repaste(env)
    report = await env.cog._setup_objects(env.g, PID)
    assert sum("removed 1 old tea table" in line for line in report) == 12
    for i, room in enumerate(env.rooms):
        pinned = [m for m in room.sent if m.pinned and m.kw.get("view") is not None]
        assert len(pinned) == 1 and pinned[0] is not old[i] and old[i].deleted and not old[i].pinned
    assert sorted((await env.conf.objects())[PID]) == sorted(str(r.id) for r in env.rooms)


async def test_a_lookalike_from_someone_else_or_in_another_channel_is_still_ignored(env, caplog):
    m = member(env, 401)
    await wake(env, m)
    other = env.g.add_channel(950, "elsewhere")
    stranger = tk.FakeMember(env.g, 402)
    fake_here = await env.rooms[0].send(embed=table(env, 0).embed, view=object())
    fake_here.author = stranger
    fake_there = await other.send(embed=table(env, 0).embed, view=object())
    await repaste(env)
    for message in (fake_here, fake_there):
        inter = tk.FakeInteraction(env.g, m, message)
        await env.cog.handle_trail(inter, "look")
        assert inter.replies[0][0] == "Nothing happens."
    assert "tea table click ignored" in caplog.text


async def test_a_click_that_is_ignored_says_why_in_the_log(env, caplog):
    m = member(env, 403)
    await wake(env, m)
    await env.conf.enabled.set(False)
    inter = tk.FakeInteraction(env.g, m, table(env, 0))
    await env.cog.handle_trail(inter, "look")
    assert inter.replies[0][0] == "Nothing happens." and "the hunt is switched off" in caplog.text


async def test_set_up_objects_reposts_a_table_that_is_gone_and_keeps_a_live_one(env):
    first = table(env, 0)
    first.deleted = True  # someone deleted the recorded table in room 0
    report = await env.cog._setup_objects(env.g, PID)
    assert any("recorded tea table was gone" in line for line in report)
    assert sum(1 for line in report if "already set up" in line) == len(env.rooms) - 1
    fresh = [m for m in env.rooms[0].sent if m.kw.get("view") is not None and not m.deleted]
    assert len(fresh) == 1 and fresh[0].id != first.id
    objects = await env.conf.objects()
    assert objects[PID][str(env.rooms[0].id)] == fresh[0].id


async def test_an_admin_is_told_why_a_press_was_ignored_but_a_member_is_not(env):
    stray = await env.rooms[0].send("hi")
    adm = tk.FakeInteraction(env.g, env.admin, stray)
    await env.cog.handle_trail(adm, "look")
    assert "not a tea table" in adm.replies[0][0] and adm.replies[0][1] == {"ephemeral": True}
    m = member(env, 12)
    inter = tk.FakeInteraction(env.g, m, stray)
    await env.cog.handle_trail(inter, "look")
    assert inter.replies == [("Nothing happens.", {"ephemeral": True})]


async def test_an_unanswered_press_is_recorded_and_shown_in_status(env):
    m = member(env, 13)
    stray = await env.rooms[0].send("hi")
    await env.cog.handle_trail(tk.FakeInteraction(env.g, m, stray), "look")
    entries = await env.conf.click_log()
    assert len(entries) == 1 and entries[0][2] == 13 and "not a tea table" in entries[0][1]
    embed = await env.cog._status_embed(env.g)
    assert "Unanswered tea table clicks" in embed.description and "<@13>" in embed.description
    assert len(embed.description) < 4000
    for _ in range(12):
        await env.cog.handle_trail(tk.FakeInteraction(env.g, m, stray), "look")
    assert len(await env.conf.click_log()) == 8
