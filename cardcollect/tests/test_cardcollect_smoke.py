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
import time

import discord
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
        self.dms = []  # content of every .send() call, for asserting DM behavior

    @property
    def mention(self):
        return f"<@{self.id}>"

    async def send(self, content=None, **kwargs):
        self.dms.append(content)
        return content


class FakeMember(FakeUser):
    def __init__(self, id_, guild, bot=False, display_name=None):
        super().__init__(id_, bot=bot)
        self.guild = guild
        self.display_name = display_name or f"Member{id_}"


class FakeGuild:
    def __init__(self, id_, name="Test Guild"):
        self.id = id_
        self.name = name
        self.members = {}
        self.channels = {}

    def get_member(self, member_id):
        return self.members.get(member_id)

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)


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
        self.fetch_message_calls = 0
        guild.channels[id_] = self

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
        self.fetch_message_calls += 1
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


class FakeRawReactionPayload:
    """Duck-typed stand-in for discord.RawReactionActionEvent -- only the
    attributes on_raw_reaction_add actually reads."""

    def __init__(self, guild_id, user_id, message_id, channel_id, emoji):
        self.guild_id = guild_id
        self.user_id = user_id
        self.message_id = message_id
        self.channel_id = channel_id
        self.emoji = emoji


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
async def test_first_duplicate_claim_becomes_a_tradeable_spare(cog):
    """Locked decision: MAX_COPIES_KEPT=2 -- a member's *first* duplicate of
    a card becomes a real second collection entry (a tradeable spare, not a
    sell token), so `.card give` has something to actually hand off."""
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
    assert final_state.collection == [card_id, card_id]  # a real second copy, not a sell token
    assert final_state.sell_tokens == []

    result_embed = channel.sent[-1].embeds[0]
    assert "Duplicate" in result_embed.title
    assert "spare" in result_embed.title.lower()


@pytest.mark.asyncio
async def test_claim_at_the_duplicate_cap_becomes_a_sell_token(cog):
    """A 3rd claim of the same card (already at MAX_COPIES_KEPT=2) converts
    straight to a sell token instead of piling up more duplicates."""
    guild = FakeGuild(24)
    admin = FakeMember(240, guild)
    winner = FakeMember(241, guild)
    guild.members = {240: admin, 241: winner}

    channel = FakeChannel(2400, guild)
    cog.bot.register_channel(channel)
    ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes((200, 10, 200)))])
    await cog.card.commands["addcard"].callback(cog, ctx, "epic", name_and_series="Cap | Some Anime")

    pool = await cog.config.guild(guild).pool()
    card_id = int(next(iter(pool)))

    from cardcollect.models import MemberState

    # winner is already at the cap (2 copies)
    state = MemberState(collection=[card_id, card_id])
    await cog._save_member_state(winner, state)

    await cog._post_drop(channel, guild, is_test=False)
    drop_message = channel.sent[-1]
    drop = cog.active_drops[drop_message.id]
    drop.cards[0]["card_id"] = card_id
    real_emoji = drop.cards[0]["emoji"]
    reaction = next(r for r in drop_message.reactions if r.emoji == real_emoji)
    reaction._reactor_objs = [winner]

    await cog._resolve_claim_window(channel.id, drop_message.id, real_emoji, window=0)

    final_state = await cog._member_state(winner)
    assert final_state.collection == [card_id, card_id]  # unchanged, no 3rd copy
    assert len(final_state.sell_tokens) == 1
    assert final_state.sell_tokens[0].card_id == card_id

    result_embed = channel.sent[-1].embeds[0]
    assert "sell token" in result_embed.title.lower()


@pytest.mark.asyncio
async def test_claim_at_the_cap_still_counts_against_daily_quota(cog):
    """A sell-token (at-cap) claim still burns quota, same as Mudae/Karuta
    rate-limit claim *attempts* against the pool, not just new pickups (see
    the comment above the daily_claims increment in
    _resolve_claim_window) -- otherwise a maxed-out member could keep
    re-rolling a card they already have two of forever without it ever
    counting against them."""
    guild = FakeGuild(22)
    admin = FakeMember(220, guild)
    winner = FakeMember(221, guild)
    guild.members = {220: admin, 221: winner}

    channel = FakeChannel(2200, guild)
    cog.bot.register_channel(channel)
    ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes((5, 5, 5)))])
    await cog.card.commands["addcard"].callback(cog, ctx, "epic", name_and_series="Dupe | Some Anime")
    await cog.config.guild(guild).claim_quota.set(5)

    pool = await cog.config.guild(guild).pool()
    card_id = int(next(iter(pool)))

    from cardcollect.models import MemberState

    # already at the duplicate cap (2 copies)
    await cog._save_member_state(winner, MemberState(collection=[card_id, card_id]))

    await cog._post_drop(channel, guild, is_test=False)
    drop_message = channel.sent[-1]
    drop = cog.active_drops[drop_message.id]
    drop.cards[0]["card_id"] = card_id
    real_emoji = drop.cards[0]["emoji"]
    reaction = next(r for r in drop_message.reactions if r.emoji == real_emoji)
    reaction._reactor_objs = [winner]

    await cog._resolve_claim_window(channel.id, drop_message.id, real_emoji, window=0)

    final_state = await cog._member_state(winner)
    assert len(final_state.sell_tokens) == 1  # at-cap path, not a new pickup or a 3rd copy
    assert final_state.daily_claims == 1, "an at-cap claim must still burn quota"


@pytest.mark.asyncio
async def test_first_duplicate_claim_also_counts_against_daily_quota(cog):
    """Same as the at-cap case above, but for the *first* duplicate (kept as
    a tradeable spare rather than converted to a sell token) -- that must
    burn quota too, not just sell-token conversions."""
    guild = FakeGuild(25)
    admin = FakeMember(250, guild)
    winner = FakeMember(251, guild)
    guild.members = {250: admin, 251: winner}

    channel = FakeChannel(2500, guild)
    cog.bot.register_channel(channel)
    ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes((5, 6, 7)))])
    await cog.card.commands["addcard"].callback(cog, ctx, "epic", name_and_series="Spare | Some Anime")
    await cog.config.guild(guild).claim_quota.set(5)

    pool = await cog.config.guild(guild).pool()
    card_id = int(next(iter(pool)))

    from cardcollect.models import MemberState

    await cog._save_member_state(winner, MemberState(collection=[card_id]))

    await cog._post_drop(channel, guild, is_test=False)
    drop_message = channel.sent[-1]
    drop = cog.active_drops[drop_message.id]
    drop.cards[0]["card_id"] = card_id
    real_emoji = drop.cards[0]["emoji"]
    reaction = next(r for r in drop_message.reactions if r.emoji == real_emoji)
    reaction._reactor_objs = [winner]

    await cog._resolve_claim_window(channel.id, drop_message.id, real_emoji, window=0)

    final_state = await cog._member_state(winner)
    assert final_state.collection == [card_id, card_id]
    assert final_state.sell_tokens == []
    assert final_state.daily_claims == 1, "a spare-duplicate claim must still burn quota"


