"""Canvas views, modals and the hub/mod/weekly glue, against real discord.py and real Red objects.

The interactions are fakes that record what the bot would have sent; the logic under them (service, store,
renderer) is the real thing. Needs real discord.py and Red, like test_post_mode_real_red.py.
"""
import asyncio
import copy
import time
from datetime import datetime
from types import SimpleNamespace

import pytest

import discord

if not hasattr(discord, "__version__"):
    pytest.skip("real discord.py not installed", allow_module_level=True)
try:
    from redbot.core import commands as _red_commands
    _real_red = hasattr(_red_commands.Cog, "listener")
except ImportError:
    _real_red = False
if not _real_red:
    pytest.skip("real Red not installed", allow_module_level=True)

from gamble import canvas_render, canvas_store as cs, canvas_ui as ui
from gamble.canvas_service import CanvasService
from gamble.canvas_store import CanvasStore
from gamble.gamblethreads import SEEDED_FLAGSHIPS, GambleThreads
from gamble.test_canvas_service import FakeBank


# ------------------------------------------------------------------ fakes

class Resp:
    def __init__(self, inter):
        self.inter, self.done = inter, False

    async def send_message(self, content=None, **kw):
        self.done = True
        self.inter.sent.append({"content": content, **kw})

    async def edit_message(self, **kw):
        self.done = True
        self.inter.edits.append(kw)

    async def defer(self, **kw):
        self.done = True
        self.inter.deferred.append(kw)

    async def send_modal(self, modal):
        self.done = True
        self.inter.modals.append(modal)

    def is_done(self):
        return self.done


DISPATCHED: list = []   # bot.dispatch calls made through interaction.client


class Inter:
    def __init__(self, user, guild=None, data=None):
        self.user, self.guild, self.data = user, guild, data or {}
        self.client = SimpleNamespace(dispatch=lambda *a: DISPATCHED.append(a))
        self.sent, self.edits, self.deferred, self.modals, self.followups, self.original = [], [], [], [], [], []
        self.response = Resp(self)
        self.followup = SimpleNamespace(send=self._followup)

    async def _followup(self, content=None, **kw):
        self.followups.append({"content": content, **kw})

    async def edit_original_response(self, **kw):
        self.original.append(kw)


def user(uid, roles=(), manage_guild=False):
    return SimpleNamespace(id=uid, roles=list(roles), guild_permissions=SimpleNamespace(administrator=False, manage_guild=manage_guild))


def fill(modal, **values):
    """Type into a modal's boxes by attribute name (first, second, color, text, xy, who)."""
    for name, value in values.items():
        getattr(modal, name)._value = value


@pytest.fixture
def env(tmp_path):
    store = CanvasStore(str(tmp_path / "c.sqlite"))
    bank = FakeBank({1: 100_000, 2: 100_000, 3: 500})
    modlog, lurkers = [], set()

    async def post(content):
        modlog.append(content)

    async def is_lurker(m):
        return m.id in lurkers

    svc = CanvasService(store, bank, is_lurker=is_lurker, modlog=post)
    yield SimpleNamespace(svc=svc, store=store, bank=bank, modlog=modlog, lurkers=lurkers)
    store.close()


def embed_text(embed):
    parts = [embed.title or "", embed.description or ""]
    for f in embed.fields:
        parts += [f.name, f.value]
    parts.append(embed.footer.text if embed.footer else "")
    return "\n".join(parts)


# ------------------------------------------------------------------ the main view

async def test_opening_the_canvas_is_private_and_has_the_five_buttons(env):
    inter = Inter(user(1))
    await ui.open_canvas(inter, env.svc)
    assert inter.deferred == [{"ephemeral": True}]
    msg = inter.followups[0]
    assert msg["ephemeral"] is True and msg["file"].filename == "canvas.png"
    labels = [c.label for c in msg["view"].children]
    assert labels[:5] == ["Zoom", "Inspect", "Paint", "Paint area", "Leaderboard"]
    assert "1,000 wc" in msg["embed"].description and "100×50" in msg["embed"].description


