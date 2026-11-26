"""Behaviour tests for Acrophobia against fakes. They prove the round logic (phases, entries, votes, prize, cap,
mod tools, stats, restart) and clean imports, NOT live Discord behaviour: real button/modal rendering and the
ephemeral pop-ups need a try in the server."""
import sys
import types
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import discord

if not hasattr(discord.ui, "Select") or not hasattr(discord, "SelectOption"):
    pytest.skip("real discord.py not installed", allow_module_level=True)

from verdict import constants
from verdict import _testkit as tk
from verdict.test_verdict_cog import Member, cmd, mod, env, ts, Ctx  # noqa: F401  (fixtures and fakes)

LA = ZoneInfo("America/Los_Angeles")
T0 = ts(2026, 11, 26, 12, 10)
MOD_ROLE = types.SimpleNamespace(id=426696709780013066)


class AClick:
    """An interaction: a button/select click, a modal submit, or a modal opener."""

    def __init__(self, guild, user, cid, *, values=None, text=None, key="text"):
        self.guild, self.user = guild, user
        self.data = {"custom_id": cid}
        if values is not None:
            self.data["values"] = values
        if text is not None:
            self.data["components"] = [{"type": 1, "components": [{"type": 4, "custom_id": key, "value": text}]}]
        self.out, self.modal = [], None
        outer = self

        class Resp:
            def is_done(self):
                return bool(outer.out) or outer.modal is not None

            async def send_message(self, content=None, **kw):
                outer.out.append(("send", content, kw))

            async def edit_message(self, **kw):
                outer.out.append(("edit", kw.get("content"), kw))

            async def send_modal(self, modal):
                outer.modal = modal

        self.response = Resp()

    @property
    def text(self):
        return self.out[-1][1]


class Clock:
    def __init__(self, t):
        self.t = t

    def time(self):
        return self.t


@pytest.fixture
async def acro(env, monkeypatch):
    clock = Clock(T0)
    amod = sys.modules["verdict.acro"]
    monkeypatch.setattr(amod, "time", clock)
    await env.conf.acro_enabled.set(True)
    env.clock, env.amod, env.bank = clock, amod, amod.bank
    return env


def good(letters, tag=""):
    return " ".join(f"{c}{tag}ox" for c in letters)


async def cur(env):
    return await env.conf.acro_current()


async def start(env, now=T0, **kw):
    env.clock.t = now
    status = await env.cog.acro_start(env.g, now, manual=True)
    assert status.startswith("Started"), status
    return await cur(env)


async def enter(env, user, text=None, tag=None):
    c = await cur(env)
    text = text if text is not None else good(c["letters"], tag or str(user.id))
    click = AClick(env.g, user, f"verdict:acro:{c['rid']}:entry", text=text)
    await env.cog.on_interaction(click)
    return click


async def vote(env, user, for_user):
    c = await cur(env)
    idx = c["order"].index(str(for_user.id))
    click = AClick(env.g, user, f"verdict:acro:{c['rid']}:v{idx}")
    await env.cog.on_interaction(click)
    return click


async def to_vote(env, n=4):
    """Start a round, n members enter, submit phase closes. Returns the members."""
    c = await start(env)
    players = [Member(env.g, 100 + i) for i in range(n)]
    for p in players:
        assert "Entered" in (await enter(env, p)).text
    env.clock.t = c["submit_ends_ts"] + 1
    await env.cog._acro_tick(env.g, env.clock.t)
    assert (await cur(env))["phase"] == "vote"
    return players


async def finish(env):
    c = await cur(env)
    env.clock.t = c["vote_ends_ts"] + 1
    await env.cog._acro_tick(env.g, env.clock.t)
    assert await cur(env) == {}


# ---------------------------------------------------------------- a whole round


