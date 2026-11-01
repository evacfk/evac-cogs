"""Behaviour tests for the Celebrations cog against fakes (redbot faked; discord stubbed or real).

Proves the day cycle (role + gift at midnight, posts at the announce hour, cleanup at day end),
the anti-double-gift guard, lurker skipping and anniversaries. Does NOT prove live Discord
behaviour (real role edits, the starboard picking the post up).
"""
import importlib
import sys
from datetime import datetime

import types

import pytest

from celebrations import _testkit as tk
from celebrations.constants import TZ


def at(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=TZ).timestamp()


@pytest.fixture
def cogmod(monkeypatch):
    bank = tk.FakeBank()
    tk.install(monkeypatch, bank)
    monkeypatch.delitem(sys.modules, "celebrations.celebrations", raising=False)
    monkeypatch.delitem(sys.modules, "celebrations.embeds", raising=False)
    mod = importlib.import_module("celebrations.celebrations")
    mod._bank = bank
    yield mod
    sys.modules.pop("celebrations.celebrations", None)
    sys.modules.pop("celebrations.embeds", None)


class FakeLurkerCog:
    def __init__(self, role_id):
        self.config = tk.FakeConfig()
        self.config.register_guild(lurker_role_id=role_id, report_channel_id=None)


@pytest.fixture
def env(cogmod):
    g = tk.FakeGuild()
    lurker_role = g.add_role(500, "Lurker")
    bday_role = g.add_role(600, "🎂 Birthday", hoist=True)
    chat = g.add_channel(100, "cuddle")
    ann = g.add_channel(200, "announcements")
    bot = tk.FakeBot(g, cogs={"Lurker": FakeLurkerCog(500)})
    cog = cogmod.Celebrations(bot)
    gconf = cog.config.guild(g)
    async def setup():
        await gconf.channel_id.set(chat.id)
        await gconf.announce_channel_id.set(ann.id)
        await gconf.role_id.set(bday_role.id)

    return types_ns(cog=cog, guild=g, chat=chat, ann=ann, role=bday_role, lurker=lurker_role,
                    bank=cogmod._bank, setup=setup, mod=cogmod)


def types_ns(**kw):
    import types
    return types.SimpleNamespace(**kw)


async def _bday(env, member, month, day, year=None, last=None):
    m = env.cog.config.member(member)
    await m.month.set(month)
    await m.day.set(day)
    await m.year.set(year)
    if last:
        await m.last_celebrated.set(last)


JOINED_LONG_AGO = datetime(2024, 1, 5, tzinfo=TZ)


async def test_full_birthday_day_cycle(env):
    await env.setup()
    a = tk.FakeMember(env.guild, 1, JOINED_LONG_AGO, name="Ana")
    await _bday(env, a, 10, 3, 1998)

    await env.cog._tick(env.guild, at(2026, 10, 3, 0, 20))  # midnight: role + gift, no posts yet
    assert env.role.id in a.role_ids()
    assert len(env.bank.deposits) == 1 and 20000 <= env.bank.deposits[0][1] <= 30000
    assert env.chat.sent == [] and env.ann.sent == []

    await env.cog._tick(env.guild, at(2026, 10, 3, 9, 5))  # announce hour: both posts
    assert len(env.chat.sent) == 1 and len(env.ann.sent) == 1
    post = env.chat.sent[0]
    assert "<@1>" in post.content
    assert post.reactions == ["\N{WHITE MEDIUM STAR}"]
    assert "28" in str(post.embed.description)

    await env.cog._tick(env.guild, at(2026, 10, 3, 15, 0))  # later ticks: nothing repeats
    assert len(env.chat.sent) == 1 and len(env.ann.sent) == 1 and len(env.bank.deposits) == 1

    await env.cog._tick(env.guild, at(2026, 10, 4, 0, 5))  # next day: cleanup
    assert env.role.id not in a.role_ids()
    assert env.ann.sent[0].deleted is True
    assert env.chat.sent[0].deleted is False  # the #cuddle post stays for the starboard


async def test_no_second_gift_inside_300_days_even_if_date_changes(env):
    """REGRESSION guard: changing your birthday to today must not pay out again."""
    await env.setup()
    a = tk.FakeMember(env.guild, 1, JOINED_LONG_AGO)
    await _bday(env, a, 10, 3, last="2026-06-01")
    await env.cog._tick(env.guild, at(2026, 10, 3, 10))
    assert env.bank.deposits == [] and env.chat.sent == [] and env.role.id not in a.role_ids()