@pytest.mark.asyncio
async def test_showcase_add_rejects_a_pool_card_the_member_does_not_own(cog):
    """Distinct from the 'card doesn't exist at all' rejection: this is a
    real pool card that simply isn't in this member's collection yet."""
    guild = FakeGuild(23)
    admin = FakeMember(230, guild)
    non_owner = FakeMember(231, guild)
    guild.members = {230: admin, 231: non_owner}
    channel = FakeChannel(2300, guild)

    ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes((1, 2, 3)))])
    await cog.card.commands["addcard"].callback(cog, ctx, "common", name_and_series="Unowned | S")
    pool = await cog.config.guild(guild).pool()
    card_id = int(next(iter(pool)))

    add_ctx = FakeCtx(non_owner, guild, channel)
    await cog.card.commands["showcase"].commands["add"].callback(cog, add_ctx, card_arg=str(card_id))
    assert "don't own" in add_ctx.sent[-1].content.lower()

    state = await cog._member_state(non_owner)
    assert state.showcase_card_ids == []


@pytest.mark.asyncio
async def test_card_gallery_viewable_for_another_member(cog):
    guild = FakeGuild(26)
    admin = FakeMember(260, guild)
    owner = FakeMember(261, guild, display_name="Nia")
    empty_member = FakeMember(262, guild, display_name="Empty")
    guild.members = {260: admin, 261: owner, 262: empty_member}
    channel = FakeChannel(2600, guild)

    ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes((11, 22, 33)))])
    await cog.card.commands["addcard"].callback(cog, ctx, "common", name_and_series="Viewed | S")
    pool = await cog.config.guild(guild).pool()
    card_id = int(next(iter(pool)))

    from cardcollect.models import MemberState

    await cog._save_member_state(owner, MemberState(collection=[card_id]))

    # own gallery: no caption
    own_ctx = FakeCtx(owner, guild, channel)
    await cog.card.callback(cog, own_ctx, member=None)
    assert own_ctx.sent[-1].content is None
    assert own_ctx.sent[-1].files

    # someone else's gallery: captioned with their name
    other_ctx = FakeCtx(admin, guild, channel)
    await cog.card.callback(cog, other_ctx, member=owner)
    assert "nia" in other_ctx.sent[-1].content.lower()
    assert other_ctx.sent[-1].files

    # a member with nothing claimed yet gets a clear message, not an error
    empty_ctx = FakeCtx(admin, guild, channel)
    await cog.card.callback(cog, empty_ctx, member=empty_member)
    assert "hasn't claimed" in empty_ctx.sent[-1].content.lower()


@pytest.mark.asyncio
async def test_card_showcase_viewable_for_another_member_with_tier_breakdown(cog):
    guild = FakeGuild(27)
    admin = FakeMember(270, guild)
    owner = FakeMember(271, guild, display_name="Kess")
    guild.members = {270: admin, 271: owner}
    channel = FakeChannel(2700, guild)

    card_ids = []
    for i, rarity in enumerate(["common", "common", "rare", "legendary"]):
        ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes((i, i, i)))])
        await cog.card.commands["addcard"].callback(cog, ctx, rarity, name_and_series=f"T{i} | S")
    pool = await cog.config.guild(guild).pool()
    card_ids = sorted(int(cid) for cid in pool)

    from cardcollect.models import MemberState

    await cog._save_member_state(owner, MemberState(collection=card_ids, showcase_card_ids=[card_ids[0]]))

    # viewed by the owner themself
    own_ctx = FakeCtx(owner, guild, channel)
    await cog.card_showcase.callback(cog, own_ctx, member=None)
    text = own_ctx.sent[-1].content.lower()
    assert "2 common" in text and "1 rare" in text and "1 legendary" in text and "0 epic" in text
    assert "(4 total)" in text

    # viewed by someone else
    other_ctx = FakeCtx(admin, guild, channel)
    await cog.card_showcase.callback(cog, other_ctx, member=owner)
    other_text = other_ctx.sent[-1].content.lower()
    assert "kess" in other_text
    assert "2 common" in other_text


@pytest.mark.asyncio
async def test_card_showcase_empty_collection_shows_no_tier_line(cog):
    guild = FakeGuild(28)
    admin = FakeMember(280, guild)
    channel = FakeChannel(2800, guild)
    ctx = FakeCtx(admin, guild, channel)
    await cog.card_showcase.callback(cog, ctx, member=None)
    assert "Collection:" not in ctx.sent[-1].content


@pytest.mark.asyncio
async def test_card_give_keeps_showcase_when_giver_still_holds_a_spare(cog):
    guild = FakeGuild(29)
    admin = FakeMember(290, guild)
    giver = FakeMember(291, guild)
    receiver = FakeMember(292, guild)
    guild.members = {290: admin, 291: giver, 292: receiver}
    channel = FakeChannel(2900, guild)

    ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes((40, 40, 40)))])
    await cog.card.commands["addcard"].callback(cog, ctx, "rare", name_and_series="Spare | S")
    pool = await cog.config.guild(guild).pool()
    card_id = int(next(iter(pool)))

    from cardcollect.models import MemberState

    # giver holds 2 copies, showcased
    await cog._save_member_state(
        giver, MemberState(collection=[card_id, card_id], showcase_card_ids=[card_id])
    )

    give_ctx = FakeCtx(giver, guild, channel)
    await cog.card.commands["give"].callback(cog, give_ctx, receiver, card_arg=str(card_id))

    giver_state = await cog._member_state(giver)
    assert giver_state.collection == [card_id]  # one copy left
    assert giver_state.showcase_card_ids == [card_id]  # still showcased -- they still own one

    receiver_state = await cog._member_state(receiver)
    assert receiver_state.collection == [card_id]


@pytest.mark.asyncio
async def test_card_give_removes_showcase_entry_when_it_was_the_last_copy(cog):
    guild = FakeGuild(30)
    admin = FakeMember(300, guild)
    giver = FakeMember(301, guild)
    receiver = FakeMember(302, guild)
    guild.members = {300: admin, 301: giver, 302: receiver}
    channel = FakeChannel(3000, guild)

    ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes((50, 50, 50)))])
    await cog.card.commands["addcard"].callback(cog, ctx, "rare", name_and_series="Last | S")
    pool = await cog.config.guild(guild).pool()
    card_id = int(next(iter(pool)))

    from cardcollect.models import MemberState

    await cog._save_member_state(giver, MemberState(collection=[card_id], showcase_card_ids=[card_id]))

    give_ctx = FakeCtx(giver, guild, channel)
    await cog.card.commands["give"].callback(cog, give_ctx, receiver, card_arg=str(card_id))

    giver_state = await cog._member_state(giver)
    assert giver_state.collection == []
    assert giver_state.showcase_card_ids == []  # no longer own any copy -- removed


@pytest.mark.asyncio
async def test_card_give_converts_to_sell_token_when_receiver_at_duplicate_cap(cog):
    guild = FakeGuild(31)
    admin = FakeMember(310, guild)
    giver = FakeMember(311, guild)
    receiver = FakeMember(312, guild)
    guild.members = {310: admin, 311: giver, 312: receiver}
    channel = FakeChannel(3100, guild)

    ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes((60, 60, 60)))])
    await cog.card.commands["addcard"].callback(cog, ctx, "rare", name_and_series="Capped | S")
    pool = await cog.config.guild(guild).pool()
    card_id = int(next(iter(pool)))

    from cardcollect.models import MemberState

    await cog._save_member_state(giver, MemberState(collection=[card_id]))
    # receiver already at the cap
    await cog._save_member_state(receiver, MemberState(collection=[card_id, card_id]))

    give_ctx = FakeCtx(giver, guild, channel)
    await cog.card.commands["give"].callback(cog, give_ctx, receiver, card_arg=str(card_id))

    receiver_state = await cog._member_state(receiver)
    assert receiver_state.collection == [card_id, card_id]  # unchanged, no 3rd copy
    assert len(receiver_state.sell_tokens) == 1
    assert "duplicate limit" in give_ctx.sent[-1].content.lower()


