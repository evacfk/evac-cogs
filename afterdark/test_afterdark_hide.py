"""Hiding every robot-prefixed channel: dry run first, fully reversible, and it maps back onto the
interests (buttons) and feeds that used those channels."""
import discord
import pytest

from afterdark import constants as C
from afterdark.settings_panel import PanelError
from afterdark.test_afterdark_cog import (FEET_CHANNEL, NOW, FakeInteraction, add_two_feet_channels,  # noqa: F401
                                          expect_refused, set_cfg, w)
from afterdark.test_afterdark_feeds import f  # noqa: F401

BOT = C.HIDE_PREFIX + "・"


def ow(**kw):
    return discord.PermissionOverwrite(**kw)


async def robot_world(w):
    """Feet interest with three robot-prefixed channels, a plain channel, and a few kinds of overwrite."""
    await add_two_feet_channels(w)
    w.feet.name, w.feet2.name, w.feet3.name = BOT + "feet", BOT + "user-feet", BOT + "anime-feet"
    w.plain = w.guild.add_channel(900050, "general")
    w.other = w.guild.add_channel(900060, BOT + "bot-commands")     # a robot channel that is NOT an interest
    w.admin = w.member("admin")
    w.joined = [w.member(f"m{i}") for i in range(2)]
    for m in w.joined:
        m.roles.append(w.rabbit)
        await w.cog.handle_interest_toggle(FakeInteraction(m, w.guild), "feet")
    w.otherbot = w.member("otherbot", roles=[], bot=True)
    w.integration = w.guild.add_role(8100, "integration", managed=True)
    w.mod_role = w.guild.add_role(8200, "Mod2")
    for ch in (w.feet, w.feet2, w.feet3, w.other):
        ch.overwrites[w.otherbot] = ow(view_channel=True, send_messages=True)
        ch.overwrites[w.integration] = ow(view_channel=True)
        ch.overwrites[w.mod_role] = ow(view_channel=True, manage_messages=True)
        ch.overwrites[w.guild.default_role] = ow(view_channel=False)
    w.plain.overwrites[w.joined[0]] = ow(view_channel=True)
    return w


async def hide(w, admin=None):
    admin = admin or w.admin
    await w.cog._apply_hide_preview(w.guild, admin)
    return await w.cog._apply_hide(w.guild, admin, "hide")


def view_of(channel, target):
    return getattr(channel.overwrites.get(target), "view_channel", "missing")


async def test_preview_changes_nothing_and_flags_non_feed_channels(w):
    await robot_world(w)
    before = {c.id: dict(c.overwrites) for c in w.guild.channels.values()}
    msg = await w.cog._apply_hide_preview(w.guild, w.admin)
    assert {c.id: dict(c.overwrites) for c in w.guild.channels.values()} == before
    assert "Preview only" in msg and "#" + BOT + "feet" in msg and "general" not in msg
    assert "CHECK THESE" in msg and "bot-commands" in msg.split("CHECK THESE")[1]
    assert "Feet" in msg                                       # the button that would go
    assert await w.cog.config.guild(w.guild).hidden_channels() == {}


async def test_hide_needs_a_fresh_preview(w):
    await robot_world(w)
    await expect_refused(w.cog._apply_hide(w.guild, w.admin, "hide"), "Run the preview first")
    await w.cog._apply_hide_preview(w.guild, w.admin)
    w.clock[0] += C.HIDE_PREVIEW_TTL + 1
    await expect_refused(w.cog._apply_hide(w.guild, w.admin, "hide"), "Run the preview first")
    await expect_refused(w.cog._apply_hide(w.guild, w.admin, "maybe"), "hide or unhide")


