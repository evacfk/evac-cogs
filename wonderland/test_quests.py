"""Daily quests: pure rules (wonderland/quests.py)."""
import random
from datetime import datetime
from types import SimpleNamespace

import pytest

from wonderland import quests as Q


# ------------------------------------------------------------------ ExtendedEconomy's payday, copied verbatim
# from vrt-cogs extendedeconomy/overrides/payday.py (guild bank branch), with Red's Config reads replaced by
# plain lookups. Q.payday_amount must agree with it for every member.

def extended_economy_payday(base, author_roles, role_credit_of, conf):
    credit_amount = base
    for role in author_roles:
        role_credits = role_credit_of(role)
        if conf.stack_paydays:
            credit_amount += role_credits
        elif role_credits > credit_amount:
            credit_amount = role_credits

    if conf.role_bonuses and any(role.id in conf.role_bonuses for role in author_roles):
        # Get the highest bonus multiplier that the user has
        highest_bonus = max(conf.role_bonuses.get(role.id, 0) for role in author_roles)
        credit_amount += round(credit_amount * highest_bonus)

    if conf.role_static_bonuses and any(role.id in conf.role_static_bonuses for role in author_roles):
        highest_static = max(conf.role_static_bonuses.get(role.id, 0) for role in author_roles)
        credit_amount += highest_static
    return credit_amount


EVERYONE, BOOSTER, VIP, LEVEL20 = (SimpleNamespace(id=i) for i in (1, 2, 3, 4))
ROLE_CREDITS = {1: 0, 2: 0, 3: 3000, 4: 1500}


def ours(base, roles, conf):
    return Q.payday_amount(base, [ROLE_CREDITS[r.id] for r in roles], [r.id for r in roles],
                           stack=conf.stack_paydays, role_bonuses=conf.role_bonuses,
                           static_bonuses=conf.role_static_bonuses)


CONFS = [
    SimpleNamespace(stack_paydays=False, role_bonuses={}, role_static_bonuses={}),
    SimpleNamespace(stack_paydays=False, role_bonuses={2: 0.5}, role_static_bonuses={}),          # booster +50%
    SimpleNamespace(stack_paydays=True, role_bonuses={2: 0.25, 3: 0.1}, role_static_bonuses={2: 500, 4: 200}),
    SimpleNamespace(stack_paydays=False, role_bonuses={2: 0.333}, role_static_bonuses={3: 750}),
]
MEMBERS = {
    "plain": [EVERYONE],
    "booster": [EVERYONE, BOOSTER],
    "booster with paid roles": [EVERYONE, BOOSTER, VIP, LEVEL20],
    "paid roles, no boost": [EVERYONE, VIP, LEVEL20],
}


@pytest.mark.parametrize("conf", CONFS)
@pytest.mark.parametrize("who", list(MEMBERS))
@pytest.mark.parametrize("base", [0, 1000, 2500, 7777])
def test_payday_matches_extended_economy(conf, who, base):
    roles = MEMBERS[who]
    assert ours(base, roles, conf) == extended_economy_payday(base, roles, lambda r: ROLE_CREDITS[r.id], conf)


def test_a_booster_gets_more_than_a_plain_member():
    conf = CONFS[1]
    assert ours(2000, MEMBERS["plain"], conf) == 2000
    assert ours(2000, MEMBERS["booster"], conf) == 3000


def test_global_bank_uses_the_base_only():
    assert Q.payday_amount(900, [5000], [3], role_bonuses={3: 1.0}, global_bank=True) == 900


# ------------------------------------------------------------------ rewards

@pytest.mark.parametrize("payday,mult", [(3000, 1.0), (1000, 1.0), (2501, 1.0), (4000, 0.75), (7, 1.0), (0, 1.0)])
def test_three_quests_pay_exactly_one_payday_times_the_multiplier(payday, mult):
    parts = [Q.quest_reward(payday, mult, i) for i in range(3)]
    assert sum(parts) == round(payday * mult)
    assert max(parts) - min(parts) <= 2