@pytest.mark.asyncio
async def test_card_give_to_a_receiver_below_cap_becomes_a_real_second_copy(cog):
    """Distinct from the at-cap case above: a receiver who already owns
    exactly one copy (below MAX_COPIES_KEPT) must get a real, tradeable
    second copy from the gift -- not have it silently converted to a sell
    token just because they already own one."""
    guild = FakeGuild(32)
    admin = FakeMember(320, guild)
    giver = FakeMember(321, guild)
    receiver = FakeMember(322, guild)
    guild.members = {320: admin, 321: giver, 322: receiver}
    channel = FakeChannel(3200, guild)

    ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes((70, 70, 70)))])
    await cog.card.commands["addcard"].callback(cog, ctx, "rare", name_and_series="Below | S")
    pool = await cog.config.guild(guild).pool()
    card_id = int(next(iter(pool)))

    from cardcollect.models import MemberState

    await cog._save_member_state(giver, MemberState(collection=[card_id]))
    # receiver owns exactly one -- below the cap, room for a spare
    await cog._save_member_state(receiver, MemberState(collection=[card_id]))

    give_ctx = FakeCtx(giver, guild, channel)
    await cog.card.commands["give"].callback(cog, give_ctx, receiver, card_arg=str(card_id))

    receiver_state = await cog._member_state(receiver)
    assert receiver_state.collection == [card_id, card_id]  # a real second copy
    assert receiver_state.sell_tokens == []


@pytest.mark.asyncio
async def test_importpool_queues_behind_a_running_import_and_warns(cog, monkeypatch):
    """Regression test for the AniList 429 issue: two `.card importpool`
    runs started close together used to each start their own paging loop
    concurrently, multiplying the request rate against AniList's rate limit.
    A second run must now queue behind the first (via cog._importpool_lock)
    rather than run in parallel, and tell the admin it's queued rather than
    silently appearing to do nothing."""
    guild = FakeGuild(33)
    admin = FakeMember(330, guild)
    channel = FakeChannel(3300, guild)
    ctx = FakeCtx(admin, guild, channel)

    async def fake_fetch(*args, **kwargs):
        return []

    monkeypatch.setattr(cc_module.import_characters, "fetch_top_female_characters", fake_fetch)

    # simulate a first import already in flight
    await cog._importpool_lock.acquire()
    task = asyncio.create_task(cog.card.commands["importpool"].callback(cog, ctx, count=5))
    await asyncio.sleep(0)  # let the second call reach (and block on) the lock
    assert "already running" in ctx.sent[-1].content.lower()
    assert cog._importpool_lock.locked()

    # release the first import -- the queued second one should now proceed
    cog._importpool_lock.release()
    await task
    assert "no characters came back" in ctx.sent[-1].content.lower()


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

    await cog.card.commands["removecard"].callback(cog, ctx, card_ids=str(card_id))

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
async def test_removecard_accepts_multiple_ids_and_reports_not_found(cog):
    guild = FakeGuild(12)
    admin = FakeMember(120, guild)
    channel = FakeChannel(1200, guild)
    ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes((1, 1, 1)))])
    await cog.card.commands["addcard"].callback(cog, ctx, "common", name_and_series="A | S")
    ctx2 = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes((2, 2, 2)))])
    await cog.card.commands["addcard"].callback(cog, ctx2, "common", name_and_series="B | S")
    ctx3 = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes((3, 3, 3)))])
    await cog.card.commands["addcard"].callback(cog, ctx3, "common", name_and_series="C | S")

    pool = await cog.config.guild(guild).pool()
    ids = sorted(int(cid) for cid in pool)
    id_a, id_b, id_c = ids

    remove_ctx = FakeCtx(admin, guild, channel)
    # remove two of the three real IDs plus one that doesn't exist
    await cog.card.commands["removecard"].callback(
        cog, remove_ctx, card_ids=f"{id_a} {id_b} 99999"
    )

    embed = remove_ctx.sent[-1].embeds[0]
    field_names = [f.name for f in embed.fields]
    assert any("Retired (2)" in n for n in field_names)
    assert any("Not found (1)" in n for n in field_names)

    rollable_ids = {c.card_id for c in await cog._rollable_pool_cards(guild)}
    assert id_a not in rollable_ids
    assert id_b not in rollable_ids
    assert id_c in rollable_ids  # untouched


@pytest.mark.asyncio
async def test_viewpool_lists_every_card_including_retired(cog):
    guild = FakeGuild(13)
    admin = FakeMember(130, guild)
    channel = FakeChannel(1300, guild)
    ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes((10, 10, 10)))])
    await cog.card.commands["addcard"].callback(cog, ctx, "rare", name_and_series="Keeper | Show")
    ctx2 = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes((20, 20, 20)))])
    await cog.card.commands["addcard"].callback(cog, ctx2, "epic", name_and_series="Gone | Show")

    pool = await cog.config.guild(guild).pool()
    gone_id = int(sorted(pool, key=int)[1])
    await cog.card.commands["removecard"].callback(cog, ctx, card_ids=str(gone_id))

    view_ctx = FakeCtx(admin, guild, channel)
    await cog.card.commands["viewpool"].callback(cog, view_ctx)

    sent = view_ctx.sent[-1]
    assert sent.files, "viewpool should attach a file"
    text = sent.files[0].fp.read().decode("utf-8")
    assert "Keeper" in text
    assert "Gone" in text
    assert "[retired]" in text
    assert "2" in sent.content and "1 retired" in sent.content


@pytest.mark.asyncio
async def test_resetpool_requires_confirmation_then_retires_everything(cog):
    guild = FakeGuild(14)
    admin = FakeMember(140, guild)
    channel = FakeChannel(1400, guild)
    ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes((5, 5, 5)))])
    await cog.card.commands["addcard"].callback(cog, ctx, "common", name_and_series="X | S")
    ctx2 = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes((6, 6, 6)))])
    await cog.card.commands["addcard"].callback(cog, ctx2, "rare", name_and_series="Y | S")

    reset_ctx = FakeCtx(admin, guild, channel)
    await cog.card.commands["resetpool"].callback(cog, reset_ctx)
    assert "confirm" in reset_ctx.sent[-1].content.lower()
    # nothing actually retired yet -- still 2 rollable
    assert len(await cog._rollable_pool_cards(guild)) == 2

    confirm_ctx = FakeCtx(admin, guild, channel)
    await cog.card.commands["resetpool"].callback(cog, confirm_ctx, confirm="confirm")
    assert len(await cog._rollable_pool_cards(guild)) == 0

    # existing pool entries still resolve by id (soft delete, not hard delete)
    pool = await cog.config.guild(guild).pool()
    assert len(pool) == 2
    assert all(entry["retired"] for entry in pool.values())


