"""Behaviour tests for the HungerGames cog against fakes (redbot faked, real discord.py when installed).

Proves: the weekly start, the draft rules (Lurkers, bots, opt-outs, last week's tributes, too few messages),
volunteering, the one-swap rule with a single ping, withdrawals handing the seat to a requester, the 9 PM
top-up, cancelling when under 4, training threads and recorded results, scoring, the shop (charging, limits,
no charge on refusal), live posting, the Crowning (prize floor, Victor role, Hall, member totals), restart
resumption mid-broadcast and the test run (no coins, no pings).

It does NOT prove live Discord behaviour: real buttons and selects, ephemeral timing, thread permissions,
pins, persistent views after a real restart, or the bank against the live currency.
"""
import importlib
import sys
import types

from datetime import datetime

import pytest

from hungergames import _testkit as tk
from hungergames.constants import HOME_TZ

SUNDAY = datetime(2026, 11, 22, 10, 0, 30, tzinfo=HOME_TZ).timestamp()
H = 3600
CHANNEL, PING, VICTOR, TEASER, TESTCH, LURKER = 1573436403134505040, 1573436099127283955, 1573436201820618823, 777, 778, 555


@pytest.fixture
def mod(monkeypatch, tmp_path):
    bank = tk.FakeBank()
    tk.install(monkeypatch, bank=bank, data_path=tmp_path)
    tk.install_ui(monkeypatch)
    names = [n for n in sys.modules if n.startswith("hungergames.") and n.split(".")[1] in
             ("hungergames", "views", "embeds", "settings_panel", "panel")]
    for name in names:
        monkeypatch.delitem(sys.modules, name, raising=False)
    m = importlib.import_module("hungergames.hungergames")
    m._bank = bank
    monkeypatch.setattr(m, "CANNON_BEAT", 0)                     # no real waiting between cannons in tests
    yield m
    for name in ("hungergames.hungergames", "hungergames.views", "hungergames.embeds", "hungergames.panel",
                 "hungergames.settings_panel"):
        sys.modules.pop(name, None)


class FakePulse:
    def __init__(self, counts):
        self.counts = counts

    async def _days(self, gid, start, end):
        return {start.isoformat(): {"u": {str(k): v for k, v in self.counts.items()}}}


class FakeLurker:
    def __init__(self):
        conf = tk.FakeConfig()
        conf.register_guild(lurker_role_id=LURKER)
        self.config = conf


def world(mod, *, members=16, counts=None, lurkers=(), bots=()):
    guild = tk.FakeGuild(1)
    guild.add_channel(CHANNEL, "hunger-games")
    guild.add_channel(TEASER, "cuddle")
    guild.add_channel(TESTCH, "hg-test")
    ping = guild.add_role(PING, "HG pings")
    victor = guild.add_role(VICTOR, "Victor")
    lurk = guild.add_role(LURKER, "Lurker")
    people = []
    for i in range(members):
        uid = 100 + i
        roles = [lurk] if uid in lurkers else []
        people.append(tk.FakeMember(guild, uid, None, roles=roles, bot=uid in bots, name=f"m{uid}"))
    counts = counts if counts is not None else {p.id: 50 for p in people}
    bot = tk.FakeBot(guild, cogs={"ServerPulse": FakePulse(counts), "Lurker": FakeLurker()})
    cog = mod.HungerGames(bot)
    return cog, guild, people, (ping, victor)


async def enable(cog, guild, **extra):
    conf = cog.config.guild(guild)
    await conf.enabled.set(True)
    await conf.teaser_channel_id.set(TEASER)
    for k, v in extra.items():
        await getattr(conf, k).set(v)


def ch(guild, cid=CHANNEL):
    return guild.get_channel(cid)


def texts(channel):
    return [m.content or "" for m in channel.sent]


async def run_until(cog, guild, start, end, step=30):
    t = start
    while t <= end:
        await cog._tick(guild, now=t)
        t += step
    return t


# ---------------------------------------------------------------------------------------------- reaping


async def test_reaping_starts_on_sunday_and_draws_four_eligible(mod):
    cog, guild, people, (ping, _v) = world(mod, members=12, lurkers=(100,), bots=(101,),
                                           counts={**{100 + i: 50 for i in range(12)}, 102: 3})
    await enable(cog, guild, last_tributes=["103"])
    await cog.config.member(people[4]).opt_out.set(True)          # 104 opted out
    await cog._tick(guild, now=SUNDAY - 120)                      # Saturday night: nothing yet
    assert await cog.config.guild(guild).current() == {}
    await cog._tick(guild, now=SUNDAY)
    ed = await cog.config.guild(guild).current()
    assert ed["edition"] == 1 and len(ed["roster"]) == 4
    assert not {"100", "101", "102", "103", "104"} & set(ed["roster"])   # lurker, bot, quiet, last week, opted out
    msg = ch(guild).sent[0]
    assert msg.pinned and f"<@&{PING}>" in msg.content
    assert any("Reaping is open" in t for t in texts(ch(guild, TEASER)))
    await cog._tick(guild, now=SUNDAY + 60)                       # never starts twice in one week
    assert (await cog.config.guild(guild).current())["roster"] == ed["roster"]


