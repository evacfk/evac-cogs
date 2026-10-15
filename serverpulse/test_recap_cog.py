"""Behaviour tests for the recap wiring in the cog, against fakes (no Discord, no network, no Red).

Covers: reading a channel's history for highlights, drafting end to end (what Claude is sent, what the
mods see), the draft cap and missing-key paths, the Monday auto-draft, every button (approve, edit,
regenerate, reject) including who may press them, and the settings commands.

Does NOT prove live Discord behaviour (real pagination, real embeds, real modals) or the real API.
"""
import copy
from datetime import datetime
from types import SimpleNamespace

import discord
import pytest

from serverpulse import engine, models, recap, recap_io
from serverpulse import serverpulse as sp
from serverpulse._testdata import local_ts
from serverpulse.constants import DEFAULT_GUILD, DEFAULT_MOD_CHANNEL_ID, DEFAULT_MOD_ROLE_ID, RECAP_PUBLIC_MAX, TIMEZONE
from serverpulse.test_cog_logic import NOW, FakeConfig, clock, make_bot, make_cog  # noqa: F401  (clock is a fixture)

GOOD = "=== PUBLIC ===\nWhat a week, everyone! Movie night stole the show.\n=== MODS ===\n- Run a Saturday 8pm game night."


# ------------------------------------------------------------------ fakes

class SentMessage:
    def __init__(self, mid, content, kw):
        self.id, self.content, self.kw, self.edits = mid, content, kw, []

    async def edit(self, **kw):
        self.edits.append(kw)


class FakeChan:
    _next = 9000

    def __init__(self, cid, name, messages=()):
        self.id, self.name, self.messages, self.sent, self.history_calls = cid, name, list(messages), [], 0
        self.forbidden = False
        self.no_read = False

    @property
    def mention(self):
        return f"<#{self.id}>"

    async def send(self, content=None, **kw):
        if self.forbidden:
            raise discord.Forbidden()
        FakeChan._next += 1
        m = SentMessage(FakeChan._next, content, kw)
        self.sent.append(m)
        return m

    async def history(self, limit=None, after=None, before=None, oldest_first=True):
        self.history_calls += 1
        if self.no_read:
            raise discord.Forbidden()
        for m in sorted(self.messages, key=lambda m: m.created_at):
            if after <= m.created_at < before:
                yield m


class FakeGuild:
    def __init__(self, channels):
        self.id, self.name, self.member_count = 42, "Wonderland", 7812
        self.channels = {c.id: c for c in channels}
        self.text_channels = list(channels)

    def get_channel_or_thread(self, cid):
        return self.channels.get(cid)


def rmsg(mid, uid, text, reactions=0, ts=None, bot=False, mtype="default", attachments=0, webhook=None, name=None):
    ts = ts if ts is not None else local_ts(2026, 9, 23, 12)
    return SimpleNamespace(
        id=mid, author=SimpleNamespace(id=uid, bot=bot, display_name=name or f"user{uid}"), content=text,
        reactions=[SimpleNamespace(count=reactions)] if reactions else [], attachments=[object()] * attachments,
        jump_url=f"https://discord.com/channels/42/100/{mid}", type=SimpleNamespace(name=mtype), webhook_id=webhook,
        created_at=datetime.fromtimestamp(ts, TIMEZONE),
    )


def api_payload(text=GOOD, stop="end_turn", i=1200, o=300):
    return {"content": [{"type": "text", "text": text}], "usage": {"input_tokens": i, "output_tokens": o}, "stop_reason": stop}


def fake_report():
    return SimpleNamespace(
        label="Sep 21 – Sep 27, 2026", msgs=12345, prev_label="Sep 14 – Sep 20", prev_msgs=11000, change_pct=12.2,
        unique_chatters=210, busiest_day=SimpleNamespace(weekday=4, msgs=2100), busiest_hours=[SimpleNamespace(hour=21)],
        joins=120, leaves=35, slots_n=168, baseline=[], anomalies=[],
    )


@pytest.fixture(autouse=True)
def ui_stubs(monkeypatch):
    """The shared dev stub knows too little of discord.ui for buttons and modals; add just enough, for this file only."""
    class Item:
        def __init__(self, *a, **kw):
            self.__dict__.update(kw)

    class Modal(discord.ui.View):
        pass

    monkeypatch.setattr(discord.ui.View, "add_item", lambda self, item: self.children.append(item), raising=False)
    monkeypatch.setattr(discord.ui, "Button", Item, raising=False)
    monkeypatch.setattr(discord.ui, "Modal", Modal, raising=False)
    monkeypatch.setattr(discord.ui, "TextInput", Item, raising=False)
    monkeypatch.setattr(discord, "TextStyle", SimpleNamespace(paragraph="paragraph"), raising=False)
    monkeypatch.setattr(discord, "AllowedMentions", type("AM", (), {"none": classmethod(lambda cls: "NO_PINGS")}), raising=False)


