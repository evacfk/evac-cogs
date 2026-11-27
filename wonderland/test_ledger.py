"""Coin ledger: stack attribution, the bank.set_balance wrapper (fail open, unload), SQLite store, report maths.

The fake bank below mirrors Red 3.5.24's redbot/core/bank.py: deposit and withdraw read the balance and
then call `set_balance` as a module global, which is what lets one wrapper see every change.
"""
import sys
import textwrap
import time
import types

import pytest

from wonderland import ledger, ledger_cog

BANK_SRC = '''
balances = {}

async def get_balance(member):
    return balances.get(member.id, 0)

async def set_balance(member, amount):
    if amount < 0:
        raise ValueError("negative")
    balances[member.id] = amount
    return amount

async def deposit_credits(member, amount):
    bal = await get_balance(member)
    return await set_balance(member, bal + amount)

async def withdraw_credits(member, amount):
    bal = await get_balance(member)
    if amount > bal:
        raise ValueError("too poor")
    return await set_balance(member, bal - amount)
'''

# A stand-in third-party cog: a command, a button callback, a background loop and a modal.
CASINO_SRC = '''
async def payout(bank, member, amount):
    return await bank.deposit_credits(member, amount)

async def blackjack(self, ctx, bank, member, amount):
    await bank.withdraw_credits(member, amount)
    return await payout(bank, member, amount * 2)

async def hit(self, interaction, button, bank, member):
    return await payout(bank, member, 5)

class Loop:
    async def tick(self, bank, member):
        return await payout(bank, member, 7)
'''

# A cog that replaced another cog's command callback (ExtendedEconomy does this to Economy's payday).
OVERRIDE_SRC = '''
async def payday_override(self, ctx, bank, member):
    return await bank.deposit_credits(member, 120)
'''


def load_module(name, src):
    mod = types.ModuleType(name)
    exec(compile(textwrap.dedent(src), f"<{name}>", "exec"), mod.__dict__)
    return mod


@pytest.fixture
def bank():
    return load_module("fakebank", BANK_SRC)


@pytest.fixture
def casino():
    return load_module("fakecasino.games", CASINO_SRC)


def member(uid=1, bot=False, guild_id=42):
    return types.SimpleNamespace(id=uid, bot=bot, guild=types.SimpleNamespace(id=guild_id), display_name=f"m{uid}")


def context(name, callback=None, cog=None):
    return types.SimpleNamespace(command=types.SimpleNamespace(qualified_name=name, callback=callback, cog=cog),
                                 invoked_with=name.split()[-1], message=object())


class Host(ledger_cog.LedgerMixin):
    def __init__(self, extensions=("fakecasino", "redbot.cogs.economy", "fakeeco")):
        self.bot = types.SimpleNamespace(extensions={e: None for e in extensions})
        self._ledger_setup()


@pytest.fixture
def host(bank, tmp_path):
    h = Host()
    assert h._ledger_start(bank, ledger.LedgerStore(tmp_path / "l.sqlite3"), loop_task=False)
    yield h
    h._ledger_stop()


def rows(h):
    h._ledger_flush_now()
    with h._ledger_store._connect() as con:
        return con.execute("SELECT user_id, old_balance, new_balance, delta, cog, action FROM changes ORDER BY id").fetchall()


# ------------------------------------------------------------------ attribution

async def test_a_command_in_a_cog_module_is_attributed_to_that_cog_and_command(host, bank, casino):
    m = member()
    bank.balances[m.id] = 100
    await casino.blackjack(None, context("casino blackjack"), bank, m, 30)
    assert rows(host) == [(1, 100, 70, -30, "fakecasino", ".casino blackjack"),
                          (1, 70, 130, 60, "fakecasino", ".casino blackjack")]


async def test_a_button_is_named_by_its_label(host, bank, casino):
    interaction = types.SimpleNamespace(response=object(), type=3, data={"custom_id": "0" * 32}, command=None)
    button = types.SimpleNamespace(label="Hit", custom_id="0" * 32, callback=None, view=None)
    await casino.hit(None, interaction, button, bank, member())
    assert rows(host)[0][4:] == ("fakecasino", "button: Hit")


async def test_a_background_loop_is_named_by_its_function(host, bank, casino):
    await casino.Loop().tick(bank, member())
    assert rows(host)[0][4:] == ("fakecasino", "fn: Loop.tick")


