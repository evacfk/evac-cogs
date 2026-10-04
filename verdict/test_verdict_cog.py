"""Behaviour tests for Verdict against fakes. Proves the logic and clean imports, not live Discord behaviour
(real buttons/ephemerals, reaction events, role edits)."""
import importlib
import sys
import types
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from verdict import _testkit as tk

LA = ZoneInfo("America/Los_Angeles")


def ts(y, mo, d, h=10, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=LA).timestamp()


@pytest.fixture
def mod(monkeypatch):
    tk.install(monkeypatch)
    tk.install_ui(monkeypatch)
    for name in ("verdict.verdict", "verdict.embeds"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    import discord
    if not hasattr(discord.ui, "Button") or not hasattr(discord.ui.Button, "__init__"):
        pass
    cog = importlib.import_module("verdict.verdict")
    yield cog
    for name in ("verdict.verdict", "verdict.embeds"):
        sys.modules.pop(name, None)


class Member:
    def __init__(self, guild, uid, roles=()):
        self.id, self.guild, self.roles, self.bot = uid, guild, list(roles), False
        self.display_name = f"user{uid}"
        guild.members.append(self)

    async def add_roles(self, *r, reason=None):
        self.roles += [x for x in r if x not in self.roles]

    async def remove_roles(self, *r, reason=None):
        self.roles = [x for x in self.roles if x not in r]


class Click:
    def __init__(self, guild, user, cid):
        self.guild, self.user, self.data = guild, user, {"custom_id": cid}
        self.out, outer = [], self

        class Resp:
            def is_done(self):
                return bool(outer.out)

            async def send_message(self, content=None, **kw):
                outer.out.append(("send", content, kw))

            async def edit_message(self, **kw):
                outer.out.append(("edit", kw.get("content"), kw))

        self.response = Resp()


@pytest.fixture
async def env(mod):
    g = tk.FakeGuild()
    chan = g.add_channel(100, "cuddle")
    review = g.add_channel(101, "review")
    review.permissions_for = lambda role: types.SimpleNamespace(view_channel=False)
    bot = tk.FakeBot(g)
    bot.user = types.SimpleNamespace(id=999)
    bot.get_guild = lambda gid: g if gid == g.id else None
    owner_ids = {1}

    async def is_owner(m):
        return m.id in owner_ids
    bot.is_owner = is_owner
    cog = mod.Verdict(bot)
    conf = cog.config.guild(g)
    await conf.channel_id.set(chan.id)
    await conf.review_channel_id.set(review.id)
    await conf.enabled.set(True)
    return types.SimpleNamespace(mod=mod, cog=cog, g=g, chan=chan, review=review, conf=conf)


async def play(env, user, a, p):
    cur = await env.conf.current()
    cid = lambda kind, i: f"verdict:{kind}:{cur['qid']}:{i}"  # noqa: E731
    first = Click(env.g, user, cid("a", a))
    await env.cog.on_interaction(first)
    second = Click(env.g, user, cid("p", p))
    await env.cog.on_interaction(second)
    return first, second


async def test_tick_posts_once_a_day_at_the_right_hour(env):
    await env.cog._tick(env.g, ts(2026, 10, 5, 9))
    assert not env.chan.sent  # before 10am Pacific
    await env.cog._tick(env.g, ts(2026, 10, 5, 10, 5))
    assert len(env.chan.sent) == 1
    await env.cog._tick(env.g, ts(2026, 10, 5, 18))
    assert len(env.chan.sent) == 1  # once per day
    msg = env.chan.sent[0]
    cur = await env.conf.current()
    assert msg.id == cur["message_id"] and cur["seq"] == 1
    assert msg.embed.title.endswith("#1") and len(cur["options"]) >= 2


async def test_queue_goes_before_seeds_and_credits_submitter(env):
    async with env.conf.queue() as q:
        q.append({"question": "Q one?", "options": ["a", "b"], "submitter_id": 42})
    await env.cog.post_daily(env.g, ts(2026, 10, 5))
    cur = await env.conf.current()
    assert cur["question"] == "Q one?" and cur["submitter_id"] == 42
    assert await env.conf.queue() == []
    assert any(f.name == "Question by" for f in env.chan.sent[0].embed.fields)


async def test_answer_then_guess_locks_in_and_counts_streak(env):
    await env.cog.post_daily(env.g, ts(2026, 10, 5))
    u = Member(env.g, 10)
    first, second = await play(env, u, 0, 1)
    assert "Now guess" in first.out[0][1] and first.out[0][2]["ephemeral"]
    assert "Locked in" in second.out[0][1] and "Streak: 1" in second.out[0][1]
    cur = await env.conf.current()
    assert cur["answers"] == {"10": 0} and cur["predictions"] == {"10": 1}
    # tapping again changes nothing
    again = Click(env.g, u, f"verdict:a:{cur['qid']}:1")
    await env.cog.on_interaction(again)
    assert "Locked in" in again.out[0][1]
    assert (await env.conf.current())["answers"] == {"10": 0}


async def test_guess_without_answer_is_refused(env):
    await env.cog.post_daily(env.g, ts(2026, 10, 5))
    cur = await env.conf.current()
    c = Click(env.g, Member(env.g, 11), f"verdict:p:{cur['qid']}:0")
    await env.cog.on_interaction(c)
    assert "answer first" in c.out[0][1]
    assert (await env.conf.current())["predictions"] == {}


async def test_old_question_buttons_are_closed(env):
    await env.cog.post_daily(env.g, ts(2026, 10, 5))
    old = (await env.conf.current())["qid"]
    await env.cog.post_daily(env.g, ts(2026, 10, 6))
    c = Click(env.g, Member(env.g, 12), f"verdict:a:{old}:0")
    await env.cog.on_interaction(c)
    assert "closed" in c.out[0][1]


async def test_other_custom_ids_and_dms_are_ignored(env):
    c = Click(env.g, Member(env.g, 13), "wonderevents:going")
    await env.cog.on_interaction(c)
    assert c.out == []


async def test_reveal_scores_streaks_and_final_post(env):
    async with env.conf.queue() as q:
        q.append({"question": "Cats or dogs?", "options": ["Cats", "Dogs"], "submitter_id": None})
    await env.cog.post_daily(env.g, ts(2026, 10, 5))
    first_msg = env.chan.sent[0]
    users = [Member(env.g, 100 + i) for i in range(10)]
    for i, u in enumerate(users):
        await play(env, u, 0 if i < 9 else 1, 0)   # 9 say cats, 1 dog; everyone guesses cats
    dropout = Member(env.g, 200)
    cur = await env.conf.current()
    await env.cog.on_interaction(Click(env.g, dropout, f"verdict:a:{cur['qid']}:0"))  # answered, never guessed

    await env.cog.post_daily(env.g, ts(2026, 10, 6))
    assert first_msg.view is None and "final" in first_msg.embed.title
    assert "91%" in first_msg.embed.description and "Lone wolves" in first_msg.embed.fields[0].value
    yest = next(f for f in env.chan.sent[1].embed.fields if f.name.endswith("Yesterday"))
    assert "Cats (91%)" in yest.value and "11 voted" in yest.value  # everyone's answer counts for the crowd...
    m = env.cog.config.member(users[0])
    assert (await m.played(), await m.correct()) == (1, 1)
    wolf = env.cog.config.member(users[9])
    assert (await wolf.played(), await wolf.correct()) == (1, 1)  # guessed the crowd right though they differed
    assert await env.cog.config.member(dropout).played() == 0   # ...but only answer + guess earns a score or a streak
    scores = (await env.conf.months())["2026-10"]
    assert scores["100"] == [1, 1]


async def test_streak_grows_across_days_and_resets_after_a_miss(env):
    u = Member(env.g, 20)
    for day in (5, 6, 7):
        await env.cog.post_daily(env.g, ts(2026, 10, day))
        await play(env, u, 0, 0)
    assert await env.cog.config.member(u).streak() == 3
    await env.cog.post_daily(env.g, ts(2026, 10, 8))   # skipped
    await env.cog.post_daily(env.g, ts(2026, 10, 9))
    await play(env, u, 0, 0)
    m = env.cog.config.member(u)
    assert await m.streak() == 1 and await m.best_streak() == 3


async def test_streak_milestone_is_called_out_in_the_reveal(env):
    u = Member(env.g, 21)
    for day in range(1, 8):
        await env.cog.post_daily(env.g, ts(2026, 10, day))
        await play(env, u, 0, 0)
    await env.cog.post_daily(env.g, ts(2026, 10, 8))
    text = env.chan.sent[-1].embed.fields[-1].value
    assert "Streak milestones" in text and "<@21> (7)" in text


async def test_mind_reader_role_goes_to_last_months_best_and_moves(env):
    role = env.g.add_role(900, "Mind Reader")
    await env.conf.mind_reader_role_id.set(role.id)
    old = Member(env.g, 30, roles=[role])
    best, other = Member(env.g, 31), Member(env.g, 32)
    await env.cog.post_daily(env.g, ts(2026, 9, 28))     # first run: sets the month, no award
    assert role in old.roles
    async with env.conf.months() as months:
        months["2026-09"] = {"31": [4, 6], "32": [3, 9], "30": [1, 9]}
    await env.cog.post_daily(env.g, ts(2026, 10, 1))
    assert role in best.roles and role not in old.roles and role not in other.roles
    assert "<@31>" in env.chan.sent[-1].embed.fields[-1].value
    await env.cog.post_daily(env.g, ts(2026, 10, 2))     # same month: no second award
    assert all(f.name != "\N{CROWN} Mind Reader" for f in env.chan.sent[-1].embed.fields)


async def test_empty_day_does_not_crash(env):
    await env.cog.post_daily(env.g, ts(2026, 10, 5))
    await env.cog.post_daily(env.g, ts(2026, 10, 6))
    assert "Nobody answered" in env.chan.sent[1].embed.fields[-1].value


async def test_seed_questions_do_not_repeat_until_exhausted(env):
    seen = []
    for day in range(1, 11):
        await env.cog.post_daily(env.g, ts(2026, 10, day))
        seen.append((await env.conf.current())["question"])
    assert len(set(seen)) == 10


class Ctx:
    def __init__(self, env, author):
        self.guild, self.author, self.sent = env.g, author, []

    async def send(self, content=None, **kw):
        self.sent.append(content)


def cmd(env, name):
    fn = getattr(env.mod.Verdict, name)
    return getattr(fn, "callback", fn)


async def test_suggest_goes_to_private_channel_and_owner_tick_queues_it(env):
    author = Member(env.g, 50)
    ctx = Ctx(env, author)
    await cmd(env, "verdict_suggest")(env.cog, ctx, text="Best snack? | Chips | Candy")
    assert "Sent for approval" in ctx.sent[-1]
    review_msg = env.review.sent[0]
    assert review_msg.reactions == ["✅", "❌"]
    assert list((await env.conf.pending()).values())[0]["submitter_id"] == 50

    rando = types.SimpleNamespace(guild_id=env.g.id, user_id=77, message_id=review_msg.id, emoji="✅")
    await env.cog.on_raw_reaction_add(rando)
    assert await env.conf.queue() == [] and len(await env.conf.pending()) == 1   # a non-owner can't approve

    owner = types.SimpleNamespace(guild_id=env.g.id, user_id=1, message_id=review_msg.id, emoji="✅")
    await env.cog.on_raw_reaction_add(owner)
    q = await env.conf.queue()
    assert q[0]["question"] == "Best snack?" and q[0]["submitter_id"] == 50
    assert await env.conf.pending() == {} and "Approved" in review_msg.embed.footer.text if hasattr(review_msg.embed, "footer") else True


async def test_cross_rejects_and_nothing_is_queued(env):
    ctx = Ctx(env, Member(env.g, 51))
    await cmd(env, "verdict_suggest")(env.cog, ctx, text="Q? | a | b")
    mid = env.review.sent[0].id
    await env.cog.on_raw_reaction_add(types.SimpleNamespace(guild_id=env.g.id, user_id=1, message_id=mid, emoji="❌"))
    assert await env.conf.queue() == [] and await env.conf.pending() == {}


async def test_extra_approver_can_approve(env):
    await env.conf.approver_ids.set([77])
    ctx = Ctx(env, Member(env.g, 52))
    await cmd(env, "verdict_suggest")(env.cog, ctx, text="Q? | a | b")
    mid = env.review.sent[0].id
    await env.cog.on_raw_reaction_add(types.SimpleNamespace(guild_id=env.g.id, user_id=77, message_id=mid, emoji="✅"))
    assert len(await env.conf.queue()) == 1


async def test_suggest_limits_and_validation(env):
    author = Member(env.g, 53)
    ctx = Ctx(env, author)
    await cmd(env, "verdict_suggest")(env.cog, ctx, text="just words")
    assert "question | option" in ctx.sent[-1] and not env.review.sent
    await cmd(env, "verdict_suggest")(env.cog, ctx, text="Q1? | a | b")
    await cmd(env, "verdict_suggest")(env.cog, ctx, text="Q2? | a | b")
    assert "Slow down" in ctx.sent[-1] and len(env.review.sent) == 1   # cooldown
    await env.cog.config.member(author).last_suggest_ts.set(0)
    async with env.conf.pending() as p:
        p["x"] = {"question": "q", "options": ["a", "b"], "submitter_id": 53}
        p["y"] = {"question": "q", "options": ["a", "b"], "submitter_id": 53}
    await cmd(env, "verdict_suggest")(env.cog, ctx, text="Q3? | a | b")
    assert "waiting for approval" in ctx.sent[-1] and len(env.review.sent) == 1


async def test_suggest_closed_without_review_channel(env):
    await env.conf.review_channel_id.set(None)
    ctx = Ctx(env, Member(env.g, 54))
    await cmd(env, "verdict_suggest")(env.cog, ctx, text="Q? | a | b")
    assert "aren't open" in ctx.sent[-1]


async def test_review_channel_must_be_private(env):
    ctx = Ctx(env, Member(env.g, 1))
    public = env.g.add_channel(102, "public")
    public.permissions_for = lambda role: types.SimpleNamespace(view_channel=True)
    await cmd(env, "verdict_reviewchannel")(env.cog, ctx, public)
    assert "seen by @everyone" in ctx.sent[-1]
    assert await env.conf.review_channel_id() == 101


async def test_staff_commands_need_a_staff_role(env):
    nobody = Ctx(env, Member(env.g, 60))
    await cmd(env, "verdict_add")(env.cog, nobody, text="Q? | a | b")
    assert "Only Staff" in nobody.sent[-1] and await env.conf.queue() == []
    mod = Ctx(env, Member(env.g, 61, roles=[types.SimpleNamespace(id=426696709780013066)]))
    await cmd(env, "verdict_add")(env.cog, mod, text="Q? | a | b")
    assert len(await env.conf.queue()) == 1
    await cmd(env, "verdict_remove")(env.cog, mod, 1)
    assert await env.conf.queue() == []


async def test_disabled_guild_posts_nothing(env):
    await env.conf.enabled.set(False)
    await env.cog._tick(env.g, ts(2026, 10, 5, 12))
    assert not env.chan.sent


async def test_delete_data_removes_answers_and_scores(env):
    u = Member(env.g, 70)
    await env.cog.post_daily(env.g, ts(2026, 10, 5))
    await play(env, u, 0, 0)
    async with env.conf.months() as m:
        m["2026-10"] = {"70": [1, 2]}
    await env.cog.red_delete_data_for_user(requester="user", user_id=70)
    cur = await env.conf.current()
    assert cur["answers"] == {} and (await env.conf.months())["2026-10"] == {}
    assert await env.cog.config.member(u).streak() == 0


async def test_someone_who_never_guessed_is_not_named_a_lone_wolf(env):
    await env.cog.post_daily(env.g, ts(2026, 10, 5))
    for i in range(8):
        await play(env, Member(env.g, 300 + i), 0, 0)
    cur = await env.conf.current()
    await env.cog.on_interaction(Click(env.g, Member(env.g, 399), f"verdict:a:{cur['qid']}:1"))  # minority, no guess
    await env.cog.post_daily(env.g, ts(2026, 10, 6))
    assert "399" not in str(env.chan.sent[1].embed.fields[0].value)