@pytest.mark.asyncio
async def test_daily_claim_quota_excludes_a_reactor_who_already_hit_it(cog, monkeypatch):
    """A member at their daily quota must be excluded from the reactor pool
    entirely, the same way claimed_by/claim_cooldown exclude people -- not
    merely prevented from being picked as winner (that would still let them
    win by being the only eligible reactor)."""
    monkeypatch.setattr(cc_module.engine, "resolve_claim", lambda reactor_ids, rng=None: reactor_ids[0])

    guild = FakeGuild(15)
    admin = FakeMember(150, guild)
    maxed_out = FakeMember(151, guild)
    fresh = FakeMember(152, guild)
    guild.members = {150: admin, 151: maxed_out, 152: fresh}

    channel = FakeChannel(1500, guild)
    cog.bot.register_channel(channel)
    ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes((7, 7, 7)))])
    await cog.card.commands["addcard"].callback(cog, ctx, "common", name_and_series="Q | S")

    await cog.config.guild(guild).claim_quota.set(1)
    await cog.config.guild(guild).claim_cooldown_seconds.set(0)
    # maxed_out already used today's one claim
    from cardcollect import engine as engine_mod

    today = engine_mod.today_str("America/Los_Angeles")
    member_conf = cog.config.member_from_ids(guild.id, maxed_out.id)
    await member_conf.daily_claims.set(1)
    await member_conf.daily_claims_date.set(today)

    await cog._post_drop(channel, guild, is_test=False)
    drop_message = channel.sent[-1]
    real_emoji = cog.active_drops[drop_message.id].cards[0]["emoji"]
    reaction = next(r for r in drop_message.reactions if r.emoji == real_emoji)
    # maxed_out reacts first (would deterministically win under the pinned
    # resolve_claim if not excluded); fresh reacts too
    reaction._reactor_objs = [maxed_out, fresh]

    await cog._resolve_claim_window(channel.id, drop_message.id, real_emoji, window=0)

    maxed_state = await cog._member_state(maxed_out)
    fresh_state = await cog._member_state(fresh)
    assert maxed_state.collection == [], "a member at their daily quota must not win a card"
    assert len(fresh_state.collection) == 1
    assert fresh_state.daily_claims == 1
    assert fresh_state.daily_claims_date == today


@pytest.mark.asyncio
async def test_claim_quota_zero_means_unlimited_and_quota_command_reports_it(cog):
    guild = FakeGuild(16)
    admin = FakeMember(160, guild)
    channel = FakeChannel(1600, guild)
    ctx = FakeCtx(admin, guild, channel)

    await cog.config.guild(guild).claim_quota.set(0)
    await cog.card.commands["quota"].callback(cog, ctx)
    assert "no daily claim limit" in ctx.sent[-1].content.lower()


@pytest.mark.asyncio
async def test_set_claimquota_command_updates_config(cog):
    guild = FakeGuild(17)
    admin = FakeMember(170, guild)
    channel = FakeChannel(1700, guild)
    ctx = FakeCtx(admin, guild, channel)

    await cog.card.commands["set"].commands["claimquota"].callback(cog, ctx, 15)
    assert await cog.config.guild(guild).claim_quota() == 15
    assert "15" in ctx.sent[-1].content

    ctx2 = FakeCtx(admin, guild, channel)
    await cog.card.commands["set"].commands["claimquota"].callback(cog, ctx2, -1)
    assert "negative" in ctx2.sent[-1].content.lower()
    assert await cog.config.guild(guild).claim_quota() == 15  # unchanged


@pytest.mark.asyncio
async def test_showcase_add_remove_and_view(cog):
    guild = FakeGuild(18)
    admin = FakeMember(180, guild)
    owner = FakeMember(181, guild)
    guild.members = {180: admin, 181: owner}

    channel = FakeChannel(1800, guild)
    ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes((30, 30, 30)))])
    await cog.card.commands["addcard"].callback(cog, ctx, "common", name_and_series="Nia | S")
    pool = await cog.config.guild(guild).pool()
    card_id = int(next(iter(pool)))

    from cardcollect.models import MemberState

    await cog._save_member_state(owner, MemberState(collection=[card_id]))

    owner_ctx = FakeCtx(owner, guild, channel)
    await cog.card.commands["showcase"].callback(cog, owner_ctx)
    assert "no showcase" in owner_ctx.sent[-1].content.lower()

    add_ctx = FakeCtx(owner, guild, channel)
    await cog.card.commands["showcase"].commands["add"].callback(cog, add_ctx, card_arg=str(card_id))
    assert "added" in add_ctx.sent[-1].content.lower()

    state = await cog._member_state(owner)
    assert state.showcase_card_ids == [card_id]

    view_ctx = FakeCtx(owner, guild, channel)
    await cog.card.commands["showcase"].callback(cog, view_ctx)
    assert str(card_id) in view_ctx.sent[-1].content

    remove_ctx = FakeCtx(owner, guild, channel)
    await cog.card.commands["showcase"].commands["remove"].callback(cog, remove_ctx, card_arg=str(card_id))
    state2 = await cog._member_state(owner)
    assert state2.showcase_card_ids == []


@pytest.mark.asyncio
async def test_showcase_add_rejects_unowned_card_and_enforces_max_slots(cog):
    guild = FakeGuild(19)
    admin = FakeMember(190, guild)
    owner = FakeMember(191, guild)
    guild.members = {190: admin, 191: owner}
    channel = FakeChannel(1900, guild)

    card_ids = []
    for i in range(4):
        ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes((i * 10, 1, 1)))])
        await cog.card.commands["addcard"].callback(cog, ctx, "common", name_and_series=f"C{i} | S")
    pool = await cog.config.guild(guild).pool()
    card_ids = sorted(int(cid) for cid in pool)

    from cardcollect.models import MemberState

    await cog._save_member_state(owner, MemberState(collection=card_ids))

    unowned_ctx = FakeCtx(owner, guild, channel)
    await cog.card.commands["showcase"].commands["add"].callback(cog, unowned_ctx, card_arg="99999")
    assert "matches" in unowned_ctx.sent[-1].content.lower() or "don't own" in unowned_ctx.sent[-1].content.lower()

    # fill all 3 slots (MAX_SHOWCASE_SLOTS)
    for cid in card_ids[:3]:
        add_ctx = FakeCtx(owner, guild, channel)
        await cog.card.commands["showcase"].commands["add"].callback(cog, add_ctx, card_arg=str(cid))

    full_ctx = FakeCtx(owner, guild, channel)
    await cog.card.commands["showcase"].commands["add"].callback(cog, full_ctx, card_arg=str(card_ids[3]))
    assert "full" in full_ctx.sent[-1].content.lower()

    state = await cog._member_state(owner)
    assert len(state.showcase_card_ids) == 3


@pytest.mark.asyncio
async def test_card_info_reports_rarity_and_owner_count(cog):
    guild = FakeGuild(20)
    admin = FakeMember(200, guild)
    owner1 = FakeMember(201, guild)
    owner2 = FakeMember(202, guild)
    guild.members = {200: admin, 201: owner1, 202: owner2}

    channel = FakeChannel(2000, guild)
    ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes((90, 90, 90)))])
    await cog.card.commands["addcard"].callback(cog, ctx, "legendary", name_and_series="Star | Show")
    pool = await cog.config.guild(guild).pool()
    card_id = int(next(iter(pool)))

    from cardcollect.models import MemberState

    await cog._save_member_state(owner1, MemberState(collection=[card_id]))
    await cog._save_member_state(owner2, MemberState(collection=[card_id]))

    info_ctx = FakeCtx(admin, guild, channel)
    await cog.card.commands["info"].callback(cog, info_ctx, card_arg=str(card_id))

    sent = info_ctx.sent[-1]
    embed = sent.embeds[0]
    assert embed.title == "Star"
    field_values = {f.name: f.value for f in embed.fields}
    assert field_values["Currently owned by"] == "2 members"
    assert sent.files, "card_info should attach the card art"