class World:
    pass


async def make_world(tmp_path, monkeypatch, *, key="sk-test", cuddle_messages=None, post=True, **recap_over):
    w = World()
    w.cuddle = FakeChan(100, "cuddle", cuddle_messages if cuddle_messages is not None else [
        rmsg(1, 11, "best joke of the week <@123456789012345678>", 30, name="Sam"),
        rmsg(2, 12, "we did it", 12, name="Kit"),
        rmsg(3, 13, "nobody reacted", 0),
        rmsg(4, 14, ".gamble 100", 40),                                  # a bot command: never a highlight
        rmsg(5, 15, "bot chatter", 50, bot=True),
        rmsg(6, 16, "last week", 25, ts=local_ts(2026, 9, 10, 12)),     # outside the week
    ])
    w.modchat = FakeChan(DEFAULT_MOD_CHANNEL_ID, "mod-chat")
    w.announce = FakeChan(300, "announcements")
    w.guild = FakeGuild([w.cuddle, w.modchat, w.announce])
    tokens = {"api_key": key} if key else {}

    async def get_tokens(name):
        assert name == "anthropic"
        return tokens

    bot = make_bot()
    bot.get_shared_api_tokens = get_tokens
    w.cog = make_cog(tmp_path, bot)
    await w.cog._ensure_guild(w.guild)
    r = copy.deepcopy(DEFAULT_GUILD["recap"])
    r.update(recap_over)
    if post:
        r.setdefault("post_channel", w.announce.id)
        r["post_channel"] = r["post_channel"] or w.announce.id
    await w.cog.config.guild(w.guild).recap.set(r)

    w.calls = []
    w.reply = api_payload()

    async def fake_call(api_key, body, **kw):
        w.calls.append((api_key, body))
        if isinstance(w.reply, Exception):
            raise w.reply
        return w.reply

    monkeypatch.setattr(sp.recap_io, "call_claude", fake_call)

    async def fake_report_for(prep, kind, resolved):
        return fake_report()

    w.cog._report_for = fake_report_for
    w.resolved = engine.resolve_period("week", "last", models.local_dt(sp.time.time()).date())
    return w


async def recap_cfg(w):
    return await w.cog.config.guild(w.guild).recap()


# ------------------------------------------------------------------ reading the channel

async def test_collect_candidates_keeps_only_well_reacted_human_messages_in_the_window():
    chan = FakeChan(100, "cuddle", [
        rmsg(1, 1, "great", 5), rmsg(2, 2, "meh", 1), rmsg(3, 3, ".cmd", 9), rmsg(4, 4, "bot", 9, bot=True),
        rmsg(5, 5, "hook", 9, webhook=77), rmsg(6, 6, "excluded", 9), rmsg(7, 7, "joined!", 9, mtype="new_member"),
        rmsg(8, 8, "old", 9, ts=local_ts(2026, 9, 1, 12)), rmsg(9, 9, "", 4, attachments=1),
    ])
    out, scanned, truncated = await recap_io.collect_candidates(
        chan, local_ts(2026, 9, 21), local_ts(2026, 9, 28), min_reactions=3, prefixes=["."], exclude_commands=True, excluded_user_ids=[6],
    )
    assert [h.message_id for h in out] == [1, 9]
    assert out[1].media and out[0].reactions == 5 and out[0].author == "user1"
    assert scanned == 8 and not truncated  # the message from September 1st is outside the window, so never read


async def test_collect_candidates_sums_every_reaction_and_stops_at_the_scan_limit():
    m = rmsg(1, 1, "popular", 0)
    m.reactions = [SimpleNamespace(count=2), SimpleNamespace(count=3)]
    chan = FakeChan(100, "c", [m] + [rmsg(10 + i, 2, "x", 0) for i in range(10)])
    out, scanned, truncated = await recap_io.collect_candidates(
        chan, local_ts(2026, 9, 21), local_ts(2026, 9, 28), min_reactions=5, prefixes=[], exclude_commands=True, max_scan=4,
    )
    assert out and out[0].reactions == 5
    assert truncated and scanned == 4


# ------------------------------------------------------------------ drafting

