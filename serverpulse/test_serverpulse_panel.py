"""The `.pulse settings` panel: the `_apply_*` methods and the real discord.py widgets, driven with fake interactions.

Needs real discord.py and Red (skipped under the repo's stub). A live click-through is still needed after deploy.
"""
from types import SimpleNamespace

import pytest

import discord

if not hasattr(discord, "__version__"):
    pytest.skip("real discord.py not installed", allow_module_level=True)

from serverpulse import panel as panel_mod
from serverpulse import serverpulse as sp
from serverpulse.settings_panel import NEW_ITEM, PanelError, SettingsPanel
from serverpulse.test_cog_logic import GUILD_ID, clock, make_cog  # noqa: F401  (clock is a fixture)

if not hasattr(sp.commands.Cog, "listener"):
    pytest.skip("real Red not installed", allow_module_level=True)


class Chan(SimpleNamespace):
    @property
    def mention(self):
        return f"<#{self.id}>"


class Role(SimpleNamespace):
    @property
    def mention(self):
        return f"<@&{self.id}>"


def make_guild():
    chans = {c: Chan(id=c, name=f"c{c}") for c in (100000000000000001, 100000000000000002, 100000000000000003)}
    roles = {7: Role(id=7, name="Mods")}
    return SimpleNamespace(id=GUILD_ID, name="W", text_channels=list(chans.values()), voice_channels=[], stage_channels=[],
                           forums=[], get_channel_or_thread=chans.get, get_channel=chans.get, get_role=roles.get), chans, roles


C1, C2, C3 = 100000000000000001, 100000000000000002, 100000000000000003


async def world(tmp_path):
    guild, chans, roles = make_guild()
    cog = make_cog(tmp_path)
    await cog._ensure_guild(guild)
    calls = []

    async def update_board(g):
        calls.append(("update", (await cog.config.guild(g).board())["channel_id"]))

    async def delete_board(g, board):
        calls.append(("delete", board.get("channel_id")))
    cog._update_board, cog._delete_board_message = update_board, delete_board
    return cog, guild, chans, roles, calls


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


def admin():
    return SimpleNamespace(id=1, guild_permissions=SimpleNamespace(administrator=False, manage_guild=True))


def note(click):
    payload = click.originals[-1] if click.originals else click.response.edited[-1]
    return payload["embed"].description


async def open_it(cog, guild):
    a = admin()
    p = SettingsPanel(cog, guild, a, panel_mod.build_specs(), title="Server Pulse", intro=panel_mod.INTRO)
    await p.refresh_data()
    p.rebuild()
    return p, a


async def test_mod_channel_role_and_commands_toggle(tmp_path, clock):
    cog, guild, chans, roles, _ = await world(tmp_path)
    with pytest.raises(PanelError):
        await cog._apply_mod_channel(guild, None, None)
    await cog._apply_mod_channel(guild, None, chans[C2])
    assert (await cog.config.guild(guild).mod_channel_id()) == C2 and cog._cache[GUILD_ID]["mod_channel"] == C2
    await cog._apply_mod_role(guild, None, roles[7])
    assert cog._cache[GUILD_ID]["mod_role"] == 7
    await cog._apply_mod_role(guild, None, None)
    assert cog._cache[GUILD_ID]["mod_role"] is None
    await cog._apply_exclude_commands(guild, None, False)
    assert cog._cache[GUILD_ID]["exclude_commands"] is False


async def test_ignored_channels_parse_mentions_and_ids_and_refuse_unknown(tmp_path, clock):
    cog, guild, chans, roles, _ = await world(tmp_path)
    await cog._apply_ignored(guild, None, {"channels": f"<#{C1}> {C2} <#{C1}>"})
    assert cog._cache[GUILD_ID]["ignored"] == {C1, C2}
    with pytest.raises(PanelError, match="999999999999999999"):
        await cog._apply_ignored(guild, None, {"channels": f"<#{C3}> 999999999999999999"})
    assert cog._cache[GUILD_ID]["ignored"] == {C1, C2}  # a rejected edit changes nothing
    await cog._apply_ignored(guild, None, {"channels": ""})
    assert cog._cache[GUILD_ID]["ignored"] == set()


async def test_board_start_move_stop_and_refresh(tmp_path, clock):
    cog, guild, chans, roles, calls = await world(tmp_path)
    with pytest.raises(PanelError):
        await cog._apply_board_refresh(guild, None)
    await cog._apply_board(guild, None, chans[C1])
    assert (await cog.config.guild(guild).board())["channel_id"] == C1 and calls == [("update", C1)]
    await cog.config.guild(guild).board.set({"channel_id": C1, "message_id": 5})
    calls.clear()
    await cog._apply_board(guild, None, chans[C2])  # moving removes the old message first
    assert calls == [("delete", C1), ("update", C2)]
    await cog._apply_board_refresh(guild, None)
    await cog._apply_board(guild, None, None)
    assert (await cog.config.guild(guild).board()) == {"channel_id": None, "message_id": None}


async def test_a_board_that_cannot_post_is_rolled_back(tmp_path, clock):
    cog, guild, chans, roles, calls = await world(tmp_path)

    async def boom(g):
        raise discord.HTTPException(SimpleNamespace(status=403, reason="no"), "Missing Access")
    cog._update_board = boom
    with pytest.raises(PanelError, match="permissions"):
        await cog._apply_board(guild, None, chans[C1])
    assert (await cog.config.guild(guild).board())["channel_id"] is None