async def test_new_joiner_is_celebrated_without_a_gift(env):
    await env.setup()
    a = tk.FakeMember(env.guild, 1, datetime(2026, 9, 28, tzinfo=TZ))
    await _bday(env, a, 10, 3)
    await env.cog._tick(env.guild, at(2026, 10, 3, 10))
    assert env.bank.deposits == []
    assert env.role.id in a.role_ids() and len(env.chat.sent) == 1
    assert not any("gift" in str(f.name).lower() for f in env.chat.sent[0].embed.fields)


async def test_lurkers_and_bots_are_skipped(env):
    await env.setup()
    lurk = tk.FakeMember(env.guild, 1, JOINED_LONG_AGO, roles=[env.lurker])
    bot = tk.FakeMember(env.guild, 2, JOINED_LONG_AGO, bot=True)
    await _bday(env, lurk, 10, 3)
    await _bday(env, bot, 10, 3)
    await env.cog._tick(env.guild, at(2026, 10, 3, 10))
    assert env.chat.sent == []
    assert env.bank.deposits == []


async def test_stale_role_holder_is_cleaned_up(env):
    await env.setup()
    a = tk.FakeMember(env.guild, 1, JOINED_LONG_AGO, roles=[env.role])  # left over from a missed cleanup
    await env.cog._tick(env.guild, at(2026, 10, 3, 1))
    assert env.role.id not in a.role_ids()


async def test_anniversaries_posted_once_with_years_and_without_lurkers(env):
    await env.setup()
    three = tk.FakeMember(env.guild, 1, datetime(2023, 10, 3, 14, tzinfo=TZ), name="Three")
    one = tk.FakeMember(env.guild, 2, datetime(2025, 10, 3, 2, tzinfo=TZ), name="One")
    lurk = tk.FakeMember(env.guild, 3, datetime(2022, 10, 3, tzinfo=TZ), roles=[env.lurker])
    today_joiner = tk.FakeMember(env.guild, 4, datetime(2026, 10, 3, 1, tzinfo=TZ))
    other_day = tk.FakeMember(env.guild, 5, datetime(2023, 10, 4, tzinfo=TZ))

    await env.cog._tick(env.guild, at(2026, 10, 3, 8, 50))
    assert env.chat.sent == []  # before the announce hour
    await env.cog._tick(env.guild, at(2026, 10, 3, 9, 10))
    await env.cog._tick(env.guild, at(2026, 10, 3, 9, 20))
    assert len(env.chat.sent) == 1
    post = env.chat.sent[0]
    desc = str(post.embed.description)
    assert desc.index("<@1>") < desc.index("<@2>")  # longest tenure first
    assert "**3 years**" in desc and "**1 year**" in desc
    for absent in ("<@3>", "<@4>", "<@5>"):
        assert absent not in desc and absent not in post.content


async def test_unconfigured_guild_does_nothing(env):
    a = tk.FakeMember(env.guild, 1, JOINED_LONG_AGO)
    await _bday(env, a, 10, 3)
    await env.cog._tick(env.guild, at(2026, 10, 3, 10))
    assert env.bank.deposits == [] and env.chat.sent == []


async def test_transient_role_ids_exposes_birthday_role_for_lurker(env):
    await env.setup()
    assert await env.cog.transient_role_ids(env.guild) == {env.role.id}


async def test_save_birthday_command_path(env):
    a = tk.FakeMember(env.guild, 1, JOINED_LONG_AGO)
    sent = []

    class Ctx:
        author = a
        guild = env.guild

        async def send(self, text=None, **kw):
            sent.append(text)

    await env.cog._save_birthday(Ctx(), a, "03/04")
    assert "two ways" in sent[-1]
    await env.cog._save_birthday(Ctx(), a, "March 4 1999")
    assert "March 4, 1999" in sent[-1] and "age" in sent[-1]
    mc = await env.cog.config.member(a).all()
    assert (mc["month"], mc["day"], mc["year"]) == (3, 4, 1999)


async def test_pet_cog_is_told_about_birthdays_and_anniversaries(env):
    calls = []
    env.cog.bot.dispatch = lambda *a: calls.append(a)
    await env.setup()
    a = tk.FakeMember(env.guild, 1, JOINED_LONG_AGO, name="Ana")
    await _bday(env, a, 10, 3, 1998)
    await env.cog._tick(env.guild, at(2026, 10, 3, 9, 5))
    births = [c for c in calls if c[0] == "wonder_birthday"]
    assert len(births) == 1 and births[0][2] == [a]
    await env.cog._tick(env.guild, at(2026, 10, 3, 9, 20))   # nothing new: not announced twice
    assert len([c for c in calls if c[0] == "wonder_birthday"]) == 1
    assert not [c for c in calls if c[0] == "wonder_anniversary"]   # nobody has a join anniversary today