async def test_a_draft_reads_the_week_asks_claude_and_posts_buttons(tmp_path, monkeypatch, clock):
    w = await make_world(tmp_path, monkeypatch, voice="Cheeky. Example: 'bingo night was chaos and we loved it'", notes="Movie night was Saturday")
    await w.cog._recap_run(w.guild, w.modchat, w.resolved)

    placeholder = w.modchat.sent[0]
    assert "Reading the week" in placeholder.content and "<#100>" in placeholder.content
    final = placeholder.edits[-1]
    embed = final["embed"]
    assert embed.description == "What a week, everyone! Movie night stole the show."
    names = [f.name for f in embed.fields]
    assert names[:2] == ["By the numbers", "Top moments"] and any("growth" in n for n in names) and any("event ideas" in n for n in names)
    assert "Run a Saturday 8pm game night" in [f.value for f in embed.fields if "event ideas" in f.name][0]
    assert [b.label for b in final["view"].children] == ["Approve & post", "Edit", "Regenerate", "Reject"]
    assert [b.custom_id for b in final["view"].children] == [f"serverpulse:recap:{a}" for a in ("approve", "edit", "regen", "reject")]

    cfg = await recap_cfg(w)
    assert list(cfg["pending"]) == [str(placeholder.id)] and cfg["notes"] == ""  # a note is used once
    assert recap.drafts_used(cfg["usage"], clock()) == 1 and cfg["usage"][0][1:] == [1200, 300]
    assert w.modchat.sent[0] is placeholder and len(w.announce.sent) == 0  # nothing public yet


async def test_claude_is_sent_the_facts_highlights_voice_and_nothing_it_shouldnt_see(tmp_path, monkeypatch, clock):
    w = await make_world(tmp_path, monkeypatch, voice="Cheeky and warm", notes="Movie night was Saturday")
    await w.cog._recap_run(w.guild, w.modchat, w.resolved)

    (api_key, body), = w.calls
    assert api_key == "sk-test" and body["model"] == "claude-sonnet-5-5"
    assert "Cheeky and warm" in body["system"]
    user = body["messages"][0]["content"]
    assert "best joke of the week @member" in user and "we did it" in user
    assert "nobody reacted" not in user and ".gamble" not in user and "bot chatter" not in user and "last week" not in user
    assert "<@" not in user and "discord.com" not in user
    assert '"messages": 12345' in user and "Movie night was Saturday" in user and '"member_count_now": 7812' in user


async def test_the_draft_embed_shows_exactly_what_will_be_posted_plus_private_parts(tmp_path, monkeypatch, clock):
    w = await make_world(tmp_path, monkeypatch)
    await w.cog._recap_run(w.guild, w.modchat, w.resolved)
    embed = w.modchat.sent[0].edits[-1]["embed"]
    moments = [f.value for f in embed.fields if f.name == "Top moments"][0]
    assert moments.splitlines()[0].startswith("30× · **Sam**") and "[jump](https://discord.com/channels/42/100/1)" in moments
    numbers = [f.value for f in embed.fields if f.name == "By the numbers"][0]
    assert "**12,345** messages (+12% vs the week before)" in numbers
    assert any(f.name == "Approve posts to" and f.value == "<#300>" for f in embed.fields)
    assert "Draft 1/6 this week" in embed.footer_text and "read 5 messages" in embed.footer_text


async def test_missing_key_fails_before_reading_any_history(tmp_path, monkeypatch, clock):
    w = await make_world(tmp_path, monkeypatch, key=None)
    await w.cog._recap_run(w.guild, w.modchat, w.resolved)
    assert w.cuddle.history_calls == 0 and not w.calls
    assert "No Anthropic API key" in w.modchat.sent[0].content and "set api anthropic" in w.modchat.sent[0].content


async def test_weekly_cap_stops_a_draft_before_it_starts(tmp_path, monkeypatch, clock):
    w = await make_world(tmp_path, monkeypatch, max_drafts_week=2, usage=[[clock() - 3600, 1, 1], [clock() - 60, 1, 1]])
    await w.cog._recap_run(w.guild, w.modchat, w.resolved)
    assert w.cuddle.history_calls == 0 and not w.calls
    assert "limit of 2 drafts" in w.modchat.sent[0].content and "recap cap" in w.modchat.sent[0].content


async def test_no_highlights_channel_is_explained(tmp_path, monkeypatch, clock):
    w = await make_world(tmp_path, monkeypatch)
    w.guild.text_channels = [w.modchat]  # nothing called "cuddle", nothing configured
    await w.cog._recap_run(w.guild, w.modchat, w.resolved)
    assert "settings` → Recap" in w.modchat.sent[0].content and not w.calls


async def test_a_configured_source_beats_the_cuddle_fallback(tmp_path, monkeypatch, clock):
    w = await make_world(tmp_path, monkeypatch, source_channel=300)
    w.announce.messages = [rmsg(77, 20, "announcement love", 9)]
    await w.cog._recap_run(w.guild, w.modchat, w.resolved)
    assert w.cuddle.history_calls == 0 and w.announce.history_calls == 1
    assert "announcement love" in w.calls[0][1]["messages"][0]["content"]


