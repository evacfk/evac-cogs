"""Behaviour tests for the contest cog against fakes. They prove the logic and clean imports, NOT live Discord behaviour
(real DMs, buttons, ephemerals, permission overwrites, role icons)."""
import importlib
import io
import sys
import types
from datetime import datetime

import pytest

discord = pytest.importorskip("discord")
if not hasattr(discord, "Client"):
    pytest.skip("needs the real discord.py", allow_module_level=True)
PIL = pytest.importorskip("PIL")
from PIL import Image

from contest import _testkit as tk
from contest import constants as C
from contest import engine as E

LA = C.HOME_TZ


def ts(y, mo, d, h=12, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=LA).timestamp()


def jpg(color=(200, 40, 40)):
    buf = io.BytesIO()
    Image.new("RGB", (400, 300), color).save(buf, format="JPEG")
    return buf.getvalue()


@pytest.fixture
def mod(monkeypatch):
    tk.install(monkeypatch)
    for name in ("contest.contest", "contest.embeds", "contest.panel"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    cog = importlib.import_module("contest.contest")
    yield cog
    for name in ("contest.contest", "contest.embeds", "contest.panel"):
        sys.modules.pop(name, None)


async def _aw(value):
    return value


class World:
    def __init__(self, mod, tmp_path, monkeypatch):
        self.mod = mod
        self.g = g = tk.FakeGuild()
        self.staff_role = g.add_role(C.STAFF_ROLE_IDS[1], "Moderators")
        self.lurker, self.teen, self.muted = g.add_role(1000, "Lurker"), g.add_role(1001, "13-17"), g.add_role(1002, "Muted")
        self.approval = g.add_channel(10, "mod-chat", private=True)
        self.announce, self.results = g.add_channel(11, "announcements"), g.add_channel(12, "winners")
        self.owner = tk.FakeMember(g, 1, name="Fareed", admin=True)
        self.mod1 = tk.FakeMember(g, 2, [self.staff_role], name="Mod1")
        self.mod2 = tk.FakeMember(g, 3, [self.staff_role], name="Mod2")
        self.levels = {}
        levelup = types.SimpleNamespace(db=types.SimpleNamespace(get_conf=lambda guild: types.SimpleNamespace(
            users={u: types.SimpleNamespace(level=l) for u, l in self.levels.items()})))
        self.lurker_cog = types.SimpleNamespace(config=types.SimpleNamespace(
            guild=lambda guild: types.SimpleNamespace(lurker_role_id=lambda: _aw(1000))))
        self.cogs = {"LevelUp": levelup, "Lurker": self.lurker_cog}
        self.bot = tk.FakeBot(g, self.cogs, owner_ids={1})
        self.cog = mod.Contest(self.bot)
        self.now = ts(2026, 10, 16, 15)
        monkeypatch.setattr(self.cog, "_now", lambda: self.now)
        self.photos = tmp_path / "photos"
        monkeypatch.setattr(self.cog, "_data_dir", lambda guild: self.photos)
        self.conf = self.cog.config.guild(g)

    # ---- setup
    async def configure(self):
        c, g, o = self.cog, self.g, self.owner
        await c._apply_approval_channel(g, o, self.approval)
        await c._apply_announce_channel(g, o, self.announce)
        await c._apply_results_channel(g, o, self.results)
        await c._apply_excluded(g, o, [self.teen, self.muted])
        await c._apply_coins(g, o, {"gold": "1000", "second": "500", "third": "300", "participant": "100"})
        await c._apply_shop(g, o, {"item": "Mystery Box"})

    async def launch(self):
        await self.configure()
        return await self.cog._apply_launch(self.g, self.owner, None)

    @property
    def contest_channel(self):
        return self.g.get_channel(self.conf_value("contest_channel_id"))

    def conf_value(self, key):
        return self.cog.config.data[self.g.id].get(key)

    # ---- people and flows
    def person(self, uid, roles=(), level=1):
        m = tk.FakeMember(self.g, uid, list(roles), name=f"user{uid}")
        if level is not None:
            self.levels[uid] = level
        return m

    async def click(self, user, cid, guild=True, values=None):
        click = tk.Click(user, cid, self.g if guild else None, values)
        await self.cog.on_interaction(click)
        return click

    async def submit(self, member, n=1, title=""):
        await self.click(member, "contest:enter")
        files = [tk.FakeAttachment(f"p{i}.jpg", jpg((i * 40, 80, 120))) for i in range(n)]
        await self.cog.on_message(tk.FakeDM(member, title, files))
        return await self.click(member, f"contest:dm:submit:{self.g.id}", guild=False)

    async def approve(self, uid, by=None):
        return await self.click(by or self.mod1, f"contest:ok:{uid}")

    async def entered(self, uid, roles=(), title=""):
        m = self.person(uid, roles, level=1)
        await self.submit(m, 1, title)
        await self.approve(uid, by=self.mod2 if uid == 2 else self.mod1)
        return m


@pytest.fixture
def w(mod, tmp_path, monkeypatch):
    return World(mod, tmp_path, monkeypatch)


# --------------------------------------------------------------------------------------------- launch

async def test_launch_refuses_until_setup_is_complete(w):
    with pytest.raises(w.mod.PanelError) as err:
        await w.cog._apply_launch(w.g, w.owner, None)
    assert "review channel" in str(err.value) and "coin amounts" in str(err.value) and "shop item" in str(err.value)


async def test_review_channel_must_be_private(w):
    public = w.g.add_channel(77, "general")
    public.overwrites[w.g.default_role] = discord.PermissionOverwrite(view_channel=True)
    with pytest.raises(w.mod.PanelError, match="seen by @everyone"):
        await w.cog._apply_approval_channel(w.g, w.owner, public)


async def test_launch_creates_a_hidden_chat_off_channel_roles_and_the_announcement(w):
    msg = await w.launch()
    assert "Launched" in msg
    ch = w.contest_channel
    ev = ch.overwrites[w.g.default_role]
    assert ev.view_channel is False and ev.send_messages is False and ev.add_reactions is False
    assert ch.overwrites[w.staff_role].view_channel is True
    assert w.conf_value("launched") is True and w.conf_value("opened") is not True
    ann = w.announce.sent[-1]
    assert ann.custom_ids == ["contest:enter"]
    assert "Costume #N" in ann.embed.description and "Mystery Box" in ann.embed.description
    assert "1,000 wondercoins" in ann.embed.description
    assert w.g.get_role(w.conf_value("bronze_role_id")).name == C.BRONZE_ROLE_NAME
    assert w.g.get_role(w.conf_value("gold_role_id")).name == C.GOLD_ROLE_NAME
    assert w.conf_value("excluded_role_ids") == [1001, 1002, 1000]   # the Lurker role is added automatically
    assert ch.sent[0].pinned                                         # the status message


async def test_launch_twice_does_not_duplicate(w):
    await w.launch()
    channels = len(w.g.channels)
    assert "already" in await w.cog._apply_launch(w.g, w.owner, None)
    assert len(w.g.channels) == channels and len(w.announce.sent) == 1


async def test_roles_fall_back_to_plain_when_icons_are_not_allowed(w):
    w.g.icon_allowed = False
    await w.launch()
    assert w.g.get_role(w.conf_value("bronze_role_id")).icon_given is False


# --------------------------------------------------------------------------------------------- entering

async def test_enter_sends_a_dm_with_the_rules(w):
    await w.launch()
    p = w.person(101)
    click = await w.click(p, "contest:enter")
    assert "Check your DMs" in click.text
    assert "Wonderland" in p.dms[-1].content and "1 or 2 photos" in p.dms[-1].content


@pytest.mark.parametrize("role_attr", ["teen", "muted", "lurker"])
async def test_excluded_roles_cannot_enter(w, role_attr):
    await w.launch()
    p = w.person(101, [getattr(w, role_attr)])
    click = await w.click(p, "contest:enter")
    assert "can't take part" in click.text and not p.dms


async def test_closed_dms_get_a_helpful_message(w):
    await w.launch()
    p = w.person(101)
    p.dm_closed = True
    assert "allow direct messages" in (await w.click(p, "contest:enter")).text


async def test_entries_close_at_11pm_on_oct_31(w):
    await w.launch()
    p = w.person(101)
    w.now = ts(2026, 10, 31, 22, 59)
    assert "Check your DMs" in (await w.click(p, "contest:enter")).text
    w.now = ts(2026, 10, 31, 23, 0)
    assert "closed" in (await w.click(p, "contest:enter")).text


async def test_a_submission_lands_in_the_review_channel_with_buttons_and_photos(w):
    await w.launch()
    p = w.person(101, level=1)
    click = await w.submit(p, 2, "Count Dracula")
    assert "Submitted" in click.text
    card = w.approval.sent[-1]
    assert card.custom_ids == ["contest:ok:101", "contest:no:101"]
    assert len(card.files) == 2 and "Count Dracula" in card.embed.description
    entry = w.cog.config.data[w.g.id]["entries"]["101"]
    assert entry["status"] == "pending" and len(entry["photos"]) == 2
    assert all((w.photos / n).exists() for n in entry["photos"])
    assert 101 not in w.cog._sessions


async def test_only_two_photos_are_kept(w):
    await w.launch()
    p = w.person(101)
    await w.click(p, "contest:enter")
    files = [tk.FakeAttachment(f"p{i}.jpg", jpg()) for i in range(3)]
    await w.cog.on_message(tk.FakeDM(p, "", files))
    assert len(w.cog._sessions[101]["photos"]) == 2


async def test_non_photos_and_garbage_are_refused_in_the_dm(w):
    await w.launch()
    p = w.person(101)
    await w.click(p, "contest:enter")
    dm = tk.FakeDM(p, "", [tk.FakeAttachment("movie.mp4", b"x"), tk.FakeAttachment("fake.jpg", b"not an image")])
    await w.cog.on_message(dm)
    text = dm.replies[-1][0]
    assert "isn't a jpg" in text and "couldn't read" in text and not w.cog._sessions[101]["photos"]


async def test_resubmitting_replaces_the_pending_card_and_files(w):
    await w.launch()
    p = w.person(101)
    await w.submit(p, 1)
    first_card = w.approval.sent[-1]
    first_files = list(w.cog.config.data[w.g.id]["entries"]["101"]["photos"])
    w.now += 60                                         # a later submission gets its own file names
    await w.submit(p, 1)
    assert first_card.deleted
    assert not any((w.photos / n).exists() for n in first_files)
    assert w.cog.config.data[w.g.id]["entries"]["101"]["queue_msg_id"] == w.approval.sent[-1].id


async def test_a_session_that_timed_out_must_start_again(w):
    await w.launch()
    p = w.person(101)
    await w.click(p, "contest:enter")
    w.now += 3600
    dm = tk.FakeDM(p, "", [tk.FakeAttachment("a.jpg", jpg())])
    await w.cog.on_message(dm)
    assert "timed out" in dm.replies[-1][0] and 101 not in w.cog._sessions


async def test_start_over_and_cancel(w):
    await w.launch()
    p = w.person(101)
    await w.click(p, "contest:enter")
    await w.cog.on_message(tk.FakeDM(p, "Ghost", [tk.FakeAttachment("a.jpg", jpg())]))
    await w.click(p, f"contest:dm:reset:{w.g.id}", guild=False)
    assert w.cog._sessions[101]["photos"] == [] and w.cog._sessions[101]["title"] == ""
    c = await w.click(p, f"contest:dm:cancel:{w.g.id}", guild=False)
    assert "Cancelled" in c.text and 101 not in w.cog._sessions


# --------------------------------------------------------------------------------------------- review

async def test_approval_posts_an_anonymous_numbered_costume(w):
    await w.launch()
    await w.entered(101, title="Witch")
    await w.entered(102)
    posts = [m for m in w.contest_channel.sent if m.custom_ids and m.custom_ids[0].startswith("contest:vote")]
    assert [m.content for m in posts] == ["**Costume #1** \N{EM DASH} Witch", "**Costume #2**"]
    assert posts[0].custom_ids == ["contest:vote:1"] and len(posts[0].files) == 1
    assert "101" not in posts[0].content and "user101" not in posts[0].content
    assert "approved" in w.g.get_member(101).dms[-1].content and "#1" in w.g.get_member(101).dms[-1].content


async def test_only_mods_can_approve(w):
    await w.launch()
    p = w.person(101)
    await w.submit(p)
    random_member = w.person(300)
    click = await w.approve(101, by=random_member)
    assert "Only the mods" in click.text
    assert w.cog.config.data[w.g.id]["entries"]["101"]["status"] == "pending"


async def test_a_mod_cannot_approve_their_own_entry_but_the_owner_can(w):
    await w.launch()
    await w.submit(w.mod1)
    assert "own entry" in (await w.approve(2, by=w.mod1)).text
    assert w.cog.config.data[w.g.id]["entries"]["2"]["is_mod"] is True
    assert "approved" not in (w.mod1.dms[-1].content if w.mod1.dms else "")
    await w.approve(2, by=w.mod2)
    assert w.cog.config.data[w.g.id]["entries"]["2"]["status"] == "approved"
    await w.submit(w.owner)
    await w.approve(1, by=w.owner)
    assert w.cog.config.data[w.g.id]["entries"]["1"]["status"] == "approved"


async def test_an_entry_cannot_be_approved_twice(w):
    await w.launch()
    await w.entered(101)
    again = await w.approve(101)
    assert "already handled" in again.text
    assert len([m for m in w.contest_channel.sent if m.custom_ids and m.custom_ids[0].startswith("contest:vote")]) == 1


async def test_rejecting_sends_the_reason_deletes_the_photos_and_allows_a_new_entry(w):
    await w.launch()
    p = w.person(101)
    await w.submit(p)
    names = list(w.cog.config.data[w.g.id]["entries"]["101"]["photos"])
    card = w.approval.sent[-1]
    ask = await w.click(w.mod1, "contest:no:101")
    assert ask.out[0][2]["view"].children[0].custom_id == "contest:why:101"
    done = await w.click(w.mod1, "contest:why:101", values=["no_note"])
    assert "No username" in done.text
    assert "No username + Wonderland note" in p.dms[-1].content
    assert not any((w.photos / n).exists() for n in names)
    assert card.files == [] and "Rejected" in card.embed.description
    assert w.cog.config.data[w.g.id]["entries"]["101"]["status"] == "rejected"
    await w.submit(p)                                   # may try again
    assert w.cog.config.data[w.g.id]["entries"]["101"]["status"] == "pending"


async def test_an_approved_member_cannot_enter_again(w):
    await w.launch()
    p = await w.entered(101)
    assert "already in as **Costume #1**" in (await w.click(p, "contest:enter")).text


# --------------------------------------------------------------------------------------------- voting

async def three(w):
    await w.launch()
    for uid in (101, 102, 103):
        await w.entered(uid)


async def test_voting_is_locked_until_the_open_time(w):
    await three(w)
    v = w.person(201)
    assert "opens Mon Oct 26, 12:00 AM PT" in (await w.click(v, "contest:vote:1")).text
    assert w.cog.config.data[w.g.id]["votes"] == {} if "votes" in w.cog.config.data[w.g.id] else True


async def test_a_vote_needs_level_one_and_fails_closed_when_level_is_unreadable(w):
    await three(w)
    w.now = ts(2026, 10, 26, 9)
    assert "level 1" in (await w.click(w.person(201, level=0), "contest:vote:1")).text
    del w.cog.bot._cogs["LevelUp"]
    assert "couldn't check your level" in (await w.click(w.person(202, level=1), "contest:vote:1")).text
    assert not w.cog.config.data[w.g.id].get("votes")


async def test_excluded_roles_cannot_vote(w):
    await three(w)
    w.now = ts(2026, 10, 26, 9)
    assert "can't vote" in (await w.click(w.person(201, [w.muted]), "contest:vote:1")).text


async def test_vote_rules_in_the_cog(w):
    await three(w)
    voter = w.person(201)
    w.now = ts(2026, 10, 26, 9)
    assert "own costume" in (await w.click(w.g.get_member(101), "contest:vote:1")).text
    assert "not in the contest" in (await w.click(voter, "contest:vote:9")).text
    ok = await w.click(voter, "contest:vote:2")
    assert "vote for today is **Costume #2**" in ok.text and "1 of 6" in ok.text
    await w.click(voter, "contest:vote:3")                       # same day: switches
    assert w.cog.config.data[w.g.id]["votes"]["201"] == {"2026-10-26": 3}
    w.now = ts(2026, 10, 27, 9)
    assert "vote for today is **Costume #1**" in (await w.click(voter, "contest:vote:1")).text
    assert "already voted for Costume #3" in (await w.click(voter, "contest:vote:3")).text
    assert w.cog.config.data[w.g.id]["votes"]["201"] == {"2026-10-26": 3, "2026-10-27": 1}


async def test_voting_closes_at_midnight_after_oct_31(w):
    await three(w)
    v = w.person(201)
    w.now = ts(2026, 10, 31, 23, 59)
    assert "vote for today" in (await w.click(v, "contest:vote:1")).text
    w.now = ts(2026, 11, 1, 0, 0)
    assert "closed" in (await w.click(w.person(202), "contest:vote:1")).text


async def test_removing_an_entry_drops_its_votes_and_post(w):
    await three(w)
    w.now = ts(2026, 10, 26, 9)
    await w.click(w.person(201), "contest:vote:2")
    post = next(m for m in w.contest_channel.sent if m.custom_ids == ["contest:vote:2"])
    msg = await w.cog.remove_entry(w.g, 2)
    assert "removed" in msg and post.deleted
    assert w.cog.config.data[w.g.id]["votes"] == {}
    assert "no approved Costume #2" in str(await _raises(w.cog.remove_entry(w.g, 2)))


async def _raises(coro):
    try:
        await coro
    except Exception as exc:
        return exc
    return "no error"


# --------------------------------------------------------------------------------------------- schedule

async def test_the_channel_opens_by_itself_at_the_open_time_and_the_check_passes(w):
    await three(w)
    await w.cog._tick(w.g, ts(2026, 10, 25, 23, 59))
    assert w.contest_channel.overwrites[w.g.default_role].view_channel is False
    await w.cog._tick(w.g, ts(2026, 10, 26, 0, 1))
    ch = w.contest_channel
    assert ch.overwrites[w.g.default_role].view_channel is True
    assert ch.overwrites[w.g.default_role].send_messages is False            # chat stays off
    for role in (w.lurker, w.teen, w.muted):
        assert ch.overwrites[role].view_channel is False
    assert ch.overwrites[w.staff_role].view_channel is True                  # mods always see it
    assert w.conf_value("opened") is True
    report = w.approval.sent[-1].content
    assert "visibility check" in report and "CROSS" not in report and "\N{CROSS MARK}" not in report
    sends = len(ch.sent)
    await w.cog._tick(w.g, ts(2026, 10, 26, 0, 2))
    assert len(ch.sent) == sends                                             # idempotent


async def test_open_now_and_hide_again(w):
    await three(w)
    assert "visible" in await w.cog._apply_open_now(w.g, w.owner, None)
    assert w.contest_channel.overwrites[w.g.default_role].view_channel is True
    await w.cog._apply_hide(w.g, w.owner, None)
    assert w.contest_channel.overwrites[w.g.default_role].view_channel is False
    assert w.conf_value("opened") is False


async def test_schedule_edit_validates_and_saves(w):
    await w.launch()
    with pytest.raises(w.mod.PanelError):
        await w.cog._apply_schedule(w.g, w.owner, {"voting_close_ts": "2026-10-20 00:00"})
    await w.cog._apply_schedule(w.g, w.owner, {"entries_close_ts": "2026-10-30 12:00"})
    assert w.conf_value("entries_close_ts") == ts(2026, 10, 30, 12)


# --------------------------------------------------------------------------------------------- reveal

async def scored(w):
    """5 entries: #5 is a mod. People's Choice: #1, #2, #3. #4 is the only pickable Mods' Choice."""
    await w.launch()
    for uid in (101, 102, 103, 104):
        await w.entered(uid)
    await w.entered(2)           # Mod1 enters (Mod2 approves)
    votes = {"201": {"d1": 1, "d2": 2}, "202": {"d1": 1, "d2": 3}, "203": {"d1": 1}, "204": {"d1": 2}}
    await w.conf.votes.set(votes)
    for uid in (101, 102, 103, 104):
        w.g.get_member(uid)


async def test_picks_cannot_include_mods_or_the_podium(w):
    await scored(w)
    for bad in ({"first": "5"}, {"first": "1"}, {"first": "4", "second": "4"}, {"first": "x"}):
        with pytest.raises(w.mod.PanelError):
            await w.cog._apply_picks(w.g, w.owner, bad)
    assert "1st #4" in await w.cog._apply_picks(w.g, w.owner, {"first": "4", "second": "", "third": ""})


async def test_reveal_pays_one_tier_per_person_gives_roles_and_posts_results(w):
    await scored(w)
    await w.cog._apply_picks(w.g, w.owner, {"first": "4"})
    w.now = ts(2026, 11, 1, 12, 5)
    out = await w.cog.reveal(w.g)
    bank = sys.modules["redbot.core"].bank
    paid = dict(bank.deposits)
    assert paid == {101: 1000, 102: 500, 103: 300, 104: 1000, 2: 100}
    bronze, gold = w.g.get_role(w.conf_value("bronze_role_id")), w.g.get_role(w.conf_value("gold_role_id"))
    assert gold in w.g.get_member(101).roles and bronze not in w.g.get_member(101).roles
    assert gold in w.g.get_member(104).roles
    assert bronze in w.g.get_member(102).roles and bronze in w.g.get_member(103).roles and bronze in w.mod1.roles
    results = w.contest_channel.sent[-1]
    assert "People's Choice" in results.embed.description and "<@101>" in results.embed.description
    text = w.results.sent[-1].content
    assert "Mods' Choice" in text and "https://discord.com" not in text    # permanent post carries no photo links
    assert "Mystery Box" in w.approval.sent[-1].content and "<@101>" in w.approval.sent[-1].content
    assert w.conf_value("revealed") is True and len(w.conf_value("ledger")) == 5
    assert "Results posted" in out


async def test_reveal_cannot_double_pay(w):
    await scored(w)
    await w.cog._apply_picks(w.g, w.owner, {"first": "4"})
    await w.cog.reveal(w.g)
    bank = sys.modules["redbot.core"].bank
    n = len(bank.deposits)
    assert "already" in await w.cog.reveal(w.g)
    assert len(bank.deposits) == n
    # and a crash-and-retry: ledger rows already written are skipped
    await w.conf.revealed.set(False)
    await w.cog.reveal(w.g)
    assert len(bank.deposits) == n


async def test_a_person_in_both_awards_is_paid_once_at_their_best_tier(w):
    await scored(w)
    await w.conf.votes.set({"201": {"d1": 4}, "202": {"d1": 4}})      # #4 wins People's Choice
    pod = w.cog._podium(await w.conf.all())
    assert pod["pc"] == {4: 1} and 4 not in pod["eligible"]
    await w.cog.reveal(w.g)
    bank = sys.modules["redbot.core"].bank
    assert [a for u, a in bank.deposits if u == 104] == [1000]


async def test_auto_reveal_waits_for_the_picks_then_goes(w):
    await scored(w)
    bank = sys.modules["redbot.core"].bank
    await w.cog._tick(w.g, ts(2026, 11, 1, 11, 5))                        # an hour before: nudges the mods
    assert any("not complete" in m for m in w.bot.owner_msgs)
    await w.cog._tick(w.g, ts(2026, 11, 1, 12, 5))                        # held
    assert w.conf_value("revealed") is not True and not bank.deposits
    assert any("held" in m for m in w.bot.owner_msgs)
    await w.cog._apply_picks(w.g, w.owner, {"first": "4"})
    await w.cog._tick(w.g, ts(2026, 11, 1, 12, 6))
    assert w.conf_value("revealed") is True and bank.deposits


async def test_departed_entrants_are_skipped_not_paid(w):
    await scored(w)
    await w.cog._apply_picks(w.g, w.owner, {"first": "4"})
    w.g.members.remove(w.g.get_member(103))
    await w.cog.reveal(w.g)
    bank = sys.modules["redbot.core"].bank
    assert 103 not in dict(bank.deposits)
    assert next(r for r in w.conf_value("ledger") if r["user_id"] == 103)["skipped"] == "left the server"


# --------------------------------------------------------------------------------------------- purge

async def test_purge_waits_for_the_reveal_and_the_purge_time(w):
    await scored(w)
    assert "not been revealed" in str(await _raises(w.cog.purge(w.g)))
    await w.cog._apply_picks(w.g, w.owner, {"first": "4"})
    await w.cog.reveal(w.g)
    await w.cog._tick(w.g, ts(2026, 11, 9, 12))
    assert w.conf_value("purged") is not True
    await w.cog._tick(w.g, ts(2026, 11, 10, 12, 1))
    assert w.conf_value("purged") is True


async def test_purge_deletes_everything_but_the_ledger(w):
    await scored(w)
    cards = [m for m in w.approval.sent if m.custom_ids and m.custom_ids[0].startswith("contest:")]
    ch = w.contest_channel
    await w.cog._apply_picks(w.g, w.owner, {"first": "4"})
    await w.cog.reveal(w.g)
    assert any(w.photos.glob("*.jpg"))
    preview = w.cog.purge_preview(await w.conf.all())
    assert "5 stored photo" in preview and "5 review card" in preview
    await w.cog.purge(w.g)
    assert ch.deleted and all(m.deleted for m in cards)
    assert not w.photos.exists()
    data = w.cog.config.data[w.g.id]
    assert data["entries"] == {} and data["votes"] == {} and len(data["ledger"]) == 5 and data["purged"] is True
    assert "ended" in w.announce.sent[0].edits[-1]["content"]
    assert "Already" in await w.cog.purge(w.g)


async def test_hold_purge_blocks_the_automatic_purge(w):
    await scored(w)
    await w.cog._apply_picks(w.g, w.owner, {"first": "4"})
    await w.cog.reveal(w.g)
    await w.cog._apply_hold(w.g, w.owner, True)
    await w.cog._tick(w.g, ts(2026, 11, 11))
    assert w.conf_value("purged") is not True
    await w.cog._apply_hold(w.g, w.owner, False)
    await w.cog._tick(w.g, ts(2026, 11, 11, 0, 1))
    assert w.conf_value("purged") is True


async def test_data_deletion_request_removes_a_users_entry_and_votes(w):
    await three(w)
    await w.conf.votes.set({"201": {"d1": 2}, "102": {"d1": 1}})
    await w.cog.red_delete_data_for_user(requester="user", user_id=102)
    data = w.cog.config.data[w.g.id]
    assert "102" not in data["entries"] and data["votes"] == {}   # their entry's votes and their own votes are gone


# --------------------------------------------------------------------------------------------- panel and commands

async def test_check_setup_reports_problems_and_passes_when_ready(w):
    first = await w.cog._apply_check(w.g, w.owner, None)
    assert "\N{CROSS MARK} Missing:" in first
    await w.configure()
    second = await w.cog._apply_check(w.g, w.owner, None)
    assert "\N{WHITE HEAVY CHECK MARK} Launch settings are complete" in second and "LevelUp is loaded" in second


async def test_coin_amounts_are_validated(w):
    with pytest.raises(w.mod.PanelError, match="whole numbers"):
        await w.cog._apply_coins(w.g, w.owner, {"gold": "lots", "second": "1", "third": "1", "participant": "1"})
    with pytest.raises(w.mod.PanelError, match="go down the podium"):
        await w.cog._apply_coins(w.g, w.owner, {"gold": "100", "second": "500", "third": "300", "participant": "10"})


async def test_status_summary_and_panel_specs(w):
    await three(w)
    text = await w.cog._apply_summary(w.g, w.owner, None)
    assert "phase: **entries**" in text and "Approved 3" in text
    from contest import panel
    keys = [s.key for s in panel.build_specs()]
    assert len(keys) == len(set(keys)) and {"launch", "picks", "reveal", "purge", "preview"} <= set(keys)


async def test_unrelated_interactions_and_messages_are_ignored(w):
    await w.launch()
    p = w.person(101)
    click = tk.Click(p, "verdict:a:1x1:0", w.g)
    await w.cog.on_interaction(click)
    assert click.out == []
    await w.cog.on_message(tk.FakeDM(p, "hello", []))     # no session: nothing happens
    assert 101 not in w.cog._sessions


async def test_nothing_can_be_approved_after_the_reveal(w):
    await scored(w)
    late = w.person(150)
    await w.submit(late)
    await w.cog._apply_picks(w.g, w.owner, {"first": "4"})
    await w.cog.reveal(w.g)
    assert "contest is over" in (await w.approve(150)).text
    assert w.cog.config.data[w.g.id]["entries"]["150"]["status"] == "pending"


async def test_the_announcement_fits_discords_limits(w):
    await w.launch()
    embed = w.announce.sent[-1].embed
    assert len(embed.description) < 4000 and len(embed.title) < 256


async def test_malformed_ids_are_ignored_quietly(w):
    await w.launch()
    p = w.person(101)
    for cid in ("contest:vote:abc", "contest:ok", "contest:dm:submit", "contest:"):
        click = await w.click(p, cid)
        assert click.out == []
