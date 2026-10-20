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
    assert await say(env, m, "recap") == WELL["sisters_done_dm"]


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