async def test_volunteer_then_full_roster_swap_pings_once(mod):
    cog, guild, people, _ = world(mod, members=20)
    await enable(cog, guild)
    await cog._tick(guild, now=SUNDAY)
    ed = await cog.config.guild(guild).current()
    free = [p for p in people if str(p.id) not in ed["roster"]]
    for p in free[:8]:
        await cog.on_volunteer(tk.FakeInteraction(guild, p))
    ed = await cog.config.guild(guild).current()
    assert len(ed["roster"]) == 12 and ed["counts"]["volunteers"] == 8
    drafted = next(u for u, t in ed["roster"].items() if t["origin"] == "drafted")
    a, b = free[8], free[9]
    before = len(ch(guild).sent)
    await cog._swap_request(tk.FakeInteraction(guild, a), drafted)
    await cog._swap_request(tk.FakeInteraction(guild, b), drafted)
    new = ch(guild).sent[before:]
    assert len(new) == 1                                          # one message, so one ping
    assert new[0].kw["allowed_mentions"].users[0].id == int(drafted)
    assert new[0].edits and f"<@{b.id}>" in new[0].edits[-1]["content"]
    second = tk.FakeInteraction(guild, a)
    await cog._swap_request(second, drafted)                      # a has used their one request
    assert any("can't send a swap" in t for t in second.texts())
    ed = await cog.config.guild(guild).current()
    assert ed["swap_requests"][drafted]["volunteers"] == [str(a.id), str(b.id)]


async def test_withdraw_hands_the_seat_to_the_first_requester(mod):
    cog, guild, people, _ = world(mod, members=20)
    await enable(cog, guild)
    await cog._tick(guild, now=SUNDAY)
    ed = await cog.config.guild(guild).current()
    free = [p for p in people if str(p.id) not in ed["roster"]]
    for p in free[:8]:
        await cog.on_volunteer(tk.FakeInteraction(guild, p))
    drafted = next(u for u, t in (await cog.config.guild(guild).current())["roster"].items() if t["origin"] == "drafted")
    await cog._swap_request(tk.FakeInteraction(guild, free[8]), drafted)
    leaver = guild.get_member(int(drafted))
    await cog._do_withdraw(tk.FakeInteraction(guild, leaver), never=True)
    ed = await cog.config.guild(guild).current()
    assert drafted not in ed["roster"] and str(free[8].id) in ed["roster"]
    assert ed["roster"][str(free[8].id)]["origin"] == "volunteer"
    assert await cog.config.member(leaver).opt_out() is True


async def test_keep_my_spot_clears_the_tag_and_requests(mod):
    cog, guild, people, _ = world(mod, members=20)
    await enable(cog, guild)
    await cog._tick(guild, now=SUNDAY)
    ed = await cog.config.guild(guild).current()
    free = [p for p in people if str(p.id) not in ed["roster"]]
    for p in free[:8]:
        await cog.on_volunteer(tk.FakeInteraction(guild, p))
    drafted = next(u for u, t in (await cog.config.guild(guild).current())["roster"].items() if t["origin"] == "drafted")
    await cog._swap_request(tk.FakeInteraction(guild, free[8]), drafted)
    await cog.on_keep(tk.FakeInteraction(guild, guild.get_member(int(drafted))))
    ed = await cog.config.guild(guild).current()
    assert ed["roster"][drafted]["tag"] is False and drafted not in ed["swap_requests"]


async def test_lurkers_cant_volunteer(mod):
    cog, guild, people, _ = world(mod, members=10, lurkers=(109,))
    await enable(cog, guild)
    await cog._tick(guild, now=SUNDAY)
    inter = tk.FakeInteraction(guild, guild.get_member(109))
    await cog.on_volunteer(inter)
    assert "109" not in (await cog.config.guild(guild).current())["roster"]


async def test_second_reaping_tops_up_to_eight_and_lock_commits_the_number(mod):
    cog, guild, people, _ = world(mod, members=20)
    await enable(cog, guild)
    await cog._tick(guild, now=SUNDAY)
    await cog._tick(guild, now=SUNDAY + 11 * H)                  # 9 PM
    ed = await cog.config.guild(guild).current()
    assert len(ed["roster"]) == 8 and engine_phase(ed) == "second_reaping"
    await cog._tick(guild, now=SUNDAY + 14 * H)                  # past 11:59 PM
    assert await cog.config.guild(guild).edition_number() == 1
    m = await cog.config.member_from_ids(1, int(next(iter(ed["roster"])))).all()
    assert m["editions"] == 1 and m["drafted"] == 1