@pytest.mark.asyncio
async def test_importpool_skips_characters_already_in_the_pool(cog):
    """Regression test for the 'importpool re-adds the same character on a
    second run' gap: an AniList character already in the pool (matched by
    anilist_id) must not be imported again under a new local card_id."""
    guild = FakeGuild(21)
    admin = FakeMember(210, guild)
    channel = FakeChannel(2100, guild)
    ctx = FakeCtx(admin, guild, channel)

    from cardcollect.models import Card as CardModel

    async with cog.config.guild(guild).all() as guild_data:
        guild_data["pool"]["1"] = CardModel(
            card_id=1, name="Existing", series="S", rarity="common",
            image_path="1.png", favourites=100, anilist_id=42,
        ).to_dict()
        guild_data["next_id"] = 2

    class FakeAniListResponse:
        def __init__(self, payload):
            self._payload = payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def raise_for_status(self):
            pass

        async def json(self):
            return self._payload

    class FakeAniListSession:
        def __init__(self, pages):
            self._pages = list(pages)
            self._calls = 0

        def post(self, url, json=None):
            payload = self._pages[min(self._calls, len(self._pages) - 1)]
            self._calls += 1
            return FakeAniListResponse(payload)

        def get(self, url):
            return FakeAniListResponse(None)  # unused directly; image bytes come via aenter override below

    def make_char(id_, name, favourites):
        return {
            "id": id_,
            "name": {"full": name, "native": name},
            "image": {"large": f"https://example.invalid/{id_}.png"},
            "favourites": favourites,
            "gender": "Female",
            "media": {"nodes": [{"title": {"romaji": "S"}}]},
        }

    page = {
        "data": {
            "Page": {
                "pageInfo": {"hasNextPage": False},
                "characters": [make_char(42, "Existing", 100), make_char(43, "Fresh", 100)],
            }
        }
    }

    class FakeImageResponse(FakeAniListResponse):
        def __init__(self):
            super().__init__(None)

        async def read(self):
            return fake_art_bytes()

    class FakeSessionWithImages(FakeAniListSession):
        def get(self, url):
            return FakeImageResponse()

    cog._session = FakeSessionWithImages([page])

    await cog.card.commands["importpool"].callback(cog, ctx, count=5)

    pool = await cog.config.guild(guild).pool()
    # use anilist_id, not name, to check for the dupe -- a set of names
    # would silently collapse two distinct pool entries that happen to
    # share a name (as "Existing" imported twice would) into one element,
    # masking exactly the bug this test exists to catch
    anilist_ids = [entry.get("anilist_id") for entry in pool.values()]
    assert sorted(x for x in anilist_ids if x is not None) == [42, 43], (
        f"expected anilist id 42 (pre-existing) to appear exactly once and 43 (new) once, got {anilist_ids}"
    )
    assert len(pool) == 2, f"expected exactly 2 pool entries total, got {len(pool)}"


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


@pytest.mark.asyncio
async def test_one_card_per_person_per_drop(cog, monkeypatch):
    """User-requested rule: a drop still has multiple independently-claimable
    cards (that part is unchanged), but a single person may only ever redeem
    one of them. Set up a 2-card drop where the same user reacts to both
    cards, but a second person also reacts to the second card -- the greedy
    reactor must win exactly one card total, and the other reactor must
    still be able to win the second card despite not being first/fastest.

    resolve_claim is normally a random pick among eligible reactors, which
    would make this test flaky (a pass could just mean the RNG happened to
    pick "other"). Pin it to "first eligible reactor" so the assertions
    below are actually discriminating: if the one-card-per-drop filtering
    disappeared, greedy (who reacted first, at index 0) would deterministically
    win card B too, not just possibly."""
    monkeypatch.setattr(cc_module.engine, "resolve_claim", lambda reactor_ids, rng=None: reactor_ids[0])

    guild = FakeGuild(10)
    # (see below) claim_cooldown_seconds is set to 0 for this guild so the
    # unrelated per-user claim-cooldown mechanism can't incidentally exclude
    # greedy from card B's reactor list itself and mask a missing
    # claimed_by check -- same pitfall as the double-award race test above
    admin = FakeMember(100, guild)
    greedy = FakeMember(101, guild)
    other = FakeMember(102, guild)
    guild.members = {100: admin, 101: greedy, 102: other}

    channel = FakeChannel(1000, guild)
    cog.bot.register_channel(channel)
    ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes((5, 5, 5)))])
    await cog.card.commands["addcard"].callback(cog, ctx, "common", name_and_series="Mio | Some Anime")
    ctx2 = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes((250, 5, 5)))])
    await cog.card.commands["addcard"].callback(cog, ctx2, "common", name_and_series="Sora | Some Anime")

    await cog.config.guild(guild).drop_size.set(2)
    await cog.config.guild(guild).decoys_enabled.set(False)
    await cog.config.guild(guild).claim_cooldown_seconds.set(0)

    await cog._post_drop(channel, guild, is_test=False)
    drop_message = channel.sent[-1]
    drop = cog.active_drops[drop_message.id]
    assert len(drop.cards) == 2

    emoji_a = drop.cards[0]["emoji"]
    emoji_b = drop.cards[1]["emoji"]
    reaction_a = next(r for r in drop_message.reactions if r.emoji == emoji_a)
    reaction_b = next(r for r in drop_message.reactions if r.emoji == emoji_b)
    # greedy reacts to both, trying to claim all the cards; other only
    # reacts to the second one
    reaction_a._reactor_objs = [greedy]
    reaction_b._reactor_objs = [greedy, other]

    await cog._resolve_claim_window(channel.id, drop_message.id, emoji_a, window=0)
    await cog._resolve_claim_window(channel.id, drop_message.id, emoji_b, window=0)

    greedy_state = await cog._member_state(greedy)
    other_state = await cog._member_state(other)
    assert len(greedy_state.collection) == 1, f"one person should win exactly one card, got {len(greedy_state.collection)}"
    # excluded from card B entirely -- the OTHER reactor wins it instead of
    # the card going unclaimed
    assert len(other_state.collection) == 1, "the second card should still go to someone, not be forfeited"
    assert drop.claimed_positions == {0, 1}


@pytest.mark.asyncio
async def test_testdrop_reports_empty_pool_instead_of_silently_doing_nothing(cog):
    """Live bug report: an admin ran .card setchannel then .card testdrop
    before adding any characters, and testdrop silently did nothing -- no
    error, no drop, no feedback at all. _post_drop's empty-pool guard used
    to be a bare `return`; it must now report why, and the command must
    relay that back to the admin instead of swallowing it."""
    guild = FakeGuild(8)
    admin = FakeMember(80, guild)
    channel = FakeChannel(800, guild)
    cog.bot.register_channel(channel)
    ctx = FakeCtx(admin, guild, channel)

    await cog.card.commands["setchannel"].callback(cog, ctx, channel)
    messages_before = len(ctx.sent)

    await cog.card.commands["testdrop"].callback(cog, ctx)

    new_messages = ctx.sent[messages_before:]
    assert len(new_messages) == 1, "testdrop with an empty pool must say something, not nothing"
    assert "pool is empty" in new_messages[0].content.lower()
    # and, critically, no drop image was actually posted
    assert not any(m.files for m in new_messages)