async def test_another_member_cannot_press_my_buttons(env):
    view = ui.MainView(env.svc, user(1))
    stranger = Inter(user(2))
    assert await view.interaction_check(stranger) is False and stranger.sent[0]["ephemeral"] is True
    assert await view.interaction_check(Inter(user(1))) is True


async def test_zoom_and_inspect_open_a_modal_and_reject_bad_input(env):
    view = ui.MainView(env.svc, user(1))
    inter = Inter(user(1))
    await view.zoom_button.callback(inter)
    modal = inter.modals[0]
    fill(modal, xy="nope")
    bad = Inter(user(1))
    await modal.on_submit(bad)
    assert "two numbers" in bad.sent[0]["content"]
    fill(modal, xy="150,3")
    out = Inter(user(1))
    await modal.on_submit(out)
    assert "x must be" in out.sent[0]["content"]


async def test_zoom_returns_a_close_up_with_prices(env):
    await env.svc.paint(user(1), [(50, 25)], "#ff0000", "", (1000,))
    view = ui.MainView(env.svc, user(1))
    inter = Inter(user(1))
    await view._zoom(inter, (50, 25))
    msg = inter.followups[0]
    assert msg["ephemeral"] and msg["file"].filename == "zoom.png"
    assert "45–54" in msg["embed"].description and "1,000 wc" in msg["embed"].description


async def test_inspect_shows_colour_owner_price_and_escaped_text(env):
    await env.svc.paint(user(1), [(4, 4)], "#ffb7c5", "@everyone *hi*", (1000,))
    inter = Inter(user(2))
    await ui.MainView(env.svc, user(2))._inspect(inter, (4, 4))
    text = embed_text(inter.sent[0]["embed"])
    assert "Rose #ffb7c5" in text and "<@1>" in text and "1,200 wc" in text
    assert "@everyone" not in text and "hi" in text and inter.sent[0]["ephemeral"]
    blank = Inter(user(2))
    await ui.MainView(env.svc, user(2))._inspect(blank, (9, 9))
    assert "Blank" in blank.sent[0]["embed"].description and "1,000 wc" in blank.sent[0]["embed"].description


async def test_pixel_text_is_never_on_the_rendered_canvas_or_zoom(env):
    # the renderers only receive colours (and prices for the zoom), so pixel text cannot leak onto an image
    import inspect
    assert list(inspect.signature(canvas_render.render_canvas).parameters) == ["colors"]
    assert list(inspect.signature(canvas_render.render_zoom).parameters) == ["cx", "cy", "lookup"]
    await env.svc.paint(user(1), [(4, 4)], "#ff0000", "SECRET WORDS", (1000,))
    assert env.svc.zoom_image and b"SECRET" not in await env.svc.image()


async def test_leaderboard_button_shows_week_all_time_and_pixels(env):
    await env.svc.paint(user(1), [(0, 0)], "#000000", "", (1000,))
    inter = Inter(user(1))
    await ui.MainView(env.svc, user(1)).leaderboard_button.callback(inter)
    text = embed_text(inter.sent[0]["embed"])
    assert "last 7 days" in text and "all time" in text and "Most pixels owned" in text and "<@1>" in text
    assert inter.sent[0]["ephemeral"]


# ------------------------------------------------------------------ painting flow

async def submit_paint(env, uid, first, color="", text="", second=None, area=False):
    modal = ui.PaintModal(env.svc, user(uid), area=area)
    fill(modal, first=first, color=color, text=text)
    if area:
        fill(modal, second=second)
    inter = Inter(user(uid))
    await modal.on_submit(inter)
    return inter


async def test_paint_with_a_hex_goes_straight_to_a_confirm_screen_with_the_price(env):
    inter = await submit_paint(env, 1, "4,4", color="#ffb7c5", text="hi")
    msg = inter.sent[0]
    text = embed_text(msg["embed"])
    assert msg["ephemeral"] and "(4,4)" in text and "1,000 wc" in text and "hi" in text
    assert "Your balance: 100,000 wc" in text
    assert [c.label for c in msg["view"].children] == ["Confirm", "Cancel"]
    assert env.store.painted_count() == 0 and env.bank.log == []          # confirming is what charges


