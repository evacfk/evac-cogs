"""Import-smoke test for cardcollect.py (the redbot/discord-facing main cog
file) under the conftest.py redbot stub -- same lesson photodrop's bug #10
taught this project: a cog file that only ever gets exercised inside a real
running bot can still ship a plain import error, so it needs at least an
import-level test even without the real dependency installed.

Beyond the bare import, this also drives a full drop -> claim cycle through
the actual cog code (message listener -> _post_drop -> reaction handling ->
_resolve_claim_window) against lightweight fake discord objects, since
that's the newest and most bug-prone part of this cog.
"""

import asyncio
import io

import pytest
from PIL import Image

import cardcollect.cardcollect as cc_module
from cardcollect import storage
from cardcollect.models import Card


def test_cardcollect_module_imports_cleanly():
    assert hasattr(cc_module, "CardCollect")


# ---------------------------------------------------------------------------
# fake discord objects -- just enough surface for the code paths exercised
# below, not a full discord.py emulation
# ---------------------------------------------------------------------------


class FakeUser:
    def __init__(self, id_, bot=False):
        self.id = id_
        self.bot = bot

    @property
    def mention(self):
        return f"<@{self.id}>"


class FakeMember(FakeUser):
    def __init__(self, id_, guild, bot=False):
        super().__init__(id_, bot=bot)
        self.guild = guild


class FakeGuild:
    def __init__(self, id_, name="Test Guild"):
        self.id = id_
        self.name = name
        self.members = {}

    def get_member(self, member_id):
        return self.members.get(member_id)

    def get_channel(self, channel_id):
        return None


class FakeReaction:
    def __init__(self, emoji):
        self.emoji = emoji
        self._reactor_objs = []

    async def users(self):
        # a real suspension point (asyncio.sleep(0)), not just an `async
        # def` with no internal await -- without this, awaiting a coroutine
        # that never itself suspends runs to completion without ever
        # ceding control back to the event loop, so two "concurrent" tasks
        # started via asyncio.gather would never actually interleave and a
        # race condition in the code under test could never be observed
        await asyncio.sleep(0)
        for u in self._reactor_objs:
            yield u


class FakeMessage:
    def __init__(self, id_, channel, content=None, embed=None, file=None):
        self.id = id_
        self.channel = channel
        self.content = content
        self.embeds = [embed] if embed else []
        self.files = [file] if file else []
        self.reactions = []

    async def add_reaction(self, emoji):
        self.reactions.append(FakeReaction(emoji))


class FakeChannel:
    def __init__(self, id_, guild):
        self.id = id_
        self.guild = guild
        self._messages = {}
        self._next_id = 1
        self.sent = []

    @property
    def mention(self):
        return f"<#{self.id}>"

    async def send(self, content=None, embed=None, file=None):
        msg = FakeMessage(self._next_id, self, content=content, embed=embed, file=file)
        self._messages[msg.id] = msg
        self._next_id += 1
        self.sent.append(msg)
        return msg

    async def fetch_message(self, message_id):
        await asyncio.sleep(0)  # real suspension point, see FakeReaction.users
        return self._messages[message_id]


class FakeBot:
    def __init__(self):
        self.user = FakeUser(999999999)
        self._channels = {}

    def register_channel(self, channel):
        self._channels[channel.id] = channel

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)


class FakeAttachment:
    def __init__(self, data: bytes):
        self._data = data

    async def read(self):
        return self._data


class FakeCtxMessage:
    def __init__(self, attachments=None):
        self.attachments = attachments or []


class FakeCtx:
    def __init__(self, author, guild, channel, attachments=None):
        self.author = author
        self.guild = guild
        self.channel = channel
        self.message = FakeCtxMessage(attachments)
        self.sent = []

    async def send(self, *args, **kwargs):
        msg = await self.channel.send(*args, **kwargs)
        self.sent.append(msg)
        return msg


