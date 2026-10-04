"""Cog-level tests for new-member retention (joins, invite sources, leaves, the join-log rebuild).

Fakes only: proves the decision logic, not live Discord (real invite counts, real log formats).
"""
from datetime import datetime
from types import SimpleNamespace

import pytest

from serverpulse import cohorts, embeds
from serverpulse import serverpulse as sp
from serverpulse._testdata import local_ts
from serverpulse.cohort_store import CohortStore
from serverpulse.constants import TIMEZONE
from serverpulse.test_cog_logic import NOW, Clock, _fn, make_cog
from serverpulse.test_embeds import assert_fits

GID = 42


@pytest.fixture
def clock(monkeypatch):
    c = Clock(NOW)
    monkeypatch.setattr(sp.time, "time", c)
    return c


class Invite(SimpleNamespace):
    pass


class Guild(SimpleNamespace):
    def __init__(self, invites=(), members=(), channels=None):
        super().__init__(id=GID, name="Wonderland", text_channels=[], voice_channels=[], stage_channels=[], forums=[],
                         features=[], members=list(members), _invites=list(invites), _channels=channels or {})

    async def invites(self):
        return [Invite(**vars(i)) for i in self._invites]

    def get_channel_or_thread(self, cid):
        return self._channels.get(cid)


def member(uid, guild, joined_ts, roles=1, onboarded=None, rejoin=False):
    return SimpleNamespace(
        id=uid, bot=False, guild=guild, joined_at=datetime.fromtimestamp(joined_ts, TIMEZONE),
        roles=["@everyone"] + ["r"] * roles, flags=SimpleNamespace(completed_onboarding=onboarded, did_rejoin=rejoin),
    )


def chat(guild, uid, ts, content="hi"):
    return SimpleNamespace(
        guild=guild, author=SimpleNamespace(bot=False, id=uid), webhook_id=None,
        created_at=datetime.fromtimestamp(ts, TIMEZONE), content=content,
        type=SimpleNamespace(name="default"), channel=SimpleNamespace(id=100, parent_id=None),
    )


async def test_join_records_the_invite_used_and_follows_the_member(tmp_path, clock):
    dis = Invite(code="etywguQBWq", uses=10, inviter=SimpleNamespace(display_name="DISBOARD"))
    me = Invite(code="829baEszgg", uses=4, inviter=SimpleNamespace(display_name="evac"))
    guild = Guild(invites=[dis, me])
    cog = make_cog(tmp_path)
    await cog._ensure_guild(guild)
    await cog._warm_invites(guild)

    dis.uses = 11
    newbie = member(1001, guild, NOW)
    await cog.on_member_join(newbie)
    rec = cog._cohorts[GID].open_record(1001)
    assert rec["s"] == "Disboard" and rec["c"] == "etywguQBWq" and rec["o"] == "live"

    me.uses = 5
    await cog.on_member_join(member(1002, guild, NOW + 5))
    assert cog._cohorts[GID].open_record(1002)["s"] == "discord.me"

    dis.uses, me.uses = 12, 6  # two joins land between snapshots: can't tell them apart
    await cog.on_member_join(member(1003, guild, NOW + 9))
    assert cog._cohorts[GID].open_record(1003)["s"] == "Unknown"

    # a command and an ignored-channel message still count as "they talked"
    clock.t = NOW + 120
    await cog.on_message(chat(guild, 1001, NOW + 120, content=".gamble"))
    assert cog._cohorts[GID].open_record(1001)["n"] == 1

    clock.t = NOW + 300
    await cog.on_member_remove(member(1001, guild, NOW, roles=2, onboarded=True))
    rec = cog._cohorts[GID].records[cohorts.record_key(rec)]
    assert rec["l"] == NOW + 300 and rec["ob"] is True and rec["r"] == 2
    assert cog._cohorts[GID].open_record(1001) is None


async def test_no_invite_permission_still_records_the_join(tmp_path, clock):
    class NoPerms(Guild):
        async def invites(self):
            raise sp.discord.Forbidden.__new__(sp.discord.Forbidden)

    guild = NoPerms()
    cog = make_cog(tmp_path)
    await cog._ensure_guild(guild)
    await cog.on_member_join(member(2001, guild, NOW))
    assert cog._cohorts[GID].open_record(2001)["s"] == "Unknown"