@pytest.mark.parametrize("reply, shown", [
    (recap.RecapError("The API key was rejected. Set it again."), "key was rejected"),
    (api_payload(text="", stop="end_turn"), "empty draft"),
    (api_payload(stop="max_tokens"), "cut off"),
    (api_payload(text="=== MODS ===\nonly private"), "no public section"),
])
async def test_failures_are_reported_in_plain_words_and_leave_nothing_pending(tmp_path, monkeypatch, clock, reply, shown):
    w = await make_world(tmp_path, monkeypatch)
    w.reply = reply
    await w.cog._recap_run(w.guild, w.modchat, w.resolved)
    last = w.modchat.sent[0].edits[-1]
    assert last["content"].startswith("⚠️") and shown in last["content"] and last["embed"] is None and last["view"] is None
    assert (await recap_cfg(w))["pending"] == {}
    assert w.cog._recap_running == set()  # a failure must not wedge the next attempt


async def test_unexpected_errors_do_not_leak_details_into_the_channel(tmp_path, monkeypatch, clock):
    w = await make_world(tmp_path, monkeypatch)
    w.reply = RuntimeError("secret internal detail sk-test")
    await w.cog._recap_run(w.guild, w.modchat, w.resolved)
    text = w.modchat.sent[0].edits[-1]["content"]
    assert "Something went wrong" in text and "secret" not in text and "sk-test" not in text
    assert w.cog._recap_running == set()


async def test_a_second_draft_while_one_is_running_is_refused(tmp_path, monkeypatch, clock):
    w = await make_world(tmp_path, monkeypatch)
    w.cog._recap_running.add(w.guild.id)
    await w.cog._recap_run(w.guild, w.modchat, w.resolved)
    assert "already being written" in w.modchat.sent[0].content and not w.calls


async def test_too_little_data_for_the_week_is_explained(tmp_path, monkeypatch, clock):
    w = await make_world(tmp_path, monkeypatch)

    async def thin(prep, kind, resolved):
        r = fake_report()
        r.slots_n = 5
        return r

    w.cog._report_for = thin
    await w.cog._recap_run(w.guild, w.modchat, w.resolved)
    assert "full day of activity data" in w.modchat.sent[0].edits[-1]["content"] and not w.calls


async def test_unanswered_drafts_are_trimmed_to_the_newest_few(tmp_path, monkeypatch, clock):
    w = await make_world(tmp_path, monkeypatch, max_drafts_week=30)
    for _ in range(8):
        await w.cog._recap_run(w.guild, w.modchat, w.resolved)
    pending = (await recap_cfg(w))["pending"]
    assert len(pending) == sp.RECAP_PENDING_MAX
    assert str(w.modchat.sent[-1].id) in pending and str(w.modchat.sent[0].id) not in pending


# ------------------------------------------------------------------ Monday auto-draft

async def test_auto_draft_runs_once_per_week_and_only_when_switched_on(tmp_path, monkeypatch, clock):
    w = await make_world(tmp_path, monkeypatch)
    monday = datetime(2026, 10, 5, 9, 5, tzinfo=TIMEZONE)
    await w.cog._run_recap_check(w.guild, monday)
    assert not w.calls  # off by default

    await w.cog.config.guild(w.guild).recap.set({**await recap_cfg(w), "auto": True})
    w.cog._cache[w.guild.id]["mod_channel"] = w.modchat.id
    await w.cog._run_recap_check(w.guild, monday)
    assert len(w.calls) == 1 and len(w.modchat.sent) == 1
    await w.cog._run_recap_check(w.guild, monday.replace(minute=40))
    assert len(w.calls) == 1  # same week: nothing more
    assert (await recap_cfg(w))["last"] == engine.weekly_due(monday)[0]


async def test_auto_draft_marks_the_week_before_trying_so_failures_do_not_repeat(tmp_path, monkeypatch, clock):
    w = await make_world(tmp_path, monkeypatch, auto=True)
    w.reply = recap.RecapError("down")
    monday = datetime(2026, 10, 5, 9, 5, tzinfo=TIMEZONE)
    await w.cog._run_recap_check(w.guild, monday)
    await w.cog._run_recap_check(w.guild, monday.replace(minute=10))
    assert len(w.calls) == 1


async def test_the_digest_check_also_triggers_the_recap(tmp_path, monkeypatch, clock):
    w = await make_world(tmp_path, monkeypatch, auto=True)
    seen = []

    async def fake_check(guild, nowl):
        seen.append(nowl)

    async def fake_send(*a, **k):
        return True

    w.cog._run_recap_check, w.cog._send_digest = fake_check, fake_send
    now = datetime(2026, 10, 5, 9, 5, tzinfo=TIMEZONE)
    await w.cog._run_digest_check(w.guild, now)
    assert seen == [now]


# ------------------------------------------------------------------ buttons

class Resp:
    def __init__(self):
        self.calls, self.done = [], False

    def is_done(self):
        return self.done

    async def send_message(self, content=None, **kw):
        self.calls.append(("send_message", content, kw))
        self.done = True

    async def defer(self, **kw):
        self.calls.append(("defer",))
        self.done = True

    async def edit_message(self, **kw):
        self.calls.append(("edit_message", kw))
        self.done = True

    async def send_modal(self, modal):
        self.calls.append(("send_modal", modal))
        self.done = True