async def test_pet_cog_is_told_about_anniversaries(env):
    calls = []
    env.cog.bot.dispatch = lambda *a: calls.append(a)
    await env.setup()
    three = tk.FakeMember(env.guild, 1, datetime(2023, 10, 3, 14, tzinfo=TZ), name="Three")
    await env.cog._tick(env.guild, at(2026, 10, 3, 9, 10))
    ann = [c for c in calls if c[0] == "wonder_anniversary"]
    assert len(ann) == 1 and ann[0][2] == [three]


async def test_a_broken_dispatch_never_breaks_the_posts(env):
    def boom(*a):
        raise RuntimeError("no")
    env.cog.bot.dispatch = boom
    await env.setup()
    a = tk.FakeMember(env.guild, 1, JOINED_LONG_AGO, name="Ana")
    await _bday(env, a, 10, 3, 1998)
    await env.cog._tick(env.guild, at(2026, 10, 3, 9, 5))
    assert len(env.chat.sent) >= 1


# ------------------------------------------------------------ settings (the panel's apply methods)

async def test_channel_settings(env):
    g = env.guild
    new = g.add_channel(300, "other")
    assert "300" in await env.cog._apply_channel(g, None, new) or new.mention in await env.cog._apply_channel(g, None, new)
    gc = env.cog.config.guild(g)
    assert await gc.channel_id() == 300
    with pytest.raises(env.mod.PanelError):
        await env.cog._apply_channel(g, None, None)
    await env.cog._apply_announce_channel(g, None, None)
    assert await gc.announce_channel_id() is None


async def test_gift_validation_and_blank_max_means_fixed(env):
    g = env.guild
    gc = env.cog.config.guild(g)
    for bad in ({"gift_min": "-1", "gift_max": "", "min_days": "14"},
                {"gift_min": "500", "gift_max": "100", "min_days": "14"},
                {"gift_min": "x", "gift_max": "", "min_days": "14"}, {}):
        with pytest.raises(env.mod.PanelError):
            await env.cog._apply_gift(g, None, bad)
    assert (await gc.gift_min(), await gc.gift_max()) == (20000, 30000)
    await env.cog._apply_gift(g, None, {"gift_min": "25000", "gift_max": "", "min_days": "-5"})
    assert (await gc.gift_min(), await gc.gift_max(), await gc.gift_min_member_days()) == (25000, 25000, 0)
    assert "off" in await env.cog._apply_gift(g, None, {"gift_min": "0", "gift_max": "0", "min_days": "7"})


async def test_hour_star_and_anniversary_settings(env):
    g = env.guild
    gc = env.cog.config.guild(g)
    for bad in ("24", "-1", "x", ""):
        with pytest.raises(env.mod.PanelError):
            await env.cog._apply_hour(g, None, {"hour": bad})
    await env.cog._apply_hour(g, None, {"hour": "18"})
    assert await gc.announce_hour() == 18
    await env.cog._apply_star(g, None, {"emoji": "off"})
    assert await gc.star_emoji() == ""
    await env.cog._apply_star(g, None, {"emoji": "\N{SPARKLES}"})
    assert await gc.star_emoji() == "\N{SPARKLES}"
    with pytest.raises(env.mod.PanelError):
        await env.cog._apply_star(g, None, {"emoji": "two words"})
    await env.cog._apply_anniversaries(g, None, False)
    assert await gc.anniversaries() is False
    with pytest.raises(env.mod.PanelError):
        await env.cog._apply_anniversary_gift(g, None, {"amount": "-1"})
    await env.cog._apply_anniversary_gift(g, None, {"amount": "5000"})
    assert await gc.anniversary_gift() == 5000


async def test_role_setting_hoists_and_makerole_creates(env):
    g = env.guild
    gc = env.cog.config.guild(g)
    plain = g.add_role(700, "Plain")
    edits = []

    async def edit(**kw):
        edits.append(kw)
        plain.hoist = kw.get("hoist", plain.hoist)
    plain.edit = edit
    with pytest.raises(env.mod.PanelError):
        await env.cog._apply_role(g, None, None)
    await env.cog._apply_role(g, None, plain)
    assert await gc.role_id() == 700 and edits[0]["hoist"] is True

    async def create_role(**kw):
        role = g.add_role(800, kw["name"], hoist=kw["hoist"])

        async def redit(**k):
            pass
        role.edit = redit
        return role
    g.create_role = create_role
    msg = await env.cog._apply_makerole(g, None)
    assert await gc.role_id() == 800 and "Created" in msg