@pytest.mark.asyncio
async def test_first_ever_drop_not_blocked_by_monotonic_clock_sentinel(cog, monkeypatch):
    """Regression test: last_drop_time used to default to 0.0 for a guild
    that had never dropped, compared as `now - last < cooldown` against
    time.monotonic(). monotonic()'s reference point is undefined -- it is
    NOT guaranteed to be a large number -- so on a host/sandbox where it
    happens to return something smaller than drop_cooldown_seconds, a
    guild's very first-ever drop would be incorrectly treated as still on
    cooldown and silently skipped. Pin monotonic() to a small value to
    reproduce that condition directly, instead of hoping the real clock
    happens to be small when the suite runs."""
    monkeypatch.setattr(cc_module.time, "monotonic", lambda: 50.0)

    guild = FakeGuild(11)
    admin = FakeMember(110, guild)
    author = FakeMember(111, guild)
    guild.members = {110: admin, 111: author}

    channel = FakeChannel(1100, guild)
    cog.bot.register_channel(channel)
    ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes((40, 200, 90)))])
    await cog.card.commands["addcard"].callback(cog, ctx, "common", name_and_series="Kana | Some Anime")
    await cog.card.commands["setchannel"].callback(cog, ctx, channel)
    await cog.config.guild(guild).drop_chance.set(1.0)
    await cog.config.guild(guild).drop_cooldown_seconds.set(3600)  # larger than the pinned monotonic() value

    assert guild.id not in cog.last_drop_time  # this guild has never dropped

    class FakeMessage2:
        def __init__(self, author, channel):
            self.author = author
            self.channel = channel
            self.guild = channel.guild

    await cog.on_message(FakeMessage2(author, channel))

    drop_messages = [m for m in channel.sent if m.files]
    assert len(drop_messages) == 1, "a guild's first-ever drop must not be blocked by the cooldown check"


@pytest.mark.asyncio
async def test_concurrent_messages_never_chain_two_drops_past_the_cooldown(cog):
    """Regression test for the drop-cooldown race: on_message used to
    check-then-set self.last_drop_time across two real Config awaits with
    no lock, so two messages arriving close together could both pass the
    cooldown check before either recorded a drop -- silently defeating
    drop_cooldown_seconds (locked decision #3: 'a cooldown afterward so it
    can't chain'). A per-guild asyncio.Lock now makes that decision atomic."""
    guild = FakeGuild(9)
    admin = FakeMember(90, guild)
    author = FakeMember(91, guild)
    guild.members = {90: admin, 91: author}

    channel = FakeChannel(900, guild)
    cog.bot.register_channel(channel)
    ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes((90, 40, 200)))])
    await cog.card.commands["addcard"].callback(cog, ctx, "common", name_and_series="Nell | Some Anime")
    await cog.card.commands["setchannel"].callback(cog, ctx, channel)
    # guarantee a drop fires on every qualifying message, and never on
    # cooldown, so the *only* thing that can prevent a second drop is the
    # lock -- this isolates the race from should_drop's own randomness
    await cog.config.guild(guild).drop_chance.set(1.0)
    await cog.config.guild(guild).drop_cooldown_seconds.set(3600)

    class FakeMessage2:
        def __init__(self, author, channel):
            self.author = author
            self.channel = channel
            self.guild = channel.guild

    messages_before = len(channel.sent)

    # two "messages" handled concurrently, as if they arrived back to back
    # (on_message is decorated only with @commands.Cog.listener(), which
    # doesn't wrap it -- it's a plain bound method, unlike the .card
    # subcommands above which go through _FakeCommand/.callback)
    await asyncio.gather(
        cog.on_message(FakeMessage2(author, channel)),
        cog.on_message(FakeMessage2(author, channel)),
    )

    drop_messages = [m for m in channel.sent[messages_before:] if m.files]
    assert len(drop_messages) == 1, f"cooldown should limit this to one drop, got {len(drop_messages)}"


# ---------------------------------------------------------------------------
# one-shot-per-drop claim mechanic (option 2): a member's first reaction on
# a drop is their only "shot" -- exercised through on_raw_reaction_add
# itself, not just _resolve_claim_window, since the gating lives there.
# ---------------------------------------------------------------------------


async def _drain_new_tasks(before_tasks):
    """asyncio.create_task inside on_raw_reaction_add fires and forgets a
    _resolve_claim_window task; awaiting it directly (rather than sleeping
    and hoping) keeps these tests deterministic and avoids leaking a
    pending task past the end of the test."""
    after = asyncio.all_tasks() - before_tasks - {asyncio.current_task()}
    if after:
        await asyncio.gather(*after)


@pytest.mark.asyncio
async def test_on_raw_reaction_add_one_shot_per_drop(cog):
    guild = FakeGuild(200)
    admin = FakeMember(2000, guild)
    reactor = FakeMember(2001, guild)
    guild.members = {2000: admin, 2001: reactor}

    channel = FakeChannel(20000, guild)
    cog.bot.register_channel(channel)
    ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes((1, 2, 3)))])
    await cog.card.commands["addcard"].callback(cog, ctx, "common", name_and_series="A | Anime")
    ctx2 = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes((4, 5, 6)))])
    await cog.card.commands["addcard"].callback(cog, ctx2, "common", name_and_series="B | Anime")

    await cog.config.guild(guild).drop_size.set(2)
    await cog.config.guild(guild).decoys_enabled.set(False)
    await cog.config.guild(guild).claim_window_seconds.set(0)

    await cog._post_drop(channel, guild, is_test=False)
    drop_message = channel.sent[-1]
    drop = cog.active_drops[drop_message.id]
    assert len(drop.cards) == 2
    emoji_a = drop.cards[0]["emoji"]
    emoji_b = drop.cards[1]["emoji"]

    reaction_a = next(r for r in drop_message.reactions if r.emoji == emoji_a)
    reaction_b = next(r for r in drop_message.reactions if r.emoji == emoji_b)
    reaction_a._reactor_objs = [reactor]
    reaction_b._reactor_objs = [reactor]

    before_tasks = asyncio.all_tasks()
    await cog.on_raw_reaction_add(
        FakeRawReactionPayload(guild.id, reactor.id, drop_message.id, channel.id, emoji_a)
    )
    # second reaction, on a different but still-open real card -- must be
    # ignored entirely: the first reaction already used this member's one
    # shot at this drop
    await cog.on_raw_reaction_add(
        FakeRawReactionPayload(guild.id, reactor.id, drop_message.id, channel.id, emoji_b)
    )
    await _drain_new_tasks(before_tasks)

    reactor_state = await cog._member_state(reactor)
    assert reactor_state.collection == [drop.cards[0]["card_id"]], (
        "member should only win the card from their first reaction -- a "
        "second reaction on another open card must not spend a fresh shot"
    )
    assert reactor.id in drop.reacted_users