async def test_hide_locks_out_members_but_keeps_you_the_bots_and_plain_channels(w):
    await robot_world(w)
    msg = await hide(w)
    for ch in (w.feet, w.feet2, w.feet3, w.other):
        assert view_of(ch, w.guild.default_role) is False
        assert all(view_of(ch, m) is False for m in w.joined if m in ch.overwrites)
        assert view_of(ch, w.mod_role) is False                 # staff roles lose it too
        assert view_of(ch, w.admin) is True                     # you keep it
        assert view_of(ch, w.guild.me) is True
        assert view_of(ch, w.otherbot) is True                  # other bots can keep posting
        assert view_of(ch, w.integration) is True
    assert w.feet.overwrites[w.mod_role].manage_messages is True   # other flags are untouched
    assert view_of(w.plain, w.joined[0]) is True                # a channel without the prefix is left alone
    assert "Hid 4 channel(s)" in msg


async def test_unhide_restores_everything_exactly(w):
    await robot_world(w)
    snapshot = {c.id: {t: dict(vars(o)) for t, o in c.overwrites.items()} for c in (w.feet, w.feet2, w.feet3, w.other)}
    await hide(w)
    msg = await w.cog._apply_hide(w.guild, w.admin, "unhide")
    for c in (w.feet, w.feet2, w.feet3, w.other):
        after = {t: dict(vars(o)) for t, o in c.overwrites.items()}
        before = {t: v for t, v in snapshot[c.id].items()}
        assert after == before, c.name
    assert await w.cog.config.guild(w.guild).hidden_channels() == {} and "Unhid 4" in msg
    await expect_refused(w.cog._apply_hide(w.guild, w.admin, "unhide"), "Nothing is hidden")


async def test_a_member_removed_while_hidden_stays_removed_after_unhide(w):
    await robot_world(w)
    await hide(w)
    gone = w.joined[0]
    await w.cog._revoke_all(gone, reason="test")                # e.g. they left Adult Chat meanwhile
    await w.cog._apply_hide(w.guild, w.admin, "unhide")
    assert gone not in w.feet.overwrites and view_of(w.feet, w.joined[1]) is True


async def test_interest_maps_back_buttons_and_members_after_unhide(w):
    await robot_world(w)
    cfg_before = await w.cog.config.guild(w.guild).interests()
    panel = w.feet.send  # noqa: F841  (the panel lives in the log channel here)
    await w.cog.config.guild(w.guild).panel_post.set({"channel_id": w.log.id, "message_id": 0})
    posted = await w.log.send("panel")
    await w.cog.config.guild(w.guild).panel_post.set({"channel_id": w.log.id, "message_id": posted.id})
    await w.cog._apply_hide_preview(w.guild, w.admin)
    msg = await w.cog._apply_hide(w.guild, w.admin, "hide")
    assert await w.cog.config.guild(w.guild).interests() == cfg_before          # the interest itself is untouched
    labels = [getattr(b, "label", None) for b in w.cog._interest_views[w.guild.id].children]
    assert labels == ["Leave Rabbit Hole"] and "Buttons removed: Feet" in msg
    assert posted.edits and "Updated the buttons" in msg
    newbie = w.member("newbie")
    newbie.roles.append(w.rabbit)
    it = FakeInteraction(newbie, w.guild)
    await w.cog.handle_interest_toggle(it, "feet")
    assert it.replies[-1] == "That one is closed." and newbie not in w.feet.overwrites
    # a mod's manual grant is refused too
    from afterdark.test_afterdark_cog import FakeCtx
    ctx = FakeCtx(w.guild, w.admin)
    await w.cog.afterdark_grantinterest.callback(w.cog, ctx, newbie, "feet") if hasattr(
        w.cog.afterdark_grantinterest, "callback") else await w.cog.afterdark_grantinterest.func(w.cog, ctx, newbie, "feet")
    assert "hidden right now" in ctx.sent[-1] and newbie not in w.feet.overwrites
    # unhide: the button is back, members are still in, and they can leave it again
    await w.cog._apply_hide(w.guild, w.admin, "unhide")
    labels = [getattr(b, "label", None) for b in w.cog._interest_views[w.guild.id].children]
    assert labels == ["Feet", "Leave Rabbit Hole"]
    m = w.joined[0]
    assert w.cog._has_full_access(m, w.cog._interests(await w.cog.config.guild(w.guild).all())["feet"])
    it = FakeInteraction(m, w.guild)
    await w.cog.handle_interest_toggle(it, "feet")
    assert "You left Feet" in it.replies[-1] and m not in w.feet.overwrites