async def test_full_round_pays_the_winner_and_shows_every_entry_with_author_and_votes(acro):
    env = acro
    c = await start(env)
    assert len(c["letters"]) in (3, 4, 5) and not set(c["letters"]) & set("QXZ")
    post = env.chan.sent[0]
    assert "Acrophobia #1" in post.embed.title and c["letters"][0] in post.embed.description
    assert [b.label for b in post.view.children] == ["Submit your acronym", "Mod"]

    a, b, d, e = [Member(env.g, 100 + i) for i in range(4)]
    for p in (a, b, d, e):
        assert "Entered" in (await enter(env, p)).text
    assert set((await cur(env))["entries"]) == {"100", "101", "102", "103"}
    env.clock.t = c["submit_ends_ts"] + 1
    await env.cog._acro_tick(env.g, env.clock.t)

    c = await cur(env)
    assert c["phase"] == "vote" and sorted(c["order"]) == ["100", "101", "102", "103"]
    vote_msg = env.chan.sent[1]
    assert "vote" in vote_msg.embed.title
    assert all(f"<@{u}>" not in vote_msg.embed.description for u in c["order"])     # anonymous while voting
    assert "submissions closed" in env.chan.sent[0].embed.title and env.chan.sent[0].view is None

    voter = Member(env.g, 200)                                                     # audience members can vote too
    for who, pick in ((b, a), (d, a), (e, a), (voter, b), (a, b)):
        assert "Voted" in (await vote(env, who, pick)).text
    await finish(env)

    results = env.chan.sent[2]
    assert "results" in results.embed.title and results.content == "\N{TROPHY} <@100>"
    body = results.embed.description
    assert "<@100> · 3 votes" in body and "<@101> · 2 votes" in body and "<@102> · 0 votes" in body
    assert "2,000" in results.embed.fields[0].value and "<@100>" in results.embed.fields[0].value
    assert env.bank.deposits == [(100, 2000)]
    assert env.chan.sent[1].view is None and "voting closed" in env.chan.sent[1].embed.title

    assert await env.conf.acro_total_paid() == 2000 and (await env.conf.acro_paid())["amount"] == 2000
    assert await env.cog.config.member(a).acro_wins() == 1 and await env.cog.config.member(b).acro_wins() == 0
    assert await env.cog.config.member(b).acro_rounds() == 1 and await env.cog.config.member(voter).acro_rounds() == 0
    assert (await env.conf.acro_months())["2026-11"] == {"100": 1}
    hist = await env.conf.acro_history()
    assert len(hist) == 1 and hist[0]["entries"] == 4 and hist[0]["votes"] == 5 and not hist[0]["cancelled"]
    assert "100" not in repr(hist) and good(c["letters"], "100") not in repr(hist)    # counts only: no authors or texts kept


async def test_ties_share_the_prize(acro):
    env = acro
    a, b, d, e = await to_vote(env)
    await vote(env, d, a)
    await vote(env, e, b)
    await finish(env)
    assert sorted(env.bank.deposits) == [(100, 1000), (101, 1000)]
    assert "tie" in env.chan.sent[2].embed.fields[0].value
    assert await env.cog.config.member(a).acro_wins() == await env.cog.config.member(b).acro_wins() == 1


async def test_nobody_voting_means_no_winner_and_no_coins(acro):
    env = acro
    await to_vote(env)
    await finish(env)
    assert env.bank.deposits == [] and "Nobody voted" in env.chan.sent[2].embed.fields[0].value
    assert env.chan.sent[2].content is None


async def test_daily_cap_cuts_the_prize_then_stops_it(acro):
    env = acro
    await env.conf.acro_daily_cap.set(3000)
    for n in range(3):
        a, b, d, e = await to_vote(env)
        await vote(env, b, a)
        await finish(env)
        env.clock.t += 400
    assert [amount for _, amount in env.bank.deposits] == [2000, 1000]                # 2,000 then the 1,000 left
    assert (await env.conf.acro_paid())["amount"] == 3000
    assert "limit is used up" in env.chan.sent[-1].embed.fields[0].value
    assert await env.cog.config.member(a).acro_wins() == 3                            # wins still count


async def test_cap_resets_the_next_pacific_day(acro):
    env = acro
    await env.conf.acro_daily_cap.set(2000)
    a, *_ = await to_vote(env)
    await vote(env, Member(env.g, 300), a)
    await finish(env)
    tomorrow = ts(2026, 11, 27, 12, 10)
    env.clock.t = tomorrow
    c = await env.cog.acro_start(env.g, tomorrow, manual=True)
    players = [Member(env.g, 400 + i) for i in range(3)]
    for p in players:
        await enter(env, p)
    cc = await cur(env)
    env.clock.t = cc["submit_ends_ts"] + 1
    await env.cog._acro_tick(env.g, env.clock.t)
    await vote(env, Member(env.g, 301), players[0])
    await finish(env)
    assert [amount for _, amount in env.bank.deposits] == [2000, 2000]


# ---------------------------------------------------------------- entries


async def test_submit_button_opens_a_modal_prefilled_with_the_current_entry(acro):
    env = acro
    c = await start(env)
    p = Member(env.g, 50)
    click = AClick(env.g, p, f"verdict:acro:{c['rid']}:submit")
    await env.cog.on_interaction(click)
    assert click.modal is not None and click.modal.children[0].default is None
    await enter(env, p, tag="a")
    again = AClick(env.g, p, f"verdict:acro:{c['rid']}:submit")
    await env.cog.on_interaction(again)
    assert again.modal.children[0].default == good(c["letters"], "a")