def fake_art_bytes(color=(120, 40, 40)):
    img = Image.new("RGB", (400, 560), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------


@pytest.fixture
def cog():
    bot = FakeBot()
    c = cc_module.CardCollect(bot)
    return c


@pytest.mark.asyncio
async def test_settings_and_setchannel_commands(cog):
    guild = FakeGuild(1)
    author = FakeMember(10, guild)
    channel = FakeChannel(100, guild)
    cog.bot.register_channel(channel)
    ctx = FakeCtx(author, guild, channel)

    await cog.card.commands["setchannel"].callback(cog, ctx, channel)
    assert "will post" in ctx.sent[-1].content

    await cog.card.commands["settings"].callback(cog, ctx)
    embed = ctx.sent[-1].embeds[0]
    assert embed.title.startswith("cardcollect settings")


@pytest.mark.asyncio
async def test_addcard_then_empty_collection_view(cog):
    guild = FakeGuild(2)
    author = FakeMember(20, guild)
    channel = FakeChannel(200, guild)
    ctx = FakeCtx(author, guild, channel, attachments=[FakeAttachment(fake_art_bytes())])

    await cog.card.commands["addcard"].callback(cog, ctx, "rare", name_and_series="Alice | Some Anime")
    embed = ctx.sent[-1].embeds[0]
    assert embed.title == "Character added to pool"

    pool = await cog.config.guild(guild).pool()
    assert len(pool) == 1
    card_id = int(next(iter(pool)))
    assert storage.card_image_exists(cog.data_path, guild.id, card_id)

    # this member owns nothing yet
    await cog.card.callback(cog, ctx)
    assert "haven't claimed" in ctx.sent[-1].content


@pytest.mark.asyncio
async def test_full_drop_and_claim_cycle_real_mode(cog):
    guild = FakeGuild(3)
    admin = FakeMember(30, guild)
    winner = FakeMember(31, guild)
    other_reactor = FakeMember(32, guild)
    guild.members = {30: admin, 31: winner, 32: other_reactor}

    channel = FakeChannel(300, guild)
    cog.bot.register_channel(channel)
    ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes((10, 200, 10)))])

    await cog.card.commands["addcard"].callback(cog, ctx, "legendary", name_and_series="Lux | Some Anime")

    # force an immediate real (non-test) drop, bypassing the chance/cooldown gate
    await cog._post_drop(channel, guild, is_test=False)
    assert channel.sent, "drop should have posted a message"

    drop_message = channel.sent[-1]
    drop = cog.active_drops[drop_message.id]
    assert len(drop.cards) >= 1
    real_emoji = drop.cards[0]["emoji"]
    card_id = drop.cards[0]["card_id"]

    reaction = next(r for r in drop_message.reactions if r.emoji == real_emoji)
    reaction._reactor_objs = [winner, other_reactor]

    await cog._resolve_claim_window(channel.id, drop_message.id, real_emoji, window=0)

    # resolution picks randomly among everyone who reacted within the
    # window (that's the whole point of the claim-fairness mechanic), so
    # either eligible reactor could have won -- check exactly one did
    winner_state = await cog._member_state(winner)
    other_state = await cog._member_state(other_reactor)
    winners = [s for s in (winner_state, other_state) if card_id in s.collection]
    assert len(winners) == 1

    result_embed = channel.sent[-1].embeds[0]
    assert result_embed.title == "New card claimed!"


@pytest.mark.asyncio
async def test_test_mode_drop_awards_nothing(cog):
    guild = FakeGuild(4)
    admin = FakeMember(40, guild)
    winner = FakeMember(41, guild)
    guild.members = {40: admin, 41: winner}

    channel = FakeChannel(400, guild)
    cog.bot.register_channel(channel)
    ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes((10, 10, 200)))])

    await cog.card.commands["addcard"].callback(cog, ctx, "common", name_and_series="Nova | Some Anime")

    await cog._post_drop(channel, guild, is_test=True)
    drop_message = channel.sent[-1]
    drop = cog.active_drops[drop_message.id]
    assert drop.is_test is True

    real_emoji = drop.cards[0]["emoji"]
    reaction = next(r for r in drop_message.reactions if r.emoji == real_emoji)
    reaction._reactor_objs = [winner]

    await cog._resolve_claim_window(channel.id, drop_message.id, real_emoji, window=0)

    state = await cog._member_state(winner)
    assert state.collection == []  # test mode: nothing awarded

    result_embed = channel.sent[-1].embeds[0]
    assert "Test" in result_embed.title


@pytest.mark.asyncio
async def test_duplicate_claim_becomes_sell_token(cog):
    guild = FakeGuild(5)
    admin = FakeMember(50, guild)
    winner = FakeMember(51, guild)
    guild.members = {50: admin, 51: winner}

    channel = FakeChannel(500, guild)
    cog.bot.register_channel(channel)
    ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes((200, 200, 10)))])
    await cog.card.commands["addcard"].callback(cog, ctx, "epic", name_and_series="Rin | Some Anime")

    pool = await cog.config.guild(guild).pool()
    card_id = int(next(iter(pool)))

    # give the winner the card already, directly, then have them "claim" it again
    from cardcollect.models import MemberState

    state = MemberState(collection=[card_id])
    await cog._save_member_state(winner, state)

    await cog._post_drop(channel, guild, is_test=False)
    drop_message = channel.sent[-1]
    drop = cog.active_drops[drop_message.id]
    # force the drop's only card to be the one the winner already owns, so
    # this test deterministically exercises the dupe path regardless of the
    # random tier/card roll
    drop.cards[0]["card_id"] = card_id
    real_emoji = drop.cards[0]["emoji"]
    reaction = next(r for r in drop_message.reactions if r.emoji == real_emoji)
    reaction._reactor_objs = [winner]

    await cog._resolve_claim_window(channel.id, drop_message.id, real_emoji, window=0)

    final_state = await cog._member_state(winner)
    assert final_state.collection == [card_id]  # no duplicate tile
    assert len(final_state.sell_tokens) == 1
    assert final_state.sell_tokens[0].card_id == card_id

    result_embed = channel.sent[-1].embeds[0]
    assert "Duplicate" in result_embed.title