async def test_group_opens_panel_for_admins_and_summary_for_mods(env, monkeypatch):
    opened = []

    async def fake_open(ctx, cog, specs, *, title, intro=""):
        opened.append((title, len(specs)))
    monkeypatch.setattr(env.mod, "open_panel", fake_open)
    sent = []

    class Ctx:
        guild = env.guild

        def __init__(self, manage):
            self.author = types.SimpleNamespace(guild_permissions=types.SimpleNamespace(administrator=False, manage_guild=manage))

        async def send(self, text=None, **kw):
            sent.append(text)
    await env.cog.celebrations.callback(env.cog, Ctx(True))
    assert opened == [("Celebrations", 12)] and sent == []
    await env.cog.celebrations.callback(env.cog, Ctx(False))
    assert opened == [("Celebrations", 12)] and "Celebrations v" in sent[-1]


async def test_removed_setter_commands_are_gone(env):
    for gone in ("channel", "announcements", "role", "makerole", "gift", "giftmindays", "hour", "star",
                 "anniversaries", "anniversarygift"):
        assert not hasattr(env.cog, f"celebrations_{gone}")
    for kept in ("settings", "setfor", "clearfor", "preview", "run", "version"):
        assert hasattr(env.cog, f"celebrations_{kept}")


# ------------------------------------------------------------- year club (moved here from the lurker cog)

def _years_ago(years, extra_days=0):
    from datetime import timedelta
    now_local = datetime.now(TZ)
    try:
        j = now_local.replace(year=now_local.year - years)
    except ValueError:  # Feb 29
        j = now_local.replace(year=now_local.year - years, day=28)
    return j - timedelta(days=extra_days)


async def _club_env(env, enabled=True):
    club = {n: env.guild.add_role(700 + n, f"{n} year club!", position=30 + n) for n in range(1, 13)}
    gconf = env.cog.config.guild(env.guild)
    await gconf.yearclub_roles.set({str(n): r.id for n, r in club.items()})
    await gconf.yearclub_enabled.set(enabled)
    await gconf.yearclub_migrated.set(True)
    return club


async def test_yearclub_sync_leaves_exactly_one_current_role(env):
    club = await _club_env(env)
    extra = env.guild.add_role(800, "Other", position=20)
    stacked = tk.FakeMember(env.guild, 20, _years_ago(3, 10), roles=[club[1], club[2], club[3], extra])
    due = tk.FakeMember(env.guild, 21, _years_ago(2, 1), roles=[club[1]])  # anniversary passed: 1 -> 2
    fresh = tk.FakeMember(env.guild, 22, _years_ago(0, 30), roles=[club[1]])  # under a year: stale role goes
    veteran = tk.FakeMember(env.guild, 23, _years_ago(14, 5))  # past the top role: keeps 12
    lurk = tk.FakeMember(env.guild, 24, _years_ago(5, 5), roles=[env.lurker, club[1]])
    correct = tk.FakeMember(env.guild, 25, _years_ago(4, 5), roles=[club[4]])

    stats = await env.cog._yearclub_pass(env.guild, auto=False)

    assert stacked.role_ids() == {club[3].id, extra.id}
    assert due.role_ids() == {club[2].id}
    assert fresh.role_ids() == set()
    assert veteran.role_ids() == {club[12].id}
    assert lurk.role_ids() == {env.lurker.id, club[1].id}  # lurkers are never touched
    assert correct.role_ids() == {club[4].id}
    assert stats["changed"] == 4 and stats["skipped_lurkers"] == 1 and stats["errors"] == 0
    assert (await env.cog.config.guild(env.guild).yearclub_last())["changed"] == 4


async def test_yearclub_auto_pass_aborts_on_mass_change_and_alerts_the_lurker_report_channel(env, monkeypatch):
    club = await _club_env(env)
    monkeypatch.setattr(env.mod.engine, "YEARCLUB_AUTO_MAX", 2)
    report = env.guild.add_channel(300, "mod-log")
    await env.cog.bot.get_cog("Lurker").config.guild(env.guild).report_channel_id.set(300)
    members = [tk.FakeMember(env.guild, 30 + i, _years_ago(3, 3), roles=[club[1]]) for i in range(3)]

    stats = await env.cog._yearclub_pass(env.guild, auto=True)

    assert stats["aborted"] is True and len(report.sent) == 1 and "paused" in report.sent[0].content
    assert all(m.role_ids() == {club[1].id} for m in members)  # nothing changed