async def test_bad_entries_are_refused_privately_with_the_reason_and_not_stored(acro):
    env = acro
    c = await start(env)
    p = Member(env.g, 51)
    short = await enter(env, p, text=c["letters"][0] + "ig")
    assert short.out[0][2]["ephemeral"] is True and "exactly" in short.text and "You wrote 1" in short.text
    wrong = await enter(env, p, text=" ".join(["zebra"] * len(c["letters"])))
    assert "has to start with" in wrong.text and f"**{c['letters'][0]}**" in wrong.text
    link = await enter(env, p, text="<@1> " + " ".join(["x"] * (len(c["letters"]) - 1)))
    assert "mentions" in link.text
    assert (await cur(env))["entries"] == {}


async def test_entry_can_be_changed_until_the_phase_ends_and_one_per_member(acro):
    env = acro
    c = await start(env)
    p = Member(env.g, 52)
    await enter(env, p, tag="1")
    ok = await enter(env, p, tag="2")
    assert "Entered" in ok.text
    assert (await cur(env))["entries"] == {"52": good(c["letters"], "2")}
    env.clock.t = c["submit_ends_ts"] + 1                                              # time's up, tick hasn't run yet
    late = await enter(env, p, tag="3")
    assert "Too late" in late.text and (await cur(env))["entries"]["52"] == good(c["letters"], "2")
    late_click = AClick(env.g, p, f"verdict:acro:{c['rid']}:submit")
    await env.cog.on_interaction(late_click)
    assert late_click.modal is None and "closed" in late_click.text


async def test_markdown_in_an_entry_is_shown_literally(acro):
    env = acro
    c = await start(env)
    letters = c["letters"]
    words = [f"{ch}**" for ch in letters]
    ok = await enter(env, Member(env.g, 53), text=" ".join(words))
    assert "Entered" in ok.text and "\\*\\*" in ok.text


async def test_round_with_fewer_than_three_entries_is_cancelled_with_a_friendly_line(acro):
    env = acro
    c = await start(env)
    for i in range(2):
        await enter(env, Member(env.g, 60 + i))
    env.clock.t = c["submit_ends_ts"] + 1
    await env.cog._acro_tick(env.g, env.clock.t)
    assert await cur(env) == {} and len(env.chan.sent) == 1                             # no vote message
    post = env.chan.sent[0]
    assert "not this time" in post.embed.title and "Nobody loses anything" in post.embed.description and post.view is None
    assert env.bank.deposits == []
    hist = await env.conf.acro_history()
    assert hist[0]["cancelled"] and hist[0]["entries"] == 2
    assert await env.cog.config.member(Member(env.g, 60)).acro_rounds() == 0


async def test_a_full_round_turns_new_entrants_away_but_lets_existing_ones_edit(acro):
    env = acro
    c = await start(env)
    for i in range(constants.ACRO_MAX_ENTRIES):
        await enter(env, Member(env.g, 1000 + i))
    newcomer = Member(env.g, 2000)
    click = AClick(env.g, newcomer, f"verdict:acro:{c['rid']}:submit")
    await env.cog.on_interaction(click)
    assert click.modal is None and "full" in click.text
    assert "full" in (await enter(env, newcomer)).text
    assert "Entered" in (await enter(env, env.g.get_member(1000), tag="new")).text


async def test_only_one_round_at_a_time_and_stale_buttons_are_closed(acro):
    env = acro
    c = await start(env)
    assert await env.cog.acro_start(env.g, T0, manual=True) == "A round is already running."
    await enter(env, Member(env.g, 70))
    env.clock.t = c["submit_ends_ts"] + 1
    await env.cog._acro_tick(env.g, env.clock.t)
    await env.cog.acro_cancel(env.g, env.clock.t)
    old = AClick(env.g, Member(env.g, 71), f"verdict:acro:{c['rid']}:submit")
    await env.cog.on_interaction(old)
    assert "over" in old.text


# ---------------------------------------------------------------- voting


async def test_voting_rules_one_vote_changeable_never_for_yourself(acro):
    env = acro
    a, b, d, e = await to_vote(env)
    own = await vote(env, a, a)
    assert "own entry" in own.text and (await cur(env))["votes"] == {}
    assert "Voted for" in (await vote(env, a, b)).text
    assert "Changed your vote" in (await vote(env, a, d)).text
    assert "already" in (await vote(env, a, d)).text
    assert (await cur(env))["votes"] == {"100": "102"}                                  # one vote each, the latest
    c = await cur(env)
    env.clock.t = c["vote_ends_ts"] + 1
    closed = await vote(env, e, a)
    assert "closed" in closed.text and (await cur(env))["votes"] == {"100": "102"}