@pytest.mark.asyncio
async def test_removecard_retires_but_existing_owners_keep_their_gallery_tile(cog):
    """Locked decision #17: removecard drops a character from future rolls,
    but a member who already owns it keeps their copy -- which means the
    pool entry and its art must survive, not get deleted outright."""
    guild = FakeGuild(6)
    admin = FakeMember(60, guild)
    owner = FakeMember(61, guild)
    guild.members = {60: admin, 61: owner}

    channel = FakeChannel(600, guild)
    ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes((80, 80, 200)))])
    await cog.card.commands["addcard"].callback(cog, ctx, "rare", name_and_series="Mira | Some Anime")

    pool = await cog.config.guild(guild).pool()
    card_id = int(next(iter(pool)))

    from cardcollect.models import MemberState

    await cog._save_member_state(owner, MemberState(collection=[card_id]))

    await cog.card.commands["removecard"].callback(cog, ctx, card_id)

    # art must still be on disk, and the card must still resolve by id --
    # otherwise the owner's gallery silently drops the tile
    assert storage.card_image_exists(cog.data_path, guild.id, card_id)
    card = await cog._card_by_id(guild, card_id)
    assert card is not None
    assert card.retired is True

    # but it must no longer be eligible to actually drop
    rollable = await cog._rollable_pool_cards(guild)
    assert card_id not in [c.card_id for c in rollable]

    # and the owner's gallery render must still succeed (would raise/skip
    # the card if the art or pool entry were gone)
    owner_ctx = FakeCtx(owner, guild, channel)
    await cog.card.callback(cog, owner_ctx)
    assert owner_ctx.sent[-1].files, "gallery image should still render for the retired card"


@pytest.mark.asyncio
async def test_concurrent_resolution_calls_never_double_award(cog):
    """Regression test: _resolve_claim_window used to mark
    drop.claimed_positions only after its fetch_message/reaction.users()
    awaits, leaving a window where a reaction arriving mid-resolution could
    race a second, concurrent resolution of the same card. It's now marked
    synchronously before the first await, so a second call for the same
    (message, emoji) must see the position already claimed and no-op."""
    guild = FakeGuild(7)
    admin = FakeMember(70, guild)
    # two DISTINCT reactors, deliberately -- using the same reactor twice
    # would let the unrelated claim-cooldown mechanism incidentally mask
    # this race (the second resolution's reactor list would come back empty
    # once the first sets that user's cooldown, hiding the actual bug this
    # test is for)
    reactor1 = FakeMember(71, guild)
    reactor2 = FakeMember(72, guild)
    guild.members = {70: admin, 71: reactor1, 72: reactor2}

    channel = FakeChannel(700, guild)
    cog.bot.register_channel(channel)
    ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes((10, 100, 220)))])
    await cog.card.commands["addcard"].callback(cog, ctx, "common", name_and_series="Yui | Some Anime")

    await cog._post_drop(channel, guild, is_test=False)
    drop_message = channel.sent[-1]
    real_emoji = cog.active_drops[drop_message.id].cards[0]["emoji"]
    reaction = next(r for r in drop_message.reactions if r.emoji == real_emoji)
    reaction._reactor_objs = [reactor1, reactor2]

    messages_before = len(channel.sent)

    # simulate two reaction events for the same card racing each other, as
    # if a second reaction had arrived while the first resolution was still
    # awaiting fetch_message/reaction.users()
    await asyncio.gather(
        cog._resolve_claim_window(channel.id, drop_message.id, real_emoji, window=0),
        cog._resolve_claim_window(channel.id, drop_message.id, real_emoji, window=0),
    )

    state1 = await cog._member_state(reactor1)
    state2 = await cog._member_state(reactor2)
    total_copies = len(state1.collection) + len(state2.collection)
    assert total_copies == 1, f"card must be awarded exactly once total, got {total_copies}"
    # a redundant resolution must not mint a spurious sell token for
    # whichever reactor it would have (incorrectly) picked, either
    assert state1.sell_tokens == [] and state2.sell_tokens == []

    new_messages = channel.sent[messages_before:]
    assert len(new_messages) == 1, f"expected exactly one claim result message, got {len(new_messages)}"
