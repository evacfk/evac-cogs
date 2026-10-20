"""Behaviour tests for The Looking Glass (Easy #2): riddle poem, pointer riddle, pressed button object with private
replies, emoji rebus. Run against fakes: they prove the logic and clean imports under the stub, not live Discord
behaviour (persistent buttons, ephemeral replies, pins in forum posts)."""
import importlib
import sys
import types
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from secrethunt import _testkit as tk
from secrethunt import engine
from secrethunt.constants import WATCH_EMOJI
from secrethunt.puzzles import PUZZLES
from secrethunt.settings_panel import PanelError

LA = ZoneInfo("America/Los_Angeles")
PID = "looking_glass"
NAMES = ["Gaming", "Music", "Memes", "Movies", "Cuddle"]
GLASS = PUZZLES[PID]
MODULES = ("secrethunt.secrethunt", "secrethunt.embeds", "secrethunt.views")


def ts(d, h=12, mi=0):
    return datetime(2026, 10, d, h, mi, tzinfo=LA).timestamp()


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


async def state(env, m):
    return (await env.cog.config.member(m).puzzles()).get(PID)


async def objects(env):
    return (await env.conf.objects())[PID]


async def start(env, m):
    await env.cog._on_guild_message(chat(env, m, ".mirror"))
    return await state(env, m)


def message_in(env, channel):
    """The pinned glass in a room (the message the buttons live on)."""
    return next(m for m in channel.sent if m.kw.get("view") is not None)


async def press(env, m, channel, key, now=None):
    inter = tk.FakeInteraction(env.g, m, message_in(env, channel))
    await env.cog.handle_click(inter, key)
    assert len(inter.replies) == 1 and inter.replies[0][1] == {"ephemeral": True}  # always one private reply
    return inter.replies[0][0]


async def right(env, m):
    st = await state(env, m)
    return env.chans[st["variant"]], GLASS["buttons"][st["variant"]]


async def wrong_button(env, m):
    ch, key = await right(env, m)
    return ch, next(k for k in ("drink", "eat", "knock", "look") if k != key)


async def at_final(env, uid=11):
    m = member(env, uid)
    await start(env, m)
    ch, key = await right(env, m)
    await press(env, m, ch, key)
    await env.cog._deliver_due(env.g, (await state(env, m))["due_ts"] + 1)
    return m, GLASS["answers"][(await state(env, m))["variant"]]


# ---------------------------------------------------------------- setup and trigger


async def test_setup_pins_one_button_message_in_every_room(env):
    for ch in env.chans:
        glass = message_in(env, ch)
        assert glass.pinned and glass.kw["view"] is not None and glass.embed.title == "A looking glass"
    assert len((await objects(env))) == 5


async def test_cog_load_registers_the_persistent_buttons_and_unload_stops_them(env):
    await env.cog.cog_load()
    assert env.bot.views == [env.cog._view, env.cog._witness_view]  # the glass buttons and the trial's witness button
    stopped = []
    env.cog._view.stop = lambda: stopped.append("glass")  # works with the repo's stub View and with real discord.py
    env.cog._witness_view.stop = lambda: stopped.append("witness")
    env.cog.cog_unload()
    assert stopped == ["glass", "witness"]


async def test_the_poem_word_starts_the_chain_and_the_message_is_deleted(env):
    m = member(env, 11)
    msg = chat(env, m, ".mirror")
    await env.cog._on_guild_message(msg)
    assert msg.deleted and (await state(env, m))["stage"] == engine.STAGE_RIDDLE
    assert len(m.dms) == 1 and "ripples" in m.dms[0]


async def test_the_other_puzzles_word_does_nothing_here(env):
    m = member(env, 11)
    msg = chat(env, m, ".tick")  # Late Rabbit is not published in this fixture
    await env.cog._on_guild_message(msg)
    assert not msg.deleted and m.dms == []