async def test_paint_without_a_hex_offers_the_pastel_dropdown_then_confirms(env):
    inter = await submit_paint(env, 1, "4,4")
    view = inter.sent[0]["view"]
    select = next(c for c in view.children if isinstance(c, discord.ui.Select))
    assert len(select.options) == len(canvas_render.PASTELS)
    pick = Inter(user(1), data={"values": ["#BDE0FE"]})
    await view._picked(pick)
    assert "Sky #bde0fe" in embed_text(pick.edits[0]["embed"])
    assert [c.label for c in pick.edits[0]["view"].children] == ["Confirm", "Cancel"]


async def test_bad_input_is_refused_before_anything_is_priced(env):
    cases = [(dict(first="4,4", color="red"), "hex colour"),
             (dict(first="100,4", color="#fff"), "x 0–99"),
             (dict(first="nope", color="#fff"), "coordinates"),
             (dict(first="0,0", second="10,0", color="#fff", area=True), "at most 10×10"),
             (dict(first="0,0", second="9", color="#fff", area=True), "coordinates")]
    for kw, expect in cases:
        inter = await submit_paint(env, 1, **kw)
        assert expect in inter.sent[0]["content"].lower() or expect in inter.sent[0]["content"], (kw, inter.sent)
        assert "view" not in inter.sent[0]


async def test_banned_and_lurker_members_are_stopped_at_the_modal(env):
    env.store.ban(1, 99)
    env.lurkers.add(2)
    assert "banned" in (await submit_paint(env, 1, "4,4", color="#fff")).sent[0]["content"]
    assert "Lurkers" in (await submit_paint(env, 2, "4,4", color="#fff")).sent[0]["content"]


async def test_confirm_paints_and_charges_exactly_the_confirmed_price(env):
    inter = await submit_paint(env, 1, "4,4", color="#ffb7c5", text="hi")
    view = inter.sent[0]["view"]
    click = Inter(user(1))
    await view.confirm.callback(click)
    result = click.original[0]
    assert "Painted" in result["embed"].title and "1,200 wc" in result["embed"].description
    assert result["view"] is None and result["attachments"][0].filename == "canvas.png"
    assert env.bank.balances[1] == 99_000 and env.store.get_pixel(4, 4).owner == 1
    assert len(env.modlog) == 1 and "hi" in env.modlog[0]


async def test_a_paint_tells_daily_quests_once_and_a_failed_paint_does_not(env):
    DISPATCHED.clear()
    inter = await submit_paint(env, 1, "4,4", color="#ffb7c5")
    await inter.sent[0]["view"].confirm.callback(Inter(user(1)))
    assert DISPATCHED == [("wonderland_quest", user(1), "canvas", 1)] or (
        len(DISPATCHED) == 1 and DISPATCHED[0][0] == "wonderland_quest" and DISPATCHED[0][2:] == ("canvas", 1))
    DISPATCHED.clear()
    poor = await submit_paint(env, 1, "5,5", color="#ffb7c5")
    await env.svc.paint(user(2), [(5, 5)], "#000000", "", (1000,))         # price moves: no paint, no quest
    await poor.sent[0]["view"].confirm.callback(Inter(user(1)))
    assert DISPATCHED == []


