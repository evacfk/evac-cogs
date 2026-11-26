"""The canvas money flow: coins are taken before the save and refunded if it fails; prices are re-checked under a lock."""
import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from gamble import canvas_render, canvas_store as cs
from gamble.canvas_service import CanvasService, esc
from gamble.canvas_store import CanvasStore


class FakeBank:
    def __init__(self, balances):
        self.balances, self.log = dict(balances), []
        self.fail_deposit = False
        self.pause = False

    async def get_balance(self, member):
        return self.balances.get(member.id, 0)

    async def withdraw_credits(self, member, amount):
        self.log.append(("withdraw", member.id, amount))
        if self.pause:
            await asyncio.sleep(0)       # let a rival's paint interleave here if the lock doesn't hold
        if amount > self.balances.get(member.id, 0):
            raise ValueError("Insufficient funds")
        self.balances[member.id] -= amount
        return self.balances[member.id]

    async def deposit_credits(self, member, amount):
        self.log.append(("deposit", member.id, amount))
        if self.fail_deposit:
            raise RuntimeError("bank down")
        self.balances[member.id] = self.balances.get(member.id, 0) + amount


def member(uid):
    return SimpleNamespace(id=uid, roles=[])


@pytest.fixture
def env(tmp_path):
    store = CanvasStore(str(tmp_path / "c.sqlite"))
    bank = FakeBank({1: 100_000, 2: 100_000, 3: 500})
    modlog = []

    async def post(content):
        modlog.append(content)

    lurkers = set()

    async def is_lurker(m):
        return m.id in lurkers

    svc = CanvasService(store, bank, is_lurker=is_lurker, modlog=post, clock=lambda: 1_800_000_000.0)
    yield SimpleNamespace(svc=svc, store=store, bank=bank, modlog=modlog, lurkers=lurkers)
    store.close()


async def paint(env, uid, cells, color="#ffb7c5", text="", confirmed=None):
    confirmed = env.store.quote(cells) if confirmed is None else confirmed
    return await env.svc.paint(member(uid), cells, color, text, confirmed)


# ---- the happy path

async def test_paint_takes_the_price_and_saves_the_pixel(env):
    out = await paint(env, 1, [(4, 4)])
    assert out.status == "ok" and out.total == 1000 and out.next_price == 1200
    assert env.bank.balances[1] == 99_000
    assert env.store.get_pixel(4, 4).owner == 1


async def test_the_previous_owner_gets_nothing(env):
    await paint(env, 1, [(4, 4)])
    await paint(env, 2, [(4, 4)])
    assert env.bank.balances[1] == 99_000                       # no payout to the old owner
    assert env.bank.balances[2] == 100_000 - 1200
    assert not any(kind == "deposit" for kind, *_ in env.bank.log)    # nothing is ever paid out: 100% burned


async def test_coins_are_withdrawn_before_the_save(env, monkeypatch):
    order = []
    real_paint = env.store.paint
    monkeypatch.setattr(env.store, "paint", lambda *a, **k: (order.append("save"), real_paint(*a, **k))[1])
    real_withdraw = env.bank.withdraw_credits

    async def withdraw(m, amount):
        order.append("withdraw")
        return await real_withdraw(m, amount)

    env.bank.withdraw_credits = withdraw
    await paint(env, 1, [(0, 0)])
    assert order == ["withdraw", "save"]


# ---- price moved / not enough / eligibility

async def test_a_moved_price_charges_nothing_and_reports_the_new_price(env):
    stale = env.store.quote([(4, 4)])
    await paint(env, 2, [(4, 4)])                               # someone else gets there first
    before = env.bank.balances[1]
    out = await env.svc.paint(member(1), [(4, 4)], "#ffb7c5", "", stale)
    assert out.status == "price_changed" and out.prices == (1200,) and out.total == 1200
    assert env.bank.balances[1] == before and env.store.get_pixel(4, 4).owner == 2
    assert ("withdraw", 1, 1000) not in env.bank.log


async def test_a_double_confirm_cannot_paint_twice(env):
    prices = env.store.quote([(4, 4)])
    first = await env.svc.paint(member(1), [(4, 4)], "#ffb7c5", "", prices)
    second = await env.svc.paint(member(1), [(4, 4)], "#ffb7c5", "", prices)
    assert (first.status, second.status) == ("ok", "price_changed")
    assert env.bank.balances[1] == 99_000


async def test_not_enough_coins_charges_nothing_and_paints_nothing(env):
    out = await paint(env, 3, [(4, 4)])                         # user 3 has 500
    assert out.status == "insufficient" and out.balance == 500 and out.total == 1000
    assert env.bank.balances[3] == 500 and env.store.painted_count() == 0
    assert env.store.meta_get("pending") is None