async def test_a_dm_trigger_works_too(env):
    m = member(env, 11)
    await env.cog._on_dm(dm(m, "mirror"))
    assert (await state(env, m))["stage"] == engine.STAGE_RIDDLE


# ---------------------------------------------------------------- the pointer riddle


async def test_the_riddle_points_at_the_room_in_words_never_as_a_jumble(env):
    for v in range(5):
        text = env.cog._riddle_text(env.g, GLASS, [c.id for c in env.chans], v)
        assert "letters)" not in text and "{" not in text
        assert engine.object_hint(GLASS, v) in text
        assert NAMES[v].lower() not in text.lower().replace("games", "")  # the room's own name is never given away


async def test_the_pointer_styles_are_mixed_across_variants(env):
    pool = [c.id for c in env.chans]
    texts = [env.cog._riddle_text(env.g, GLASS, pool, v) for v in range(5)]
    letter = [v for v, t in enumerate(texts) if "ends in" in t]
    assert letter == GLASS["letter_variants"]  # exactly the letter-clue variants use the letter clue
    assert len(letter) < 5  # and the rest are written descriptions


async def test_a_room_no_keyword_fits_falls_back_to_the_letter_clue(env):
    env.chans[2].name = "Artwork"
    text = env.cog._riddle_text(env.g, GLASS, [c.id for c in env.chans], 2)
    assert "6 letters" not in text and "7 letters and ends in **K**" in text


async def test_a_missing_room_falls_back_to_its_mention_without_crashing(env):
    pool = [c.id for c in env.chans[:4]] + [999]
    text = env.cog._riddle_text(env.g, GLASS, pool, 4)
    assert "a room I cannot name" in text and engine.object_hint(GLASS, 4) in text


# ---------------------------------------------------------------- the object (buttons)


async def test_right_room_and_right_button_finds_the_glass_privately(env):
    m = member(env, 11)
    await start(env, m)
    ch, key = await right(env, m)
    reply = await press(env, m, ch, key)
    st = await state(env, m)
    assert reply == GLASS["found"] and st["stage"] == engine.STAGE_FOUND and st["due_ts"] > st["found_ts"]
    assert len(m.dms) == 1  # only the riddle: the result of a press is never sent by DM


async def test_right_room_wrong_button_is_a_decoy_then_a_lockout(env):
    m = member(env, 11)
    await start(env, m)
    ch, key = await wrong_button(env, m)
    assert await press(env, m, ch, key) == GLASS["decoy"]
    assert await press(env, m, ch, key) == GLASS["lockout"]
    st = await state(env, m)
    assert st["stage"] == engine.STAGE_RIDDLE and st["locked_until"] > 0
    ch2, key2 = await right(env, m)
    assert await press(env, m, ch2, key2) == GLASS["locked"]  # even the right press does nothing while locked out
    assert (await state(env, m))["stage"] == engine.STAGE_RIDDLE


async def test_right_button_in_the_wrong_room_is_a_decoy(env):
    m = member(env, 11)
    await start(env, m)
    ch, key = await right(env, m)
    other = env.chans[(env.chans.index(ch) + 1) % 5]
    assert await press(env, m, other, key) == GLASS["decoy"]
    assert (await state(env, m))["decoys"] == 1


async def test_a_press_before_starting_or_after_finding_changes_nothing(env):
    m = member(env, 11)
    ch = env.chans[0]
    assert await press(env, m, ch, "look") == GLASS["idle"]
    assert await state(env, m) is None  # never creates progress
    await start(env, m)
    ch, key = await right(env, m)
    await press(env, m, ch, key)
    before = dict(await state(env, m))
    assert await press(env, m, ch, key) == GLASS["idle"]
    assert await state(env, m) == before


async def test_buttons_do_nothing_when_the_hunt_is_off(env):
    m = member(env, 11)
    await start(env, m)
    await env.conf.enabled.set(False)
    ch, key = await right(env, m)
    assert await press(env, m, ch, key) == "Nothing happens."
    assert (await state(env, m))["stage"] == engine.STAGE_RIDDLE


