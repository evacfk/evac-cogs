"""Behaviour tests for WonderEvents against fakes (redbot faked; discord stubbed or real 2.7.1).

Proves: event creation (post, ping, scheduled event, vote), RSVP toggling, the reminder /
start / attendance / end timeline, rescheduling and form errors. It does NOT prove live
Discord behaviour (real modals, scheduled-event permissions, poll rendering).
"""
import importlib
import sys
import types
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from wonderevents import _testkit as tk

LA = ZoneInfo("America/Los_Angeles")


@pytest.fixture
def mod(monkeypatch):
    tk.install(monkeypatch)
    tk.install_ui(monkeypatch)
    for name in ("wonderevents.wonderevents", "wonderevents.embeds"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    m = importlib.import_module("wonderevents.wonderevents")
    yield m
    for name in ("wonderevents.wonderevents", "wonderevents.embeds"):
        sys.modules.pop(name, None)


class FakeScheduled:
    def __init__(self, sid, **kw):
        self.id, self.kw, self.calls = sid, kw, []

    async def start(self):
        self.calls.append("start")

    async def end(self):
        self.calls.append("end")

    async def cancel(self):
        self.calls.append("cancel")

    async def edit(self, **kw):
        self.calls.append(("edit", kw))


class EventsGuild(tk.FakeGuild):
    def __init__(self):
        super().__init__()
        self.scheduled = {}

    async def create_scheduled_event(self, **kw):
        se = FakeScheduled(7000 + len(self.scheduled), **kw)
        self.scheduled[se.id] = se
        return se

    def get_scheduled_event(self, sid):
        return self.scheduled.get(sid)


@pytest.fixture
async def env(mod):  # async: real discord.py Views need a running loop
    g = EventsGuild()
    role = g.add_role(1536593335966367784, "Movie Night")
    chan = g.add_channel(100, "server-updates")
    vc = g.add_channel(200, "some auto room")
    bot = tk.FakeBot(g)
    bot.add_view = lambda view: None
    cog = mod.WonderEvents(bot)
    return types.SimpleNamespace(mod=mod, cog=cog, guild=g, role=role, chan=chan, vc=vc)


async def _setup(env):
    gconf = env.cog.config.guild(env.guild)
    await gconf.channel_id.set(env.chan.id)
    await gconf.kinds.set({"movie": {"emoji": "🎬", "duration": 120}})


START = datetime(2026, 10, 17, 18, 0, tzinfo=LA)


async def _create(env, options=("Project Hail Mary", "Dune 2")):
    ev, note = await env.cog.create_event(env.guild, kind="movie", host_id=42, title="Movie Night", start=START,
                                          desc="Snacks!", image=None, options=list(options), created_by=42)
    return ev, note


class Interaction:
    def __init__(self, guild, user_id, message=None):
        self.guild, self.message = guild, message
        self.user = Member(user_id)
        self.user.guild, self.user.guild_permissions = guild, None
        self.edits, self.sent = [], []
        outer = self

        class Resp:
            def is_done(self):
                return False

            async def edit_message(self, **kw):
                outer.edits.append(kw)

            async def send_message(self, text=None, **kw):
                outer.sent.append((text, kw))

            async def defer(self, **kw):
                pass

            async def send_modal(self, modal):
                outer.sent.append(("modal", modal))

        class Followup:
            async def send(self, text=None, **kw):
                outer.sent.append((text, kw))

        self.response, self.followup = Resp(), Followup()


async def test_create_posts_pings_schedules_and_votes(env):
    await _setup(env)
    ev, note = await _create(env)
    assert "Posted event #1" in note
    post, poll = env.chan.sent
    assert post.content.startswith("<@&1536593335966367784>") and "<@42> is hosting" in post.content
    assert post.kw["view"] is env.cog.rsvp_view
    assert post.embed is not None
    se = env.guild.scheduled[ev["scheduled_event_id"]]
    assert "channel" not in se.kw and se.kw["location"] and se.kw["end_time"] > se.kw["start_time"]
    assert se.kw["start_time"] == START.astimezone(timezone.utc)
    assert env.guild.get_role(ev["role_id"]).name == "🎬 Movie Night · Oct 17"
    assert poll.kw["poll"].question == "What should we watch?"
    stored = (await env.cog.config.guild(env.guild).events())["1"]
    assert stored["message_id"] == post.id and stored["poll_message_id"] == poll.id
    assert await env.cog.config.guild(env.guild).next_id() == 2


async def test_create_without_channel_is_refused(env):
    ev, note = await _create(env)
    assert ev is None and "events channel" in note


async def test_single_vote_option_posts_no_poll(env):
    await _setup(env)
    await _create(env, options=["only one"])
    assert len(env.chan.sent) == 1


async def test_rsvp_buttons_toggle_and_redraw(env):
    await _setup(env)
    ev, _ = await _create(env, options=())
    msg = env.chan.sent[0]
    it = Interaction(env.guild, 5, message=msg)
    await env.cog.handle_rsvp(it, "going")
    await env.cog.handle_rsvp(Interaction(env.guild, 6, message=msg), "maybe")
    assert (await env.cog._get_event(env.guild, 1))["rsvp"] == {"5": "going", "6": "maybe"}
    assert it.edits and it.edits[0]["view"] is env.cog.rsvp_view
    await env.cog.handle_rsvp(Interaction(env.guild, 5, message=msg), "going")  # click again = undo
    assert (await env.cog._get_event(env.guild, 1))["rsvp"] == {"6": "maybe"}


async def test_full_timeline_remind_start_attendance_end(env):
    await _setup(env)
    ev, _ = await _create(env, options=())
    msg = env.chan.sent[0]
    for uid, choice in ((5, "going"), (6, "maybe"), (7, "no")):
        await env.cog.handle_rsvp(Interaction(env.guild, uid, message=msg), choice)
    s = START.timestamp()

    await env.cog._tick(env.guild, s - 3000)  # 50 min before: reminder to going + maybe only
    reminder = env.chan.sent[-1]
    assert "<@5>" in reminder.content and "<@6>" in reminder.content and "<@7>" not in reminder.content
    await env.cog._tick(env.guild, s - 2900)
    assert env.chan.sent[-1] is reminder  # once only

    env.vc.members = [types.SimpleNamespace(id=5, bot=False), types.SimpleNamespace(id=9, bot=False),
                      types.SimpleNamespace(id=1, bot=True)]  # 9 never RSVP'd, 1 is a bot
    await env.cog._tick(env.guild, s + 30)
    start_msg = env.chan.sent[-1]
    assert "starting now" in start_msg.content and "<@5>" in start_msg.content and "<@6>" not in start_msg.content
    se = env.guild.scheduled[ev["scheduled_event_id"]]
    assert se.calls == ["start"]
    for minute in range(5, 30, 5):
        await env.cog._tick(env.guild, s + 30 + minute * 60)
    stored = await env.cog._get_event(env.guild, 1)
    assert stored["attend"] == {"5": 30}

    await env.cog._tick(env.guild, s + 121 * 60)
    stored = await env.cog._get_event(env.guild, 1)
    assert stored["ended"] and se.calls[-1] == "end"
    assert msg.view is None  # buttons removed


async def test_reschedule_resets_reminder_and_tells_rsvps(env):
    await _setup(env)
    ev, _ = await _create(env, options=())
    msg = env.chan.sent[0]
    await env.cog.handle_rsvp(Interaction(env.guild, 5, message=msg), "going")
    await env.cog._tick(env.guild, START.timestamp() - 3000)
    later = datetime(2026, 10, 18, 18, 0, tzinfo=LA)
    note = await env.cog.update_event(env.guild, 1, title="Movie Night", start=later, desc="", image=None)
    assert "Updated" in note
    stored = await env.cog._get_event(env.guild, 1)
    assert stored["start_ts"] == later.timestamp() and stored["reminded"] is False
    assert "moved" in env.chan.sent[-1].content and "<@5>" in env.chan.sent[-1].content
    assert env.guild.scheduled[ev["scheduled_event_id"]].calls[-1][0] == "edit"


async def test_form_with_bad_time_offers_a_retry(env, monkeypatch):
    await _setup(env)
    modal = env.mod.EventModal(env.cog, guild_id=env.guild.id, kind="movie", host_id=42,
                               values={"title": "Movie Night", "when": "someday"})
    it = Interaction(env.guild, 42)
    await env.cog.handle_form(it, modal)
    text, kw = it.sent[-1]
    assert "time" in text.lower() or "date" in text.lower()
    assert kw.get("ephemeral") is True and kw.get("view") is not None
    assert env.chan.sent == []


async def test_form_creates_event_in_staff_timezone(env, monkeypatch):
    await _setup(env)
    await env.cog.config.member_from_ids(env.guild.id, 42).tz.set("ET")
    modal = env.mod.EventModal(env.cog, guild_id=env.guild.id, kind="movie", host_id=42,
                               values={"title": "Hail Mary night", "when": "Oct 17 9pm", "options": "A\nB"})
    it = Interaction(env.guild, 42)
    it.user.guild = env.guild
    monkeypatch.setattr(env.mod, "datetime", type("D", (), {"now": staticmethod(lambda tz=None: datetime(2026, 10, 3, tzinfo=timezone.utc)),
                                                          "fromtimestamp": datetime.fromtimestamp}))
    await env.cog.handle_form(it, modal)
    ev = await env.cog._get_event(env.guild, 1)
    assert ev["title"] == "Hail Mary night"
    assert ev["start_ts"] == datetime(2026, 10, 17, 21, 0, tzinfo=ZoneInfo("America/New_York")).timestamp()


async def test_modal_respects_discord_limits(mod):
    m = mod.EventModal(None, guild_id=1, kind="movie", host_id=1)
    assert len(m.children) == 5
    for item in m.children:
        assert len(item.label) <= 45
    edit = mod.EventModal(None, guild_id=1, kind="movie", host_id=1, event_id=3)
    assert len(edit.children) == 4


async def test_cancel_marks_and_cancels_scheduled_event(env):
    await _setup(env)
    ev, _ = await _create(env, options=())

    class Ctx:
        guild = env.guild
        author = types.SimpleNamespace(guild_permissions=types.SimpleNamespace(administrator=True, manage_guild=True),
                                       roles=[types.SimpleNamespace(id=723377047950590033)], guild=env.guild)
        replies = []

        async def send(self, text=None, **kw):
            self.replies.append(text)

    ctx = Ctx()
    fn = getattr(env.mod.WonderEvents.event_cancel, "callback", env.mod.WonderEvents.event_cancel)
    await fn(env.cog, ctx, 1)
    stored = await env.cog._get_event(env.guild, 1)
    assert stored["cancelled"] and env.guild.scheduled[ev["scheduled_event_id"]].calls == ["cancel"]
    assert env.chan.sent[0].view is None


class Member:
    def __init__(self, uid):
        self.id, self.bot, self.roles = uid, False, []

    async def add_roles(self, *r, reason=None):
        self.roles += [x for x in r if x not in self.roles]

    async def remove_roles(self, *r, reason=None):
        self.roles = [x for x in self.roles if x not in r]


async def test_rsvp_role_given_on_going_maybe_removed_on_no_and_deleted_at_end(env):
    await _setup(env)
    ev, _ = await _create(env, options=())
    msg = env.chan.sent[0]
    role = env.guild.get_role(ev["role_id"])
    m = Member(5)
    ia = Interaction(env.guild, 5, message=msg)
    ia.user = m
    m.guild = env.guild
    await env.cog.handle_rsvp(ia, "going")
    assert role in m.roles
    await env.cog.handle_rsvp(ia, "maybe")
    assert role in m.roles
    await env.cog.handle_rsvp(ia, "no")
    assert role not in m.roles
    await env.cog.handle_rsvp(ia, "going")
    assert role in m.roles
    await env.cog._tick(env.guild, START.timestamp() + 121 * 60)
    stored = await env.cog._get_event(env.guild, 1)
    assert role.deleted and stored["role_id"] is None


async def test_cancel_deletes_role(env):
    await _setup(env)
    ev, _ = await _create(env, options=())
    role = env.guild.get_role(ev["role_id"])
    async with env.cog._lock(env.guild.id):
        await env.cog._drop_role(env.guild, ev)
    assert role.deleted


async def test_attendance_counts_rsvps_in_any_room_not_strangers(env):
    await _setup(env)
    ev, _ = await _create(env, options=())
    await env.cog.handle_rsvp(Interaction(env.guild, 5, message=env.chan.sent[0]), "going")
    room = env.guild.add_channel(555, "meow")  # an auto-created room
    room.members = [types.SimpleNamespace(id=5, bot=False), types.SimpleNamespace(id=9, bot=False)]
    stored = await env.cog._get_event(env.guild, ev["id"])
    await env.cog._do_sample(env.guild, stored, 1.0)
    assert stored["attend"] == {"5": 5}


async def test_builtin_kinds_use_hardcoded_ping_roles_without_setup(env):
    kinds = await env.cog._kinds(env.guild)
    assert kinds["game"]["ping_role_id"] == 1536590356492320768
    assert kinds["movie"]["ping_role_id"] == 1536593335966367784
    assert kinds["event"]["ping_role_id"] == 1556157565262364754
    await env.cog.config.guild(env.guild).kinds.set({"trivia": {"emoji": "x", "duration": 60},
                                                     "game": {"emoji": "g", "duration": 90, "ping_role_id": 1}})
    kinds = await env.cog._kinds(env.guild)
    assert kinds["trivia"]["ping_role_id"] == 1556157565262364754 and kinds["trivia"]["duration"] == 60
    assert kinds["game"]["ping_role_id"] == 1536590356492320768 and kinds["game"]["duration"] == 90  # role can't be overridden


async def test_only_staff_moderators_superpowers_may_host(env):
    env.cog.bot.is_owner = lambda m: _false()
    for rid in (1556159237451681803, 426696709780013066, 723377047950590033):
        assert await env.cog._can_host(types.SimpleNamespace(roles=[types.SimpleNamespace(id=rid)]))
    admin = types.SimpleNamespace(roles=[types.SimpleNamespace(id=1)],
                                  guild_permissions=types.SimpleNamespace(administrator=True, manage_guild=True, manage_events=True))
    assert not await env.cog._can_host(admin)  # Discord permissions alone are not enough
    assert not await env.cog._can_host(types.SimpleNamespace(roles=[]))


async def _false():
    return False
