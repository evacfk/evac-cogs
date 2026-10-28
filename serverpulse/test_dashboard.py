"""Dashboard page tests, run against fakes (no Red, no dashboard cog).

They prove the page data, the template, the permission bar, that a page view writes
nothing, and the hop onto the bot's event loop. They do NOT prove that the live
dashboard registers or renders the page (including whether `?days=` reaches the
handler) -- that is verified on the host after deploy.
"""
import asyncio
import copy
import datetime as dt
import re
import threading
from datetime import date
from types import SimpleNamespace

import jinja2
import pytest

from serverpulse import dashboard_view as dv
from serverpulse import models, storage
from serverpulse import serverpulse as sp
from serverpulse._testdata import flat_day, local_ts, traffic
from serverpulse.constants import DEFAULT_MOD_ROLE_ID
from serverpulse.test_cog_logic import GUILD_ID, NOW, Clock, make_bot, make_cog


# ------------------------------------------------------------------ pure view helpers

@pytest.mark.parametrize(
    "value,expected",
    [(None, dv.DEFAULT_WINDOW), ("7", 7), (14, 14), ("56", 56), ("28", 28), (["14"], 14), ([], dv.DEFAULT_WINDOW),
     ("9999", dv.DEFAULT_WINDOW), ("abc", dv.DEFAULT_WINDOW), ("-7", dv.DEFAULT_WINDOW), ("7; drop", dv.DEFAULT_WINDOW)],
)
def test_parse_window(value, expected):
    assert dv.parse_window(value) == expected


def test_window_from_kwargs_accepts_every_way_the_dashboard_might_pass_it():
    assert dv.window_from_kwargs("14") == 14
    assert dv.window_from_kwargs(days="7") == 7
    assert dv.window_from_kwargs(None, extra_kwargs={"days": "56"}) == 56
    assert dv.window_from_kwargs(None, extra_kwargs="nonsense") == dv.DEFAULT_WINDOW
    assert dv.window_from_kwargs(None, other="x") == dv.DEFAULT_WINDOW
    assert dv.window_from_kwargs() == dv.DEFAULT_WINDOW


def test_ramp_is_clamped_and_text_stays_readable():
    assert dv.ramp_rgb(-5) == dv.RAMP[0] and dv.ramp_rgb(9) == dv.RAMP[-1]
    assert all(0 <= c <= 255 for c in dv.ramp_rgb(0.37))


def test_hour_labels():
    assert [dv.hour_label(h) for h in (0, 1, 11, 12, 13, 23)] == ["12a", "1a", "11a", "12p", "1p", "11p"]
    assert dv.hour_range(22, 2) == "10p–12a"


def _data(window=28, **kw):
    docs, t0, _end = traffic(date(2026, 9, 1), 40, seed=3)
    names = {"111": "general", "222": "cuddle", "333": "pillow-talk"}
    args = dict(days=docs, today=date(2026, 10, 10), now_ts=local_ts(2026, 10, 10, 12, 30), coverage_start=t0,
                ignored=set(), window_days=window, channel_name=names.get)
    args.update(kw)
    return dv.build_page_data(**args), docs


def test_page_data_shape_and_counts():
    page, docs = _data()
    assert page["has_data"] and page["window_days"] == 28
    assert len(page["heat_rows"]) == 7 and all(len(r["cells"]) == 24 for r in page["heat_rows"])
    assert len(page["hour_bars"]) == 24 and len(page["heat_hours"]) == 24
    assert [c["label"] for c in page["cards"]][:2] == ["Messages", "Average per full day"]
    # the evening peak of the synthetic traffic is the busiest hour and marked
    top = [b["label"] for b in page["hour_bars"] if b["top"]]
    assert "9p" in top and len(top) == 3
    assert max(page["hour_bars"], key=lambda b: float(b["pct"]))["pct"] == "100.0"
    # newest day first, today is flagged as not finished
    assert page["daily"][0]["label"] == "Sat Oct 10" and page["daily"][0]["partial"] is True
    assert page["daily"][1]["partial"] is False
    assert [w["days"] for w in page["windows"] if w["active"]] == [28]


def test_message_total_matches_the_stored_days():
    # "now" is exactly 13:00, so hours 0..12 of the last day have finished and later hours have not happened
    page, docs = _data(window=7, now_ts=local_ts(2026, 10, 10, 13, 0))
    start, today = date(2026, 10, 4), date(2026, 10, 10)
    expected = 0
    for dk, doc in docs.items():
        d = date.fromisoformat(dk)
        if start <= d <= today:
            expected += sum(r["m"] for key, r in doc["h"].items() if d < today or int(key) <= 12)
    got = int(page["cards"][0]["value"].replace(",", ""))
    assert got == expected and got > 0