async def test_a_press_on_some_other_message_is_just_a_quiet_reply(env):
    m = member(env, 11)
    await start(env, m)
    stray = tk.FakeMessage(env.chans[0], content="hello")
    inter = tk.FakeInteraction(env.g, m, stray)
    await env.cog.handle_click(inter, "look")
    assert inter.replies == [("Nothing happens.", {"ephemeral": True})]
    assert (await state(env, m))["stage"] == engine.STAGE_RIDDLE


async def test_a_clock_reaction_on_a_button_object_is_ignored(env):
    m = member(env, 11)
    await start(env, m)
    ch, _ = await right(env, m)
    glass = message_in(env, ch)
    payload = types.SimpleNamespace(guild_id=env.g.id, user_id=m.id, message_id=glass.id, channel_id=ch.id,
                                    emoji=WATCH_EMOJI)
    await env.cog._on_reaction(payload)
    assert glass.removed == [] and (await state(env, m))["stage"] == engine.STAGE_RIDDLE and (await state(env, m))["decoys"] == 0


async def test_a_failed_reply_never_breaks_the_cog(env):
    m = member(env, 11)
    await start(env, m)

    class Boom(tk.FakeInteraction):
        async def _send(self, content=None, **kw):
            raise tk.http_error("NotFound")

    ch, key = await right(env, m)
    inter = Boom(env.g, m, message_in(env, ch))
    inter.response = types.SimpleNamespace(send_message=inter._send)
    await env.cog.handle_click(inter, key)  # must not raise
    assert (await state(env, m))["stage"] == engine.STAGE_FOUND


# ---------------------------------------------------------------- the rebus and the finish


async def test_the_delayed_clue_is_a_rebus_with_a_letter_count_not_a_jumble(env):
    m, answer = await at_final(env)
    final = m.dms[-1]
    v = (await state(env, m))["variant"]
    assert GLASS["rebuses"][v] in final and f"({len(answer)} letters)" in final
    assert answer not in final and (await state(env, m))["stage"] == engine.STAGE_FINAL


async def test_the_right_word_solves_it_and_lists_the_member(env):
    m, answer = await at_final(env)
    await env.cog._on_dm(dm(m, answer.upper()))
    assert (await state(env, m))["stage"] == engine.STAGE_SOLVED
    assert GLASS["solved"].split("{")[0] in m.dms[-1] or "Hall of Secrets" in m.dms[-1]
    hall = next(msg for msg in env.hall.sent if msg.embed.title == "Easy \N{MIDDLE DOT} The Looking Glass")
    assert "1. user11" in hall.embed.description
    assert m.roles == []  # Easy never has a role
    line = env.announce.sent[-1].content
    assert "The Looking Glass" in line and "(Easy)" in line


async def test_a_wrong_word_gets_a_gentle_reply_and_the_chain_stays_open(env):
    m, answer = await at_final(env)
    await env.cog._on_dm(dm(m, "teapot"))
    assert m.dms[-1] in GLASS["wrong"] and (await state(env, m))["stage"] == engine.STAGE_FINAL


async def test_each_variant_has_its_own_answer_so_one_solver_cannot_leak_it(env):
    seen = set()
    for uid in range(1, 40):
        m = member(env, uid)
        seen.add((await start(env, m))["variant"])
    assert seen == {0, 1, 2, 3, 4}
    assert len(set(GLASS["answers"])) == 5 and len(set(GLASS["rebuses"])) == 5


async def run(ctx, coro):
    """Await an _apply_* coroutine the way the panel does: its message (or PanelError text) is what the admin sees."""
    try:
        ctx.out.append(await coro)
    except PanelError as exc:
        ctx.out.append(str(exc))