def test_each_quest_is_a_third_and_there_is_no_set_bonus():
    assert [Q.quest_reward(3000, 1.0, i) for i in range(3)] == [1000, 1000, 1000]


# ------------------------------------------------------------------ day, deal, progress, streak

def test_reset_is_midnight_pacific():
    late = datetime(2026, 11, 26, 23, 30, tzinfo=Q.TZ)
    assert Q.today(late) == "2026-11-26" and Q.seconds_to_reset(late) == 1800
    utc_next_day = datetime(2026, 11, 27, 7, 59, tzinfo=Q.ZoneInfo("UTC"))   # still the 26th in LA
    assert Q.today(utc_next_day) == "2026-11-26"
    assert Q.fmt_duration(1800) == "30m" and Q.fmt_duration(5 * 3600 + 7 * 60) == "5h 07m"


def test_deal_picks_three_distinct_and_favours_quests_not_done_this_week():
    day = "2026-11-26"
    keys = [q.key for q in Q.QUESTS]
    recent = {k: "2026-11-25" for k in keys if k not in ("fish", "star")}
    hits = {"fish": 0, "star": 0}
    for seed in range(600):
        picked = Q.deal(keys, recent, day, random.Random(seed))
        assert len(picked) == 3 == len(set(picked))
        for k in hits:
            hits[k] += k in picked
    stale = sum(hits.values()) / 600
    assert stale > 0.5   # two fresh quests out of 12, weighted x3: well above the unweighted 0.5 expectation of 2*3/12


def test_deal_never_exceeds_what_can_be_done():
    assert Q.deal(["chat", "fish"], {}, "2026-11-26", random.Random(1)) in (["chat", "fish"], ["fish", "chat"])
    assert Q.deal([], {}, "2026-11-26", random.Random(1)) == []


def test_progress_caps_at_target_and_pays_each_quest_once():
    s = Q.roll_day(None, "2026-11-26")
    s["assigned"] = ["chat", "card", "fish"]
    for _ in range(14):
        Q.add_progress(s, "chat", 1)
    assert s["progress"]["chat"] == 10
    assert Q.settle(s) == (["chat"], False)
    assert Q.settle(s) == ([], False)
    Q.add_progress(s, "card", 1)
    Q.add_progress(s, "fish", 5)
    new, full = Q.settle(s)
    assert sorted(new) == ["card", "fish"] and full and s["streak"] == 1 and s["sets"] == 1
    Q.add_progress(s, "card", 1)
    assert Q.settle(s) == ([], False) and s["sets"] == 1          # one set per day


def test_unassigned_progress_never_pays():
    s = Q.roll_day(None, "2026-11-26")
    s["assigned"] = ["verdict"]
    Q.add_progress(s, "card", 1)
    assert Q.settle(s) == ([], False) and s["last_done"]["card"] == "2026-11-26"


def test_streak_counts_consecutive_full_days_and_breaks_on_a_gap():
    s = Q.roll_day(None, "2026-11-24")
    for day in ("2026-11-24", "2026-11-25"):
        s = Q.roll_day(s, day)
        s["assigned"] = ["card"]
        Q.add_progress(s, "card", 1)
        Q.settle(s)
    assert s["streak"] == 2 and Q.current_streak(s, "2026-11-26") == 2   # alive until a whole day is missed
    assert Q.current_streak(s, "2026-11-27") == 0
    s = Q.roll_day(s, "2026-11-27")
    s["assigned"] = ["card"]
    Q.add_progress(s, "card", 1)
    Q.settle(s)
    assert s["streak"] == 1 and s["best_streak"] == 2