def test_channels_are_named_ranked_and_respect_the_ignore_list():
    page, _ = _data()
    assert {c["name"] for c in page["channels"]} == {"general", "cuddle", "pillow-talk"}
    assert page["channels"][0]["pct"] == "100.0"
    page2, _ = _data(ignored={111})
    assert "general" not in {c["name"] for c in page2["channels"]}
    page3, _ = _data(channel_name=lambda cid: None)
    assert {c["name"] for c in page3["channels"]} == {"deleted or unknown channel"}


def test_no_data_before_coverage_gives_the_collecting_state():
    page, _ = _data(coverage_start=None)
    assert page["has_data"] is False and "cards" not in page
    page, _ = _data(coverage_start=local_ts(2026, 10, 10, 12, 0))  # less than one finished hour
    assert page["has_data"] is False


def test_quiet_and_busy_days_surface_as_anomalies():
    docs = {}
    for i in range(70):
        d = date.fromordinal(date(2026, 8, 1).toordinal() + i)
        docs[d.isoformat()] = flat_day(d, 20)
    spike = date(2026, 10, 8)
    docs[spike.isoformat()] = flat_day(spike, 200)
    start_ts = local_ts(2026, 8, 1)
    page = dv.build_page_data(days=docs, today=date(2026, 10, 9), now_ts=local_ts(2026, 10, 9, 12),
                              coverage_start=start_ts, ignored=set(), window_days=14, channel_name=lambda c: None)
    assert any("Busy day" in a and "Oct 8" in a for a in page["anomalies"])


def test_user_data_never_reaches_the_page_data():
    docs, t0, _ = traffic(date(2026, 9, 1), 20, seed=5)
    secret = "918273645564738291"
    for doc in docs.values():
        doc["u"] = {secret: 99}
    page = dv.build_page_data(days=docs, today=date(2026, 9, 20), now_ts=local_ts(2026, 9, 20, 12), coverage_start=t0,
                              ignored=set(), window_days=14, channel_name=lambda c: None)
    assert secret not in repr(page)
    assert "top_chatters" not in page and "users" not in page


# ------------------------------------------------------------------ the template

def _render(autoescape, **ctx):
    env = jinja2.Environment(autoescape=autoescape)
    return env.from_string(dv.PAGE_TEMPLATE).render(**ctx)


def _ctx(page, **kw):
    base = {"guild_name": "Wonderland", "denied": False, "pulse_disabled": False, "pulse": page,
            "pulse_now": [("Last message", "3s ago")]}
    base.update(kw)
    return base


@pytest.mark.parametrize("autoescape", [True, False])
def test_template_renders_every_section(autoescape):
    page, _ = _data()
    html = _render(autoescape, **_ctx(page))
    for needle in ("Right now", "When it is busy", "Average by hour of day", "Messages per day", "Busiest channels",
                   "Unusual days and hours", "general", "Sat Oct 10", 'href="?days=7"', "<strong>28 days</strong>"):
        assert needle in html, needle


@pytest.mark.parametrize("autoescape", [True, False])
def test_hostile_channel_and_guild_names_are_escaped_exactly_once(autoescape):
    page, _ = _data(channel_name=lambda cid: "<script>alert(1)</script>")
    html = _render(autoescape, **_ctx(page, guild_name='"><img src=x onerror=alert(2)>'))
    assert "<script>alert" not in html and "<img src=x" not in html
    assert "&lt;script&gt;" in html
    assert "&amp;lt;" not in html  # not double-escaped


@pytest.mark.parametrize("autoescape", [True, False])
def test_style_attributes_only_carry_numbers_and_rgb_colours(autoescape):
    page, _ = _data()
    html = _render(autoescape, **_ctx(page))
    styles = re.findall(r'style="([^"]*)"', html)
    assert styles
    ok = re.compile(r"^(background:(rgb\(\d{1,3},\d{1,3},\d{1,3}\)|transparent);color:(#111|#fff|inherit)"
                    r"|display:block;width:\d{1,3}(\.\d)?%|width:10rem)$")
    bad = [s for s in styles if not ok.match(s)]
    assert not bad, bad[:3]


def test_template_states_denied_disabled_and_collecting():
    page, _ = _data()
    denied = _render(True, **_ctx({}, denied=True))
    assert "mod role" in denied and "Heatmap" not in denied and "When it is busy" not in denied
    disabled = _render(True, **_ctx({}, pulse_disabled=True))
    assert "switched off" in disabled and "When it is busy" not in disabled
    empty, _ = _data(coverage_start=None)
    collecting = _render(True, **_ctx(empty, now=[]))
    assert "still collecting" in collecting and "When it is busy" not in collecting