async def test_lurkers_and_bots_cannot_play(acro):
    env = acro
    lurker_role = env.g.add_role(777, "Lurker")

    class LurkerConfig:
        def guild(self, guild):
            async def rid():
                return 777
            return types.SimpleNamespace(lurker_role_id=rid)
    env.cog.bot._cogs["Lurker"] = types.SimpleNamespace(config=LurkerConfig())
    c = await start(env)
    lurker = Member(env.g, 80, roles=[lurker_role])
    click = AClick(env.g, lurker, f"verdict:acro:{c['rid']}:submit")
    await env.cog.on_interaction(click)
    assert click.modal is None and "chat first" in click.text
    assert "chat first" in (await enter(env, lurker)).text and (await cur(env))["entries"] == {}
    bot = Member(env.g, 81)
    bot.bot = True
    assert "chat first" in (await enter(env, bot)).text
    regular = Member(env.g, 82)
    assert "Entered" in (await enter(env, regular)).text
    for i in range(2):
        await enter(env, Member(env.g, 90 + i))
    env.clock.t = c["submit_ends_ts"] + 1
    await env.cog._acro_tick(env.g, env.clock.t)
    assert "chat first" in (await vote(env, lurker, regular)).text and (await cur(env))["votes"] == {}


# ---------------------------------------------------------------- mods


async def test_mod_button_is_staff_only_and_removing_an_entry_clears_its_votes(acro):
    env = acro
    a, b, d, e = await to_vote(env)
    await vote(env, b, a)
    await vote(env, d, a)
    await vote(env, e, b)
    c = await cur(env)
    nobody = AClick(env.g, Member(env.g, 500), f"verdict:acro:{c['rid']}:mod")
    await env.cog.on_interaction(nobody)
    assert "Only Staff" in nobody.text
    staff = Member(env.g, 501, roles=[MOD_ROLE])
    opened = AClick(env.g, staff, f"verdict:acro:{c['rid']}:mod")
    await env.cog.on_interaction(opened)
    assert opened.out[0][2]["ephemeral"] is True
    select = next(x for x in opened.out[0][2]["view"].children if getattr(x, "custom_id", "").endswith(":m_rm"))
    assert len(select.options) == 4

    number = c["order"].index("100") + 1
    nope = AClick(env.g, Member(env.g, 502), f"verdict:acro:{c['rid']}:m_rm", values=[str(number)])
    await env.cog.on_interaction(nope)
    assert "Only Staff" in nope.text and (await cur(env))["removed"] == []
    rm = AClick(env.g, staff, f"verdict:acro:{c['rid']}:m_rm", values=[str(number)])
    await env.cog.on_interaction(rm)
    assert rm.out[0][0] == "edit" and f"Removed entry #{number}" in rm.out[0][2]["embed"].fields[-1].value
    now = await cur(env)
    assert now["removed"] == ["100"] and "100" not in now["votes"].values()
    vote_msg = env.chan.sent[1]
    assert "removed by a mod" in vote_msg.embed.description
    assert next(x for x in vote_msg.view.children if x.custom_id.endswith(f":v{number - 1}")).disabled is True
    assert "removed" in (await vote(env, Member(env.g, 503), a)).text
    assert (await env.cog.acro_remove_entry(env.g, number)).startswith("Entry")           # already removed
    assert "No such" in await env.cog.acro_remove_entry(env.g, 99)

    await finish(env)
    results = env.chan.sent[2]
    assert "<@100>" not in results.embed.description and "1 removed by a mod" in results.embed.footer.text
    assert env.bank.deposits == [(101, 2000)]
    assert await env.cog.config.member(a).acro_wins() == 0


async def test_removal_only_works_during_the_vote(acro):
    env = acro
    await start(env)
    assert "vote is open" in await env.cog.acro_remove_entry(env.g, 1)


async def test_mod_can_start_and_cancel_a_round_and_nothing_is_paid(acro):
    env = acro
    staff = Member(env.g, 510, roles=[MOD_ROLE])
    panel = AClick(env.g, staff, "verdict:acro:0:m_refresh")
    await env.cog.on_interaction(panel)
    view = panel.out[0][2]["view"]
    assert next(b for b in view.children if b.custom_id.endswith("m_start")).disabled is False
    assert next(b for b in view.children if b.custom_id.endswith("m_cancel")).disabled is True
    start_click = AClick(env.g, staff, "verdict:acro:0:m_start")
    await env.cog.on_interaction(start_click)
    assert "Started Acrophobia #1" in start_click.out[0][2]["embed"].fields[-1].value
    c = await cur(env)
    assert c["manual"] is True
    for i in range(3):
        await enter(env, Member(env.g, 520 + i))
    cancel = AClick(env.g, staff, f"verdict:acro:{c['rid']}:m_cancel")
    await env.cog.on_interaction(cancel)
    assert "Cancelled Acrophobia #1" in cancel.out[0][2]["embed"].fields[-1].value
    assert await cur(env) == {} and env.bank.deposits == []
    assert "A mod stopped" in env.chan.sent[0].embed.description and env.chan.sent[0].view is None
    hist = await env.conf.acro_history()
    assert hist[0]["aborted"] is True
    stale = AClick(env.g, staff, f"verdict:acro:{c['rid']}:m_cancel")
    await env.cog.on_interaction(stale)
    assert "already over" in stale.out[0][2]["embed"].fields[-1].value