async def test_an_overridden_command_callback_counts_for_the_command_owner(host, bank):
    override = load_module("fakeeco.overrides", OVERRIDE_SRC)
    economy_cog = type("Economy", (), {"__module__": "redbot.cogs.economy.economy"})()
    ctx = context("payday", callback=override.payday_override, cog=economy_cog)
    await override.payday_override(None, ctx, bank, member())
    assert rows(host)[0][4:] == ("economy", ".payday")


async def test_code_outside_any_loaded_cog_is_unknown(host, bank):
    await bank.deposit_credits(member(), 5)       # straight from the bank module, no cog on the stack
    assert rows(host)[0][4:] == ("unknown", "unknown")


def test_cog_of_maps_modules_to_loaded_extensions():
    ext = {"casino": None, "redbot.cogs.economy": None}
    assert ledger.cog_of("casino.engine", ext) == "casino"
    assert ledger.cog_of("redbot.cogs.economy.economy", ext) == "economy"
    assert ledger.cog_of("redbot.core.bank", ext) is None
    assert ledger.cog_of("casinoextra", ext) is None
    assert ledger.cog_of(None, ext) is None


async def test_bots_and_zero_changes_are_not_recorded(host, bank, casino):
    await casino.payout(bank, member(uid=9, bot=True), 50)
    await bank.set_balance(member(uid=2), 0)        # 0 -> 0
    assert rows(host) == []
    assert bank.balances[9] == 50                   # the bot's change still happened


# ------------------------------------------------------------------ fail open

async def test_attribution_crash_still_moves_the_coins(host, bank, casino, monkeypatch):
    monkeypatch.setattr(ledger, "attribute", lambda *a: 1 / 0)
    assert await casino.payout(bank, member(), 40) == 40
    assert bank.balances[1] == 40 and rows(host) == []
    assert host._ledger_state["errors"] == 1


async def test_balance_read_crash_still_moves_the_coins(host, bank, casino):
    real = bank.get_balance
    calls = {"n": 0}

    async def flaky(m):           # deposit's own read works; the ledger's extra read fails
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("config down")
        return await real(m)
    bank.get_balance = flaky
    assert await casino.payout(bank, member(), 40) == 40
    assert bank.balances[1] == 40 and rows(host) == []


async def test_recording_crash_still_returns_the_new_balance(host, bank, casino, monkeypatch):
    def boom(*a):
        raise RuntimeError("disk full")
    monkeypatch.setattr(host, "_ledger_record", boom)
    host._ledger_stop()                                   # re-wrap so the wrapper picks up the broken recorder
    host._ledger_start(bank, host._ledger_store, loop_task=False)
    assert await casino.payout(bank, member(), 40) == 40
    assert bank.balances[1] == 40


async def test_the_original_is_called_exactly_once_and_its_errors_still_raise(host, bank, casino):
    with pytest.raises(ValueError, match="too poor"):
        await bank.withdraw_credits(member(), 10)
    assert rows(host) == []


# ------------------------------------------------------------------ install / unload

def test_unload_restores_the_original(bank, tmp_path):
    original = bank.set_balance
    h = Host()
    h._ledger_start(bank, ledger.LedgerStore(tmp_path / "l.sqlite3"), loop_task=False)
    assert bank.set_balance is not original and ledger_cog.is_ours(bank.set_balance)
    h._ledger_stop()
    assert bank.set_balance is original


async def test_unload_under_another_wrapper_leaves_it_and_goes_inert(bank, casino, tmp_path):
    original = bank.set_balance
    h = Host()
    h._ledger_start(bank, ledger.LedgerStore(tmp_path / "l.sqlite3"), loop_task=False)
    ours = bank.set_balance

    async def theirs(member, amount):
        return await ours(member, amount)
    bank.set_balance = theirs
    h._ledger_stop()
    assert bank.set_balance is theirs                     # not torn off
    await casino.payout(bank, member(), 5)
    assert rows(h) == [] and bank.balances[1] == 5         # ours passes through without recording
    bank.set_balance = original


def test_a_second_load_does_not_stack_wrappers(bank, tmp_path):
    original = bank.set_balance
    first, second = Host(), Host()
    first._ledger_start(bank, ledger.LedgerStore(tmp_path / "a.sqlite3"), loop_task=False)
    second._ledger_start(bank, ledger.LedgerStore(tmp_path / "b.sqlite3"), loop_task=False)  # first never unloaded
    assert bank.set_balance.__ledger_original__ is original
    second._ledger_stop()
    assert bank.set_balance is original