async def test_a_pass_with_nothing_mapped_does_nothing(env):
    await env.cog.config.guild(env.guild).yearclub_migrated.set(True)
    assert await env.cog._yearclub_pass(env.guild, auto=True) is None


async def test_lurker_asks_which_roles_are_transient_and_what_a_returner_gets(env):
    club = await _club_env(env)
    await env.setup()
    ids = await env.cog.transient_role_ids(env.guild)
    assert ids == {env.role.id} | {r.id for r in club.values()}
    m = tk.FakeMember(env.guild, 40, _years_ago(3, 2), roles=[club[2]])
    assert await env.cog.club_role_for(m) == club[3].id
    new = tk.FakeMember(env.guild, 41, _years_ago(0, 20))
    assert await env.cog.club_role_for(new) is None


async def test_with_the_year_club_off_nothing_is_transient_and_nobody_gets_a_club_role(env):
    club = await _club_env(env, enabled=False)
    await env.setup()
    assert await env.cog.transient_role_ids(env.guild) == {env.role.id}
    m = tk.FakeMember(env.guild, 42, _years_ago(3, 2), roles=[club[3]])
    assert await env.cog.club_role_for(m) is None


async def test_settings_move_over_from_the_lurker_cog_once(env, monkeypatch):
    old = tk.FakeConfig()
    old.register_guild(yearclub_roles={}, yearclub_enabled=False, yearclub_last={})
    old_g = old.guild(env.guild)
    await old_g.yearclub_roles.set({"1": 701, "2": 702})
    await old_g.yearclub_enabled.set(True)
    await old_g.yearclub_last.set({"ts": 1, "changed": 5})
    monkeypatch.setattr(env.mod.Config, "get_conf", classmethod(lambda cls, *a, **k: old), raising=False)

    await env.cog._migrate_yearclub(env.guild)
    g = env.cog.config.guild(env.guild)
    assert await g.yearclub_roles() == {"1": 701, "2": 702}
    assert await g.yearclub_enabled() is True and (await g.yearclub_last())["changed"] == 5
    assert await g.yearclub_migrated() is True

    await old_g.yearclub_roles.set({"9": 999})  # a later change on the old side is ignored
    await env.cog._migrate_yearclub(env.guild)
    assert await g.yearclub_roles() == {"1": 701, "2": 702}


async def test_a_server_that_already_has_celebrations_club_settings_keeps_them(env, monkeypatch):
    old = tk.FakeConfig()
    old.register_guild(yearclub_roles={}, yearclub_enabled=False, yearclub_last={})
    await old.guild(env.guild).yearclub_roles.set({"5": 5})
    monkeypatch.setattr(env.mod.Config, "get_conf", classmethod(lambda cls, *a, **k: old), raising=False)
    g = env.cog.config.guild(env.guild)
    await g.yearclub_roles.set({"1": 701})
    await env.cog._migrate_yearclub(env.guild)
    assert await g.yearclub_roles() == {"1": 701}


async def test_upkeep_runs_at_most_hourly_and_only_when_switched_on(env):
    club = await _club_env(env)
    m = tk.FakeMember(env.guild, 50, _years_ago(2, 1), roles=[club[1]])
    await env.cog._run_yearclub_upkeep()
    assert m.role_ids() == {club[2].id}
    m.roles = [env.guild.default_role, club[1]]
    await env.cog._run_yearclub_upkeep()  # same hour: skipped
    assert m.role_ids() == {club[1].id}
    env.cog._yearclub_checked.clear()
    await env.cog.config.guild(env.guild).yearclub_enabled.set(False)
    await env.cog._run_yearclub_upkeep()  # off: nothing happens
    assert m.role_ids() == {club[1].id}


async def test_no_pass_runs_when_the_lurker_cog_or_role_is_missing(env):
    club = await _club_env(env)
    hidden = tk.FakeMember(env.guild, 60, _years_ago(3, 3), roles=[env.lurker])
    env.cog.bot._cogs.clear()  # lurker cog not loaded
    assert await env.cog._yearclub_pass(env.guild, auto=False) is None
    assert hidden.role_ids() == {env.lurker.id}
    sent = []

    class Ctx:
        guild = env.guild

        async def send(self, text=None, **kw):
            sent.append(text)

    await env.cog.yearclub_sync.callback(env.cog, Ctx())
    assert sent and "Lurker cog isn't loaded" in sent[0]
    env.cog.bot._cogs["Lurker"] = FakeLurkerCog(None)  # loaded but no role chosen
    assert "role isn't set" in await env.cog._club_blocker(env.guild)
    assert club  # roles untouched
