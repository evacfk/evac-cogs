"""2.11.0: roles for completing card sets."""

import random
from types import SimpleNamespace

import pytest

from cardcollect.settings_panel import PanelError
from test_cardcollect_smoke import (
    FakeAttachment,
    FakeChannel,
    FakeCtx,
    FakeGuild,
    FakeMember,
    _admin,
    cog,  # noqa: F401  (fixture)
    fake_art_bytes,
)


class Role:
    def __init__(self, id_):
        self.id = id_
        self.mention = f"<@&{id_}>"


class RoleGuild(FakeGuild):
    def __init__(self, id_, roles):
        super().__init__(id_)
        self._roles = {r.id: r for r in roles}

    def get_role(self, role_id):
        return self._roles.get(role_id)


class RoleMember(FakeMember):
    def __init__(self, id_, guild):
        super().__init__(id_, guild)
        self.roles = []

    async def add_roles(self, *roles, reason=None):
        self.roles.extend(roles)


async def setup(cog, gid):
    collector, master = Role(gid * 7), Role(gid * 7 + 1)
    guild = RoleGuild(gid, [collector, master])
    admin = _admin(gid * 10, guild)
    a = RoleMember(gid * 10 + 1, guild)
    guild.members = {admin.id: admin, a.id: a}
    channel = FakeChannel(gid * 100, guild)
    cog.bot.register_channel(channel)
    for series in ("Alpha", "Beta"):
        for i in range(3):
            ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes())])
            await cog.card.commands["addcard"].callback(cog, ctx, "common", name_and_series=f"{series}{i} | {series}")
    cog._rng = random.Random(gid)
    return guild, admin, a, channel, collector, master


@pytest.mark.asyncio
async def test_finishing_sets_hands_out_the_roles_for_each_count(cog):
    guild, admin, a, channel, collector, master = await setup(cog, 1101)
    await cog._apply_set_role_save(guild, admin, None, {"sets": "1"}, [collector])
    await cog._apply_set_role_save(guild, admin, None, {"sets": "2"}, [master])
    pool = {d["name"]: int(k) for k, d in (await cog.config.guild(guild).pool()).items()}
    for name in ("Alpha0", "Alpha1", "Alpha2"):
        await cog._wishlist_for(guild, a.id, pool[name], name)  # harmless; just exercising state
    state = await cog._member_state(a)
    for name in ("Alpha0", "Alpha1", "Alpha2"):
        card = await cog._card_by_id(guild, pool[name])
        outcome, _ = cog._grant_card(state, card, {})
        await cog._complete_set(guild, a, state, card, outcome)
    assert a.roles == [collector]
    for name in ("Beta0", "Beta1", "Beta2"):
        card = await cog._card_by_id(guild, pool[name])
        outcome, _ = cog._grant_card(state, card, {})
        await cog._complete_set(guild, a, state, card, outcome)
    assert a.roles == [collector, master]


@pytest.mark.asyncio
async def test_give_roles_now_catches_up_people_who_already_qualify(cog):
    guild, admin, a, channel, collector, _master = await setup(cog, 1102)
    state = await cog._member_state(a)
    state.sets_claimed = ["Alpha"]
    await cog._save_member_state(a, state)
    with pytest.raises(PanelError):
        await cog._apply_set_roles_sync(guild, admin, None)
    await cog._apply_set_role_save(guild, admin, None, {"sets": "1"}, [SimpleNamespace(id=collector.id)])
    assert "Gave 1 role to 1 member" in await cog._apply_set_roles_sync(guild, admin, None)
    assert a.roles == [collector]
    assert "already has" in await cog._apply_set_roles_sync(guild, admin, None)


@pytest.mark.asyncio
async def test_set_roles_can_be_moved_to_a_new_count_and_removed(cog):
    guild, admin, _a, _c, collector, _m = await setup(cog, 1103)
    await cog._apply_set_role_save(guild, admin, None, {"sets": "1"}, [collector])
    await cog._apply_set_role_save(guild, admin, "1", {"sets": "3"}, [collector])
    assert await cog.config.guild(guild).set_roles() == {"3": collector.id}
    await cog._apply_set_role_remove(guild, admin, "3")
    assert await cog.config.guild(guild).set_roles() == {}
    with pytest.raises(PanelError):
        await cog._apply_set_role_save(guild, admin, None, {"sets": "0"}, [collector])