def test_a_changed_set_balance_shape_is_not_wrapped(bank, tmp_path):
    async def set_balance(member, amount, *, reason=None):
        return amount
    bank.set_balance = set_balance
    h = Host()
    assert h._ledger_start(bank, ledger.LedgerStore(tmp_path / "l.sqlite3"), loop_task=False) is False
    assert bank.set_balance is set_balance and h._ledger_status.startswith("off")


def test_signature_check():
    async def good(member, amount):
        return amount

    async def renamed(user, amount):
        return amount

    def sync(member, amount):
        return amount
    assert ledger.signature_ok(good)
    assert not ledger.signature_ok(renamed) and not ledger.signature_ok(sync)


def test_reds_set_balance_still_has_the_shape_the_ledger_wraps():
    """Pins Red's real signature. Skipped where Red isn't installed (run it in a Red 3.5 venv before deploys)."""
    pytest.importorskip("redbot.core.bank")
    if "redbot.core.bank" not in sys.modules or not hasattr(sys.modules["redbot.core.bank"], "set_balance"):
        pytest.skip("redbot is a test stub here")
    from redbot.core import bank as red_bank
    assert ledger.signature_ok(red_bank.set_balance)
    assert "set_balance(member, bal - amount)" in __import__("inspect").getsource(red_bank.withdraw_credits)
    assert "set_balance(member, amount + bal)" in __import__("inspect").getsource(red_bank.deposit_credits)


# ------------------------------------------------------------------ store

def _row(ts, delta, cog="casino", action=".casino", user=1, guild=42):
    return (ts, guild, user, 1000, 1000 + delta, delta, cog, action)


def test_rollup_moves_old_rows_into_daily_totals_and_keeps_the_sums(tmp_path):
    store = ledger.LedgerStore(tmp_path / "l.sqlite3")
    now = time.time()
    old = now - 40 * 86400
    store.add([_row(old, 500), _row(old + 1, -200), _row(old + 2, 300, cog="heist"), _row(now - 60, 50)])
    before = store.flows(now - 60 * 86400)
    assert store.rollup(now) == 3
    assert store.count() == 1
    assert store.flows(now - 60 * 86400) == before          # same totals, now from the daily table
    assert store.rollup(now) == 0                           # idempotent
    assert store.flows(now - 7 * 86400) == {"casino": {"in": 50, "out": 0, "events": 1, "actions": {".casino": 50}}}


def test_history_is_newest_first_and_capped(tmp_path):
    store = ledger.LedgerStore(tmp_path / "l.sqlite3")
    now = time.time()
    store.add([_row(now - i, i + 1) for i in range(30)] + [_row(now, 999, user=2)])
    hist = store.history(1)
    assert len(hist) == 25 and hist[0][3] == 1 and hist[-1][3] == 25
    assert store.delete_user(1) == 30 and store.history(1) == []


def test_guild_scope(tmp_path):
    store = ledger.LedgerStore(tmp_path / "l.sqlite3")
    now = time.time()
    store.add([_row(now, 10, guild=42), _row(now, 20, guild=7)])
    assert store.flows(now - 60, 42)["casino"]["in"] == 10
    assert store.flows(now - 60)["casino"]["in"] == 30       # global bank: everything
    assert store.net_between(now - 60, now + 1, 7) == 20


# ------------------------------------------------------------------ report maths and embeds

FLOWS = {
    "heist": {"in": 900, "out": 100, "events": 9, "actions": {".heist play": 800}},
    "economy": {"in": 500, "out": 0, "events": 5, "actions": {".payday": 450, ".bank set": 50}},
    "casino": {"in": 300, "out": 700, "events": 20, "actions": {".blackjack": -300, ".slot": -100}},
    "shop": {"in": 0, "out": 250, "events": 2, "actions": {".shop buy": -250}},
}


def test_rank_orders_faucets_and_sinks_biggest_first():
    faucets, sinks = ledger.rank(FLOWS)
    assert [f[0] for f in faucets] == ["heist", "economy"] and faucets[1][4] == ".payday"
    assert [s[0] for s in sinks] == ["casino", "shop"] and sinks[0][4] == ".blackjack"
    assert ledger.total_net(FLOWS) == 650


def test_house_result_sign():
    result = dict((s, (r, n)) for s, r, n in ledger.house(FLOWS))
    assert result["casino"] == (400, 20)      # members lost 400: house up
    assert result["heist"] == (-800, 9)       # heist paid out 800 more than it took: house down
    assert result["lottery"] == (0, 0)