async def test_mod_start_needs_acrophobia_switched_on(acro):
    env = acro
    await env.conf.acro_enabled.set(False)
    staff = Member(env.g, 511, roles=[MOD_ROLE])
    click = AClick(env.g, staff, "verdict:acro:0:m_start")
    await env.cog.on_interaction(click)
    assert "is off" in click.out[0][2]["embed"].fields[-1].value and await cur(env) == {}


async def test_mods_edit_the_theme_list_through_a_modal(acro):
    env = acro
    staff = Member(env.g, 512, roles=[MOD_ROLE])
    opener = AClick(env.g, staff, "verdict:acro:0:m_themes")
    await env.cog.on_interaction(opener)
    assert opener.modal is not None and "a pickup line" in opener.modal.children[0].default
    save = AClick(env.g, staff, "verdict:acro:0:themes", text="my first theme\n\nsecond one", key="themes")
    await env.cog.on_interaction(save)
    assert await env.conf.acro_themes() == ["my first theme", "second one"] and "2 themes" in save.text
    bad = AClick(env.g, staff, "verdict:acro:0:themes", text="x" * 90, key="themes")
    await env.cog.on_interaction(bad)
    assert "too long" in bad.text and await env.conf.acro_themes() == ["my first theme", "second one"]
    rando = AClick(env.g, Member(env.g, 513), "verdict:acro:0:themes", text="hijack", key="themes")
    await env.cog.on_interaction(rando)
    assert "Only Staff" in rando.text and await env.conf.acro_themes() == ["my first theme", "second one"]
    empty = AClick(env.g, staff, "verdict:acro:0:themes", text="", key="themes")
    await env.cog.on_interaction(empty)
    assert await env.conf.acro_themes() == []
    c = await start(env)
    assert c["theme"] is None


async def test_themed_round_shows_the_theme(acro):
    env = acro
    await env.conf.acro_themes.set(["a pickup line"])
    seen = False
    for n in range(12):
        c = await start(env, now=T0 + n * 1000)
        if c["theme"]:
            assert c["theme"] == "a pickup line" and "a pickup line" in env.chan.sent[-1].embed.description
            seen = True
        await env.cog.acro_cancel(env.g, T0)
    assert seen


async def test_staff_settings_readout_carries_the_mod_panel(acro):
    env = acro
    ctx = Ctx(env, Member(env.g, 520, roles=[MOD_ROLE]))
    ctx.author.guild_permissions = types.SimpleNamespace(administrator=False, manage_guild=False)

    class KCtx(Ctx):
        async def send(self, content=None, **kw):
            self.sent.append(content)
            self.kw = kw
    kctx = KCtx(env, ctx.author)
    await cmd(env, "verdict_settings")(env.cog, kctx)
    assert "Daily Verdict v" in kctx.sent[-1]
    assert "mod panel" in kctx.kw["embed"].title and "Rounds run" in kctx.kw["embed"].fields[0].value


# ---------------------------------------------------------------- the schedule


async def test_one_round_a_day_opens_at_the_daily_post_hour_with_hours_long_phases(acro):
    env = acro
    nine = ts(2026, 11, 26, 9, 0)
    env.clock.t = nine - 60
    await env.cog._acro_tick(env.g, nine - 60)
    assert env.chan.sent == []                                                        # before 9am: nothing
    env.clock.t = nine + 20
    await env.cog._acro_tick(env.g, nine + 20)
    assert len(env.chan.sent) == 1 and (await cur(env))["manual"] is False
    c = await cur(env)
    assert c["submit_ends_ts"] - nine - 20 == 5 * 3600                                # submit until 2pm
    assert "2:00" in env.chan.sent[0].embed.fields[1].value or "<t:" in env.chan.sent[0].embed.fields[1].value
    await env.cog.acro_cancel(env.g, nine + 40)
    await env.cog._acro_tick(env.g, nine + 100)                                       # never twice the same day
    assert len(env.chan.sent) == 1 and await cur(env) == {}
    tomorrow = ts(2026, 11, 27, 9, 5)
    env.clock.t = tomorrow
    await env.cog._acro_tick(env.g, tomorrow)
    assert len(env.chan.sent) == 2


async def test_the_day_is_skipped_when_the_bot_is_far_too_late(acro):
    env = acro
    late = ts(2026, 11, 26, 12, 0)
    env.clock.t = late
    await env.cog._acro_tick(env.g, late)
    assert env.chan.sent == []