async def test_sweep_ignores_hidden_interests_and_clocks_restart_on_unhide(w):
    await robot_world(w)
    await set_cfg(w, dry_run=False)
    state = await w.cog._get_state(w.guild)
    m = w.joined[0]
    state["feet"][str(m.id)] = {"since": NOW - 100 * C.DAY, "last": NOW - 100 * C.DAY, "warned": 0.0}
    await hide(w)
    summary = await w.cog._sweep_guild(w.guild, dry=False)
    assert summary["remove"] == [] and summary["warn"] == [] and m in w.feet.overwrites
    await w.cog._apply_hide(w.guild, w.admin, "unhide")
    entry = (await w.cog._get_state(w.guild))["feet"][str(m.id)]
    assert entry["last"] == NOW and entry["warned"] == 0.0
    summary = await w.cog._sweep_guild(w.guild, dry=False)
    assert summary["remove"] == [] and m in w.feet.overwrites      # not removed for the time it was hidden


async def test_feeds_pause_with_the_hide_and_resume_on_unhide(f):
    w = await robot_world(f)
    outside = w.guild.add_channel(900070, "plain-feed")
    from afterdark.test_afterdark_feeds import save
    await save(w, None, {"reddit": "a"}, [w.feet])
    await save(w, None, {"reddit": "mixed"}, [w.feet, outside])
    await save(w, None, {"reddit": "manual"}, [w.feet2])
    await save(w, None, {"telegram": "9"}, [w.feet3])
    w.reddit.config.mappings.data["manual"]["paused"] = True        # paused by hand before the hide
    msg = await hide(w)
    data = w.reddit.config.mappings.data
    assert data["a"]["paused"] and w.tg.config.mappings.data["9"]["paused"]
    assert not data["mixed"]["paused"]                                # still posts to a visible channel
    assert "Paused 2 feed(s)" in msg
    await w.cog._apply_hide(w.guild, w.admin, "unhide")
    assert not data["a"]["paused"] and not w.tg.config.mappings.data["9"]["paused"]
    assert data["manual"]["paused"]                                   # a hand-paused feed stays paused
    assert await w.cog.config.guild(w.guild).hidden_feeds() == []


async def test_a_failed_edit_changes_nothing(w):
    await robot_world(w)
    before = {c.id: dict(c.overwrites) for c in (w.feet, w.feet2, w.feet3, w.other)}
    w.feet3.edit_error = discord.Forbidden("no perms")
    await w.cog._apply_hide_preview(w.guild, w.admin)
    with pytest.raises(PanelError, match="Nothing was changed"):
        await w.cog._apply_hide(w.guild, w.admin, "hide")
    assert {c.id: dict(c.overwrites) for c in (w.feet, w.feet2, w.feet3, w.other)} == before
    assert await w.cog.config.guild(w.guild).hidden_channels() == {}
    assert await w.cog.config.guild(w.guild).hide_preview()          # the preview is still armed for a retry


async def test_removing_a_hidden_interest_forgets_its_snapshot(w):
    await robot_world(w)
    await hide(w)
    await w.cog._apply_interest_remove(w.guild, w.admin, "feet")
    snaps = await w.cog.config.guild(w.guild).hidden_channels()
    assert set(snaps) == {str(w.other.id)}
    assert view_of(w.feet, w.guild.default_role) is False


async def test_panel_lists_hidden_interests(w):
    from afterdark import panel as panel_mod
    await robot_world(w)
    await hide(w)
    rows = await panel_mod._get_interests(w.cog, w.guild)
    assert "HIDDEN" in rows[0][2]
    specs = {s.key for s in panel_mod.build_specs()}
    assert {"hide_preview", "hide_channels"} <= specs