async def test_too_few_tributes_cancels_without_using_the_number(mod):
    cog, guild, people, _ = world(mod, members=5, counts={100 + i: (50 if i < 3 else 0) for i in range(5)})
    await enable(cog, guild)
    await cog._tick(guild, now=SUNDAY)
    await cog._tick(guild, now=SUNDAY + 11 * H)
    await cog._tick(guild, now=SUNDAY + 14 * H)
    assert await cog.config.guild(guild).current() == {}
    assert await cog.config.guild(guild).edition_number() == 0
    assert any("cancel" in t for t in texts(ch(guild)))


def engine_phase(ed):
    from hungergames import engine
    return engine.current_phase(ed)


# ---------------------------------------------------------------------------------------------- the whole week


async def full_week_to_scores(cog, guild, people):
    await cog._tick(guild, now=SUNDAY)
    await cog._tick(guild, now=SUNDAY + 11 * H)
    await cog._tick(guild, now=SUNDAY + 14 * H)                  # lock
    await cog._tick(guild, now=SUNDAY + 22 * H)                  # Monday 8 AM: training
    ed = await cog.config.guild(guild).current()
    return ed


async def test_training_threads_results_and_scores(mod):
    cog, guild, people, _ = world(mod, members=20)
    await enable(cog, guild)
    ed = await full_week_to_scores(cog, guild, people)
    assert engine_phase(ed) == "training"
    assert len(ed["threads"]) == 8
    uid = next(iter(ed["roster"]))
    await cog.finish_challenge(guild, int(uid), "agility", {"acc": 70, "time": 10, "finished": True})
    other = list(ed["roster"])[1]
    await cog.finish_challenge(guild, int(other), "agility", {"acc": 70, "time": 20, "finished": True})
    await cog._tick(guild, now=SUNDAY + 38 * H)                  # Monday 11:59 PM: close
    ed = await cog.config.guild(guild).current()
    assert ed["roster"][uid]["stats"]["agility"] == 100 and ed["roster"][other]["stats"]["agility"] == 85
    assert ed["roster"][uid]["stats"]["charisma"] == 50          # nobody cheered: everyone gets 50
    late = list(ed["roster"])[2]
    await cog.finish_challenge(guild, int(late), "strength", {"acc": 70, "time": 5, "finished": True})
    assert "strength" not in (await cog.config.guild(guild).current())["roster"][late]["challenges"]


async def test_one_attempt_per_challenge_and_own_thread_only(mod):
    cog, guild, people, _ = world(mod, members=20)
    await enable(cog, guild)
    ed = await full_week_to_scores(cog, guild, people)
    thread_id, uid = next(iter(ed["threads"].items()))
    thread = guild.get_channel(int(thread_id))
    member = guild.get_member(int(uid))
    stranger = next(p for p in people if str(p.id) not in ed["roster"])
    wrong = tk.FakeInteraction(guild, stranger, channel=thread)
    await cog.on_train(wrong, "agility")
    assert any("own training thread" in t for t in wrong.texts())
    first = tk.FakeInteraction(guild, member, channel=thread)
    await cog.on_train(first, "agility")
    assert first.response.sent and first.response.sent[0][1].get("ephemeral")
    again = tk.FakeInteraction(guild, member, channel=thread)
    await cog.on_train(again, "agility")
    assert any("One attempt" in t for t in again.texts())