async def test_the_daily_round_follows_the_daily_post_hour_setting(acro):
    env = acro
    await env.conf.post_hour.set(7)
    t = ts(2026, 11, 26, 7, 10)
    env.clock.t = t
    await env.cog._acro_tick(env.g, t)
    assert len(env.chan.sent) == 1


async def test_phases_last_hours_and_close_on_the_clock(acro):
    env = acro
    nine = ts(2026, 11, 26, 9, 0)
    c = await start(env, now=nine)
    for i in range(3):
        await enter(env, Member(env.g, 900 + i))
    env.clock.t = nine + 4 * 3600
    await env.cog._acro_tick(env.g, env.clock.t)
    assert (await cur(env))["phase"] == "submit"                                      # still open at 1pm
    env.clock.t = nine + 5 * 3600 + 1
    await env.cog._acro_tick(env.g, env.clock.t)
    c = await cur(env)
    assert c["phase"] == "vote" and c["vote_ends_ts"] - env.clock.t == 4 * 3600
    env.clock.t = c["vote_ends_ts"] - 60
    await env.cog._acro_tick(env.g, env.clock.t)
    assert (await cur(env))["phase"] == "vote"
    env.clock.t = c["vote_ends_ts"] + 1
    await env.cog._acro_tick(env.g, env.clock.t)
    assert await cur(env) == {} and "results" in env.chan.sent[-1].embed.title



async def test_scheduler_does_nothing_when_off_or_without_a_channel(acro):
    env = acro
    await env.conf.acro_enabled.set(False)
    await env.cog._acro_tick(env.g, ts(2026, 11, 26, 9, 20))
    assert env.chan.sent == [] and await env.conf.acro_auto_date() == ""
    await env.conf.acro_enabled.set(True)
    await env.conf.channel_id.set(None)
    await env.cog._acro_tick(env.g, ts(2026, 11, 26, 9, 20))
    assert env.chan.sent == []



async def test_a_restart_in_the_middle_of_a_round_picks_up_where_it_was(acro):
    env = acro
    c = await start(env)
    for i in range(3):
        await enter(env, Member(env.g, 600 + i))
    reborn = env.mod.Verdict(env.cog.bot)
    reborn.config = env.cog.config                                                  # same stored data, fresh memory
    env.clock.t = c["submit_ends_ts"] + 5
    await reborn._acro_tick(env.g, env.clock.t)
    assert reborn._acro_active == {env.g.id}
    c = await cur(env)
    assert c["phase"] == "vote" and len(env.chan.sent) == 2
    await vote(env, Member(env.g, 650), env.g.get_member(600))
    env.clock.t = c["vote_ends_ts"] + 5
    await reborn._acro_tick(env.g, env.clock.t)
    assert await cur(env) == {} and reborn._acro_active == set() and env.bank.deposits == [(600, 2000)]


async def test_a_round_still_finishes_if_acrophobia_is_switched_off_midway(acro):
    env = acro
    await to_vote(env)
    await env.conf.acro_enabled.set(False)
    await finish(env)
    assert await cur(env) == {}


async def test_posting_failure_does_not_leave_a_stuck_round(acro):
    env = acro
    async def boom(*a, **k):
        raise tk.http_error("Forbidden")
    env.chan.send = boom
    status = await env.cog.acro_start(env.g, T0, manual=True)
    assert "couldn't post" in status and await cur(env) == {} and env.cog._acro_active == set()


# ---------------------------------------------------------------- serverpulse hours




# ---------------------------------------------------------------- stats, top, settings


async def test_verdict_top_gets_an_acro_wins_column_and_lists_acro_only_winners(acro, monkeypatch):
    env = acro
    ctx = Ctx(env, Member(env.g, 1))
    now = ts(2026, 11, 26, 12, 0)
    async with env.conf.months() as months:
        months["2026-11"] = {"7": [5, 6]}
    async with env.conf.acro_months() as am:
        am["2026-11"] = {"7": 2, "8": 3}
    monkeypatch.setattr(env.mod, "time", Clock(now))
    await cmd(env, "verdict_top")(env.cog, ctx)
    text = ctx.sent[-1]
    assert "<@7> — 5/6 right · Acro wins: 2" in text and "<@8> — no crowd guesses yet · Acro wins: 3" in text
    assert text.index("<@7>") < text.index("<@8>")


async def test_verdict_top_without_acrophobia_looks_as_before(env, monkeypatch):
    ctx = Ctx(env, Member(env.g, 1))
    now = ts(2026, 11, 26, 12, 0)
    monkeypatch.setattr(env.mod, "time", Clock(now))
    async with env.conf.months() as months:
        months["2026-11"] = {"7": [5, 6]}
    await cmd(env, "verdict_top")(env.cog, ctx)
    assert "<@7> — 5/6 right" in ctx.sent[-1] and "Acro" not in ctx.sent[-1]
    empty_ctx = Ctx(env, Member(env.g, 2))
    await env.conf.months.set({})
    await cmd(env, "verdict_top")(env.cog, empty_ctx)
    assert empty_ctx.sent[-1] == "No scores yet this month."