# ------------------------------------------------------------------ cog: permissions, read-only, loops

class _Guild(SimpleNamespace):
    pass


def make_dash_guild(members=()):
    channels = {111: "general", 222: "cuddle", 333: "pillow-talk"}
    by_id = {m.id: m for m in members}
    g = _Guild(id=GUILD_ID, name="Wonderland", text_channels=[], voice_channels=[], stage_channels=[], forums=[],
               get_member=lambda uid: by_id.get(uid),
               get_channel_or_thread=lambda cid: SimpleNamespace(name=channels[cid]) if cid in channels else None)
    for m in members:
        m.guild = g
    return g


def make_member(uid, *, mod_role=False, admin=False):
    roles = {DEFAULT_MOD_ROLE_ID} if mod_role else set()
    return SimpleNamespace(id=uid, get_role=lambda rid: object() if rid in roles else None,
                           guild_permissions=SimpleNamespace(administrator=admin))


@pytest.fixture
def clock(monkeypatch):
    c = Clock(NOW)
    monkeypatch.setattr(sp.time, "time", c)
    return c


async def seeded(tmp_path, bot=None):
    cog = make_cog(tmp_path, bot)
    docs, t0, _end = traffic(date(2026, 9, 1), 28, seed=2)
    for dk, doc in docs.items():
        storage.save_day(tmp_path, GUILD_ID, dk, doc)
    guild_stub = SimpleNamespace(id=GUILD_ID)
    await cog.config.guild(guild_stub).coverage_start.set(int(t0))
    await cog.config.guild(guild_stub).live_since.set(int(t0))
    return cog


def _content(out):
    assert out["status"] == 0
    return out["web_content"]


async def test_registration_params_and_mixin():
    args, kwargs = sp.ServerPulse.dashboard_pulse.__dashboard_decorator_params__
    assert kwargs["name"] == "overview"  # name=None registers as 'None' on the dashboard
    assert tuple(kwargs["methods"]) == ("GET",)
    assert sp.ServerPulse.__mro__[1].__name__ == "DashboardIntegration"
    cog = make_cog(None)
    added = []
    dash = SimpleNamespace(rpc=SimpleNamespace(third_parties_handler=SimpleNamespace(add_third_party=added.append)))
    await cog.on_dashboard_cog_add(dash)
    assert added == [cog]


async def test_non_mod_is_denied_and_gets_no_data(tmp_path, clock):
    cog = await seeded(tmp_path)
    guild = make_dash_guild([make_member(70)])
    wc = _content(await cog.dashboard_pulse(SimpleNamespace(id=70), guild))
    assert wc["denied"] is True and wc["pulse"] == {} and wc["pulse_now"] == []
    stranger = _content(await cog.dashboard_pulse(SimpleNamespace(id=999), guild))  # not in the guild at all
    assert stranger["denied"] is True


@pytest.mark.parametrize("kw", [dict(mod_role=True), dict(admin=True)])
async def test_mod_role_and_administrators_see_the_page(tmp_path, clock, kw):
    cog = await seeded(tmp_path)
    guild = make_dash_guild([make_member(71, **kw)])
    wc = _content(await cog.dashboard_pulse(SimpleNamespace(id=71), guild))
    assert wc["denied"] is False and wc["pulse"]["has_data"] is True
    assert {c["name"] for c in wc["pulse"]["channels"]} == {"general", "cuddle", "pillow-talk"}
    assert dict(wc["pulse_now"])["Last message"]
    html = _render(True, **{k: v for k, v in wc.items() if k != "source"})
    assert "When it is busy" in html


async def test_red_mods_and_the_bot_owner_pass_like_the_commands_do(tmp_path, clock):
    bot = make_bot()

    async def true(*a, **k):
        return True

    cog = await seeded(tmp_path, bot)
    guild = make_dash_guild([make_member(72)])
    bot.is_mod = true  # Red's own mod/admin roles
    assert _content(await cog.dashboard_pulse(SimpleNamespace(id=72), guild))["denied"] is False
    bot.is_mod = bot.is_owner  # back to nobody
    assert _content(await cog.dashboard_pulse(SimpleNamespace(id=72), guild))["denied"] is True
    bot.is_owner = true  # the owner needs no membership
    assert _content(await cog.dashboard_pulse(SimpleNamespace(id=12345), guild))["denied"] is False