def test_daily_quests_button_sits_after_payday_only_when_quests_are_on():
    from gamble.gamblethreads import QUESTS_CUSTOM_ID, QuestsRouteView
    cog = object.__new__(GambleThreads)
    games = {k: {"emoji": "x", "label": k.title(), "command": k} for k in ("profile", "dailycards", "payday", "gamble")}
    off = cog._build_hub_view(games)
    assert QUESTS_CUSTOM_ID not in [getattr(c, "custom_id", None) for c in off.children]
    rows_off = off.to_components()
    assert [c["custom_id"] for c in rows_off[0]["components"]][-1] == "gamblehub:active"   # layout unchanged
    on = cog._build_hub_view(games, quests=True)
    rows = on.to_components()
    assert [c["custom_id"] for c in rows[0]["components"]] == [
        "gamblehub:flagship:profile", "gamblehub:flagship:dailycards", "gamblehub:flagship:payday",
        QUESTS_CUSTOM_ID, "gamblehub:flagship:gamble"]
    assert rows[1]["components"][0]["custom_id"] == "gamblehub:select"
    assert rows[2]["components"][0]["custom_id"] == "gamblehub:active"
    route = QuestsRouteView(cog)
    assert route.is_persistent() and route.children[0].custom_id == QUESTS_CUSTOM_ID


async def test_daily_quests_click_hands_off_to_dailyquests_or_says_unavailable():
    cog = object.__new__(GambleThreads)
    opened = []

    class DailyQuests:
        on = True

        async def quests_open(self, guild):
            return self.on

        async def open_quest_panel(self, interaction):
            opened.append(interaction)

    wl = DailyQuests()
    cog.bot = SimpleNamespace(get_cog=lambda name: wl if name == "DailyQuests" else None)
    inter = Inter(user(1), guild=SimpleNamespace(id=5))
    await cog.handle_quests_click(inter)
    assert opened == [inter]
    wl.on = False
    off = Inter(user(1), guild=SimpleNamespace(id=5))
    await cog.handle_quests_click(off)
    assert opened == [inter] and "aren't available" in off.sent[0]["content"] and off.sent[0]["ephemeral"]
    cog.bot = SimpleNamespace(get_cog=lambda name: None)
    gone = Inter(user(1), guild=SimpleNamespace(id=5))
    await cog.handle_quests_click(gone)
    assert "aren't available" in gone.sent[0]["content"]


async def test_if_the_price_moved_the_screen_shows_the_new_price_and_nobody_is_charged(env):
    inter = await submit_paint(env, 1, "4,4", color="#ffb7c5")
    view = inter.sent[0]["view"]
    await env.svc.paint(user(2), [(4, 4)], "#000000", "", (1000,))        # someone paints it while 1 deliberates
    click = Inter(user(1))
    await view.confirm.callback(click)
    redraw = click.original[0]
    text = embed_text(redraw["embed"])
    assert "not charged" in text and "1,200 wc" in text and "1,000 wc" in text
    assert env.bank.balances[1] == 100_000 and env.store.get_pixel(4, 4).owner == 2
    again = Inter(user(1))
    await redraw["view"].confirm.callback(again)                            # confirming the NEW price works
    assert "Painted" in again.original[0]["embed"].title and env.bank.balances[1] == 100_000 - 1200


async def test_a_double_click_on_confirm_charges_once(env):
    inter = await submit_paint(env, 1, "4,4", color="#ffb7c5")
    view = inter.sent[0]["view"]
    await asyncio.gather(view.confirm.callback(Inter(user(1))), view.confirm.callback(Inter(user(1))))
    assert env.bank.balances[1] == 99_000


async def test_not_enough_coins_disables_confirm_and_says_how_short(env):
    inter = await submit_paint(env, 3, "4,4", color="#ffb7c5")              # user 3 has 500
    view = inter.sent[0]["view"]
    assert view.confirm.disabled and "short by 500 wc" in embed_text(inter.sent[0]["embed"])


async def test_if_the_balance_drops_after_the_screen_the_withdrawal_fails_cleanly(env):
    inter = await submit_paint(env, 1, "4,4", color="#ffb7c5")
    env.bank.balances[1] = 10
    click = Inter(user(1))
    await inter.sent[0]["view"].confirm.callback(click)
    assert "Not painted" in click.original[0]["embed"].title and "You have 10 wc" in click.original[0]["embed"].description
    assert env.bank.balances[1] == 10 and env.store.painted_count() == 0