async def test_shop_charges_enforces_limits_and_never_charges_on_refusal(mod):
    cog, guild, people, _ = world(mod, members=20)
    await enable(cog, guild)
    await full_week_to_scores(cog, guild, people)
    await cog._tick(guild, now=SUNDAY + 38 * H)
    await cog._tick(guild, now=SUNDAY + 50 * H)                  # Tuesday noon: scores, shop opens
    ed = await cog.config.guild(guild).current()
    assert ed["messages"].get("scores")
    tributes = list(ed["roster"])
    sponsor = next(p for p in people if str(p.id) not in ed["roster"])
    mod._bank.balances[sponsor.id] = 200_000
    now = SUNDAY + 50 * H + 60
    cog._now = lambda: now
    await cog.buy(tk.FakeInteraction(guild, sponsor), tributes[0], "water")
    assert mod._bank.withdrawals == [(sponsor.id, 1500)]
    refused = tk.FakeInteraction(guild, sponsor)
    await cog.buy(refused, tributes[0], "bread")                 # one per tribute per sponsor
    assert len(mod._bank.withdrawals) == 1 and any("already sent" in t for t in refused.texts())
    await cog.buy(tk.FakeInteraction(guild, sponsor), tributes[1], "weapon")
    assert mod._bank.withdrawals[-1] == (sponsor.id, 100000)
    poor = next(p for p in people if str(p.id) not in ed["roster"] and p.id != sponsor.id)
    broke = tk.FakeInteraction(guild, poor)
    await cog.buy(broke, tributes[2], "med_kit")
    assert any("You need" in t for t in broke.texts())
    ed = await cog.config.guild(guild).current()
    assert ed["roster"][tributes[1]]["inventory"]["gear"] == ["weapon"] and len(ed["ledger"]) == 2
    m = await cog.config.member(sponsor).all()
    assert m["items_sent"] == 2 and m["coins_spent"] == 101500
    assert any("silver parachute" in t for t in texts(ch(guild)))
    assert [(e, a[1:]) for e, *a in getattr(cog.bot, "dispatched", []) if e == "wonderland_quest"] == [("wonderland_quest", ["sponsor", 1])] * 2   # refused buys don't count


async def test_broadcasts_post_live_then_crown_the_victor(mod):
    cog, guild, people, (ping, victor_role) = world(mod, members=20)
    await enable(cog, guild)
    await full_week_to_scores(cog, guild, people)
    await cog._tick(guild, now=SUNDAY + 38 * H)
    await cog._tick(guild, now=SUNDAY + 50 * H)
    ed = await cog.config.guild(guild).current()
    sponsor = next(p for p in people if str(p.id) not in ed["roster"])
    mod._bank.balances[sponsor.id] = 500_000
    cog._now = lambda: SUNDAY + 50 * H + 60
    await cog.buy(tk.FakeInteraction(guild, sponsor), next(iter(ed["roster"])), "med_kit")
    # Tuesday 7 PM onward: run ticks every 45 s through Saturday night
    await run_until(cog, guild, SUNDAY + 57 * H, SUNDAY + 6 * 24 * H + 12 * H, step=45)
    assert await cog.config.guild(guild).current() == {}
    hall = await cog.config.guild(guild).hall()
    assert len(hall) == 1 and hall[0]["title"] == "The 1st Hunger Games"
    winner = guild.get_member(int(hall[0]["uid"]))
    assert victor_role in winner.roles
    assert (int(hall[0]["uid"]), 10000) in mod._bank.deposits    # 15% of 20,000 = 3,000 < the 10,000 floor
    assert (await cog.config.member(winner).all())["wins"] == 1
    reports = await cog.config.guild(guild).reports()
    assert reports[-1]["spent"] == 20000 and reports[-1]["minted"] == 7000
    sent = texts(ch(guild))
    assert sum("is starting now" in t for t in sent) == 5        # four broadcasts and the finale
    assert any("Victor of" in t for t in sent)
    assert all("{tribute" not in t for t in sent)
    assert await cog.config.guild(guild).last_tributes() == list(ed["roster"])


async def test_training_threads_lock_at_close_and_are_deleted_after_the_crowning(mod):
    cog, guild, people, _ = world(mod, members=20)
    await enable(cog, guild)
    ed = await full_week_to_scores(cog, guild, people)
    threads = [guild.get_channel(int(t)) for t in ed["threads"]]
    starters = [ch(guild).get_partial_message(m) for m in ed["starters"].values()]
    assert len(threads) == 8 and len(starters) == 8
    assert not any(t.locked for t in threads)
    await cog._tick(guild, now=SUNDAY + 38 * H)                  # Monday 11:59 PM: training closes
    assert all(t.locked for t in threads) and not any(t.deleted for t in threads)   # readable, not deleted
    await cog._tick(guild, now=SUNDAY + 50 * H)
    await run_until(cog, guild, SUNDAY + 57 * H, SUNDAY + 6 * 24 * H + 12 * H, step=45)
    assert await cog.config.guild(guild).current() == {}
    assert all(t.deleted for t in threads) and all(m.deleted for m in starters)


async def test_reaping_post_says_done_instead_of_counting_up_and_closes_at_the_lock(mod):
    cog, guild, people, _ = world(mod, members=20)
    await enable(cog, guild)
    await cog._tick(guild, now=SUNDAY)
    ed = await cog.config.guild(guild).current()
    from hungergames import embeds
    second = ed["schedule"][1][1]
    assert "✅ done" not in embeds.reaping_embed(ed, now=second - 60).description
    after = embeds.reaping_embed(ed, now=second + 60)
    assert "✅ done" in after.description and f"<t:{int(second)}:R>" not in after.description
    closed = embeds.reaping_embed(ed, closed=True, now=second + 3600)
    assert "closed" in closed.description and "Roster locks" not in closed.description
    await cog._tick(guild, now=SUNDAY + 11 * H)
    await cog._tick(guild, now=SUNDAY + 14 * H)                  # the lock re-renders the post as closed
    reaping = ch(guild).get_partial_message((await cog.config.guild(guild).current())["messages"]["reaping"])
    last = reaping.edits[-1]
    assert last["view"] is None and "closed" in last["embed"].description


