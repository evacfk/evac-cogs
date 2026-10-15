"""The `.card settings` panel through real discord.py components with fake interactions (live click-through still needed)."""
from types import SimpleNamespace

import pytest

import cardcollect.cardcollect as cc_module
from cardcollect import panel as panel_mod
from cardcollect.settings_panel import SettingsPanel
from test_cardcollect_smoke import FakeBot, FakeChannel, FakeCtx, FakeGuild, FakeMember


@pytest.fixture
def cog():
    return cc_module.CardCollect(FakeBot())


class Resp:
    def __init__(self):
        self.done, self.edited, self.modal = False, [], None

    def is_done(self):
        return self.done

    async def defer(self):
        self.done = True

    async def send_message(self, content=None, **kw):
        self.done = True

    async def edit_message(self, **kw):
        self.done = True
        self.edited.append(kw)

    async def send_modal(self, modal):
        self.done = True
        self.modal = modal


class Click:
    def __init__(self, user, values=None):
        self.user, self.data, self.response = user, {"values": values or []}, Resp()
        self.originals = []
        self.followup = SimpleNamespace(send=self._noop)

    async def _noop(self, *a, **k):
        pass

    async def edit_original_response(self, **kw):
        self.originals.append(kw)


def note(click):
    payload = click.originals[-1] if click.originals else click.response.edited[-1]
    return payload["embed"].description


def staff(member, manage=True):
    member.guild_permissions = SimpleNamespace(administrator=False, manage_guild=manage)
    return member


async def open_it(cog, guild, admin):
    p = SettingsPanel(cog, guild, admin, panel_mod.build_specs(), title="Card Collect", intro=panel_mod.INTRO)
    await p.refresh_data()
    p.rebuild()
    return p


async def test_panel_renders_edits_and_saves_through_the_widgets(cog):
    guild = FakeGuild(500)
    admin = staff(FakeMember(5000, guild))
    p = await open_it(cog, guild, admin)
    assert set(p.tabs) == {"Drops", "Claims & pulls"}
    for tab in p.tabs:
        await p._on_tab(Click(admin, [tab]))
        for spec in p.tab_specs():
            await p._on_setting(Click(admin, [spec.key]))
            assert len(p.to_components()) <= 5 and p.build_embed().description
    await p._on_tab(Click(admin, ["Drops"]))
    await p._on_setting(Click(admin, ["rules"]))
    click = Click(admin)
    await p._on_edit_modal(click)
    modal = click.response.modal
    assert set(modal.boxes) == {"chance", "cooldown", "expiry", "wrong", "penalty"}  # Discord's limit is five
    assert modal.boxes["chance"].default == "1" or float(modal.boxes["chance"].default) > 0
    modal.boxes["chance"]._value, modal.boxes["expiry"]._value = "3", "20"
    bad = Click(admin)
    await modal.on_submit(bad)
    assert "⚠" in note(bad)
    modal.boxes["expiry"]._value = "300"
    good = Click(admin)
    await modal.on_submit(good)
    assert "✅" in note(good) and await cog.config.guild(guild).drop_expiry_seconds() == 300
    await p._on_tab(Click(admin, ["Claims & pulls"]))
    await p._on_setting(Click(admin, ["odds"]))
    click = Click(admin)
    await p._on_edit_modal(click)
    box = click.response.modal.boxes
    assert [box[t].default for t in ("common", "rare", "epic", "legendary")] != [""] * 4
    box["legendary"]._value = "2"
    await click.response.modal.on_submit(Click(admin))
    assert (await cog.config.guild(guild).daily_weights())["legendary"] == 2.0
    assert await p.interaction_check(Click(FakeMember(1, guild))) is False
    assert await p.interaction_check(Click(admin)) is True


async def test_settings_command_opens_the_panel_for_admins_and_the_summary_for_members(cog):
    from cardcollect.settings_panel import SettingsPanel as Panel

    guild = FakeGuild(501)
    channel = FakeChannel(5010, guild)
    admin = staff(FakeMember(5011, guild))
    ctx = FakeCtx(admin, guild, channel)
    await cog.card.commands["settings"].callback(cog, ctx)
    assert isinstance(ctx.sent[-1].view, Panel)
    member = staff(FakeMember(5012, guild), manage=False)
    ctx = FakeCtx(member, guild, channel)
    await cog.card.commands["settings"].callback(cog, ctx)
    assert ctx.sent[-1].view is None and ctx.sent[-1].embeds[0].title.startswith("cardcollect settings")