async def test_join_records_flush_is_throttled_and_survive_a_reload(tmp_path, clock):
    guild = Guild()
    cog = make_cog(tmp_path)
    await cog._ensure_guild(guild)
    await cog.on_member_join(member(3001, guild, NOW))
    await cog._flush_cohorts(GID)  # first flush goes through
    for i in range(5):
        await cog.on_message(chat(guild, 3001, NOW + 10 + i))
    await cog._flush_cohorts(GID)  # within 10 minutes: held back
    disk = CohortStore(tmp_path, GID)
    disk.load()
    assert disk.open_record(3001)["n"] == 0
    cog.cog_unload()  # unload always writes
    disk.load()
    assert disk.open_record(3001)["n"] == 5


class LogChannel(SimpleNamespace):
    async def history(self, limit=None, after=None, oldest_first=False):
        msgs = sorted(self.msgs, key=lambda m: m.created_at, reverse=not oldest_first)
        for m in msgs[: limit or None]:
            if after is None or m.created_at > after:
                yield m


def log_msg(ts, content, uid=None, mtype="default", author=None):
    return SimpleNamespace(
        created_at=datetime.fromtimestamp(ts, TIMEZONE), content=content, embeds=[], type=SimpleNamespace(name=mtype),
        raw_mentions=[uid] if uid else [], author=SimpleNamespace(id=author or 555, bot=True),
    )


def human_msg(ts, content, uid):
    m = log_msg(ts, content, uid=uid)
    m.author = SimpleNamespace(id=999, bot=False)
    m.webhook_id = None
    return m


async def test_joinlog_rebuild_end_to_end(tmp_path, clock):
    a, b, c = 111111111111111111, 222222222222222222, 333333333333333333
    t1, t2 = local_ts(2026, 9, 20, 15), local_ts(2026, 9, 21, 10)
    log = LogChannel(id=900, name="join-log", msgs=[
        log_msg(t1, f"Welcome <@{a}> to Wonderland!", uid=a),
        log_msg(t1 + 240, f"<@{a}> left the server", uid=a),
        log_msg(t2, f"Welcome <@{b}>!", uid=b),
        log_msg(t2 + 60, "server settings changed"),  # unreadable: no id, no wording
        log_msg(t2 + 90, "", mtype="new_member", author=c),  # Discord's own join message
        human_msg(t2 + 100, f"welcome <@{a}>!", uid=a),  # a member typing in the log channel: ignored
    ])
    guild = Guild(members=[SimpleNamespace(id=b, bot=False, joined_at=datetime.fromtimestamp(t2 + 2, TIMEZONE)),
                           SimpleNamespace(id=c, bot=False, joined_at=datetime.fromtimestamp(t2 + 90, TIMEZONE))],
                  channels={900: log})
    cog = make_cog(tmp_path)
    cog.bot.user = SimpleNamespace(id=555)
    await cog._ensure_guild(guild)
    await cog.config.guild(guild).joinlog.set({"join_channel": 900, "leave_channel": None})
    sent = []

    class Typing:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    async def send(text=None, embed=None, **kw):
        sent.append(embed or text)

    ctx = SimpleNamespace(guild=guild, send=send, typing=lambda: Typing())
    await _fn(sp.ServerPulse.pulse_joinlog_backfill)(cog, ctx, 60)

    recs = {r["u"]: r for r in cog._cohorts[GID].records.values()}
    assert set(recs) == {a, b, c}
    assert recs[a]["l"] == t1 + 240 and recs[b]["l"] is None and recs[c]["l"] is None
    result = sent[-1]
    assert_fits(result)
    values = {f.name: f.value for f in result.fields}
    assert values["Joins found"] == "3" and values["Leaves found"] == "1" and values["Couldn't read"] == "1"

    summary = cohorts.summarize(list(cog._cohorts[GID].records.values()), NOW)
    e = embeds.retention_embed(summary, cohorts.weekly_cohorts(list(cog._cohorts[GID].records.values()), NOW), [], "last 60 days", [])
    assert_fits(e)
    assert "3" in e.description


def test_retention_embed_fits_with_lots_of_data():
    now = local_ts(2026, 10, 3, 12)
    recs = []
    for i in range(400):
        j = now - (i % 120) * 86400 - 3600
        r = cohorts.new_record(10**17 + i, j, source=["Disboard", "discord.me", "evac's invite", "Unknown"][i % 4])
        r["l"] = None if i % 3 else j + (i % 50) * 60
        r["d"] = [0, 8] if i % 5 == 0 else []
        recs.append(r)
    s = cohorts.summarize(recs, now)
    e = embeds.retention_embed(s, cohorts.weekly_cohorts(recs, now), cohorts.by_source(recs, now), "last 120 days", ["note"])
    assert_fits(e)
    assert_fits(embeds.sources_embed(cohorts.by_source(recs, now), "last 30 days", now))