class Followup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kw):
        self.sent.append((content, kw))


def press(w, message, action, *, mod=True):
    user = SimpleNamespace(
        display_name="Fareed", guild=w.guild, guild_permissions=SimpleNamespace(administrator=False),
        get_role=lambda rid: object() if (mod and rid == DEFAULT_MOD_ROLE_ID) else None,
    )
    return SimpleNamespace(
        guild=w.guild, user=user, message=message, data={"custom_id": f"serverpulse:recap:{action}"},
        response=Resp(), followup=Followup(),
    )


async def drafted(tmp_path, monkeypatch, clock, **kw):
    w = await make_world(tmp_path, monkeypatch, **kw)
    await w.cog._recap_run(w.guild, w.modchat, w.resolved)
    w.draft = w.modchat.sent[0]
    return w


async def test_non_mods_cannot_press_anything(tmp_path, monkeypatch, clock):
    w = await drafted(tmp_path, monkeypatch, clock)
    for action in recap.ACTIONS:
        i = press(w, w.draft, action, mod=False)
        await w.cog.on_interaction(i)
        assert i.response.calls[0][0] == "send_message" and "Only mods" in i.response.calls[0][1]
    assert len(w.announce.sent) == 0 and len((await recap_cfg(w))["pending"]) == 1


async def test_other_buttons_are_ignored_by_the_listener(tmp_path, monkeypatch, clock):
    w = await drafted(tmp_path, monkeypatch, clock)
    for cid in ("rejoinwatch:ban:5", "serverpulse:recap:nuke", None):
        i = press(w, w.draft, "approve")
        i.data = {"custom_id": cid}
        await w.cog.on_interaction(i)
        assert i.response.calls == []


async def test_approve_posts_the_public_part_only_without_pings_and_closes_the_draft(tmp_path, monkeypatch, clock):
    w = await drafted(tmp_path, monkeypatch, clock)
    i = press(w, w.draft, "approve")
    await w.cog.on_interaction(i)

    posted, = w.announce.sent
    pub = posted.kw["embed"]
    assert posted.kw["allowed_mentions"] == "NO_PINGS"
    assert pub.description == "What a week, everyone! Movie night stole the show."
    assert [f.name for f in pub.fields] == ["By the numbers", "Top moments"]  # no growth, no event ideas, no leaves
    flat = pub.description + " ".join(f.value for f in pub.fields)
    assert "left" not in flat and "Disboard" not in flat and "game night" not in flat
    done = w.draft.edits[-1]
    assert done["view"] is None and done["embed"].title.startswith("✅ Recap posted")
    assert "posted by Fareed" in done["embed"].footer_text
    assert (await recap_cfg(w))["pending"] == {}


async def test_approve_twice_posts_once(tmp_path, monkeypatch, clock):
    w = await drafted(tmp_path, monkeypatch, clock)
    first, second = press(w, w.draft, "approve"), press(w, w.draft, "approve")
    await w.cog.on_interaction(first)
    await w.cog.on_interaction(second)
    assert len(w.announce.sent) == 1
    assert "already handled" in second.response.calls[0][1]


async def test_approve_without_a_post_channel_asks_for_one_and_keeps_the_draft(tmp_path, monkeypatch, clock):
    w = await drafted(tmp_path, monkeypatch, clock, post=False, post_channel=None)
    cfg = await recap_cfg(w)
    cfg["post_channel"] = None
    await w.cog.config.guild(w.guild).recap.set(cfg)
    i = press(w, w.draft, "approve")
    await w.cog.on_interaction(i)
    assert "Set one in `.pulse settings`" in i.response.calls[0][1] and i.response.calls[0][2]["ephemeral"]
    assert len((await recap_cfg(w))["pending"]) == 1 and not w.announce.sent


async def test_approve_when_the_bot_cannot_post_keeps_the_draft_pending(tmp_path, monkeypatch, clock):
    w = await drafted(tmp_path, monkeypatch, clock)
    w.announce.forbidden = True
    i = press(w, w.draft, "approve")
    await w.cog.on_interaction(i)
    assert "couldn't post" in i.followup.sent[0][0] and i.followup.sent[0][1]["ephemeral"]
    assert str(w.draft.id) in (await recap_cfg(w))["pending"] and w.draft.edits[-1].get("view") is not None


async def test_reject_closes_the_draft_and_posts_nothing(tmp_path, monkeypatch, clock):
    w = await drafted(tmp_path, monkeypatch, clock)
    i = press(w, w.draft, "reject")
    await w.cog.on_interaction(i)
    kind, kw = i.response.calls[0]
    assert kind == "edit_message" and kw["view"] is None and kw["embed"].title.startswith("❌ Recap rejected")
    assert not w.announce.sent and (await recap_cfg(w))["pending"] == {}


