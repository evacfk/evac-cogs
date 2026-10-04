"""Behaviour tests for the Celebrations cog against fakes (redbot faked; discord stubbed or real).

Proves the day cycle (role + gift at midnight, posts at the announce hour, cleanup at day end),
the anti-double-gift guard, lurker skipping and anniversaries. Does NOT prove live Discord
behaviour (real role edits, the starboard picking the post up).
"""
import importlib
import sys
from datetime import datetime

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
        self.config.register_guild(lurker_role_id=role_id)


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