async def test_crowning_shows_the_victors_avatar_and_total_wins(mod, monkeypatch):
    monkeypatch.setattr(mod, "arena_art", None)                  # text card: the avatar is the thumbnail
    cog, guild, people, _ = world(mod, members=20)
    for p in people:
        p.display_avatar = types.SimpleNamespace(url=f"https://cdn.example/{p.id}.png")
    await enable(cog, guild)
    await full_week_to_scores(cog, guild, people)
    await cog._tick(guild, now=SUNDAY + 38 * H)
    await cog._tick(guild, now=SUNDAY + 50 * H)
    ed = await cog.config.guild(guild).current()
    uids = [u for u in ed["roster"] if u.isdigit()]
    for u in uids:                                               # whoever wins already has 2 wins on record
        await cog.config.member_from_ids(1, int(u)).wins.set(2)
    await run_until(cog, guild, SUNDAY + 57 * H, SUNDAY + 6 * 24 * H + 12 * H, step=45)
    crown = next(m.embed for m in ch(guild).sent if m.embed is not None and "Victor of" in (m.embed.title or ""))
    winner = (await cog.config.guild(guild).hall())[0]["uid"]
    assert crown.thumbnail.url == f"https://cdn.example/{winner}.png"
    fields = {f.name: f.value for f in crown.fields}
    assert fields["Total wins"] == "3"                           # 2 before + this one


async def test_signup_message_posts_pins_and_refreshes_in_place(mod):
    cog, guild, people, _ = world(mod, members=5)
    cog._now = lambda: SUNDAY - 2 * 24 * H                       # the Friday before
    msg = await cog._act_signup(guild, people[0], None)
    assert "Posted and pinned" in msg
    posted = [m for m in ch(guild).sent if m.embed is not None]
    assert len(posted) == 1 and posted[0].pinned
    assert {c.custom_id for c in posted[0].view.children} == {"hg:pings", "hg:optout"}
    assert "Next Reaping" in posted[0].embed.description
    again = await cog._act_signup(guild, people[0], None)        # pressing again edits, never double-posts
    assert "Refreshed" in again and len([m for m in ch(guild).sent if m.embed is not None]) == 1
    assert posted[0].edits
    await cog.config.guild(guild).channel_id.set(0)
    with pytest.raises(mod.PanelError):
        await cog._act_signup(guild, people[0], None)


async def test_restart_mid_broadcast_resumes_without_rerolling(mod):
    cog, guild, people, _ = world(mod, members=20)
    await enable(cog, guild)
    await full_week_to_scores(cog, guild, people)
    await cog._tick(guild, now=SUNDAY + 38 * H)
    await cog._tick(guild, now=SUNDAY + 50 * H)
    t = SUNDAY + 57 * H
    await cog._tick(guild, now=t)                                # generates broadcast 1
    for i in range(1, 4):
        await cog._tick(guild, now=t + 45 * i)
    ed = await cog.config.guild(guild).current()
    queue, pos = ed["broadcast"]["queue"], ed["broadcast"]["pos"]
    assert 0 < pos < len(queue)
    cog2 = mod.HungerGames(cog.bot)                              # "restart": same Config, new cog
    cog2.config = cog.config
    await cog2._tick(guild, now=t + 45 * 8)                      # past the 3-5 minute line gap
    ed2 = await cog2.config.guild(guild).current()
    assert ed2["broadcast"]["queue"] == queue and ed2["broadcast"]["pos"] == pos + 1


async def test_test_run_moves_no_coins_and_pings_nobody(mod):
    cog, guild, people, _ = world(mod, members=20)
    await cog.config.guild(guild).test_channel_id.set(TESTCH)
    admin = people[0]
    start = SUNDAY + 3 * 24 * H                                  # a Wednesday
    cog._now = lambda: start
    msg = await cog._act_test(guild, admin, None)
    assert "Test run started" in msg
    ed = await cog.config.guild(guild).current()
    assert ed["test"] and str(admin.id) in ed["roster"] and len(ed["roster"]) == 5
    await run_until(cog, guild, start, start + 1500, step=3)
    assert await cog.config.guild(guild).current() == {}
    assert mod._bank.deposits == [] and mod._bank.withdrawals == []
    assert await cog.config.guild(guild).hall() == [] and await cog.config.guild(guild).edition_number() == 0
    for m in guild.get_channel(TESTCH).sent:
        am = m.kw.get("allowed_mentions")
        assert am is None or not getattr(am, "roles", False)
    assert ch(guild).sent == [] and ch(guild, TEASER).sent == []