async def test_digest_and_joinlog_settings(tmp_path, clock):
    cog, guild, chans, roles, _ = await world(tmp_path)
    await cog._apply_digest(guild, None, "digest_monthly", False)
    assert (await cog.config.guild(guild).digest_monthly())["enabled"] is False
    with pytest.raises(PanelError):
        await cog._apply_joinlog(guild, None, "join", None)
    await cog._apply_joinlog(guild, None, "join", chans[C1])
    await cog._apply_joinlog(guild, None, "leave", chans[C2])
    assert await cog.config.guild(guild).joinlog() == {"join_channel": C1, "leave_channel": C2}
    await cog._apply_joinlog(guild, None, "leave", None)
    assert (await cog.config.guild(guild).joinlog())["leave_channel"] is None
    await cog._apply_joinlog(guild, None, "join", chans[C3])  # changing joins keeps the leave setting
    assert (await cog.config.guild(guild).joinlog())["join_channel"] == C3


async def test_invite_names_add_rename_remove_and_validate(tmp_path, clock):
    cog, guild, chans, roles, _ = await world(tmp_path)
    with pytest.raises(PanelError):
        await cog._apply_source_save(guild, None, None, {"code": "https://discord.gg/abc123", "name": ""})
    with pytest.raises(PanelError):
        await cog._apply_source_save(guild, None, None, {"code": "bad code!", "name": "X"})
    await cog._apply_source_save(guild, None, None, {"code": "https://discord.gg/abc123", "name": "Twitter"})
    assert (await cog.config.guild(guild).invite_sources())["abc123"] == "Twitter"
    await cog._apply_source_save(guild, None, "abc123", {"name": "X" * 80})
    assert len((await cog.config.guild(guild).invite_sources())["abc123"]) == 40
    await cog._apply_source_remove(guild, None, "abc123")
    with pytest.raises(PanelError):
        await cog._apply_source_remove(guild, None, "abc123")
    with pytest.raises(PanelError):
        await cog._apply_source_save(guild, None, "gone", {"name": "x"})


async def test_panel_shows_every_tab_and_edits_through_widgets(tmp_path, clock):
    cog, guild, chans, roles, calls = await world(tmp_path)
    p, a = await open_it(cog, guild)
    assert set(p.tabs) == {"Access", "Board & digests", "Join log", "Recap"}
    for tab in p.tabs:
        await p._on_tab(Click(a, [tab]))
        for spec in p.tab_specs():
            await p._on_setting(Click(a, [spec.key]))
            assert len(p.to_components()) <= 5 and p.build_embed().description
    # modal prefill and save
    await p._on_tab(Click(a, ["Recap"]))
    await p._on_setting(Click(a, ["recap_options"]))
    click = Click(a)
    await p._on_edit_modal(click)
    modal = click.response.modal
    assert modal.boxes["model"].default == "claude-sonnet-5-5" and modal.boxes["drafts"].default == "6"
    modal.boxes["drafts"]._value = "9"
    await modal.on_submit(Click(a))
    assert (await cog.config.guild(guild).recap())["max_drafts_week"] == 9
    # a stored voice longer than a text box still opens (prefill is clipped to Discord's limit)
    rec = await cog.config.guild(guild).recap()
    rec["voice"] = "v" * 5000
    await cog.config.guild(guild).recap.set(rec)
    await p.refresh_data()
    await p._on_setting(Click(a, ["recap_voice"]))
    click = Click(a)
    await p._on_edit_modal(click)
    assert len(click.response.modal.boxes["voice"].default) == panel_mod.VOICE_FIELD_MAX
    # collection: add an invite name through the form
    await p._on_tab(Click(a, ["Join log"]))
    await p._on_setting(Click(a, ["sources"]))
    await p._on_item(Click(a, [NEW_ITEM]))
    click = Click(a)
    await p._on_item_edit(click)
    modal = click.response.modal
    assert set(modal.boxes) == {"code", "name"}
    modal.boxes["code"]._value, modal.boxes["name"]._value = "zzTop99", "Reddit"
    done = Click(a)
    await modal.on_submit(done)
    assert "✅" in note(done) and (await cog.config.guild(guild).invite_sources())["zzTop99"] == "Reddit"
    await p._on_item(Click(a, ["zzTop99"]))
    await p._on_item_remove(Click(a))
    assert "zzTop99" not in await cog.config.guild(guild).invite_sources()
    # gating
    assert await p.interaction_check(Click(SimpleNamespace(id=2, guild_permissions=a.guild_permissions))) is False
    a.guild_permissions.manage_guild = False
    assert await p.interaction_check(Click(a)) is False


async def test_settings_command_opens_panel_for_admins_and_summary_for_mods(tmp_path, clock):
    cog, guild, chans, roles, _ = await world(tmp_path)
    sent = {}

    class Ctx:
        def __init__(self, member):
            self.guild, self.author = guild, member

        async def send(self, content=None, **kw):
            sent.update(kw)
            return SimpleNamespace(id=1)
    fn = cog.pulse_settings.callback
    await fn(cog, Ctx(admin()))
    assert isinstance(sent["view"], SettingsPanel)
    sent.clear()
    mod = SimpleNamespace(id=3, guild_permissions=SimpleNamespace(administrator=False, manage_guild=False))
    await fn(cog, Ctx(mod))
    assert "view" not in sent and "Server Pulse settings" in sent["embed"].title