def recon_ok(gap, supply_now=10_000, **extra):
    pct = abs(gap) / supply_now
    return {"status": "ok", "since": time.time() - 86400, "hours": 24.0, "supply": 600, "ledger": 600 - gap, "gap": gap,
            "pct": pct, "alert": pct > ledger.UNATTRIBUTED_ALERT, "off_seconds": 0, "supply_now": supply_now, **extra}


def test_ledger_embed_reads_right():
    from wonderland import embeds
    e = embeds.ledger_embed("wondercoin", time.time() - 86400, FLOWS, FLOWS, recon_ok(50), "Ledger on")
    f = {x.name: x.value for x in e.fields}
    assert f["Faucets, last 7 days (net +650)"].startswith("**heist** +800 (in 900 / out 100) · mostly .heist play")
    assert "**casino**: house up **400** (20 changes)" in f["House result, last 30 days"]
    assert "**heist**: house down **800**" in f["House result, last 30 days"]
    assert len(e) < 6000


def test_member_embed_lists_changes():
    from wonderland import embeds
    now = time.time()
    e = embeds.ledger_member_embed("wondercoin", "Ann", [(now, 100, 70, -30, "casino", ".casino blackjack")])
    assert "**-30** → 70 · casino · .casino blackjack" in e.description


# ------------------------------------------------------------------ snapshots carry what the check needs

def test_snapshots_carry_supply_and_time_only_when_given():
    from wonderland import economy
    stats = economy.summarize([100, 200])
    with_both = economy.snapshot(stats, "2026-11-26", supply=1_234, ts=1_700_000_000.1234)
    assert with_both["supply"] == 1_234 and with_both["ts"] == 1_700_000_000.123
    assert "supply" not in economy.snapshot(stats, "2026-11-26")           # older callers and old snapshots: unchanged
    assert economy.change(economy.snapshot(stats, "2026-11-25"), with_both)["total"] == 0


# ------------------------------------------------------------------ reconciliation

BASE = {"ts": 1_000.0, "supply": 10_000}
DAY = 1_000.0 + 86_400


def test_the_gap_is_the_change_in_supply_minus_the_ledgers_net():
    r = ledger.reconcile(BASE, 10_500, DAY, net=300, started_at=0, keep_from=0)
    assert (r["status"], r["supply"], r["ledger"], r["gap"]) == ("ok", 500, 300, 200)
    assert r["hours"] == 24 and r["pct"] == pytest.approx(200 / 10_500) and r["alert"] is True


def test_a_ledger_that_saw_everything_has_no_gap():
    r = ledger.reconcile(BASE, 9_700, DAY, net=-300, started_at=0, keep_from=0)
    assert r["gap"] == 0 and r["pct"] == 0 and r["alert"] is False


def test_the_alert_is_strictly_more_than_one_percent_of_supply():
    exactly = ledger.reconcile(BASE, 10_000, DAY, net=-100, started_at=0, keep_from=0)    # gap 100 = 1.00%
    over = ledger.reconcile(BASE, 10_000, DAY, net=-101, started_at=0, keep_from=0)
    assert exactly["pct"] == 0.01 and exactly["alert"] is False and over["alert"] is True
    assert ledger.reconcile(BASE, 9_900, DAY, net=0, started_at=0, keep_from=0)["gap"] == -100   # coins vanished: negative


def test_reconcile_will_not_report_a_gap_for_a_window_it_could_not_have_seen():
    assert ledger.reconcile(None, 5, DAY, 0, 0, 0)["status"] == "no_baseline"
    assert ledger.reconcile({"date": "2026-11-25", "total": 1}, 5, DAY, 0, 0, 0)["status"] == "no_baseline"   # old snapshot
    assert ledger.reconcile(BASE, 5, DAY, 0, started_at=2_000.0, keep_from=0)["status"] == "too_early"
    assert ledger.reconcile(BASE, 5, DAY, 0, started_at=0, keep_from=2_000.0)["status"] == "too_old"


def test_reconcile_counts_time_the_ledger_was_not_running_inside_the_window():
    r = ledger.reconcile(BASE, 10_000, 5_000.0, 0, 0, 0, gaps=[(500.0, 1_600.0), (2_000.0, 2_600.0), (4_900.0, 9_000.0)])
    assert r["off_seconds"] == 600 + 600 + 100         # clipped to [1000, 5000]