async def test_cancel_refunds_sponsors(mod):
    cog, guild, people, _ = world(mod, members=20)
    await enable(cog, guild)
    await full_week_to_scores(cog, guild, people)
    await cog._tick(guild, now=SUNDAY + 38 * H)
    await cog._tick(guild, now=SUNDAY + 50 * H)
    ed = await cog.config.guild(guild).current()
    sponsor = next(p for p in people if str(p.id) not in ed["roster"])
    mod._bank.balances[sponsor.id] = 10_000
    cog._now = lambda: SUNDAY + 50 * H + 60
    await cog.buy(tk.FakeInteraction(guild, sponsor), next(iter(ed["roster"])), "water")
    out = await cog._act_cancel(guild, people[0], None)
    assert "Refunded 1,500" in out and mod._bank.balances[sponsor.id] == 10_000
    assert await cog.config.guild(guild).current() == {}


async def test_panel_specs_match_cog_methods(mod):
    from hungergames import panel
    cog, guild, people, _ = world(mod)
    for spec in panel.build_specs():
        for fn in (spec.get, spec.apply):
            if fn is not None:
                assert callable(fn)
    names = {"_apply_enabled", "_apply_channel", "_apply_ping_role", "_apply_victor_role", "_apply_teaser",
             "_apply_test_channel", "_apply_prize", "_apply_prices", "_act_preview", "_act_advance", "_act_cancel",
             "_act_test", "_act_report", "_status_get"}
    assert all(hasattr(cog, n) for n in names)
    assert "Turned on" in await cog._apply_enabled(guild, people[0], True)
    assert "15%" in await cog._apply_prize(guild, people[0], {"share": "15", "floor": "10,000"})


async def _first_broadcast(mod, *, avatars=False):
    import types
    cog, guild, people, _ = world(mod, members=20)
    if avatars:
        for p in people:
            p.display_avatar = types.SimpleNamespace(url=f"https://cdn.example/{p.id}.png")
    await enable(cog, guild)
    await full_week_to_scores(cog, guild, people)
    await cog._tick(guild, now=SUNDAY + 38 * H)
    await cog._tick(guild, now=SUNDAY + 50 * H)
    return cog, guild, people, SUNDAY + 57 * H


def _cards(guild):
    return [m for m in ch(guild).sent if m.embed is not None and "is starting now" not in (m.content or "")]


async def test_a_broadcast_is_a_few_edited_scene_cards_not_a_message_per_line(mod):
    cog, guild, people, t = await _first_broadcast(mod)
    start = len(ch(guild).sent)
    queue = []
    for k in range(0, 4 * H, 30):
        await cog._tick(guild, now=t + k)
        b = (await cog.config.guild(guild).current()).get("broadcast")
        if b:
            queue = b["queue"]
    assert queue                                                 # captured before the cog cleared it
    new = ch(guild).sent[start:]
    plain = [m for m in new if not m.embed and "is starting now" not in (m.content or "")]
    assert plain == []                                           # no loose one-line messages any more
    posted = [m for m in new if m.embed is not None]
    n_lines = sum(1 for i in queue if i["t"] == "line")
    assert n_lines >= 6 and len(posted) <= 5                     # a few cards plus the fallen, not a post per line
    assert any(m.edits for m in posted)                          # the cards were edited as lines arrived
    assert all("Next update" not in (m.embed.description or "") for m in posted)   # finished cards drop the countdown
    body = "\n".join(m.embed.description or "" for m in posted)
    assert all(i["text"] in body for i in queue if i["t"] == "line")               # every line made it into a card


async def test_mid_scene_card_shows_the_lines_so_far_and_a_live_countdown(mod):
    cog, guild, people, t = await _first_broadcast(mod)
    await cog._tick(guild, now=t)                                # generates the broadcast
    await run_until(cog, guild, t + 10, t + 700, step=10)
    ed = await cog.config.guild(guild).current()
    b = ed["broadcast"]
    assert 2 < b["pos"] < len(b["queue"])
    card = next(m for m in reversed(ch(guild).sent) if m.embed is not None)
    d = card.embed.description
    assert f"<t:{int(b['next_ts'])}:R>" in d and "Next update" in d and "⏳" in d
    lines = [i for i in b["queue"][:b["pos"]] if i["t"] == "line"]
    assert lines and lines[0]["text"] in d or lines[-1]["text"] in d