@pytest.mark.asyncio
async def test_on_raw_reaction_add_decoy_guess_spends_shot_and_sets_penalty(cog):
    guild = FakeGuild(201)
    admin = FakeMember(2010, guild)
    reactor = FakeMember(2011, guild)
    guild.members = {2010: admin, 2011: reactor}

    channel = FakeChannel(20100, guild)
    cog.bot.register_channel(channel)
    ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes((7, 8, 9)))])
    await cog.card.commands["addcard"].callback(cog, ctx, "common", name_and_series="C | Anime")

    await cog.config.guild(guild).decoys_enabled.set(True)
    await cog.config.guild(guild).decoy_count.set(5)
    await cog.config.guild(guild).wrong_guess_penalty_seconds.set(120)
    await cog.config.guild(guild).claim_window_seconds.set(0)

    await cog._post_drop(channel, guild, is_test=False)
    drop_message = channel.sent[-1]
    drop = cog.active_drops[drop_message.id]
    assert drop.decoy_emojis, "need at least one decoy to exercise this path"
    decoy_emoji = drop.decoy_emojis[0]

    before = time.monotonic()
    await cog.on_raw_reaction_add(
        FakeRawReactionPayload(guild.id, reactor.id, drop_message.id, channel.id, decoy_emoji)
    )

    assert reactor.id in drop.reacted_users
    assert cog.wrong_guess_penalty_until.get(reactor.id, 0.0) > before

    # their shot is now spent -- a reaction on a real, still-open card does
    # nothing either
    real_emoji = drop.cards[0]["emoji"]
    reaction = next(r for r in drop_message.reactions if r.emoji == real_emoji)
    reaction._reactor_objs = [reactor]
    before_tasks = asyncio.all_tasks()
    await cog.on_raw_reaction_add(
        FakeRawReactionPayload(guild.id, reactor.id, drop_message.id, channel.id, real_emoji)
    )
    await _drain_new_tasks(before_tasks)

    reactor_state = await cog._member_state(reactor)
    assert reactor_state.collection == [], "the decoy guess should have used up their only shot"


@pytest.mark.asyncio
async def test_on_raw_reaction_add_ignores_already_claimed_card_without_spending_shot(cog):
    guild = FakeGuild(202)
    admin = FakeMember(2020, guild)
    reactor = FakeMember(2021, guild)
    guild.members = {2020: admin, 2021: reactor}

    channel = FakeChannel(20200, guild)
    cog.bot.register_channel(channel)
    ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes((11, 22, 33)))])
    await cog.card.commands["addcard"].callback(cog, ctx, "common", name_and_series="D | Anime")
    ctx2 = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes((44, 55, 66)))])
    await cog.card.commands["addcard"].callback(cog, ctx2, "common", name_and_series="E | Anime")

    await cog.config.guild(guild).drop_size.set(2)
    await cog.config.guild(guild).decoys_enabled.set(False)
    await cog.config.guild(guild).claim_window_seconds.set(0)

    await cog._post_drop(channel, guild, is_test=False)
    drop_message = channel.sent[-1]
    drop = cog.active_drops[drop_message.id]
    emoji_a = drop.cards[0]["emoji"]
    emoji_b = drop.cards[1]["emoji"]

    # simulate card A having already been claimed by someone else, via a
    # reaction that landed a split second earlier
    drop.claimed_positions.add(drop.cards[0]["position"])

    await cog.on_raw_reaction_add(
        FakeRawReactionPayload(guild.id, reactor.id, drop_message.id, channel.id, emoji_a)
    )
    assert reactor.id not in drop.reacted_users, (
        "landing on an already-claimed card isn't the member's fault -- it must not burn their shot"
    )

    # their shot is still available -- a reaction on the still-open card
    # works normally
    reaction_b = next(r for r in drop_message.reactions if r.emoji == emoji_b)
    reaction_b._reactor_objs = [reactor]
    before_tasks = asyncio.all_tasks()
    await cog.on_raw_reaction_add(
        FakeRawReactionPayload(guild.id, reactor.id, drop_message.id, channel.id, emoji_b)
    )
    await _drain_new_tasks(before_tasks)

    reactor_state = await cog._member_state(reactor)
    assert reactor_state.collection == [drop.cards[1]["card_id"]]


@pytest.mark.asyncio
async def test_wrong_guess_penalty_excludes_reactor_from_winning(cog):
    guild = FakeGuild(203)
    admin = FakeMember(2030, guild)
    penalized = FakeMember(2031, guild)
    other = FakeMember(2032, guild)
    guild.members = {2030: admin, 2031: penalized, 2032: other}

    channel = FakeChannel(20300, guild)
    cog.bot.register_channel(channel)
    ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes((77, 88, 99)))])
    await cog.card.commands["addcard"].callback(cog, ctx, "common", name_and_series="F | Anime")

    await cog.config.guild(guild).claim_cooldown_seconds.set(0)
    cog.wrong_guess_penalty_until[penalized.id] = time.monotonic() + 120

    await cog._post_drop(channel, guild, is_test=False)
    drop_message = channel.sent[-1]
    drop = cog.active_drops[drop_message.id]
    real_emoji = drop.cards[0]["emoji"]
    card_id = drop.cards[0]["card_id"]

    reaction = next(r for r in drop_message.reactions if r.emoji == real_emoji)
    reaction._reactor_objs = [penalized, other]

    await cog._resolve_claim_window(channel.id, drop_message.id, real_emoji, window=0)

    penalized_state = await cog._member_state(penalized)
    other_state = await cog._member_state(other)
    assert card_id not in penalized_state.collection, "a member under the wrong-guess penalty must not win"
    assert card_id in other_state.collection, "the card should still go to the other eligible reactor"


@pytest.mark.asyncio
async def test_card_set_decoycount_and_wrongguesspenalty_commands(cog):
    guild = FakeGuild(204)
    admin = FakeMember(2040, guild)
    channel = FakeChannel(20400, guild)
    ctx = FakeCtx(admin, guild, channel)

    await cog.card.commands["set"].commands["decoycount"].callback(cog, ctx, 12)
    assert await cog.config.guild(guild).decoy_count() == 12
    assert "12" in ctx.sent[-1].content

    ctx2 = FakeCtx(admin, guild, channel)
    await cog.card.commands["set"].commands["decoycount"].callback(cog, ctx2, -1)
    assert "negative" in ctx2.sent[-1].content.lower()
    assert await cog.config.guild(guild).decoy_count() == 12  # unchanged

    ctx3 = FakeCtx(admin, guild, channel)
    await cog.card.commands["set"].commands["wrongguesspenalty"].callback(cog, ctx3, 90)
    assert await cog.config.guild(guild).wrong_guess_penalty_seconds() == 90
    assert "90" in ctx3.sent[-1].content

    ctx4 = FakeCtx(admin, guild, channel)
    await cog.card.commands["set"].commands["wrongguesspenalty"].callback(cog, ctx4, -5)
    assert "negative" in ctx4.sent[-1].content.lower()
    assert await cog.config.guild(guild).wrong_guess_penalty_seconds() == 90  # unchanged