async def test_buttons_on_a_stale_draft_say_so(tmp_path, monkeypatch, clock):
    w = await drafted(tmp_path, monkeypatch, clock)
    stranger = SentMessage(123456, None, {})
    i = press(w, stranger, "approve")
    await w.cog.on_interaction(i)
    assert "already handled, or it's too old" in i.response.calls[0][1] and not w.announce.sent


async def test_regenerate_rewrites_in_place_counts_toward_the_cap_and_keeps_the_buttons(tmp_path, monkeypatch, clock):
    w = await drafted(tmp_path, monkeypatch, clock)
    w.reply = api_payload(text="=== PUBLIC ===\nTake two: a calmer recap.\n=== MODS ===\n- Try trivia.", i=900, o=200)
    before = len(w.draft.edits)  # the original draft already edited its placeholder once
    i = press(w, w.draft, "regen")
    await w.cog.on_interaction(i)

    assert i.response.calls[0] == ("defer",)
    assert w.draft.edits[before] == {"content": "✍️ Rewriting…"}
    final = w.draft.edits[-1]
    assert final["content"] is None and final["embed"].description == "Take two: a calmer recap."
    assert [b.label for b in final["view"].children][0] == "Approve & post"
    cfg = await recap_cfg(w)
    assert cfg["pending"][str(w.draft.id)]["public"] == "Take two: a calmer recap." and recap.drafts_used(cfg["usage"], clock()) == 2
    assert len(w.calls) == 2
    assert w.calls[1][1]["messages"][0]["content"] == w.calls[0][1]["messages"][0]["content"]  # same facts, no second scan
    assert w.cuddle.history_calls == 1 and w.cog._recap_running == set()


async def test_regenerate_failure_keeps_the_old_draft(tmp_path, monkeypatch, clock):
    w = await drafted(tmp_path, monkeypatch, clock)
    w.reply = recap.RecapError("The Claude API is busy right now. Try again in a minute.")
    await w.cog.on_interaction(press(w, w.draft, "regen"))
    assert w.draft.edits[-1] == {"content": "⚠️ The Claude API is busy right now. Try again in a minute."}
    assert (await recap_cfg(w))["pending"][str(w.draft.id)]["public"].startswith("What a week")
    assert w.cog._recap_running == set()


async def test_regenerate_at_the_cap_is_refused_privately(tmp_path, monkeypatch, clock):
    w = await drafted(tmp_path, monkeypatch, clock, max_drafts_week=1)
    i = press(w, w.draft, "regen")
    await w.cog.on_interaction(i)
    assert "limit of 1 drafts" in i.response.calls[0][1] and i.response.calls[0][2]["ephemeral"]
    assert len(w.calls) == 1


async def test_edit_opens_a_box_prefilled_with_the_public_text(tmp_path, monkeypatch, clock):
    w = await drafted(tmp_path, monkeypatch, clock)
    i = press(w, w.draft, "edit")
    await w.cog.on_interaction(i)
    kind, modal = i.response.calls[0]
    assert kind == "send_modal"
    assert modal.box.default == "What a week, everyone! Movie night stole the show." and modal.box.max_length == RECAP_PUBLIC_MAX
    assert (await recap_cfg(w))["pending"][str(w.draft.id)]["public"].startswith("What a week")  # opening it changes nothing


async def test_edit_submit_updates_the_draft_and_strips_pings(tmp_path, monkeypatch, clock):
    w = await drafted(tmp_path, monkeypatch, clock)
    opener = press(w, w.draft, "edit")
    await w.cog.on_interaction(opener)
    modal = opener.response.calls[0][1]
    modal.box.value = "My own words, thanks @everyone <@123456789012345678>"
    submit = press(w, w.draft, "edit")
    await modal.on_submit(submit)

    cfg = (await recap_cfg(w))["pending"][str(w.draft.id)]
    assert cfg["public"].startswith("My own words, thanks") and "@everyone" not in cfg["public"] and "<@" not in cfg["public"]
    kind, kw = submit.response.calls[0]
    assert kind == "edit_message" and kw["embed"].description == cfg["public"] and kw["view"] is not None
    # and Approve now posts the edited text
    await w.cog.on_interaction(press(w, w.draft, "approve"))
    assert w.announce.sent[0].kw["embed"].description == cfg["public"]


async def test_editing_a_draft_that_was_rejected_meanwhile_does_not_resurrect_it(tmp_path, monkeypatch, clock):
    w = await drafted(tmp_path, monkeypatch, clock)
    opener = press(w, w.draft, "edit")
    await w.cog.on_interaction(opener)
    modal = opener.response.calls[0][1]
    await w.cog.on_interaction(press(w, w.draft, "reject"))
    modal.box.value = "too late"
    late = press(w, w.draft, "edit")
    await modal.on_submit(late)
    assert "already handled" in late.response.calls[0][1] and (await recap_cfg(w))["pending"] == {}