async def test_story_lines_wait_three_to_five_minutes_between_each_other(mod):
    cog, guild, people, t = await _first_broadcast(mod)
    await cog._tick(guild, now=t)
    await run_until(cog, guild, t + 10, t + 400, step=5)
    b = (await cog.config.guild(guild).current())["broadcast"]
    assert b["pos"] <= 4                                         # ~6 minutes in: a header and two or three lines
    ed = await cog.config.guild(guild).current()
    nxt_gap = ed["broadcast"]["next_ts"] - (t + 400)
    assert nxt_gap <= 300


async def test_a_deleted_card_is_replaced_and_no_line_is_lost(mod):
    cog, guild, people, t = await _first_broadcast(mod)
    await cog._tick(guild, now=t)
    await run_until(cog, guild, t + 10, t + 200, step=5)
    card = next(m for m in reversed(ch(guild).sent) if m.embed is not None)
    ch(guild).sent.remove(card)                                  # a mod deletes the live card
    real = ch(guild).get_partial_message
    ch(guild).get_partial_message = lambda mid: (_ for _ in ()).throw(mod.discord.NotFound(
        types.SimpleNamespace(status=404, reason="x"), "gone")) if mid == card.id else real(mid)
    before = len(ch(guild).sent)
    await run_until(cog, guild, t + 205, t + 700, step=5)
    assert len(ch(guild).sent) > before                          # the next line made a fresh card
    new = ch(guild).sent[before]
    ed = await cog.config.guild(guild).current()
    b = ed["broadcast"]
    done = [i["text"] for i in b["queue"][:b["pos"]] if i["t"] == "line"]
    assert done and done[-1] in "".join(m.embed.description or "" for m in ch(guild).sent[before:] if m.embed)


async def test_the_big_moments_show_the_tributes_avatar(mod):
    cog, guild, people, t = await _first_broadcast(mod, avatars=True)
    await run_until(cog, guild, t, t + 6 * H, step=30)
    thumbs = [m.embed.thumbnail.url for m in ch(guild).sent if m.embed is not None and getattr(m.embed, "thumbnail", None)
              and m.embed.thumbnail.url]
    assert thumbs and all(u.startswith("https://cdn.example/") for u in thumbs)


async def test_status_does_not_spoil_deaths_from_a_broadcast_still_posting(mod):
    cog, guild, people, t = await _first_broadcast(mod)
    await cog._tick(guild, now=t)
    ed = await cog.config.guild(guild).current()
    from hungergames import engine as eng, embeds as emb
    assert eng.pending_deaths(ed) and all(ed["roster"][u]["alive"] is False for u in eng.pending_deaths(ed))
    shown = emb.status_embed(ed, [], t, None, False)
    marks = shown.fields[0].value
    assert "💀" not in marks                                     # nobody visibly dead mid-broadcast
    ed["broadcast"]["pos"] = len(ed["broadcast"]["queue"])       # the broadcast finished posting
    assert eng.pending_deaths(ed) == set()
    assert "💀" in emb.status_embed(ed, [], t, None, False).fields[0].value


async def _drain_broadcast(cog, guild, t, hours=3, step=5):
    """Tick until the first broadcast has posted everything; returns (seconds taken, queue, the board of the night)."""
    queue = None
    done_at = None
    for k in range(0, hours * H, step):
        await cog._tick(guild, now=t + k)
        b = (await cog.config.guild(guild).current()).get("broadcast")
        if b:
            queue = b["queue"]
            if b["pos"] >= len(queue) and done_at is None:
                done_at = k
    return done_at, queue


async def test_a_night_lasts_about_thirty_minutes_whatever_the_line_count(mod):
    cog, guild, people, t = await _first_broadcast(mod)
    done_at, queue = await _drain_broadcast(cog, guild, t, hours=2)
    assert done_at is not None and queue
    assert 18 * 60 <= done_at <= 40 * 60, done_at / 60


async def test_the_feast_night_is_allowed_to_run_longer(mod):
    cog, guild, people, t = await _first_broadcast(mod)
    await cog._tick(guild, now=t)
    ed = await cog.config.guild(guild).current()
    assert ed["broadcast"]["gap"] >= 45
    from hungergames.constants import BROADCAST_SECONDS, FEAST_SECONDS, FINALE_SECONDS
    assert FEAST_SECONDS > BROADCAST_SECONDS and FINALE_SECONDS == BROADCAST_SECONDS