async def test_an_area_is_all_or_nothing_when_the_withdrawal_fails(env):
    env.bank.balances[1] = 1999
    cells = cs.rect_cells(0, 0, 1, 0)
    out = await paint(env, 1, cells)
    assert out.status == "insufficient" and env.bank.balances[1] == 1999
    assert env.store.painted_count() == 0


async def test_an_area_charges_the_sum_and_saves_every_pixel(env):
    await paint(env, 2, [(1, 0)])                               # (1,0) now costs 1200
    cells = cs.rect_cells(0, 0, 2, 0)
    out = await paint(env, 1, cells, text="hi")
    assert out.status == "ok" and out.total == 1000 + 1200 + 1000
    assert env.bank.balances[1] == 100_000 - 3200
    assert [env.store.price_at(*c) for c in cells] == [1200, 1400, 1200]
    assert all(env.store.get_pixel(*c).owner == 1 for c in cells)


async def test_banned_painters_are_refused_before_any_charge(env):
    env.store.ban(1, 99)
    out = await paint(env, 1, [(0, 0)])
    assert out.status == "banned" and env.bank.log == []


async def test_lurkers_cannot_paint(env):
    env.lurkers.add(1)
    out = await paint(env, 1, [(0, 0)])
    assert out.status == "lurker" and env.bank.log == [] and env.store.painted_count() == 0


async def test_a_broken_lurker_check_does_not_lock_everyone_out(env):
    async def boom(m):
        raise RuntimeError("lurker cog gone")
    env.svc.is_lurker = boom
    assert (await paint(env, 1, [(0, 0)])).status == "ok"


def test_validate_cells():
    v = CanvasService.validate_cells
    assert v([]) and v([(0, 0), (0, 0)]) and v([(100, 0)]) and v([(0, -1)]) and v([(0, 50)])
    assert v(cs.rect_cells(0, 0, 10, 0))                        # 11 wide
    assert v(cs.rect_cells(0, 0, 9, 9)) is None and v([(99, 49)]) is None


# ---- failures after the coins are taken