@pytest.mark.asyncio
async def test_decoy_guess_on_a_test_drop_does_not_set_a_real_penalty(cog):
    """An admin previewing the decoy/one-shot mechanic with `.card testdrop`
    must not walk away with a real wrong_guess_penalty_until entry -- that
    would lock them out of winning *actual* drops afterward, the same
    test-mode exemption claim_cooldown_until and the daily quota already
    get in _resolve_claim_window. The one-shot gate itself (reacted_users)
    still applies in test mode, so the preview is otherwise accurate."""
    guild = FakeGuild(205)
    admin = FakeMember(2050, guild)
    channel = FakeChannel(20500, guild)
    cog.bot.register_channel(channel)
    ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes((3, 6, 9)))])
    await cog.card.commands["addcard"].callback(cog, ctx, "common", name_and_series="G | Anime")

    await cog.config.guild(guild).decoys_enabled.set(True)
    await cog.config.guild(guild).decoy_count.set(5)
    await cog.config.guild(guild).wrong_guess_penalty_seconds.set(120)

    await cog._post_drop(channel, guild, is_test=True)
    drop_message = channel.sent[-1]
    drop = cog.active_drops[drop_message.id]
    assert drop.is_test is True
    assert drop.decoy_emojis, "need at least one decoy to exercise this path"
    decoy_emoji = drop.decoy_emojis[0]

    await cog.on_raw_reaction_add(
        FakeRawReactionPayload(guild.id, admin.id, drop_message.id, channel.id, decoy_emoji)
    )

    # the one-shot gate still fired (this is a real part of the preview)...
    assert admin.id in drop.reacted_users
    # ...but no real penalty should have been recorded against them
    assert admin.id not in cog.wrong_guess_penalty_until


@pytest.mark.asyncio
async def test_decoy_guess_dms_the_reactor_with_the_penalty_duration(cog):
    """There's no interaction token on a raw reaction event, so a true
    Discord ephemeral reply isn't possible here -- a DM is the closest
    thing that's actually private to just the person who guessed wrong.
    It should mention how long they're locked out for."""
    guild = FakeGuild(206)
    admin = FakeMember(2060, guild)
    reactor = FakeMember(2061, guild)
    guild.members = {2060: admin, 2061: reactor}

    channel = FakeChannel(20600, guild)
    cog.bot.register_channel(channel)
    ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes((12, 34, 56)))])
    await cog.card.commands["addcard"].callback(cog, ctx, "common", name_and_series="H | Anime")

    await cog.config.guild(guild).decoys_enabled.set(True)
    await cog.config.guild(guild).decoy_count.set(5)
    await cog.config.guild(guild).wrong_guess_penalty_seconds.set(90)

    await cog._post_drop(channel, guild, is_test=False)
    drop_message = channel.sent[-1]
    drop = cog.active_drops[drop_message.id]
    assert drop.decoy_emojis, "need at least one decoy to exercise this path"
    decoy_emoji = drop.decoy_emojis[0]

    await cog.on_raw_reaction_add(
        FakeRawReactionPayload(guild.id, reactor.id, drop_message.id, channel.id, decoy_emoji)
    )

    assert len(reactor.dms) == 1, "a wrong guess should DM the reactor exactly once"
    assert "90" in reactor.dms[0]
    assert admin.dms == [], "only the person who guessed wrong should get a DM"


@pytest.mark.asyncio
async def test_decoy_guess_dm_failure_is_swallowed(cog):
    """A member with DMs closed is a routine, expected case (Forbidden), not
    something that should crash reaction handling or otherwise surface to
    the channel."""
    guild = FakeGuild(207)
    admin = FakeMember(2070, guild)
    reactor = FakeMember(2071, guild)
    guild.members = {2070: admin, 2071: reactor}

    class FakeHTTPResponse:
        status = 403
        reason = "Forbidden"

    async def closed_dms(*args, **kwargs):
        raise discord.Forbidden(response=FakeHTTPResponse(), message="Cannot send messages to this user")

    reactor.send = closed_dms

    channel = FakeChannel(20700, guild)
    cog.bot.register_channel(channel)
    ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes((65, 43, 21)))])
    await cog.card.commands["addcard"].callback(cog, ctx, "common", name_and_series="I | Anime")

    await cog.config.guild(guild).decoys_enabled.set(True)
    await cog.config.guild(guild).decoy_count.set(5)
    await cog.config.guild(guild).wrong_guess_penalty_seconds.set(60)

    await cog._post_drop(channel, guild, is_test=False)
    drop_message = channel.sent[-1]
    drop = cog.active_drops[drop_message.id]
    decoy_emoji = drop.decoy_emojis[0]

    # must not raise -- a closed-DM Forbidden is expected, not exceptional
    await cog.on_raw_reaction_add(
        FakeRawReactionPayload(guild.id, reactor.id, drop_message.id, channel.id, decoy_emoji)
    )

    # the actual penalty must still have been applied even though the DM failed
    assert reactor.id in cog.wrong_guess_penalty_until


@pytest.mark.asyncio
async def test_concurrent_resolutions_for_the_same_drop_reuse_one_message_fetch(cog):
    """Live bug report: a fresh 3-card drop where all 3 cards get claimed
    within the same second or two used to fire an independent
    channel.fetch_message() per card, all hitting the exact same message --
    redundant enough (2-3+ near-simultaneous GETs to the same message) to
    occasionally trip Discord's per-route rate limit. discord.py handles a
    429 by sleeping and retrying rather than raising, so nothing was ever
    lost -- but a claim embed could sit unposted for several seconds until
    its window's fetch finally went through, which is exactly what was
    reported (third card only showed up "if delayed"). Concurrent
    resolutions for the same message should now share one fetch."""
    guild = FakeGuild(210)
    admin = FakeMember(2100, guild)
    reactor_a = FakeMember(2101, guild)
    reactor_b = FakeMember(2102, guild)
    reactor_c = FakeMember(2103, guild)
    guild.members = {2100: admin, 2101: reactor_a, 2102: reactor_b, 2103: reactor_c}

    channel = FakeChannel(21000, guild)
    cog.bot.register_channel(channel)
    for i, color in enumerate([(1, 2, 3), (4, 5, 6), (7, 8, 9)]):
        ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes(color))])
        await cog.card.commands["addcard"].callback(cog, ctx, "common", name_and_series=f"Card{i} | Anime")

    await cog.config.guild(guild).drop_size.set(3)
    await cog.config.guild(guild).decoys_enabled.set(False)
    await cog.config.guild(guild).claim_cooldown_seconds.set(0)

    await cog._post_drop(channel, guild, is_test=False)
    drop_message = channel.sent[-1]
    drop = cog.active_drops[drop_message.id]
    assert len(drop.cards) == 3

    reactors = [reactor_a, reactor_b, reactor_c]
    for card_entry, reactor in zip(drop.cards, reactors):
        reaction = next(r for r in drop_message.reactions if r.emoji == card_entry["emoji"])
        reaction._reactor_objs = [reactor]

    fetch_calls_before = channel.fetch_message_calls
    # all three cards claimed at once, as if reacted to within the same
    # second -- resolve them concurrently, same as real near-simultaneous
    # claim windows would
    await asyncio.gather(*(
        cog._resolve_claim_window(channel.id, drop_message.id, card_entry["emoji"], window=0)
        for card_entry in drop.cards
    ))

    assert channel.fetch_message_calls - fetch_calls_before == 1, (
        "concurrent resolutions for the same message should share one fetch, not one each -- "
        f"got {channel.fetch_message_calls - fetch_calls_before}"
    )

    # and correctness wasn't sacrificed for the dedup -- all three still resolved correctly
    for card_entry, reactor in zip(drop.cards, reactors):
        state = await cog._member_state(reactor)
        assert card_entry["card_id"] in state.collection

    # the drop is fully resolved -- its message-fetch cache/lock entries
    # must be cleaned up too, not just active_drops itself
    assert drop_message.id not in cog.active_drops
    assert drop_message.id not in cog._message_cache
    assert drop_message.id not in cog._message_fetch_locks