async def test_member_record_shows_acro_wins_once_they_have_played(acro):
    env = acro
    p = Member(env.g, 700)
    ctx = Ctx(env, p)
    await cmd(env, "verdict")(env.cog, ctx)
    assert "Acrophobia" not in ctx.sent[-1]
    await env.cog.config.member(p).acro_rounds.set(4)
    await env.cog.config.member(p).acro_wins.set(1)
    await cmd(env, "verdict")(env.cog, ctx)
    assert "Acrophobia: 1 wins in 4 rounds" in ctx.sent[-1]


async def test_status_shows_review_and_flags_thin_rounds(acro):
    env = acro
    status, stats = await env.cog._acro_status(env.g, T0)
    assert "off" not in status.splitlines()[0] and "Review: starts the first time" in stats
    await env.cog._apply_acro_enabled(env.g, None, True)
    launch = await env.conf.acro_launch_ts()
    assert launch > 0
    await env.cog._apply_acro_enabled(env.g, None, False)
    await env.cog._apply_acro_enabled(env.g, None, True)
    assert await env.conf.acro_launch_ts() == launch                                  # the clock starts once
    await env.conf.acro_launch_ts.set(T0 - 22 * 86400)
    for n in (3, 2, 4):
        async with env.conf.acro_history() as h:
            h.append({"seq": n, "ts": T0 - 86400, "letters": "ABC", "entries": n, "votes": 0, "cancelled": n < 3,
                      "winners": 0, "paid": 0, "manual": False, "aborted": False})
    _, stats = await env.cog._acro_status(env.g, T0)
    assert "Review due" in stats and "Retire it?" in stats and "3.0 entries" in stats
    assert "Rounds run: 3" in stats and "1 cancelled" in stats


async def test_settings_validate_and_save(acro):
    env = acro
    PanelError = env.mod.PanelError
    await env.conf.channel_id.set(None)
    with pytest.raises(PanelError):
        await env.cog._apply_acro_enabled(env.g, None, True)
    await env.conf.channel_id.set(env.chan.id)
    await env.cog._apply_acro_enabled(env.g, None, True)
    assert await env.conf.acro_enabled() is True

    await env.cog._apply_acro_prize(env.g, None, {"prize": "1,500", "cap": "6000"})
    assert (await env.conf.acro_prize(), await env.conf.acro_daily_cap()) == (1500, 6000)
    for prize, cap in (("abc", "4000"), ("2000", ""), ("-1", "4000"), ("200000", "400000"), ("3000", "2000")):
        with pytest.raises(PanelError):
            await env.cog._apply_acro_prize(env.g, None, {"prize": prize, "cap": cap})
    assert (await env.conf.acro_prize(), await env.conf.acro_daily_cap()) == (1500, 6000)

    await env.cog._apply_acro_themes(env.g, None, {"themes": "a\nb"})
    assert await env.conf.acro_themes() == ["a", "b"]
    with pytest.raises(PanelError):
        await env.cog._apply_acro_themes(env.g, None, {"themes": "y" * 100})

    assert "Started" in await env.cog._apply_acro_start(env.g, None, None)
    with pytest.raises(PanelError):
        await env.cog._apply_acro_start(env.g, None, None)                           # already running
    assert "Cancelled" in await env.cog._apply_acro_cancel(env.g, None, None)
    with pytest.raises(PanelError):
        await env.cog._apply_acro_cancel(env.g, None, None)


async def test_erasing_a_member_removes_their_entry_votes_and_wins(acro):
    env = acro
    a, b, d, e = await to_vote(env)
    await vote(env, b, a)
    await vote(env, a, b)
    async with env.conf.acro_months() as am:
        am["2026-11"] = {"100": 2, "101": 1}
    await env.cog.red_delete_data_for_user(requester="user", user_id=100)
    c = await cur(env)
    assert "100" not in c["entries"] and "100" not in c["votes"] and "100" not in c["votes"].values()
    assert "100" in c["removed"]
    assert (await env.conf.acro_months())["2026-11"] == {"101": 1}
    await finish(env)                                                                 # the round still closes cleanly
    assert await env.cog.config.member(a).acro_wins() == 0