async def test_a_button_handler_crash_replies_politely(tmp_path, monkeypatch, clock):
    w = await drafted(tmp_path, monkeypatch, clock)

    async def boom(*a, **k):
        raise RuntimeError("kaboom")

    w.cog._recap_button = boom
    i = press(w, w.draft, "approve")
    await w.cog.on_interaction(i)
    assert "Something went wrong" in i.response.calls[0][1]


# ------------------------------------------------------------------ commands

class Ctx:
    def __init__(self, w):
        self.guild, self.channel, self.sent = w.guild, w.modchat, []
        self.author = SimpleNamespace(id=1)

    async def send(self, content=None, **kw):
        self.sent.append((content, kw))


async def test_recap_command_rejects_a_bad_week_and_accepts_the_usual_ones(tmp_path, monkeypatch, clock):
    w = await make_world(tmp_path, monkeypatch)
    ctx = Ctx(w)
    await w.cog.pulse_digest_recap.func(w.cog, ctx, "banana")
    assert "couldn't read" in ctx.sent[0][0] and not w.calls
    await w.cog.pulse_digest_recap.func(w.cog, ctx, "last")
    assert len(w.calls) == 1


async def test_recap_options_are_validated_and_stored(tmp_path, monkeypatch, clock):
    from serverpulse.settings_panel import PanelError
    w = await make_world(tmp_path, monkeypatch)
    cog, g = w.cog, w.guild
    await cog._apply_recap_channel(g, None, "source", w.announce)
    await cog._apply_recap_channel(g, None, "post", w.cuddle)
    ok = {"model": "claude-opus-5-5", "drafts": "30", "reactions": "1"}
    await cog._apply_recap_options(g, None, ok)
    cfg = await recap_cfg(w)
    assert (cfg["source_channel"], cfg["post_channel"]) == (300, 100)
    assert cfg["max_drafts_week"] == 30 and cfg["min_reactions"] == 1 and cfg["model"] == "claude-opus-5-5"
    for bad in ({**ok, "model": "bad name!!"}, {**ok, "drafts": "31"}, {**ok, "drafts": "0"}, {**ok, "reactions": "101"},
                {**ok, "reactions": "x"}, {**ok, "drafts": ""}):
        with pytest.raises(PanelError):
            await cog._apply_recap_options(g, None, bad)
    assert (await recap_cfg(w))["max_drafts_week"] == 30
    with pytest.raises(PanelError):
        await cog._apply_recap_channel(g, None, "post", None)
    await cog._apply_recap_channel(g, None, "source", None)  # back to the "cuddle" fallback
    assert (await recap_cfg(w))["source_channel"] is None


async def test_turning_auto_on_skips_the_stale_backlog(tmp_path, monkeypatch, clock):
    w = await make_world(tmp_path, monkeypatch)
    await w.cog._apply_recap_auto(w.guild, None, True)
    cfg = await recap_cfg(w)
    assert cfg["auto"] and cfg["last"] == engine.weekly_due(models.local_dt(clock()))[0]
    await w.cog._apply_recap_auto(w.guild, None, False)
    assert not (await recap_cfg(w))["auto"]


async def test_voice_is_saved_cleared_and_a_long_saved_voice_is_not_truncated(tmp_path, monkeypatch, clock):
    from serverpulse.constants import RECAP_VOICE_MAX
    from serverpulse.panel import VOICE_FIELD_MAX
    from serverpulse.settings_panel import PanelError
    w = await make_world(tmp_path, monkeypatch)
    cog, g = w.cog, w.guild
    await cog._apply_recap_voice(g, None, {"voice": "  first announcement\n\nsecond announcement "})
    assert (await recap_cfg(w))["voice"] == "first announcement\n\nsecond announcement"
    with pytest.raises(PanelError):
        await cog._apply_recap_voice(g, None, {"voice": "x" * (RECAP_VOICE_MAX + 1)})
    long = "y" * 5000  # saved earlier through the old command: more than a form can show
    cfg = await recap_cfg(w)
    cfg["voice"] = long
    await cog.config.guild(g).recap.set(cfg)
    msg = await cog._apply_recap_voice(g, None, {"voice": long[:VOICE_FIELD_MAX]})  # the untouched prefill comes back
    assert "left as it is" in msg and (await recap_cfg(w))["voice"] == long
    await cog._apply_recap_voice(g, None, {"voice": ""})
    assert (await recap_cfg(w))["voice"] == ""


async def test_note_is_saved_clipped_and_cleared(tmp_path, monkeypatch, clock):
    w = await make_world(tmp_path, monkeypatch)
    cog, g = w.cog, w.guild
    await cog._apply_recap_note(g, None, {"note": "Movie night was great"})
    assert (await recap_cfg(w))["notes"] == "Movie night was great"
    await cog._apply_recap_note(g, None, {"note": "z" * 900})
    assert len((await recap_cfg(w))["notes"]) == 600
    await cog._apply_recap_note(g, None, {"note": ""})
    assert (await recap_cfg(w))["notes"] == ""


