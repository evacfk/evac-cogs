"""Settings for the puzzle cog: the `_apply_*` methods and the `.puzzle` panel (real Red + discord.py)."""
from types import SimpleNamespace

import pytest

import discord

try:
    from puzzle import panel as panel_mod
    from puzzle.puzzle import Puzzle
    from puzzle.settings_panel import PanelError, SettingsPanel
except Exception as exc:  # pragma: no cover - environment without Red/Pillow
    pytest.skip(f"real Red/Pillow not available: {exc}", allow_module_level=True)


class Val:
    def __init__(self, data):
        self.data = data

    def __call__(self):
        return self

    def __await__(self):
        async def _get():
            return self.data
        return _get().__await__()

    async def set(self, value):
        self.data = value


DEFAULTS = dict(channel_id=None, interval_min_hours=4, interval_max_hours=8, claim_emoji="\N{JIGSAW PUZZLE PIECE}",
                reward_role_id=None, winners_count=1, next_piece_count=None, shared_mode=False,
                shared_window_minutes=1, announce_channel_id=None, live_status_channel_id=None,
                live_status_message_id=None, low_pool_threshold=2, alert_channel_id=None, win_payout=20000)


class Group:
    def __init__(self, **values):
        self.vals = {k: Val(v) for k, v in {**DEFAULTS, **values}.items()}
        for k, v in self.vals.items():
            setattr(self, k, v)

    async def all(self):
        return {k: v.data for k, v in self.vals.items()}


class Channel:
    def __init__(self, cid, fail=False):
        self.id, self.mention, self.sent, self.fail = cid, f"<#{cid}>", [], fail

    async def send(self, **kw):
        if self.fail:
            raise discord.HTTPException(SimpleNamespace(status=403, reason="Forbidden"), "no access")
        self.sent.append(kw)
        return SimpleNamespace(id=900 + len(self.sent))


class Guild:
    id, name = 1, "Wonderland"

    def __init__(self):
        self.chans = {10: Channel(10), 20: Channel(20)}
        self.roles = {5: SimpleNamespace(id=5, mention="<@&5>", name="Champ")}

    def get_channel(self, cid):
        return self.chans.get(cid)

    def get_role(self, rid):
        return self.roles.get(rid)


def make(**values):
    cog = object.__new__(Puzzle)
    group = Group(**values)
    cog.config = SimpleNamespace(guild=lambda g: group)

    async def status(guild, active):
        return discord.Embed(description="status")

    async def active(guild):
        return None
    cog._render_status_embed, cog._get_active = status, active
    return cog, group


async def test_channel_and_interval():
    cog, g = make()
    guild = Guild()
    with pytest.raises(PanelError):
        await cog._apply_channel(guild, None, None)
    assert "<#10>" in await cog._apply_channel(guild, None, guild.chans[10]) and g.channel_id.data == 10
    for bad in ({"min_hours": "0", "max_hours": "5"}, {"min_hours": "x", "max_hours": "5"}, {}):
        with pytest.raises(PanelError):
            await cog._apply_interval(guild, None, bad)
    assert (g.interval_min_hours.data, g.interval_max_hours.data) == (4, 8)
    await cog._apply_interval(guild, None, {"min_hours": "9", "max_hours": "3"})  # swapped like the old command
    assert (g.interval_min_hours.data, g.interval_max_hours.data) == (3.0, 9.0)


async def test_winners_pieces_emoji_validation():
    cog, g = make()
    for bad in ({"count": "0"}, {"count": "x"}):
        with pytest.raises(PanelError):
            await cog._apply_winners(None, None, bad)
    await cog._apply_winners(None, None, {"count": "3"})
    assert g.winners_count.data == 3
    for bad in ({"count": "1"}, {"count": "26"}):
        with pytest.raises(PanelError):
            await cog._apply_default_pieces(None, None, bad)
    await cog._apply_default_pieces(None, None, {"count": "12"})
    assert g.next_piece_count.data == 12
    for bad in ("", "  ", "a b", "x" * 65):
        with pytest.raises(PanelError):
            await cog._apply_emoji(None, None, {"emoji": bad})
    await cog._apply_emoji(None, None, {"emoji": " \N{WHITE MEDIUM STAR} "})
    assert g.claim_emoji.data == "\N{WHITE MEDIUM STAR}"