def test_new_day_clears_quests_but_keeps_history():
    s = Q.roll_day(None, "2026-11-25")
    s.update(assigned=["card"], paid=["card"], earned=900, opened=True, payday=True, streak=3, sets=4)
    s["last_done"]["card"] = "2026-11-25"
    t = Q.roll_day(s, "2026-11-26")
    assert (t["assigned"], t["paid"], t["earned"], t["opened"], t["payday"]) == ([], [], 0, False, False)
    assert t["streak"] == 3 and t["sets"] == 4 and t["last_done"] == {"card": "2026-11-25"}


def test_custom_targets_and_titles():
    assert Q.target_of("chat", {"chat": 5}) == 5 and Q.title_of("chat", {"chat": 5}) == "Send 5 messages in {#cuddle}"
    assert Q.target_of("chat", {"chat": "x"}) == 10 and Q.target_of("chat", {"chat": 0}) == 1


# ------------------------------------------------------------------ pool rules

def test_every_quest_has_exactly_one_source_and_command_quests_name_their_command():
    for q in Q.QUESTS:
        assert q.source in (Q.HOOK, Q.COMMAND, Q.CHAT, Q.CALL)
        assert bool(q.command) == (q.source == Q.COMMAND)
    assert len({q.key for q in Q.QUESTS}) == len(Q.QUESTS)


def test_no_quest_touches_secret_hunt_afterdark_rabbit_hole_or_pillow_talk():
    for q in Q.QUESTS:
        text = f"{q.key} {q.title} {q.how} {q.command}".lower()
        assert not any(word in text for word in Q.FORBIDDEN_WORDS), q.key


def test_canvas_and_plant_are_in_the_pool():
    assert "canvas" in Q.BY_KEY and "plant" in Q.BY_KEY and Q.BY_KEY["plant"].source == Q.HOOK


# ------------------------------------------------------------------ usage counts

def test_completion_rates_and_the_20_percent_flag():
    stats = {}
    for day, offered, done in (("2026-11-20", 10, 1), ("2026-11-26", 10, 0)):
        entry = Q.stats_day(stats, day)
        entry["offered"]["fish"] = offered
        entry["done"]["fish"] = done
    Q.stats_day(stats, "2026-09-01")
    Q.prune(stats, "2026-11-26")
    assert "2026-09-01" not in stats
    offered, done = Q.completion(stats, "2026-11-26")["fish"]
    assert (offered, done) == (20, 1) and "under 20%" in Q.rate_text(offered, done)
    assert "under" not in Q.rate_text(10, 2)


def test_day_line_shows_the_share_of_payday_users_who_opened():
    line = Q.day_line({"opens": 40, "payday": 80, "both": 30, "sets": 12, "paid": 36000})
    assert "30/80 payday users (37% of payday)" in line and "12 full sets" in line


def test_faucet():
    assert Q.faucet(100, 2500, 1.0) == 250_000 and Q.faucet(100, 2500, 0.75) == 187_500


# ------------------------------------------------------------------ the .wld guide

def test_start_page_mentions_daily_quests_only_while_they_are_on():
    from wonderland import content, engine
    start = next(p for p in content.PAGES if p.key == "start")
    assert "**Your daily loop**" in start.body
    off = engine.gate_body(start, lambda name: False).body
    on = engine.gate_body(start, lambda name: name == "@quests").body
    assert "Daily Quests" not in off and "[[" not in off and "Payday" in off
    assert "📜 **Daily Quests**: three small quests a day; each pays a third of your payday\n" in on
    assert off.endswith("**When you have a minute**") and on.endswith("**When you have a minute**")


def test_a_quest_removed_from_the_pool_drops_out_of_a_set_dealt_before_the_removal():
    old = Q.fresh_state()
    old.update(day="2026-11-26", assigned=["card", "rsvp", "chat"], paid=["rsvp"], progress={"rsvp": 1, "card": 1})
    state = Q.roll_day(old, "2026-11-26")
    assert state["assigned"] == ["card", "chat"] and state["paid"] == []
    assert "rsvp" not in Q.BY_KEY and "acro" in Q.BY_KEY
    assert Q.title_of("acro") == "Play Acrophobia"

