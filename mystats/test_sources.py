"""Readers against fake cogs: right cog, right config group, and one broken cog never breaks the rest."""
import types

import pytest

from mystats import sources


class Group:
    def __init__(self, data):
        self.data = data

    async def all(self):
        return dict(self.data)

    def __getattr__(self, name):
        async def get():
            return self.data[name]
        return get


class Config:
    """member()/user()/guild() return the same bucket, remembering which kind was asked for."""

    def __init__(self, member=None, user=None, guild=None):
        self._b = {"member": member or {}, "user": user or {}, "guild": guild or {}}
        self.asked = []

    def member(self, m):
        self.asked.append("member")
        return Group(self._b["member"])

    def user(self, m):
        self.asked.append("user")
        return Group(self._b["user"])

    def guild(self, g):
        self.asked.append("guild")
        return Group(self._b["guild"])


class Bot:
    def __init__(self, **cogs):
        self.cogs = cogs

    def get_cog(self, name):
        return self.cogs.get(name)


GUILD = types.SimpleNamespace(id=1)
MEMBER = types.SimpleNamespace(id=42, guild=GUILD)


async def test_every_reader_returns_none_when_its_cog_is_not_loaded():
    out = await sources.gather(Bot(), MEMBER)
    assert set(out) == set(sources.READERS)
    assert all(v is None for v in out.values())


async def test_minigames_duel_verdict_bumps_read_member_config():
    mh = types.SimpleNamespace(config=Config(member={"games": {"hunt": {"good": 1}}, "boss_damage": 9}))
    duel = types.SimpleNamespace(config=Config(member={"wins": 2, "losses": 1}))
    ver = types.SimpleNamespace(config=Config(member={"played": 3, "correct": 2}))
    bump = types.SimpleNamespace(config=Config(member={"total_bumps": 7}))
    out = await sources.gather(Bot(MinigameHub=mh, Duel=duel, Verdict=ver, BumpReward=bump), MEMBER)
    assert out["minigames"] == {"games": {"hunt": {"good": 1}}, "boss_damage": 9}
    assert out["duel"]["wins"] == 2
    assert out["verdict"]["correct"] == 2
    assert out["bumps"] == {"total_bumps": 7}


async def test_heist_and_lottery_read_user_config_not_member():
    heist = types.SimpleNamespace(config=Config(user={"stats": {"success": 1}, "level": 2}))
    lot = types.SimpleNamespace(config=Config(user={"tickets": {"x": 3}}))
    out = await sources.gather(Bot(Heist=heist, Lottery=lot), MEMBER)
    assert heist.config.asked == ["user"] and out["heist"]["level"] == 2
    assert lot.config.asked == ["user"] and out["lottery"] == {"tickets": {"x": 3}}


@pytest.mark.parametrize("is_global,expected", [(True, "user"), (False, "member")])
async def test_casino_follows_the_casino_global_switch(is_global, expected):
    async def casino_is_global():
        return is_global

    cfg = Config(user={"Played": {"Dice": 1}}, member={"Played": {"Dice": 1}})
    casino = types.SimpleNamespace(config=cfg, casino_is_global=casino_is_global)
    out = await sources.gather(Bot(Casino=casino), MEMBER)
    assert cfg.asked == [expected]
    assert out["casino"]["Played"] == {"Dice": 1}


async def test_cards_use_member_state_and_count_collection():
    async def _member_state(member):
        return types.SimpleNamespace(collection=[1, 2, 3], daily_streak=4)

    out = await sources.gather(Bot(CardCollect=types.SimpleNamespace(_member_state=_member_state)), MEMBER)
    assert out["cards"] == {"owned": 3, "daily_streak": 4}


async def test_puzzle_reads_this_members_lifetime_entry_and_missing_member_is_empty():
    cog = types.SimpleNamespace(config=Config(guild={"lifetime_stats": {"42": {"puzzles_won": 2}, "7": {"puzzles_won": 9}}}))
    out = await sources.gather(Bot(Puzzle=cog), MEMBER)
    assert out["puzzle"] == {"puzzles_won": 2}
    other = types.SimpleNamespace(id=99, guild=GUILD)
    assert (await sources.gather(Bot(Puzzle=cog), other))["puzzle"] == {}


async def test_pet_sums_this_members_care_across_kept_weeks():
    weeks = {"2026-W40": {"42": 3, "7": 5}, "2026-W41": {"42": 4}, "2026-W42": {}}
    cog = types.SimpleNamespace(config=Config(guild={"week_carers": weeks}, member={"best_streak": 5}))
    out = await sources.gather(Bot(WonderPet=cog), MEMBER)
    assert out["pet"] == {"care_recent": 7, "best_streak": 5}


async def test_a_broken_reader_is_isolated_and_logged(caplog):
    class Broken:
        @property
        def config(self):
            raise RuntimeError("renamed attribute")

    good = types.SimpleNamespace(config=Config(member={"total_bumps": 1}))
    out = await sources.gather(Bot(Duel=Broken(), BumpReward=good), MEMBER)
    assert out["duel"] is None
    assert out["bumps"] == {"total_bumps": 1}
    assert "reader 'duel' failed" in caplog.text
