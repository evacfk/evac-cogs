"""The one-time unpin: what it finds, what it must never touch, and that a dry run changes nothing."""
import types

import discord
import pytest

from secrethunt import cleanup

BOT = 100


class Msg:
    def __init__(self, mid, author=BOT, title=None):
        self.id = mid
        self.author = types.SimpleNamespace(id=author)
        self.embeds = [types.SimpleNamespace(title=title)] if title else []
        self.pinned = True

    async def unpin(self, reason=None):
        self.pinned = False


class BadMsg(Msg):
    async def unpin(self, reason=None):
        raise discord.Forbidden(types.SimpleNamespace(status=403, reason="no"), "missing perms")


class Room:
    def __init__(self, cid, msgs, archived=False, iterator=False, wrap=False):
        self.id, self.msgs, self.archived, self.iterator, self.wrap = cid, msgs, archived, iterator, wrap
        self.mention = f"<#{cid}>"
        self.edits = []

    def pins(self):
        items = [types.SimpleNamespace(message=m) for m in self.msgs if m.pinned] if self.wrap else [m for m in self.msgs if m.pinned]
        if not self.iterator:
            async def get():
                return items
            return get()

        async def gen():
            for i in items:
                yield i
        return gen()

    async def edit(self, archived):
        self.edits.append(archived)
        self.archived = archived


class Guild:
    def __init__(self, *rooms):
        self.rooms = {r.id: r for r in rooms}

    def get_channel_or_thread(self, cid):
        return self.rooms.get(cid)

    async def fetch_channel(self, cid):
        raise discord.NotFound(types.SimpleNamespace(status=404, reason="gone"), "gone")


DATA = {
    "pool": [1, 2],
    "trail": [3],
    "objects": {"late_rabbit": {"1": 11}, "tea": {"3": 31}},
    "poems": {"2": 22},
    "poem_targets": {"x": 4},
    "test_channel_id": 5,
    "test_poems": {"late_rabbit": 51},
}


def test_config_targets_collects_every_room_and_recorded_message():
    rooms, recorded = cleanup.config_targets(DATA)
    assert rooms == {1, 2, 3, 4, 5}
    assert recorded == {1: {11}, 3: {31}, 2: {22}, 5: {51}}


def test_config_targets_survives_an_empty_or_odd_config():
    assert cleanup.config_targets({}) == (set(), {})
    assert cleanup.config_targets({"pool": ["x", None], "objects": {"p": None}}) == (set(), {})


def test_title_matching_includes_the_witness_label_but_not_lookalikes():
    assert cleanup.title_matches("A tea table")
    assert cleanup.title_matches("A witness waits here \N{EM DASH} Gaming")
    assert not cleanup.title_matches("A tea table for two")
    assert not cleanup.title_matches("Pixel Canvas")
    assert not cleanup.title_matches(None)


async def test_dry_run_reports_but_unpins_nothing():
    pinned = Msg(11, title="A pocket watch")
    room = Room(1, [pinned])
    report = await cleanup.run(Guild(room), BOT, DATA, confirm=False)
    assert pinned.pinned and room.edits == []
    assert report.found == 1 and report.unpinned == 0 and report.rooms == 1
    assert "Dry run" in report.summary(False)


async def test_confirm_unpins_recorded_and_forgotten_hunt_messages():
    recorded = Msg(11, title="A pocket watch")
    forgotten = Msg(12, title="A pocket watch")       # an older object the config lost track of
    poem = Msg(22, title="Hung where the Queen forgot it")
    report = await cleanup.run(Guild(Room(1, [recorded, forgotten]), Room(2, [poem])), BOT, DATA, confirm=True)
    assert not recorded.pinned and not forgotten.pinned and not poem.pinned
    assert report.found == 3 and report.unpinned == 3 and report.failed == 0 and report.rooms == 2


async def test_other_bot_pins_and_other_peoples_pins_are_left_alone():
    canvas = Msg(90, title="Pixel Canvas")                          # another cog's pin, same bot, same room
    plain = Msg(91)                                                  # a bot pin with no embed
    impostor = Msg(92, author=7, title="A pocket watch")             # a member's message that merely looks like one
    mine = Msg(11, title="A pocket watch")
    report = await cleanup.run(Guild(Room(1, [canvas, plain, impostor, mine])), BOT, DATA, confirm=True)
    assert canvas.pinned and plain.pinned and impostor.pinned
    assert not mine.pinned and report.unpinned == 1


async def test_a_recorded_id_is_unpinned_even_if_its_embed_is_gone_but_only_if_the_bot_posted_it():
    ours = Msg(11)
    theirs = Msg(11, author=7)
    assert cleanup.is_hunt_pin(ours, BOT, {11}) and not cleanup.is_hunt_pin(theirs, BOT, {11})


async def test_archived_forum_post_is_reopened_then_archived_again():
    msg = Msg(31, title="A tea table")
    room = Room(3, [msg], archived=True)
    report = await cleanup.run(Guild(room), BOT, DATA, confirm=True)
    assert not msg.pinned and room.edits == [False, True] and room.archived
    assert report.unpinned == 1


async def test_a_room_without_hunt_pins_is_not_touched_or_counted():
    room = Room(3, [Msg(90, title="Pixel Canvas")], archived=True)
    report = await cleanup.run(Guild(room), BOT, DATA, confirm=True)
    assert room.edits == [] and report.found == 0 and report.rooms == 0


async def test_a_missing_room_is_reported_and_the_rest_still_runs():
    msg = Msg(11, title="A pocket watch")
    report = await cleanup.run(Guild(Room(1, [msg])), BOT, DATA, confirm=True)
    assert not msg.pinned
    assert any("not found" in line for line in report.lines)


async def test_a_failed_unpin_is_counted_not_fatal():
    bad = BadMsg(11, title="A pocket watch")
    good = Msg(12, title="A pocket watch")
    report = await cleanup.run(Guild(Room(1, [bad, good])), BOT, DATA, confirm=True)
    assert report.unpinned == 1 and report.failed == 1 and not good.pinned and bad.pinned
    assert "1 failed" in report.summary(True)


@pytest.mark.parametrize("iterator,wrap", [(False, False), (True, False), (True, True)])
async def test_every_discordpy_pins_shape_works(iterator, wrap):
    msg = Msg(11, title="A pocket watch")
    report = await cleanup.run(Guild(Room(1, [msg], iterator=iterator, wrap=wrap)), BOT, DATA, confirm=True)
    assert not msg.pinned and report.unpinned == 1


async def test_running_it_twice_is_harmless():
    msg = Msg(11, title="A pocket watch")
    guild = Guild(Room(1, [msg]))
    await cleanup.run(guild, BOT, DATA, confirm=True)
    again = await cleanup.run(guild, BOT, DATA, confirm=True)
    assert again.found == 0 and again.unpinned == 0