async def test_a_failed_save_refunds_the_coins(env, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(env.store, "paint", broken)
    out = await paint(env, 1, [(0, 0)])
    assert out.status == "error" and "refunded" in out.message
    assert env.bank.balances[1] == 100_000
    assert env.bank.log == [("withdraw", 1, 1000), ("deposit", 1, 1000)]
    assert env.store.meta_get("pending") is None


async def test_a_failed_refund_is_reported_to_mod_log(env, monkeypatch):
    monkeypatch.setattr(env.store, "paint", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")))
    env.bank.fail_deposit = True
    out = await paint(env, 1, [(0, 0)])
    assert out.status == "refund_failed"
    assert any("refund failed" in line.lower() and "<@1>" in line and "1,000 wc" in line for line in env.modlog)


async def test_a_crash_between_withdraw_and_save_leaves_a_pending_record(env):
    async def crash(m, amount):
        raise asyncio.CancelledError()                          # the process dying mid-paint, as far as we can model it
    env.bank.withdraw_credits = crash
    with pytest.raises(asyncio.CancelledError):
        await paint(env, 1, [(3, 4)])
    pending = await env.svc.take_pending()
    assert pending["user"] == 1 and pending["amount"] == 1000 and pending["cells"] == "(3,4)"
    assert await env.svc.take_pending() is None                 # reported once


# ---- concurrency

async def test_two_painters_racing_for_one_pixel_cannot_both_pay_the_old_price(env):
    env.bank.pause = True
    prices = env.store.quote([(7, 7)])
    a, b = await asyncio.gather(env.svc.paint(member(1), [(7, 7)], "#aaaaaa", "", prices),
                                env.svc.paint(member(2), [(7, 7)], "#bbbbbb", "", prices))
    assert sorted([a.status, b.status]) == ["ok", "price_changed"]
    assert sum(100_000 - env.bank.balances[u] for u in (1, 2)) == 1000      # exactly one payment
    assert env.store.price_at(7, 7) == 1200


# ---- mod-log and text

async def test_text_is_posted_to_mod_log_with_member_coordinates_and_text(env):
    await paint(env, 1, [(4, 4)], text="hello there")
    assert len(env.modlog) == 1
    line = env.modlog[0]
    assert "<@1>" in line and "(4,4)" in line and "hello there" in line


async def test_an_area_logs_one_line_with_the_rectangle(env):
    await paint(env, 1, cs.rect_cells(2, 3, 4, 5), text="wall")
    assert len(env.modlog) == 1 and "(2,3)–(4,5)" in env.modlog[0]


async def test_no_text_means_no_mod_log_line(env):
    await paint(env, 1, [(4, 4)])
    assert env.modlog == []


async def test_logged_text_cannot_ping_or_format(env):
    await paint(env, 1, [(0, 0)], text="@everyone **bold** <@5>")
    line = env.modlog[0]
    assert "@everyone" not in line and "**bold**" not in line
    assert esc("@here") == "@​here"


async def test_a_failing_mod_log_never_blocks_the_paint(env):
    async def boom(content):
        raise RuntimeError("no channel")
    env.svc.modlog = boom
    assert (await paint(env, 1, [(0, 0)], text="x")).status == "ok"


# ---- image cache

async def test_the_image_is_redrawn_only_after_a_change(env, monkeypatch):
    calls = []
    real = canvas_render.render_canvas
    monkeypatch.setattr(canvas_render, "render_canvas", lambda colors: (calls.append(1), real(colors))[1])
    a = await env.svc.image()
    b = await env.svc.image()
    assert a is b and len(calls) == 1
    await paint(env, 1, [(0, 0)])
    c = await env.svc.image()
    assert len(calls) == 2 and c != a
    await asyncio.gather(*(env.svc.image() for _ in range(5)))
    assert len(calls) == 2                                      # concurrent viewers share one render


# ---- moderation

async def test_mod_clear_text(env):
    await paint(env, 1, [(4, 4)], text="rude")
    env.modlog.clear()
    assert "Cleared" in await env.svc.mod_clear_text(member(9), 4, 4)
    assert env.store.get_pixel(4, 4).text == "" and "rude" in env.modlog[0]
    assert "no text" in await env.svc.mod_clear_text(member(9), 4, 4)
    assert "outside" in await env.svc.mod_clear_text(member(9), 100, 4)


async def test_mod_wipe_previews_then_blanks(env):
    await paint(env, 1, cs.rect_cells(0, 0, 1, 0))
    preview = env.svc.wipe_preview(1)
    assert preview == {"count": 2, "worth": 2400}
    assert env.store.painted_count() == 2                       # the preview changed nothing
    await env.svc.mod_wipe(member(9), 1)
    assert env.store.painted_count() == 0 and env.store.price_at(0, 0) == 1000
    assert env.bank.balances[1] == 100_000 - 2000               # no refund


async def test_mod_ban_blocks_painting_and_unban_restores_it(env):
    await env.svc.mod_ban(member(9), 1)
    assert (await paint(env, 1, [(0, 0)])).status == "banned"
    assert "already" in await env.svc.mod_ban(member(9), 1)
    await env.svc.mod_unban(member(9), 1)
    assert (await paint(env, 1, [(0, 0)])).status == "ok"


async def test_mod_reset_previews_then_clears_and_keeps_history(env):
    await paint(env, 1, [(0, 0)])
    await paint(env, 2, [(5, 5)])
    assert env.svc.reset_preview() == {"pixels": 2, "owners": 2, "worth": 2400}
    assert env.store.painted_count() == 2
    await env.svc.mod_reset(member(9))
    assert env.store.painted_count() == 0 and env.store.burned() == 2000


# ---- reports

async def test_leaderboard(env):
    await paint(env, 1, [(0, 0)])
    await paint(env, 2, [(0, 0)])
    board = env.svc.leaderboard()
    assert board["all_time"][0][:2] == (2, 1200) and board["burned_total"] == 2200
    assert board["pixels"] == [(2, 1)]


def test_usage_recommends_retiring_only_after_review_with_fewer_than_10_painters(env):
    launched = env.store.launched_at()
    early = env.svc.usage(launched + 5 * 86400)
    assert early["review_due"] is False and early["retire_suggested"] is False
    late = env.svc.usage(launched + 22 * 86400)
    assert late["review_due"] and late["retire_suggested"] and late["painters_all"] == 0
    for uid in range(100, 110):
        env.store.paint(uid, [(uid - 100, 0)], "#000000", "", (1000,), now=launched + 86400)
    busy = env.svc.usage(launched + 22 * 86400)
    assert busy["painters_all"] == 10 and busy["review_due"] and not busy["retire_suggested"]


def test_weekly_report_covers_the_week_before_the_boundary(env):
    boundary = datetime(2026, 11, 22, 18, tzinfo=cs.PACIFIC).timestamp()
    assert env.svc.weekly_report(boundary) is None              # nobody painted: nothing to post
    env.store.paint(1, [(0, 0)], "#000000", "", (1000,), now=boundary - 3 * 86400)
    env.store.paint(2, [(0, 0)], "#000000", "", (1200,), now=boundary - 86400)
    env.store.paint(2, [(1, 1)], "#000000", "", (1000,), now=boundary - 3600)
    env.store.paint(3, [(9, 9)], "#000000", "", (1000,), now=boundary + 60)      # after: next week's
    report = env.svc.weekly_report(boundary)
    assert report["top_spender"][:2] == (2, 2200) and report["priciest"]["paid"] == 1200
    assert report["burned"] == 3200 and report["painters"] == 2 and report["actions"] == 3
    assert env.svc.weekly_last_posted() == 0
    env.svc.weekly_mark_posted(boundary)
    assert env.svc.weekly_last_posted() == boundary