async def test_excluded_members_come_from_mentions_or_ids_without_duplicates(tmp_path, monkeypatch, clock):
    w = await make_world(tmp_path, monkeypatch)
    cog, g = w.cog, w.guild
    await cog._apply_recap_excluded(g, None, {"users": "<@11> 11"})
    assert (await recap_cfg(w))["excluded_users"] == []  # 11 is too short to be a Discord ID: ignored
    uid = 123456789012345678
    await cog._apply_recap_excluded(g, None, {"users": f"<@{uid}> {uid}"})
    assert (await recap_cfg(w))["excluded_users"] == [uid]
    await cog._apply_recap_excluded(g, None, {"users": ""})
    assert (await recap_cfg(w))["excluded_users"] == []


async def test_excluded_members_are_left_out_of_what_claude_reads(tmp_path, monkeypatch, clock):
    w = await make_world(tmp_path, monkeypatch)
    cfg = await recap_cfg(w)
    cfg["excluded_users"] = [11]
    await w.cog.config.guild(w.guild).recap.set(cfg)
    await w.cog._recap_run(w.guild, w.modchat, w.resolved)
    sent_to_claude = w.calls[0][1]["messages"][0]["content"]
    assert "best joke" not in sent_to_claude and "we did it" in sent_to_claude  # user 11 wrote the joke


async def test_status_shows_what_is_missing(tmp_path, monkeypatch, clock):
    w = await make_world(tmp_path, monkeypatch, key=None, post=False)
    cfg = await recap_cfg(w)
    cfg["post_channel"] = None
    await w.cog.config.guild(w.guild).recap.set(cfg)
    ctx = Ctx(w)
    await w.cog.pulse_recap_status.func(w.cog, ctx)
    embed = ctx.sent[0][1]["embed"]
    values = {f.name: f.value for f in embed.fields}
    assert "<#100>" == values["Highlights from"]  # found by name, "cuddle"
    assert "not set" in values["Approved posts go to"] and "missing" in values["API key"]
    assert values["Drafts, last 7 days"].startswith("0/6")


# ------------------------------------------------------------------ limits and privacy

def embed_size(e):
    return len(e.title or "") + len(e.description or "") + sum(len(f.name) + len(f.value) for f in e.fields) + len(e.footer_text or "")


async def test_the_fullest_possible_draft_still_fits_discords_embed_limits(tmp_path, monkeypatch, clock):
    w = await make_world(tmp_path, monkeypatch, cuddle_messages=[
        rmsg(i, 100 + i, "y" * 900, 50 - i, name="N" * 40) for i in range(1, 9)
    ])
    w.reply = api_payload(text="=== PUBLIC ===\n" + "p" * 9000 + "\n=== MODS ===\n" + "m" * 9000)
    await w.cog._recap_run(w.guild, w.modchat, w.resolved)
    draft = w.modchat.sent[0].edits[-1]["embed"]
    assert len(draft.description) == RECAP_PUBLIC_MAX
    assert embed_size(draft) < 6000 and all(len(f.value) <= 1024 for f in draft.fields)
    pending = (await recap_cfg(w))["pending"][str(w.modchat.sent[0].id)]
    assert embed_size(embeds_public(pending)) < 6000


def embeds_public(pending):
    from serverpulse import embeds
    return embeds.recap_public_embed(pending)


async def test_a_data_deletion_request_drops_pending_drafts_that_quote_that_person(tmp_path, monkeypatch, clock):
    w = await drafted(tmp_path, monkeypatch, clock)
    cfg = w.cog.config
    cfg.all_guilds = lambda: _async({w.guild.id: cfg._guilds[w.guild.id]})
    cfg.guild_from_id = lambda gid: cfg.guild(SimpleNamespace(id=gid))
    await w.cog.red_delete_data_for_user(requester="user", user_id=999)  # nobody quoted 999
    assert len((await recap_cfg(w))["pending"]) == 1
    await w.cog.red_delete_data_for_user(requester="user", user_id=11)  # user 11 wrote the best-reacted message
    assert (await recap_cfg(w))["pending"] == {}


async def _async(value):
    return value


async def test_a_channel_the_bot_cannot_read_is_explained(tmp_path, monkeypatch, clock):
    w = await make_world(tmp_path, monkeypatch)
    w.cuddle.no_read = True
    await w.cog._recap_run(w.guild, w.modchat, w.resolved)
    text = w.modchat.sent[0].edits[-1]["content"]
    assert "can't read <#100>" in text and "Read Message History" in text and not w.calls
    assert w.cog._recap_running == set()