async def test_a_fresh_board_goes_at_the_bottom_each_night_and_the_old_one_is_removed(mod):
    cog, guild, people, t = await _first_broadcast(mod)
    start = len(ch(guild).sent)
    await _drain_broadcast(cog, guild, t, hours=2)
    boards = [m for m in ch(guild).sent[start:] if m.embed is not None and "The Arena" in (m.embed.title or "")]
    assert len(boards) == 1 and boards[0].pinned and boards[0].view is not None
    assert ch(guild).sent[-1] is boards[0]                      # the last thing in the channel
    assert "Still standing" in boards[0].embed.fields[0].name
    # the next night: a new board, the old one deleted
    t2 = t + 24 * H
    await _drain_broadcast(cog, guild, t2, hours=2)
    boards = [m for m in ch(guild).sent if m.embed is not None and "The Arena" in (m.embed.title or "")]
    assert len(boards) == 2 and boards[0].deleted and not boards[1].deleted and boards[1].pinned
    assert ch(guild).sent[-1] is boards[1]


async def test_the_cannons_sound_one_at_a_time_and_the_fallen_wall_is_attached(mod, monkeypatch):
    monkeypatch.setattr(mod, "CANNON_BEAT", 0.001)
    cog, guild, people, t = await _first_broadcast(mod)
    start = len(ch(guild).sent)
    await _drain_broadcast(cog, guild, t, hours=2)
    fallen = next(m for m in ch(guild).sent[start:] if m.embed is not None and "The fallen" in (m.embed.title or ""))
    assert len(fallen.edits) >= 2                               # revealed in steps, then the wall
    final = fallen.embed
    assert "remain" in final.description and final.image.url == "attachment://fallen.png"
    assert fallen.edits[-1]["attachments"][0].filename == "fallen.png"
    assert fallen.view is None                                  # the Sponsor button lives on the board now


async def test_the_crowning_has_a_victor_banner_a_recap_and_falls_back_to_text(mod, monkeypatch):
    cog, guild, people, _ = world(mod, members=20)
    await enable(cog, guild)
    await full_week_to_scores(cog, guild, people)
    await cog._tick(guild, now=SUNDAY + 38 * H)
    await cog._tick(guild, now=SUNDAY + 50 * H)
    await run_until(cog, guild, SUNDAY + 57 * H, SUNDAY + 6 * 24 * H + 12 * H, step=45)
    sent = ch(guild).sent
    crown = next(m for m in sent if m.embed is not None and "Victor of" in (m.embed.title or ""))
    assert crown.embed.image.url == "attachment://victor.png" and crown.kw["file"].filename == "victor.png"
    assert "<@&" in (crown.content or "")                       # the Hunger Games role is pinged at the Crowning
    recap = next(m for m in sent if m.embed is not None and "in numbers" in (m.embed.title or ""))
    assert sent.index(recap) > sent.index(crown) and recap.embed.fields[0].name == "The arena"
    assert not any(m.embed is not None and "The Arena" in (m.embed.title or "") and not m.deleted for m in sent)

    # Attach Files refused: the card still goes out as text
    cog2, guild2, people2, _ = world(mod, members=20)
    await enable(cog2, guild2)
    real = ch(guild2).send

    async def no_files(content=None, **kw):
        if kw.get("file") is not None:
            raise mod.discord.Forbidden(types.SimpleNamespace(status=403, reason="x"), "no")
        return await real(content, **kw)
    ch(guild2).send = no_files
    await full_week_to_scores(cog2, guild2, people2)
    await cog2._tick(guild2, now=SUNDAY + 38 * H)
    await cog2._tick(guild2, now=SUNDAY + 50 * H)
    await run_until(cog2, guild2, SUNDAY + 57 * H, SUNDAY + 6 * 24 * H + 12 * H, step=45)
    crown2 = next(m for m in ch(guild2).sent if m.embed is not None and "Victor of" in (m.embed.title or ""))
    assert crown2.embed.image.url is None and "file" not in crown2.kw


async def test_each_broadcast_gets_its_own_time_target_normal_feast_and_finale(mod, monkeypatch):
    from hungergames.constants import BROADCAST_SECONDS, FEAST_SECONDS, FINALE_SECONDS
    targets = []
    real = mod.engine.line_gap
    monkeypatch.setattr(mod.engine, "line_gap", lambda queue, target: targets.append(target) or real(queue, target))
    cog, guild, people, _ = world(mod, members=20)
    await enable(cog, guild)
    await full_week_to_scores(cog, guild, people)
    await cog._tick(guild, now=SUNDAY + 38 * H)
    await cog._tick(guild, now=SUNDAY + 50 * H)
    await run_until(cog, guild, SUNDAY + 57 * H, SUNDAY + 6 * 24 * H + 12 * H, step=45)
    assert len(targets) == 5 and targets[-1] == FINALE_SECONDS      # four nights and the Finale
    assert targets.count(FEAST_SECONDS) == 1 and set(targets) <= {BROADCAST_SECONDS, FEAST_SECONDS, FINALE_SECONDS}