def test_gaps_are_listed_by_overlap_and_old_ones_are_pruned_by_the_rollup(tmp_path):
    store = ledger.LedgerStore(tmp_path / "l.sqlite3")
    now = time.time()
    store.gap_add(now - 100 * 86400, now - 100 * 86400 + 60)
    store.gap_add(now - 3600, now - 1800)
    assert store.gaps_between(now - 7200, now) == [(now - 3600, now - 1800)]
    assert len(store.gaps_between(0, now)) == 2
    store.rollup(now)
    assert store.gaps_between(0, now) == [(now - 3600, now - 1800)]


def test_start_records_a_gap_after_a_long_silence_but_not_after_a_quick_reload(bank, tmp_path):
    store = ledger.LedgerStore(tmp_path / "l.sqlite3")
    store.meta_set("alive", str(time.time() - 3600))
    first = Host()
    first._ledger_start(bank, store, loop_task=False)
    assert len(store.gaps_between(0, time.time() + 1)) == 1        # an hour with no heartbeat
    first._ledger_stop()                                           # stamps the unload time
    second = Host()
    second._ledger_start(bank, store, loop_task=False)             # `.cog update true` a moment later
    second._ledger_stop()
    assert len(store.gaps_between(0, time.time() + 1)) == 1


def test_a_clean_unload_stamps_the_time_so_a_stale_heartbeat_is_not_a_gap(bank, tmp_path):
    store = ledger.LedgerStore(tmp_path / "l.sqlite3")
    h = Host()
    h._ledger_start(bank, store, loop_task=False)
    store.meta_set("alive", str(time.time() - 100))                # last heartbeat looks 100 s old when the cog unloads
    h._ledger_stop()
    again = Host()
    again._ledger_start(bank, store, loop_task=False)              # reloaded straight away
    again._ledger_stop()
    assert store.gaps_between(0, time.time() + 1) == []


def test_the_first_ever_start_is_not_a_gap(bank, tmp_path):
    store = ledger.LedgerStore(tmp_path / "l.sqlite3")
    h = Host()
    h._ledger_start(bank, store, loop_task=False)
    assert store.gaps_between(0, time.time() + 1) == []
    h._ledger_stop()


# ------------------------------------------------------------------ the report leads with a big gap

def test_unattributed_leads_the_report_only_when_over_one_percent():
    from wonderland import embeds
    when = time.time() - 86400
    big = embeds.ledger_embed("wondercoin", when, FLOWS, FLOWS, recon_ok(250), "Ledger on")
    assert big.fields[0].name == "⚠️ Unattributed, last 24h"
    assert "**unattributed +250** (2.50% of the 10,000 in circulation)" in big.fields[0].value
    assert "ExtendedEconomy auto-payday" in big.fields[0].value and len(big) < 6000
    small = embeds.ledger_embed("wondercoin", when, FLOWS, FLOWS, recon_ok(40), "Ledger on")
    assert small.fields[-1].name == "Unattributed, last 24h" and not any(f.name.startswith("⚠️") for f in small.fields)
    assert "Small gaps are expected" in small.fields[-1].value
    none = embeds.ledger_embed("wondercoin", when, FLOWS, FLOWS, recon_ok(0), "Ledger on")
    assert "**unattributed +0**" in none.fields[-1].value and "Small gaps" not in none.fields[-1].value


def test_unattributed_says_when_the_ledger_was_off_and_why_it_cannot_check():
    from wonderland import embeds
    when = time.time() - 86400
    off = embeds.ledger_embed("wondercoin", when, FLOWS, FLOWS, recon_ok(250, off_seconds=900), "x")
    assert "not running for 15 min" in off.fields[0].value
    for status, text in (("no_baseline", "No earlier snapshot"), ("too_early", "ledger started"), ("too_old", "older than the 30 days")):
        e = embeds.ledger_embed("wondercoin", when, FLOWS, FLOWS, {"status": status, "since": when, "started": when}, "x")
        assert text in e.fields[-1].value and e.fields[-1].name == "Unattributed"


# ------------------------------------------------------------------ modules that imported set_balance directly

@pytest.fixture
def red_bank():
    """Named like Red's module, so its functions look like Red's own to the importer scan (not in sys.modules)."""
    return load_module("redbot.core.bank", BANK_SRC)


IMPORTER_SRC = """
async def pay(member, amount):
    return await set_balance(member, amount)
"""