async def test_acro_buttons_do_not_disturb_the_daily_question_buttons(acro):
    env = acro
    await env.cog.post_daily(env.g, T0)
    daily = await env.conf.current()
    await start(env)
    u = Member(env.g, 800)
    first = AClick(env.g, u, f"verdict:a:{daily['qid']}:0")
    await env.cog.on_interaction(first)
    assert "Now guess" in first.text
    garbage = AClick(env.g, u, "verdict:acro:broken")
    await env.cog.on_interaction(garbage)                                              # malformed id: ignored quietly
    assert garbage.out == []


# ---------------------------------------------------------------- the admin settings panel (real discord.py)


class PClick:
    def __init__(self, user, values=None):
        self.user, self.data, self.originals, self.modal = user, {"values": values or []}, [], None
        outer = self

        class Resp:
            done = False

            def is_done(self):
                return self.done

            async def defer(self):
                self.done = True

            async def edit_message(self, **kw):
                self.done = True
                outer.originals.append(kw)

            async def send_message(self, *a, **k):
                self.done = True

            async def send_modal(self, modal):
                self.done = True
                outer.modal = modal

        self.response = Resp()
        self.followup = types.SimpleNamespace(send=self._noop)

    async def _noop(self, *a, **k):
        pass

    async def edit_original_response(self, **kw):
        self.originals.append(kw)


async def test_acrophobia_tab_shows_values_and_the_modals_save_and_reject(acro):
    from verdict import panel as panel_mod
    from verdict.settings_panel import SettingsPanel
    env = acro
    specs = panel_mod.build_specs()
    admin = types.SimpleNamespace(id=1, guild_permissions=types.SimpleNamespace(administrator=False, manage_guild=True))
    p = SettingsPanel(env.cog, env.g, admin, specs, title="Daily Verdict", intro=panel_mod.INTRO)
    p.tab = "Acrophobia"
    await p.refresh_data()
    p.rebuild()
    text = p.build_embed().description
    assert "2,000 per win" in text and "4,000 a day at most" in text and "20 themes" in text
    await p._on_setting(PClick(admin, ["acro_prize"]))
    click = PClick(admin)
    await p._on_edit_modal(click)
    assert [b.default for b in click.modal.children] == ["2000", "4000"]
    click.modal.children[0]._value = "3000"
    click.modal.children[1]._value = "2000"
    bad = PClick(admin)
    await click.modal.on_submit(bad)
    note = (bad.originals[-1] if bad.originals else {})["embed"].description
    assert "\u26a0" in note and await env.conf.acro_prize() == 2000
    click.modal.children[0]._value = "1500"
    click.modal.children[1]._value = "3000"
    ok = PClick(admin)
    await click.modal.on_submit(ok)
    assert (await env.conf.acro_prize(), await env.conf.acro_daily_cap()) == (1500, 3000)
    assert "1,500 per win" in ok.originals[-1]["embed"].description


async def test_turning_acrophobia_on_asks_for_confirmation_and_cancel_does_nothing(acro):
    from verdict import panel as panel_mod
    from verdict.settings_panel import SettingsPanel
    env = acro
    await env.conf.acro_enabled.set(False)
    admin = types.SimpleNamespace(id=1, guild_permissions=types.SimpleNamespace(administrator=False, manage_guild=True))
    p = SettingsPanel(env.cog, env.g, admin, panel_mod.build_specs(), title="Daily Verdict")
    p.tab = "Acrophobia"
    await p.refresh_data()
    p.rebuild()
    await p._on_setting(PClick(admin, ["acro_enabled"]))
    on = PClick(admin)
    await p._make_toggle(True)(on)
    assert p.mode == "confirm" and "pays wondercoin" in on.originals[-1]["embed"].description
    assert await env.conf.acro_enabled() is False
    await p._on_confirm(PClick(admin))
    assert await env.conf.acro_enabled() is True and await env.conf.acro_launch_ts() > 0


def quest_hits(env):
    return [(a[0].id, a[1], a[2]) for e, *a in getattr(env.cog.bot, "dispatched", []) if e == "wonderland_quest"]


async def test_entering_counts_once_for_the_daily_quest_and_changing_it_does_not(acro):
    env = acro
    await start(env)
    p = Member(env.g, 52)
    await enter(env, p, tag="1")
    await enter(env, p, tag="2")                                  # a change of mind, not a second action
    await enter(env, p, text="not an acronym")                    # refused entries never count
    q = Member(env.g, 53)
    await enter(env, q, text="nope")
    assert quest_hits(env) == [(52, "acro", 1)]


async def test_first_vote_counts_for_the_daily_quest_and_changed_or_refused_votes_do_not(acro):
    env = acro
    a, b, d, e = await to_vote(env)
    base = len(quest_hits(env))
    await vote(env, a, a)                                         # own entry: refused
    await vote(env, a, b)
    await vote(env, a, d)                                         # changing the vote
    await vote(env, a, d)                                         # same vote again
    assert quest_hits(env)[base:] == [(100, "acro", 1)]