async def test_paint_area_confirms_the_summed_price_and_paints_every_pixel(env):
    await env.svc.paint(user(2), [(1, 0)], "#000000", "", (1000,))          # (1,0) costs 1,200 now
    inter = await submit_paint(env, 1, "0,0", second="2,0", color="#c1f0d0", area=True, text="wall")
    text = embed_text(inter.sent[0]["embed"])
    assert "(0,0)–(2,0)" in text and "3 pixels" in text and "3,200 wc" in text and "2 blank" in text
    click = Inter(user(1))
    await inter.sent[0]["view"].confirm.callback(click)
    assert env.bank.balances[1] == 100_000 - 3200
    assert [env.store.get_pixel(x, 0).owner for x in range(3)] == [1, 1, 1]
    assert sum("wall" in line for line in env.modlog) == 1


async def test_area_is_all_or_nothing_if_any_price_moved(env):
    inter = await submit_paint(env, 1, "0,0", second="2,0", color="#c1f0d0", area=True)
    await env.svc.paint(user(2), [(2, 0)], "#000000", "", (1000,))
    click = Inter(user(1))
    await inter.sent[0]["view"].confirm.callback(click)
    assert "not charged" in embed_text(click.original[0]["embed"])
    assert env.bank.balances[1] == 100_000 and env.store.get_pixel(0, 0) is None


async def test_cancel_charges_nothing(env):
    inter = await submit_paint(env, 1, "4,4", color="#ffb7c5")
    click = Inter(user(1))
    await inter.sent[0]["view"].cancel.callback(click)
    assert click.edits[0]["view"] is None and env.bank.log == []


async def test_only_the_painter_can_confirm(env):
    inter = await submit_paint(env, 1, "4,4", color="#ffb7c5")
    stranger = Inter(user(2))
    assert await inter.sent[0]["view"].interaction_check(stranger) is False


# ------------------------------------------------------------------ mod panel

async def is_mod(m):
    return m.id == 9


async def test_the_mod_panel_stub_refuses_non_mods_and_opens_privately_for_mods(env):
    stub = ui.OpenModPanel(env.svc, is_mod)
    no = Inter(user(1))
    await stub.open.callback(no)
    assert "Mods only" in no.sent[0]["content"] and "view" not in no.sent[0]
    yes = Inter(user(9))
    await stub.open.callback(yes)
    assert yes.sent[0]["ephemeral"] and isinstance(yes.sent[0]["view"], ui.ModPanelView)


async def test_the_mod_panel_shows_the_usage_counts_and_review_date(env):
    await env.svc.paint(user(1), [(0, 0)], "#000000", "", (1000,))
    text = embed_text(ui.mod_embed(env.svc))
    for needle in ("Paints per day", "Distinct painters", "Coins burned per week", "Review on", "1,000 wc", "1 all time"):
        assert needle in text, needle


async def test_mod_panel_buttons_are_mod_only(env):
    view = ui.ModPanelView(env.svc, user(9), is_mod)
    assert await view.interaction_check(Inter(user(1))) is False
    assert await view.interaction_check(Inter(user(9))) is True


BIG = 123456789012345678        # real Discord IDs are 17-19 digits; the mod modals look for one


async def test_clear_text_ban_and_unban_from_the_panel(env):
    env.bank.balances[BIG] = 5000
    await env.svc.paint(user(BIG), [(4, 4)], "#000000", "rude", (1000,))
    view = ui.ModPanelView(env.svc, user(9), is_mod)
    clear_modal = Inter(user(9))
    await view.clear_text.callback(clear_modal)
    fill(clear_modal.modals[0], xy="4,4")
    done = Inter(user(9))
    await clear_modal.modals[0].on_submit(done)
    assert env.store.get_pixel(4, 4).text == "" and "Cleared" in done.edits[0]["embed"].description
    ban = Inter(user(9))
    await view.ban.callback(ban)
    fill(ban.modals[0], who=f"<@{BIG}>")
    await ban.modals[0].on_submit(Inter(user(9)))
    assert env.store.is_banned(BIG)
    unban = Inter(user(9))
    await view.unban.callback(unban)
    fill(unban.modals[0], who="1")
    bad = Inter(user(9))
    await unban.modals[0].on_submit(bad)
    assert "numeric user ID" in bad.sent[0]["content"] and env.store.is_banned(BIG)
    fill(unban.modals[0], who=str(BIG))
    await unban.modals[0].on_submit(Inter(user(9)))
    assert not env.store.is_banned(BIG)


