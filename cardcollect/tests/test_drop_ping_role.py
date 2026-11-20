"""2.16.0: opt-in card-ping role: every real drop pings it, a button under the drop toggles it."""

import pytest

from cardcollect import views
from cardcollect.constants import PING_TOGGLE_ID
from test_cardcollect_smoke import (
    FakeAttachment,
    FakeChannel,
    FakeCtx,
    FakeGuild,
    FakeInteraction,
    FakeMember,
    _admin,
    cog,  # noqa: F401  (fixture)
    fake_art_bytes,
)

ROLE_ID = 5151


class Role:
    def __init__(self, id_):
        self.id = id_
        self.mention = f"<@&{id_}>"


class RoleGuild(FakeGuild):
    def __init__(self, id_, roles=()):
        super().__init__(id_)
        self._roles = {r.id: r for r in roles}

    def get_role(self, role_id):
        return self._roles.get(role_id)


class RoleMember(FakeMember):
    def __init__(self, id_, guild, fail=False):
        super().__init__(id_, guild)
        self.roles = []
        self.fail = fail

    async def add_roles(self, *roles, reason=None):
        if self.fail:
            import discord

            raise discord.HTTPException(type("R", (), {"status": 403, "reason": "no"})(), "Missing Permissions")
        self.roles.extend(roles)

    async def remove_roles(self, *roles, reason=None):
        self.roles = [r for r in self.roles if r not in roles]


async def setup(cog, gid, with_role=True):
    role = Role(ROLE_ID)
    guild = RoleGuild(gid, [role] if with_role else [])
    admin = _admin(gid * 10, guild)
    member = RoleMember(gid * 10 + 1, guild)
    guild.members = {admin.id: admin, member.id: member}
    channel = FakeChannel(gid * 100, guild)
    cog.bot.register_channel(channel)
    ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes())])
    await cog.card.commands["addcard"].callback(cog, ctx, "common", name_and_series="Solo | Show")
    await cog.config.guild(guild).drop_size.set(1)
    await cog.config.guild(guild).drop_ping_role_id.set(ROLE_ID if with_role else None)
    return guild, admin, member, channel, role


def _toggle(view):
    return [c for c in view.children if getattr(c, "custom_id", None) == PING_TOGGLE_ID]


@pytest.mark.asyncio
async def test_a_real_drop_pings_the_role_and_shows_the_toggle(cog):
    guild, _a, _m, channel, role = await setup(cog, 901)
    assert await cog._post_drop(channel, guild, is_test=False) is None
    sent = channel.sent[-1]
    assert f"<@&{ROLE_ID}>" in sent.content
    assert len(_toggle(sent.view if hasattr(sent, "view") else cog.active_drops[sent.id].view)) == 1


@pytest.mark.asyncio
async def test_test_drops_never_ping(cog):
    guild, _a, _m, channel, _r = await setup(cog, 902)
    assert await cog._post_drop(channel, guild, is_test=True) is None
    assert f"<@&{ROLE_ID}>" not in channel.sent[-1].content


@pytest.mark.asyncio
async def test_no_role_means_no_ping_and_no_button(cog):
    guild, _a, _m, channel, _r = await setup(cog, 903, with_role=False)
    assert await cog._post_drop(channel, guild, is_test=False) is None
    sent = channel.sent[-1]
    assert "<@&" not in sent.content
    assert _toggle(cog.active_drops[sent.id].view) == []


@pytest.mark.asyncio
async def test_toggle_opts_in_then_out(cog):
    guild, _a, member, channel, role = await setup(cog, 904)
    assert "on" in (await cog.toggle_drop_ping(member))
    assert role in member.roles
    assert "off" in (await cog.toggle_drop_ping(member))
    assert role not in member.roles


@pytest.mark.asyncio
async def test_toggle_button_press_replies_privately(cog):
    guild, _a, member, channel, role = await setup(cog, 905)
    view = views.DrawView(cog, None)
    button = _toggle(view)[0]
    inter = FakeInteraction(member, channel)
    await button.callback(inter)
    assert inter.response.deferred is True
    assert role in member.roles and "on" in inter.followup.messages[-1][0]


@pytest.mark.asyncio
async def test_toggle_reports_a_permission_failure_and_a_missing_role(cog):
    guild, _a, _m, channel, role = await setup(cog, 906)
    blocked = RoleMember(9999, guild, fail=True)
    assert "couldn't" in await cog.toggle_drop_ping(blocked)
    await cog._apply_drop_ping_role(guild, None, None)
    assert "aren't set up" in await cog.toggle_drop_ping(RoleMember(9998, guild))


@pytest.mark.asyncio
async def test_legendary_drop_pings_both_roles_once_each(cog):
    guild, admin, _m, channel, role = await setup(cog, 907)
    legend = Role(7777)
    guild._roles[legend.id] = legend
    ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes())])
    await cog.card.commands["addcard"].callback(cog, ctx, "legendary", name_and_series="Big | Show")
    await cog._apply_legendary_role(guild, admin, legend)
    # force the legendary into the drop
    for _ in range(40):
        assert await cog._post_drop(channel, guild, is_test=False) is None
        if "LEGENDARY" in channel.sent[-1].content:
            break
    content = channel.sent[-1].content
    assert "LEGENDARY" in content and "<@&7777>" in content and content.count(f"<@&{ROLE_ID}>") == 1