async def test_guild_switched_off_shows_the_disabled_state(tmp_path, clock):
    cog = await seeded(tmp_path)
    cog._allowed = {999}  # serverpulse is not allowed to run in this server
    guild = make_dash_guild([make_member(73, mod_role=True)])
    wc = _content(await cog.dashboard_pulse(SimpleNamespace(id=73), guild))
    assert wc["pulse_disabled"] is True and wc["pulse"] == {}


async def test_days_parameter_changes_the_window_and_bad_values_fall_back(tmp_path, clock):
    cog = await seeded(tmp_path)
    guild = make_dash_guild([make_member(74, mod_role=True)])
    user = SimpleNamespace(id=74)
    assert _content(await cog.dashboard_pulse(user, guild, days="7"))["pulse"]["window_days"] == 7
    assert _content(await cog.dashboard_pulse(user, guild, extra_kwargs={"days": "14"}))["pulse"]["window_days"] == 14
    assert _content(await cog.dashboard_pulse(user, guild, days="1; DROP"))["pulse"]["window_days"] == 28
    assert _content(await cog.dashboard_pulse(user, guild))["pulse"]["window_days"] == 28


async def test_a_page_view_writes_nothing(tmp_path, clock):
    cog = await seeded(tmp_path)
    guild = make_dash_guild([make_member(75, mod_role=True)])
    await cog._ensure_guild(guild)  # first-load initialisation is allowed to write; the page view is not
    # something pending in memory that a flush WOULD write to disk
    await cog.on_message(SimpleNamespace(
        guild=guild, author=SimpleNamespace(bot=False, id=5), webhook_id=None,
        created_at=dt.datetime.fromtimestamp(NOW - 120, sp.TIMEZONE), content="hello",
        type=SimpleNamespace(name="default"), channel=SimpleNamespace(id=111, parent_id=None)))

    def files():
        return sorted((str(p.relative_to(tmp_path)), p.stat().st_mtime_ns, p.stat().st_size) for p in tmp_path.rglob("*") if p.is_file())

    before_files, before_cfg, before_changes = files(), copy.deepcopy(cog.config._guilds), cog._trackers[GUILD_ID].changes
    wc = _content(await cog.dashboard_pulse(SimpleNamespace(id=75), guild))
    assert wc["pulse"]["has_data"]
    assert files() == before_files
    assert cog.config._guilds == before_cfg
    assert cog._trackers[GUILD_ID].changes == before_changes


class _BotLoopThread:
    """A second event loop on its own thread, standing in for Red's loop while the test
    coroutine plays the dashboard's web-thread loop."""

    def __enter__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        return self.loop

    def __exit__(self, *exc):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        self.loop.close()


async def test_everything_the_page_reads_runs_on_the_bot_loop(tmp_path, clock):
    bot = make_bot()
    seen = {}

    async def owner_probe(user):
        seen["loop"] = asyncio.get_running_loop()
        return False

    bot.is_owner = owner_probe
    cog = await seeded(tmp_path, bot)
    guild = make_dash_guild([make_member(76, mod_role=True)])
    with _BotLoopThread() as bot_loop:
        bot.loop = bot_loop
        wc = _content(await cog.dashboard_pulse(SimpleNamespace(id=76), guild))
    assert wc["pulse"]["has_data"] is True
    assert seen["loop"] is bot_loop and seen["loop"] is not asyncio.get_running_loop()


async def test_runs_inline_when_already_on_the_bot_loop(tmp_path, clock):
    cog = await seeded(tmp_path)
    cog.bot.loop = asyncio.get_running_loop()
    guild = make_dash_guild([make_member(77, mod_role=True)])
    assert _content(await cog.dashboard_pulse(SimpleNamespace(id=77), guild))["pulse"]["has_data"] is True


def test_page_signature_is_only_user_guild_and_kwargs():
    # The dashboard reads the handler's signature; Lurker's page (known to work live) has exactly this shape.
    import inspect
    params = inspect.signature(sp.ServerPulse.dashboard_pulse).parameters
    assert list(params) == ["self", "user", "guild", "kwargs"]



async def test_page_variables_do_not_collide_with_the_dashboards_own_names(tmp_path, clock):
    # Live bug (1.6.1): a variable called `page` collided with the dashboard's own `page`
    # argument to render_template_string -> "got multiple values for keyword argument 'page'".
    cog = await seeded(tmp_path)
    guild = make_dash_guild([make_member(71, mod_role=True)])
    wc = _content(await cog.dashboard_pulse(SimpleNamespace(id=71), guild))
    reserved = {"page", "user", "guild", "request", "form", "Form", "extra_kwargs", "optional_kwargs", "csrf_token", "app", "url_for"}
    assert not (set(wc) & reserved)
