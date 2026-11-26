"""Coin drops (plant.py): amounts, burn, Grab and Rain payouts, locking, refunds, restart recovery."""
import asyncio
import copy
from types import SimpleNamespace

import pytest

import discord

if not hasattr(discord, "__version__"):
    pytest.skip("real discord.py not installed", allow_module_level=True)

from minigamehub import plant
from minigamehub.config_schema import DEFAULT_GUILD


class Val:
    def __init__(self, data):
        self.data = data

    def __call__(self):
        return self

    def __await__(self):
        async def get():
            return copy.deepcopy(self.data)
        return get().__await__()

    async def __aenter__(self):
        return self.data

    async def __aexit__(self, *exc):
        return False


class FakeBank:
    def __init__(self, balances):
        self.balances, self.fail_for = dict(balances), set()

    async def withdraw_credits(self, member, amount):
        if self.balances.get(member.id, 0) < amount:
            raise ValueError("insufficient")
        self.balances[member.id] -= amount

    async def deposit_credits(self, member, amount):
        if member.id in self.fail_for:
            raise RuntimeError("balance too high")
        self.balances[member.id] = self.balances.get(member.id, 0) + amount

    async def get_currency_name(self, guild):
        return "wc"


class Msg:
    def __init__(self, mid):
        self.id, self.content = mid, ""

    async def edit(self, content=None, view=None):
        self.content = content


class Channel:
    id, name = 50, "🧸・cuddle"

    def __init__(self):
        self.sent = []
        self.mention = "#cuddle"

    async def send(self, content, view=None):
        msg = Msg(1000 + len(self.sent))
        msg.content = content
        self.sent.append(msg)
        return msg

    def get_partial_message(self, mid):
        return next(m for m in self.sent if m.id == mid)


def member(uid, roles=()):
    return SimpleNamespace(id=uid, mention=f"<@{uid}>", display_name=f"u{uid}", bot=False,
                           roles=[SimpleNamespace(id=r) for r in roles])


class Inter:
    def __init__(self, user, guild):
        self.user, self.guild, self.said = user, guild, []
        self.response = SimpleNamespace(send_message=self._say, defer=self._noop)

    async def _say(self, text, ephemeral=False):
        self.said.append(text)

    async def _noop(self, *a, **k):
        return None


@pytest.fixture
def world(monkeypatch):
    members = {i: member(i) for i in range(1, 6)}
    channel = Channel()
    guild = SimpleNamespace(id=1, text_channels=[channel], get_member=members.get, get_channel=lambda cid: channel,
                            members=list(members.values()))
    for m in members.values():
        m.guild = guild
    conf = Val(copy.deepcopy(DEFAULT_GUILD["plant"]))
    cog = SimpleNamespace(
        config=SimpleNamespace(guild=lambda g: SimpleNamespace(plant=conf)),
        bot=SimpleNamespace(get_cog=lambda name: None, dispatch=lambda *a: cog.events.append(a),
                            guilds=[guild], wait_until_red_ready=lambda: asyncio.sleep(0)),
        _plant_lock=asyncio.Lock(), _plant_last={}, _plant_tasks=set(), events=[])
    fake = FakeBank({1: 50_000, 2: 0, 3: 0, 4: 0})
    monkeypatch.setattr(plant, "bank", fake)
    return SimpleNamespace(cog=cog, guild=guild, conf=conf, bank=fake, channel=channel, members=members)


async def drop(w, planter=1, amount="5000", mode="grab"):
    reply = await plant.start(w.cog, Inter(w.members[planter], w.guild), amount, mode)
    return reply, (w.channel.sent[-1] if w.channel.sent else None)


def live(w):
    """The Drop object behind the last post: it is the view's owner, kept alive by the running task."""
    return next(t for t in w.cog._plant_tasks).get_coro().cr_frame.f_locals["self"]


def test_parsing_and_splitting():
    assert [plant.parse_amount(t) for t in ("5000", "5k", "1,000", "1.5k", "2m", "0", "-4", "abc", "inf")] == \
        [5000, 5000, 1000, 1500, 2_000_000, None, None, None, None]
    assert [plant.parse_mode(t) for t in ("", "Grab", "rain", "R", "nope")] == ["grab", "grab", "rain", "rain", None]
    for total, n in ((900, 3), (901, 3), (5, 2), (7, 7)):
        shares = plant.split_amount(total, n)
        assert sum(shares) == total and max(shares) - min(shares) <= 1
    assert plant.week_key().count("-W") == 1


