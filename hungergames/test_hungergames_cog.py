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
    await cog2._tick(guild, now=t + 45 * 5)
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