async def test_shared_mode_window_and_threshold_validation():
    cog, g = make()
    assert "OFF" in await cog._apply_shared_mode(None, None, False)
    assert "ON" in await cog._apply_shared_mode(None, None, True) and g.shared_mode.data is True
    for bad in ("0", "61", "-1", "x"):
        with pytest.raises(PanelError):
            await cog._apply_shared_window(None, None, {"minutes": bad})
    await cog._apply_shared_window(None, None, {"minutes": "2.5"})
    assert g.shared_window_minutes.data == 2.5
    with pytest.raises(PanelError):
        await cog._apply_low_pool(None, None, {"count": "-1"})
    assert "off" in await cog._apply_low_pool(None, None, {"count": "0"})
    with pytest.raises(PanelError):
        await cog._apply_payout(None, None, {"amount": "-5"})


async def test_clearable_channels_and_role():
    cog, g = make(announce_channel_id=10, alert_channel_id=20, reward_role_id=5)
    await cog._apply_announce_channel(None, None, None)
    await cog._apply_alert_channel(None, None, None)
    await cog._apply_reward_role(None, None, None)
    assert (g.announce_channel_id.data, g.alert_channel_id.data, g.reward_role_id.data) == (None, None, None)


async def test_live_status_posts_then_disables_and_reports_failure():
    cog, g = make()
    guild = Guild()
    msg = await cog._apply_live_status(guild, None, guild.chans[10])
    assert g.live_status_channel_id.data == 10 and g.live_status_message_id.data == 901 and "<#10>" in msg
    assert "off" in await cog._apply_live_status(guild, None, None)
    assert g.live_status_channel_id.data is None and g.live_status_message_id.data is None
    broken = Channel(30, fail=True)
    with pytest.raises(PanelError):
        await cog._apply_live_status(guild, None, broken)
    assert g.live_status_channel_id.data is None


async def test_removed_setter_commands_are_gone():
    names = {c.name for c in Puzzle.puzzle.commands}
    for gone in ("setpieces", "setsharedmode", "setsharedwindow", "setannouncechannel", "livestatus", "setchannel",
                 "setinterval", "setrole", "setemoji", "setwinners", "setlowpool", "setpayout", "setalertchannel"):
        assert gone not in names
    for kept in ("version", "addimage", "settings", "start", "stop", "images", "status", "leaderboard"):
        assert kept in names


# ---------------------------------------------------------------- the panel widget

class Resp:
    def __init__(self):
        self.done, self.edited, self.modal = False, [], None

    def is_done(self):
        return self.done

    async def defer(self):
        self.done = True

    async def edit_message(self, **kw):
        self.done = True
        self.edited.append(kw)

    async def send_message(self, *a, **k):
        self.done = True

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


async def open_it(cog, guild):
    a = admin()
    p = SettingsPanel(cog, guild, a, panel_mod.build_specs(), title="Puzzle", intro=panel_mod.INTRO)
    await p.refresh_data()
    p.rebuild()
    return p, a


def note(click):
    payload = click.originals[-1] if click.originals else click.response.edited[-1]
    return payload["embed"].description


def test_specs_fit_discord_limits():
    specs = panel_mod.build_specs()
    keys = [s.key for s in specs]
    assert len(keys) == len(set(keys)) == 13
    assert all(len(s.fields) <= 5 for s in specs)
    assert {s.tab for s in specs} == {"Round", "Sharing & status", "Pool & rewards"}


async def test_panel_shows_values_and_interval_modal_saves():
    guild = Guild()
    cog, g = make(channel_id=10, interval_min_hours=2, interval_max_hours=6, win_payout=0)
    p, a = await open_it(cog, guild)
    text = p.build_embed().description
    assert "<#10>" in text and "random 2 to 6 hours" in text
    await p._on_setting(Click(a, ["interval"]))
    click = Click(a)
    await p._on_edit_modal(click)
    modal = click.response.modal
    assert [modal.boxes[k].default for k in ("min_hours", "max_hours")] == ["2", "6"]
    modal.boxes["min_hours"]._value, modal.boxes["max_hours"]._value = "1", "3"
    done = Click(a)
    await modal.on_submit(done)
    assert "✅" in note(done) and (g.interval_min_hours.data, g.interval_max_hours.data) == (1.0, 3.0)
    modal.boxes["min_hours"]._value = "0"
    bad = Click(a)
    await modal.on_submit(bad)
    assert "⚠" in note(bad) and g.interval_min_hours.data == 1.0


async def test_only_the_opener_with_manage_server_can_use_it():
    cog, g = make()
    p, a = await open_it(cog, Guild())
    assert await p.interaction_check(Click(SimpleNamespace(id=2, guild_permissions=a.guild_permissions))) is False
    assert await p.interaction_check(Click(a)) is True
    a.guild_permissions.manage_guild = False
    assert await p.interaction_check(Click(a)) is False