class Ctx:
    def __init__(self, env):
        self.guild, self.out = env.g, []
        self.author = member(env, 1, name="admin")
        self.channel = env.chans[0]
        self.message = types.SimpleNamespace(attachments=[])

    async def send(self, content=None, **kw):
        self.out.append(content if content is not None else kw.get("embed"))

    async def tick(self):
        self.out.append("tick")


async def test_preview_shows_the_pointer_button_rebus_and_answer_for_every_variant(env):
    ctx = Ctx(env)
    await env.cog.secrethunt_preview.callback(env.cog, ctx, PID)
    text = ctx.author.dms[-1]
    assert text.count("Variant") == 5 and "trigger word: `mirror`" in text
    for v in range(5):
        assert GLASS["answers"][v] in text and GLASS["rebuses"][v] in text and f"button `{GLASS['buttons'][v]}`" in text
    assert "ends in **K**" not in text and "ends in" in text  # the letter-clue variants are visible to the admin


async def test_the_riddle_poem_is_posted_pinned_and_hides_no_acrostic(env):
    ctx = Ctx(env)
    ch = env.chans[1]
    await run(ctx, env.cog._apply_poem(env.g, ctx.author, ch, PID))
    poem = next(m for m in ch.sent if m.embed is not None and m.pinned and "Queen forgot" in (m.embed.title or ""))
    assert "mirror" not in poem.embed.description.lower()
    assert (await env.conf.poems())[str(ch.id)] == poem.id


async def test_status_counts_the_second_puzzle(env):
    await start(env, member(env, 11))
    text = (await env.cog._status_embed(env.g)).description
    assert "The Looking Glass" in text and "1 started, 0 solved" in text


async def test_setup_again_skips_rooms_that_already_have_the_glass(env):
    ctx = Ctx(env)
    await run(ctx, env.cog._apply_setup(env.g, ctx.author, PID, False))
    assert ctx.out[-1].count("already set up") == 5
    assert sum(1 for ch in env.chans for m in ch.sent if m.kw.get("view") is not None) == 5


async def test_both_puzzles_can_run_side_by_side(env):
    await env.conf.published.set(["late_rabbit", PID])
    await env.cog._setup_objects(env.g, "late_rabbit")
    m = member(env, 11)
    await env.cog._on_guild_message(chat(env, m, ".tick"))
    await env.cog._on_guild_message(chat(env, m, ".mirror"))
    states = await env.cog.config.member(m).puzzles()
    assert set(states) == {"late_rabbit", PID}
    # a clock reaction still works on the Rabbit's watch and the Glass's buttons still work on the Glass
    ch, key = await right(env, m)
    assert await press(env, m, ch, key) == GLASS["found"]


# ---------------------------------------------------------------- the Easy "you have to type" hint lives in the poem


def _poem_text(puzzle):
    return engine.normalize(" ".join(puzzle["poem"]))


def test_every_easy_poem_carries_its_typing_hint_inside_the_verse():
    for pid, puzzle in PUZZLES.items():
        if puzzle["tier"] != "easy":
            continue
        hint = puzzle["typing_hint"]
        assert engine.normalize(hint) in _poem_text(puzzle), pid  # the phrase really is in the poem
        assert engine.normalize(puzzle["trigger"]) not in engine.normalize(hint)  # and never gives the word away
        assert not any(w in hint.lower() for w in ("react", "click", "press", "command", "prefix", "dot"))


def test_no_poem_has_a_footer_telling_players_what_to_do():
    embeds = importlib.import_module("secrethunt.embeds")
    assert not hasattr(importlib.import_module("secrethunt.constants"), "EASY_NUDGE")
    assert "typing" not in embeds.intro_embed([]).description


async def test_the_posted_glass_poem_contains_the_hint(env):
    await env.cog._apply_poem(env.g, member(env, 1), env.chans[1], PID)
    posted = next(m for m in env.chans[1].sent if m.embed is not None and "Queen forgot" in (m.embed.title or ""))
    assert posted.pinned and "use your fingers" in posted.embed.description