async def test_wipe_is_a_dry_run_first_and_only_the_confirm_button_changes_anything(env):
    env.bank.balances[BIG] = 5000
    await env.svc.paint(user(BIG), cs.rect_cells(0, 0, 1, 0), "#000000", "", (1000, 1000))
    view = ui.ModPanelView(env.svc, user(9), is_mod)
    ask = Inter(user(9))
    await view.wipe.callback(ask)
    fill(ask.modals[0], who=f"<@{BIG}>")
    preview = Inter(user(9))
    await ask.modals[0].on_submit(preview)
    text = embed_text(preview.edits[0]["embed"])
    assert "dry run" in text and "owns **2** pixels" in text
    assert env.store.painted_count() == 2                                   # nothing changed yet
    confirm_view = preview.edits[0]["view"]
    go = Inter(user(9))
    await confirm_view.go.callback(go)
    assert env.store.painted_count() == 0 and env.store.price_at(0, 0) == 1000
    again = Inter(user(9))
    await confirm_view.go.callback(again)                                   # a second click does nothing more
    assert not again.edits


async def test_cancelling_a_wipe_changes_nothing(env):
    env.bank.balances[BIG] = 5000
    await env.svc.paint(user(BIG), [(0, 0)], "#000000", "", (1000,))
    view = ui.ModPanelView(env.svc, user(9), is_mod)
    ask = Inter(user(9))
    await view.wipe.callback(ask)
    fill(ask.modals[0], who=str(BIG))
    preview = Inter(user(9))
    await ask.modals[0].on_submit(preview)
    click = Inter(user(9))
    await preview.edits[0]["view"].cancel.callback(click)
    assert env.store.painted_count() == 1 and "Cancelled" in click.edits[0]["embed"].description


async def test_wiping_a_member_with_no_pixels_says_so(env):
    view = ui.ModPanelView(env.svc, user(9), is_mod)
    ask = Inter(user(9))
    await view.wipe.callback(ask)
    fill(ask.modals[0], who="777777777777777777")
    out = Inter(user(9))
    await ask.modals[0].on_submit(out)
    assert "doesn't own any pixels" in out.edits[0]["embed"].description


async def test_reset_shows_a_dry_run_screen_then_a_confirm_button(env):
    await env.svc.paint(user(1), [(0, 0)], "#000000", "", (1000,))
    view = ui.ModPanelView(env.svc, user(9), is_mod)
    click = Inter(user(9))
    await view.reset.callback(click)
    assert "dry run" in click.edits[0]["embed"].title and "Nothing has changed yet" in click.edits[0]["embed"].description
    assert env.store.painted_count() == 1
    go = Inter(user(9))
    await click.edits[0]["view"].go.callback(go)
    assert env.store.painted_count() == 0 and "blank again" in go.edits[0]["embed"].description


# ------------------------------------------------------------------ the cog glue

class Chan(SimpleNamespace):
    def __init__(self, id, name, position=0, can_send=True):
        super().__init__(id=id, name=name, position=position, sent=[], can_send=can_send)

    async def send(self, content=None, **kw):
        if not self.can_send:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="no"), "missing")
        self.sent.append({"content": content, **kw})


def make_cog(tmp_path, env, channels):
    cog = object.__new__(GambleThreads)
    cog._canvases, cog._canvas_retry, cog._bg_tasks = {}, {}, set()
    guild = SimpleNamespace(id=5, text_channels=channels, get_channel=lambda cid: next((c for c in channels if c.id == cid), None))
    path = tmp_path / "canvas-5.sqlite"
    cog._canvas_path = lambda g: path
    cog.bot = SimpleNamespace(get_cog=lambda name: None)
    cog._canvases[5] = env.svc
    path.write_bytes(b"")                      # the tick only runs for a guild whose canvas exists
    return cog, guild