def importer(monkeypatch, name, held):
    """A stand-in for `from redbot.core.bank import set_balance` in a cog: its own reference to the function."""
    mod = load_module(name, IMPORTER_SRC)
    mod.set_balance = held
    monkeypatch.setitem(sys.modules, name, mod)
    return mod


async def test_a_module_that_imported_set_balance_directly_is_covered_named_and_restored(red_bank, monkeypatch, tmp_path):
    original = red_bank.set_balance
    shop = importer(monkeypatch, "fakeshop.engine", original)
    h = Host(("fakeshop",))
    assert h._ledger_start(red_bank, ledger.LedgerStore(tmp_path / "l.sqlite3"), loop_task=False)
    assert shop.set_balance is red_bank.set_balance and ledger_cog.is_ours(shop.set_balance)
    await shop.pay(member(), 40)                                   # bypassed the bank attribute before the fix
    assert rows(h) == [(1, 0, 40, 40, "fakeshop", "fn: pay")]
    assert "also covering direct set_balance imports in fakeshop.engine.set_balance" in h._ledger_status_line()
    h._ledger_stop()
    assert shop.set_balance is original and red_bank.set_balance is original


async def test_a_direct_import_of_the_true_original_is_covered_when_another_cog_overrides_the_bank(red_bank, monkeypatch, tmp_path):
    """BankEvents-style: bank.set_balance is another cog's override, but a cog still holds Red's own function."""
    true_original = red_bank.set_balance
    override = load_module("fakeevents.overrides", "async def set_balance(member, amount):\n    return await orig(member, amount)\n")
    override.orig = true_original
    red_bank.set_balance = override.set_balance
    monkeypatch.setitem(sys.modules, "fakeevents.overrides", override)
    gafishing = importer(monkeypatch, "fakegaf.shop", true_original)
    h = Host(("fakegaf", "fakeevents"))
    h._ledger_start(red_bank, ledger.LedgerStore(tmp_path / "l.sqlite3"), loop_task=False)
    assert gafishing.set_balance is red_bank.set_balance
    assert override.set_balance is not red_bank.set_balance        # the override module is left alone entirely:
    assert override.orig is true_original                           # swapping its saved original would make it call itself
    await gafishing.pay(member(), 25)
    assert rows(h) == [(1, 0, 25, 25, "fakegaf", "fn: pay")]
    assert await red_bank.set_balance(member(uid=2), 5) == 5       # the override chain still works: no recursion
    h._ledger_stop()
    assert gafishing.set_balance is true_original


def test_the_scan_survives_odd_modules_and_ignores_ones_that_hold_nothing(red_bank, monkeypatch):
    monkeypatch.setitem(sys.modules, "fakeodd", object())          # no __dict__
    plain = importer(monkeypatch, "fakeplain", lambda: None)       # holds some other function
    assert ledger_cog.find_direct_importers(red_bank.set_balance, sys.modules) == []
    assert plain.set_balance is not red_bank.set_balance


async def test_unload_does_not_undo_a_swap_someone_else_made_since(red_bank, monkeypatch, tmp_path):
    original = red_bank.set_balance
    shop = importer(monkeypatch, "fakeshop.engine", original)
    h = Host(("fakeshop",))
    h._ledger_start(red_bank, ledger.LedgerStore(tmp_path / "l.sqlite3"), loop_task=False)

    async def theirs(member, amount):
        return amount
    shop.set_balance = theirs
    h._ledger_stop()
    assert shop.set_balance is theirs


async def test_a_wrapper_left_in_an_importer_by_a_load_that_never_unloaded_is_replaced(red_bank, monkeypatch, tmp_path):
    original = red_bank.set_balance
    shop = importer(monkeypatch, "fakeshop.engine", original)
    first = Host(("fakeshop",))
    first._ledger_start(red_bank, ledger.LedgerStore(tmp_path / "a.sqlite3"), loop_task=False)
    stale = shop.set_balance                                       # the cog is replaced without cog_unload running
    second = Host(("fakeshop",))
    second._ledger_start(red_bank, ledger.LedgerStore(tmp_path / "b.sqlite3"), loop_task=False)
    assert shop.set_balance is red_bank.set_balance and shop.set_balance is not stale
    await shop.pay(member(), 15)
    assert rows(second) == [(1, 0, 15, 15, "fakeshop", "fn: pay")] and rows(first) == []
    second._ledger_stop()