def test_find_channel_matches_the_tail_only():
    ch = lambda name: SimpleNamespace(name=name)
    assert plant.find_channel(SimpleNamespace(text_channels=[ch("wondercuddle"), ch("🧸・cuddle")])).name == "🧸・cuddle"
    assert plant.find_channel(SimpleNamespace(text_channels=[ch("wonder-cuddle"), ch("cuddle-corner")])) is None


async def test_plant_burns_ten_percent_and_posts(world):
    reply, msg = await drop(world)
    assert world.bank.balances[1] == 45_000 and "4,500" in msg.content and "500 burned" in reply
    p = await world.conf
    assert (p["drops"], p["planted"], p["burned"], p["week_planters"]) == (1, 5000, 500, {"1": 5000})
    assert len(p["open"]) == 1 and world.cog.events[0][2:] == ("plant", 1)


async def test_checks_before_any_coins_move(world):
    assert "between 1,000 and 1,000,000" in (await drop(world, amount="999"))[0]
    assert "between 1,000 and 1,000,000" in (await drop(world, amount="2m"))[0]
    assert "Grab or Rain" in (await drop(world, mode="x"))[0]
    assert "enough coins" in (await drop(world, planter=2))[0]
    assert world.bank.balances[1] == 50_000 and not world.channel.sent
    await drop(world)
    assert "again in" in (await drop(world))[0] and len(world.channel.sent) == 1


async def test_grab_pays_once_even_with_simultaneous_clicks(world):
    await drop(world)
    d = live(world)
    clicks = [Inter(world.members[i], world.guild) for i in (2, 3, 4)]
    await asyncio.gather(*(d.on_click(c) for c in clicks))
    assert sum(world.bank.balances[i] for i in (2, 3, 4)) == 4500
    assert sorted(c.said == ["This drop is already gone."] for c in clicks) == [False, True, True]
    assert (await world.conf)["open"] == {}


async def test_planter_and_lurkers_cannot_claim(world):
    await drop(world)
    d = live(world)
    world.cog.bot.get_cog = lambda name: SimpleNamespace(config=SimpleNamespace(
        guild=lambda g: SimpleNamespace(lurker_role_id=Val(77))))
    world.members[3].roles = [SimpleNamespace(id=77)]
    mine, lurker = Inter(world.members[1], world.guild), Inter(world.members[3], world.guild)
    await d.on_click(mine)
    await d.on_click(lurker)
    assert "own drop" in mine.said[0] and "Lurkers" in lurker.said[0] and not d.done


async def test_rain_splits_evenly_and_a_lone_clicker_takes_all(world, monkeypatch):
    monkeypatch.setattr(plant, "RAIN_SECONDS", 0.05)
    await drop(world, mode="rain")
    d = live(world)
    for i in (2, 3, 4):
        await d.on_click(Inter(world.members[i], world.guild))
    await asyncio.sleep(0.15)
    assert sorted(world.bank.balances[i] for i in (2, 3, 4)) == [1500, 1500, 1500]
    world.cog._plant_last.clear()
    await drop(world, mode="rain")
    d = live(world)
    await d.on_click(Inter(world.members[2], world.guild))
    await asyncio.sleep(0.15)
    assert world.bank.balances[2] == 1500 + 4500


async def test_unclaimed_drop_refunds_the_planter_and_nets_out_the_stats(world, monkeypatch):
    monkeypatch.setattr(plant, "EXPIRE_SECONDS", 0.05)
    await drop(world)
    await asyncio.sleep(0.15)
    assert world.bank.balances[1] == 50_000 - 500 and "went back" in world.channel.sent[-1].content
    p = await world.conf
    assert (p["planted"], p["burned"], p["open"]) == (500, 500, {})


async def test_failed_deposit_goes_back_to_the_planter(world):
    await drop(world)
    world.bank.fail_for.add(2)
    await live(world).on_click(Inter(world.members[2], world.guild))
    assert world.bank.balances[1] == 50_000 - 500 and world.bank.balances.get(2, 0) == 0


async def test_restart_refunds_open_drops_and_a_late_click_pays_nothing(world):
    await drop(world)
    d = live(world)
    await plant.recover(world.cog)
    assert world.bank.balances[1] == 50_000 - 500 and "restart" in world.channel.sent[-1].content
    d.done = True
    await d.pay([world.members[2]])
    assert world.bank.balances.get(2, 0) == 0