def test_the_hub_gets_a_builtin_canvas_entry_without_a_command():
    spec = SEEDED_FLAGSHIPS["canvas"]
    assert spec["builtin"] and spec["mode"] == "canvas" and spec["label"] == "Pixel Canvas"
    cog = object.__new__(GambleThreads)
    games = {"canvas": {"emoji": "🎨", "label": "Pixel Canvas", "command": "", "mode": "canvas"},
             "wonderjack": {"emoji": "🃏", "label": "Wonderjack", "command": "wonderjack table", "mode": "shared"}}
    view = cog._build_hub_view(games)
    values = [o.value for o in view.game_select.options]
    assert "canvas" in values                                               # it lives in the "More…" dropdown


async def test_picking_the_canvas_in_the_dropdown_opens_it_without_resolving_a_command(tmp_path, env):
    cog, guild = make_cog(tmp_path, env, [])
    games = {"canvas": {"emoji": "🎨", "label": "Pixel Canvas", "command": "", "mode": "canvas"}}
    cog.config = SimpleNamespace(guild=lambda g: SimpleNamespace(games=lambda: _async(games)))
    cog.bot.get_command = lambda name: pytest.fail("the canvas must not resolve a command")
    resets = []
    cog._schedule_select_reset = lambda g: resets.append(g)
    inter = Inter(user(1), guild=guild)
    await cog.handle_hub_click(inter, "canvas", from_select=True)
    assert inter.followups[0]["view"].children[0].label == "Zoom" and resets == [guild]


async def _async(value):
    return copy.deepcopy(value)


def test_no_new_top_level_commands_and_the_mod_panel_lives_under_gamblehub():
    top = {c.name for c in GambleThreads.__cog_commands__ if getattr(c, "parent", None) is None}
    assert top == {"gamble", "endgambling", "gambleset", "gamblehub"}
    assert "canvas" in {c.name for c in GambleThreads.gamblehub.commands}


def test_find_cuddle_matches_the_live_channel_name_with_or_without_decoration():
    chans = [Chan(1, "general"), Chan(2, "🧸・cuddle", position=3), Chan(3, "cuddle-chat", position=1)]
    guild = SimpleNamespace(text_channels=chans)
    assert GambleThreads._find_cuddle(guild).id == 2                        # exact beats "contains"
    assert GambleThreads._find_cuddle(SimpleNamespace(text_channels=[Chan(1, "general")])) is None
    assert GambleThreads._find_cuddle(SimpleNamespace(text_channels=[Chan(3, "cuddle-chat")])).id == 3


def now_after_boundary():
    """Thursday after the Nov 22 2026 Sunday-6PM boundary, and that boundary."""
    boundary = datetime(2026, 11, 22, 18, tzinfo=cs.PACIFIC).timestamp()
    return boundary, boundary + 4 * 86400


async def test_weekly_post_goes_to_cuddle_once_with_the_priciest_paint_and_top_spender(tmp_path, env, monkeypatch):
    boundary, now = now_after_boundary()
    cuddle = Chan(77, "🧸・cuddle")
    cog, guild = make_cog(tmp_path, env, [Chan(1, "general"), cuddle])
    env.store.paint(1, [(0, 0)], "#000000", "", (1000,), now=boundary - 86400)
    env.store.paint(2, [(0, 0)], "#000000", "", (1200,), now=boundary - 3600)
    monkeypatch.setattr(time, "time", lambda: now)
    await cog._canvas_weekly_tick(guild)
    assert len(cuddle.sent) == 1
    post = cuddle.sent[0]
    text = embed_text(post["embed"])
    assert post["file"].filename == "canvas.png" and "(0,0)" in text and "1,200 wc" in text and "<@2>" in text
    assert "text" not in {f.name.lower() for f in post["embed"].fields}
    await cog._canvas_weekly_tick(guild)
    assert len(cuddle.sent) == 1                                            # not twice


async def test_weekly_post_is_skipped_before_sunday_6pm_and_when_nobody_painted(tmp_path, env, monkeypatch):
    boundary, now = now_after_boundary()
    cuddle = Chan(77, "cuddle")
    cog, guild = make_cog(tmp_path, env, [cuddle])
    monkeypatch.setattr(time, "time", lambda: now)
    await cog._canvas_weekly_tick(guild)                                    # empty week: nothing posted, but remembered
    assert cuddle.sent == [] and env.svc.weekly_last_posted() == boundary
    env.store.paint(1, [(0, 0)], "#000000", "", (1000,), now=boundary + 3600)    # this week's paint, not last week's
    await cog._canvas_weekly_tick(guild)
    assert cuddle.sent == []
    monkeypatch.setattr(time, "time", lambda: boundary + 7 * 86400 + 60)    # the next Sunday just after 6 PM
    await cog._canvas_weekly_tick(guild)
    assert len(cuddle.sent) == 1


async def test_weekly_post_waits_and_retries_if_cuddle_is_missing_or_the_send_fails(tmp_path, env, monkeypatch):
    boundary, now = now_after_boundary()
    env.store.paint(1, [(0, 0)], "#000000", "", (1000,), now=boundary - 3600)
    monkeypatch.setattr(time, "time", lambda: now)
    cog, guild = make_cog(tmp_path, env, [Chan(1, "general")])
    await cog._canvas_weekly_tick(guild)
    assert env.svc.weekly_last_posted() == 0 and cog._canvas_retry[5] > now          # no channel: not marked posted
    locked = Chan(77, "cuddle", can_send=False)
    cog, guild = make_cog(tmp_path, env, [locked])
    await cog._canvas_weekly_tick(guild)
    assert env.svc.weekly_last_posted() == 0 and cog._canvas_retry[5] == now + 3600   # failed send: retry in an hour
    locked.can_send = False
    monkeypatch.setattr(time, "time", lambda: now + 3601)
    good = Chan(77, "cuddle")
    guild.text_channels[:] = [good]
    await cog._canvas_weekly_tick(guild)
    assert len(good.sent) == 1 and env.svc.weekly_last_posted() == boundary


async def test_a_pending_charge_found_after_a_restart_is_reported_to_mod_log(tmp_path, env):
    cog, guild = make_cog(tmp_path, env, [])
    env.store.meta_set("pending", '{"user": 4, "amount": 2400, "cells": "(1,1)–(2,1)"}')
    await cog._canvas_recover(guild)
    assert "<@4>" in env.modlog[0] and "2,400 wc" in env.modlog[0] and "(1,1)–(2,1)" in env.modlog[0]
    env.modlog.clear()
    await cog._canvas_recover(guild)
    assert env.modlog == []


async def test_the_lurker_check_reads_the_lurker_cogs_role_setting(tmp_path, env):
    cog, guild = make_cog(tmp_path, env, [])
    role_id = 321
    cog.bot.get_cog = lambda name: SimpleNamespace(config=SimpleNamespace(guild=lambda g: SimpleNamespace(
        lurker_role_id=lambda: _async(role_id)))) if name == "Lurker" else None
    guild_obj = SimpleNamespace()
    lurker = SimpleNamespace(guild=guild_obj, roles=[SimpleNamespace(id=321)])
    regular = SimpleNamespace(guild=guild_obj, roles=[SimpleNamespace(id=5)])
    assert await cog._is_lurker(lurker) is True and await cog._is_lurker(regular) is False
    cog.bot.get_cog = lambda name: None
    assert await cog._is_lurker(lurker) is False


async def test_is_mod_accepts_manage_guild_or_red_mods(tmp_path, env):
    cog, guild = make_cog(tmp_path, env, [])
    async def red_mod(m): return m.id == 6
    cog.bot.is_mod = red_mod
    assert await cog._is_mod(user(1, manage_guild=True)) is True
    assert await cog._is_mod(user(6)) is True
    assert await cog._is_mod(user(7)) is False
