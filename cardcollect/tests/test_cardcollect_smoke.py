"""Import-smoke test for cardcollect.py (the redbot/discord-facing main cog
file) under the conftest.py redbot stub -- same lesson photodrop's bug #10
taught this project: a cog file that only ever gets exercised inside a real
running bot can still ship a plain import error, so it needs at least an
import-level test even without the real dependency installed.

Beyond the bare import, this also drives a full drop -> claim cycle through
the actual cog code (message listener -> _post_drop -> code matching ->
_claim) against lightweight fake discord objects, since that's the newest
and most bug-prone part of this cog.
"""

import asyncio
import io
import random
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


class FakeMessage:
    def __init__(self, id_, channel, content=None, embed=None, file=None, view=None):
        self.id = id_
        self.channel = channel
        self.content = content
        self.embeds = [embed] if embed else []
        self.files = [file] if file else []
        self.view = view
        self.reactions = []

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)


class FakeChannel:
    def __init__(self, id_, guild):
        self.id = id_
        self.guild = guild
        self._messages = {}
        self._next_id = 1
        self.sent = []
        guild.channels[id_] = self

    @property
    def mention(self):
        return f"<#{self.id}>"

    async def send(self, content=None, embed=None, file=None, view=None):
        msg = FakeMessage(self._next_id, self, content=content, embed=embed, file=file, view=view)
        self._messages[msg.id] = msg
        self._next_id += 1
        self.sent.append(msg)
        return msg


class FakeBot:
    def __init__(self):
        self.user = FakeUser(999999999)
        self._channels = {}
        self.added_views = []

    def add_view(self, view):
        self.added_views.append(view)

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
    # regression: `.card showcase` used to be a plain text list ("`303` —
    # Zero Two"), which live feedback called out as not actually showing
    # "large versions of the character" -- it must now render an image,
    # same as `.card` itself does for the full gallery
    assert view_ctx.sent[-1].files, "populated showcase must send an image, not just text"

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
# CAPTCHA claim mechanic: the drop carries a Claim button; pressing it opens a
# pop-up where the member types a card's code. These drive the real
# claim_button_refusal / submit_code / _claim code and the views.py wiring.
# ---------------------------------------------------------------------------

from cardcollect import captcha, views  # noqa: E402  (grouped with the tests that use them)
from cardcollect.models import MemberState  # noqa: E402


class FakeHTTPResponse:
    def __init__(self, status=403, reason="Forbidden"):
        self.status = status
        self.reason = reason


class FakeInteractionResponse:
    def __init__(self, defer_delay=0):
        self.defer_delay = defer_delay  # event-loop turns the acknowledgement takes
        self.deferred = None  # the ephemeral flag once deferred
        self.messages = []
        self.modal = None

    async def defer(self, ephemeral=False):
        for _ in range(self.defer_delay):
            await asyncio.sleep(0)
        self.deferred = ephemeral

    async def send_message(self, content=None, ephemeral=False):
        self.messages.append((content, ephemeral))

    async def send_modal(self, modal):
        self.modal = modal


class FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content=None, ephemeral=False):
        self.messages.append((content, ephemeral))


class FakeInteraction:
    def __init__(self, user, channel, message=None, defer_delay=0):
        self.user = user
        self.channel = channel
        self.guild = channel.guild
        self.message = message
        self.response = FakeInteractionResponse(defer_delay)
        self.followup = FakeFollowup()


async def submit(cog, channel, member, drop, code):
    """One code submission from the pop-up; returns the private reply."""
    return await cog.submit_code(member, channel, drop.message_id, code)


def code_of(drop, index=0):
    return drop.cards[index]["code"]


def wrong_code_for(drop):
    """A code-shaped string that is NOT one of this drop's codes."""
    while True:
        candidate = captcha.generate_code()
        if all(candidate != c["code"] for c in drop.cards):
            return candidate


async def ready_drop(cog, gid, n_members=2, n_cards=1, is_test=False, **guild_conf):
    """A guild with `n_cards` pool cards, a drop already posted, and a claim
    cooldown of 0 (so the unrelated cooldown can't mask what a test is about).
    Returns (guild, admin, members, channel, drop, drop_message)."""
    guild = FakeGuild(gid)
    admin = FakeMember(gid * 10, guild)
    members = [FakeMember(gid * 10 + 1 + i, guild) for i in range(n_members)]
    guild.members = {m.id: m for m in [admin, *members]}
    channel = FakeChannel(gid * 100, guild)
    cog.bot.register_channel(channel)
    for i in range(n_cards):
        ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes((10 + i * 40, 50, 90)))])
        await cog.card.commands["addcard"].callback(cog, ctx, "common", name_and_series=f"Card{i} | Anime")
    conf = cog.config.guild(guild)
    await conf.drop_size.set(n_cards)
    await conf.claim_cooldown_seconds.set(0)
    for key, value in guild_conf.items():
        await getattr(conf, key).set(value)
    random.seed(gid)  # the rolled cards/codes are random; pin them so a test can never flake on a lucky code
    assert await cog._post_drop(channel, guild, is_test=is_test) is None
    drop_message = channel.sent[-1]
    return guild, admin, members, channel, cog.active_drops[drop_message.id], drop_message


@pytest.mark.asyncio
async def test_drop_message_carries_the_claim_button_and_no_reactions(cog):
    guild, admin, members, channel, drop, drop_message = await ready_drop(cog, 2)
    assert drop_message.files
    assert isinstance(drop_message.view, views.ClaimView)
    assert "claim" in drop_message.content.lower()
    assert drop_message.reactions == [], "nothing is reaction-seeded any more"


@pytest.mark.asyncio
async def test_full_drop_and_claim_cycle_real_mode(cog):
    guild, admin, (winner, late), channel, drop, drop_message = await ready_drop(cog, 3)
    card_id = drop.cards[0]["card_id"]

    reply = await submit(cog, channel, winner, drop, code_of(drop))
    late_reply = await submit(cog, channel, late, drop, code_of(drop))  # too late, same card

    assert "claimed" in reply.lower() and "Card0" in reply
    assert (await cog._member_state(winner)).collection == [card_id]
    assert (await cog._member_state(late)).collection == []
    assert late_reply == late_reply and "over" in late_reply.lower()  # single-card drop is already released
    result_embed = channel.sent[-1].embeds[0]
    assert result_embed.title == "New card claimed!"
    assert drop_message.id not in cog.active_drops


@pytest.mark.asyncio
async def test_ambient_drop_then_claim_end_to_end(cog):
    guild = FakeGuild(4)
    admin = FakeMember(40, guild)
    member = FakeMember(41, guild)
    guild.members = {40: admin, 41: member}
    channel = FakeChannel(400, guild)
    cog.bot.register_channel(channel)
    ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes())])
    await cog.card.commands["addcard"].callback(cog, ctx, "common", name_and_series="Ann | Anime")
    await cog.card.commands["setchannel"].callback(cog, ctx, channel)
    await cog.config.guild(guild).drop_chance.set(1.0)
    await cog.config.guild(guild).drop_size.set(1)

    class ChatMessage:  # ordinary chat is what triggers the drop
        author = member
        content = "hello everyone"

    ChatMessage.channel = channel
    ChatMessage.guild = guild
    await cog.on_message(ChatMessage())
    assert len(cog.active_drops) == 1
    drop = next(iter(cog.active_drops.values()))

    interaction = FakeInteraction(member, channel, message=channel.sent[-1])
    view = channel.sent[-1].view
    await view.claim.callback(interaction)  # press the button
    assert isinstance(interaction.response.modal, views.CodeModal)
    await interaction.response.modal.handle_submit(interaction, code_of(drop))  # submit the pop-up

    assert (await cog._member_state(member)).collection == [drop.cards[0]["card_id"]]
    assert interaction.response.deferred is True, "replies to a submission are private (ephemeral)"
    text, ephemeral = interaction.followup.messages[-1]
    assert ephemeral is True and "claimed" in text.lower()


@pytest.mark.asyncio
async def test_test_mode_drop_awards_nothing(cog):
    guild, admin, (winner,), channel, drop, _ = await ready_drop(cog, 5, n_members=1, is_test=True)
    assert drop.is_test is True

    reply = await submit(cog, channel, winner, drop, code_of(drop))

    assert (await cog._member_state(winner)).collection == []  # test mode: nothing awarded
    assert "Test" in channel.sent[-1].embeds[0].title
    assert "test" in reply.lower()


@pytest.mark.asyncio
async def test_first_duplicate_claim_becomes_a_tradeable_spare(cog):
    """MAX_COPIES_KEPT=2 -- a member's *first* duplicate of a card becomes a
    real second collection entry (a tradeable spare, not a sell token), so
    `.card give` has something to actually hand off."""
    guild, admin, (winner,), channel, drop, _ = await ready_drop(cog, 6, n_members=1)
    card_id = drop.cards[0]["card_id"]
    await cog._save_member_state(winner, MemberState(collection=[card_id]))

    await submit(cog, channel, winner, drop, code_of(drop))

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
    guild, admin, (winner,), channel, drop, _ = await ready_drop(cog, 24, n_members=1)
    card_id = drop.cards[0]["card_id"]
    await cog._save_member_state(winner, MemberState(collection=[card_id, card_id]))

    await submit(cog, channel, winner, drop, code_of(drop))

    final_state = await cog._member_state(winner)
    assert final_state.collection == [card_id, card_id]  # unchanged, no 3rd copy
    assert len(final_state.sell_tokens) == 1
    assert final_state.sell_tokens[0].card_id == card_id
    assert "sell token" in channel.sent[-1].embeds[0].title.lower()


@pytest.mark.asyncio
async def test_claim_at_the_cap_still_counts_against_daily_quota(cog):
    """A sell-token (at-cap) claim still burns quota -- otherwise a
    maxed-out member could keep re-claiming a card they already have two of
    forever without it ever counting against them."""
    guild, admin, (winner,), channel, drop, _ = await ready_drop(cog, 22, n_members=1, claim_quota=5)
    card_id = drop.cards[0]["card_id"]
    await cog._save_member_state(winner, MemberState(collection=[card_id, card_id]))

    await submit(cog, channel, winner, drop, code_of(drop))

    final_state = await cog._member_state(winner)
    assert len(final_state.sell_tokens) == 1  # at-cap path, not a new pickup or a 3rd copy
    assert final_state.daily_claims == 1, "an at-cap claim must still burn quota"


@pytest.mark.asyncio
async def test_first_duplicate_claim_also_counts_against_daily_quota(cog):
    guild, admin, (winner,), channel, drop, _ = await ready_drop(cog, 25, n_members=1, claim_quota=5)
    card_id = drop.cards[0]["card_id"]
    await cog._save_member_state(winner, MemberState(collection=[card_id]))

    await submit(cog, channel, winner, drop, code_of(drop))

    final_state = await cog._member_state(winner)
    assert final_state.collection == [card_id, card_id]
    assert final_state.sell_tokens == []
    assert final_state.daily_claims == 1, "a spare-duplicate claim must still burn quota"


@pytest.mark.asyncio
async def test_daily_claim_quota_excludes_a_member_who_already_hit_it(cog):
    """A member at their daily quota can't win -- and their correct code must
    leave the card open (not burn it) for the next member."""
    guild, admin, (maxed_out, fresh), channel, drop, _ = await ready_drop(cog, 15, n_members=2, claim_quota=1)
    from cardcollect import engine as engine_mod

    today = engine_mod.today_str("America/Los_Angeles")
    member_conf = cog.config.member_from_ids(guild.id, maxed_out.id)
    await member_conf.daily_claims.set(1)
    await member_conf.daily_claims_date.set(today)

    blocked = await submit(cog, channel, maxed_out, drop, code_of(drop))
    assert (await cog._member_state(maxed_out)).collection == [], "a member at their daily quota must not win"
    assert "today's claims" in blocked
    assert drop.claimed_positions == set(), "the card must still be open"

    await submit(cog, channel, fresh, drop, code_of(drop))
    fresh_state = await cog._member_state(fresh)
    assert len(fresh_state.collection) == 1
    assert fresh_state.daily_claims == 1
    assert fresh_state.daily_claims_date == today


@pytest.mark.asyncio
async def test_concurrent_correct_codes_never_double_award(cog):
    """Two members submit the SAME card's code in the same instant. The claim
    lock plus the re-check under it must award the card exactly once. (A
    2-card drop, so the drop is still active when the second submission gets
    the lock -- otherwise 'drop forgotten' would mask a missing per-card
    check.)"""
    guild, admin, (u1, u2), channel, drop, _ = await ready_drop(cog, 7, n_members=2, n_cards=2)
    before = len(channel.sent)

    r1, r2 = await asyncio.gather(
        submit(cog, channel, u1, drop, code_of(drop, 0)),
        submit(cog, channel, u2, drop, code_of(drop, 0)),
    )

    s1, s2 = await cog._member_state(u1), await cog._member_state(u2)
    assert len(s1.collection) + len(s2.collection) == 1, "the card must be awarded exactly once"
    assert s1.sell_tokens == [] and s2.sell_tokens == []
    assert len(channel.sent[before:]) == 1, "exactly one claim result message"
    assert "claimed" in r1.lower() and "too slow" in r2.lower()
    assert s1.collection != [], "the submission that reached the lock first wins"


@pytest.mark.asyncio
async def test_one_card_per_person_per_drop(cog):
    """A drop has several independently claimable cards, but one person can
    only ever take one of them; the second card must stay available."""
    guild, admin, (greedy, other), channel, drop, drop_message = await ready_drop(cog, 10, n_members=2, n_cards=2)

    await submit(cog, channel, greedy, drop, code_of(drop, 0))
    second = await submit(cog, channel, greedy, drop, code_of(drop, 1))
    assert len((await cog._member_state(greedy)).collection) == 1
    assert "already claimed a card" in second
    assert drop.claimed_positions == {0}

    await submit(cog, channel, other, drop, code_of(drop, 1))
    assert len((await cog._member_state(other)).collection) == 1, "the second card should still go to someone"
    assert drop.claimed_positions == {0, 1}
    assert drop_message.id not in cog.active_drops


@pytest.mark.asyncio
async def test_one_card_per_person_holds_when_both_codes_arrive_at_once(cog):
    """Same rule, but the greedy member submits both codes concurrently, so
    neither pre-lock early exit can catch it -- only the re-check under the
    claim lock can."""
    guild, admin, (greedy,), channel, drop, _ = await ready_drop(cog, 12, n_members=1, n_cards=2)

    await asyncio.gather(
        submit(cog, channel, greedy, drop, code_of(drop, 0)),
        submit(cog, channel, greedy, drop, code_of(drop, 1)),
    )

    assert len((await cog._member_state(greedy)).collection) == 1
    assert len(drop.claimed_positions) == 1


@pytest.mark.asyncio
async def test_code_matches_case_insensitively_and_ignores_surrounding_whitespace(cog):
    guild, admin, (member,), channel, drop, _ = await ready_drop(cog, 13, n_members=1)
    await submit(cog, channel, member, drop, f"  {code_of(drop).lower()}  ")
    assert (await cog._member_state(member)).collection == [drop.cards[0]["card_id"]]


@pytest.mark.asyncio
async def test_a_typo_that_isnt_even_code_shaped_gets_a_hint_and_costs_nothing(cog):
    guild, admin, (member,), channel, drop, _ = await ready_drop(cog, 14, n_members=1, max_wrong_guesses=1)
    real = code_of(drop)
    for typo in ["", "   ", "hello", real[:-1], real[0] + " " + real[1:], real + "X", "12345"]:
        reply = await submit(cog, channel, member, drop, typo)
        assert "doesn't look like a code" in reply, f"{typo!r} should have been answered with a hint"
    assert drop.wrong_guesses == {}
    assert not drop.is_locked_out(member.id)
    await submit(cog, channel, member, drop, real)  # still has their whole guess budget, and it works
    assert (await cog._member_state(member)).collection == [drop.cards[0]["card_id"]]


@pytest.mark.asyncio
async def test_wrong_code_shaped_guesses_count_down_then_lock_a_member_out(cog):
    guild, admin, (guesser, other), channel, drop, _ = await ready_drop(
        cog, 200, n_members=2, max_wrong_guesses=3, wrong_guess_penalty_seconds=120
    )
    wrong = wrong_code_for(drop)
    before = time.monotonic()

    assert "2 guesses left" in await submit(cog, channel, guesser, drop, wrong)
    assert "1 guess left" in await submit(cog, channel, guesser, drop, wrong)
    assert guesser.id not in cog.wrong_guess_penalty_until
    last = await submit(cog, channel, guesser, drop, wrong)
    assert last == (
        "❌ You're out of chances and locked out from guessing for 120s. "
        "Now watch everyone else collect the cards in front of you, cuck! \U0001f921"
    )
    assert drop.wrong_guesses[guesser.id] == 3
    assert cog.wrong_guess_penalty_until[guesser.id] > before

    # locked out: even the correct code does nothing now
    assert "locked out" in await submit(cog, channel, guesser, drop, code_of(drop))
    assert (await cog._member_state(guesser)).collection == []

    # ...but everyone else is unaffected
    await submit(cog, channel, other, drop, code_of(drop))
    assert (await cog._member_state(other)).collection == [drop.cards[0]["card_id"]]


@pytest.mark.asyncio
async def test_unlimited_wrong_guesses_when_the_cap_is_zero(cog):
    guild, admin, (member,), channel, drop, _ = await ready_drop(cog, 203, n_members=1, max_wrong_guesses=0)
    wrong = wrong_code_for(drop)
    for _ in range(10):
        assert "Wrong code" in await submit(cog, channel, member, drop, wrong)
    assert not drop.is_locked_out(member.id)
    await submit(cog, channel, member, drop, code_of(drop))
    assert (await cog._member_state(member)).collection == [drop.cards[0]["card_id"]]


@pytest.mark.asyncio
async def test_correct_code_for_an_already_claimed_card_is_free(cog):
    guild, admin, (first, second), channel, drop, _ = await ready_drop(
        cog, 204, n_members=2, n_cards=2, max_wrong_guesses=1
    )
    await submit(cog, channel, first, drop, code_of(drop, 0))

    late = await submit(cog, channel, second, drop, code_of(drop, 0))  # right code, card already gone
    assert "too slow" in late.lower()
    assert drop.wrong_guesses == {}, "landing on an already-claimed card is not the member's fault"

    await submit(cog, channel, second, drop, code_of(drop, 1))
    assert len((await cog._member_state(second)).collection) == 1


@pytest.mark.asyncio
async def test_wrong_guess_penalty_excludes_a_member_from_winning(cog):
    guild, admin, (penalized, other), channel, drop, _ = await ready_drop(cog, 217, n_members=2)
    cog.wrong_guess_penalty_until[penalized.id] = time.monotonic() + 120

    reply = await submit(cog, channel, penalized, drop, code_of(drop))
    assert (await cog._member_state(penalized)).collection == [], "a member under the penalty must not win"
    assert "locked out from guessing" in reply
    assert drop.claimed_positions == set()

    await submit(cog, channel, other, drop, code_of(drop))
    assert (await cog._member_state(other)).collection == [drop.cards[0]["card_id"]]


@pytest.mark.asyncio
async def test_claim_cooldown_blocks_a_second_win_then_lets_it_through(cog):
    guild, admin, (member,), channel, drop, _ = await ready_drop(
        cog, 205, n_members=1, n_cards=2, claim_cooldown_seconds=300
    )
    await submit(cog, channel, member, drop, code_of(drop, 0))
    assert cog.claim_cooldown_until[member.id] > time.monotonic()
    # (one-card-per-drop would also stop this, so post a fresh drop to isolate the cooldown)
    assert await cog._post_drop(channel, guild, is_test=False) is None
    second_drop = cog.active_drops[channel.sent[-1].id]

    reply = await submit(cog, channel, member, second_drop, code_of(second_drop, 0))
    assert "cooldown" in reply.lower()
    assert len((await cog._member_state(member)).collection) == 1

    cog.claim_cooldown_until[member.id] = 0.0
    await submit(cog, channel, member, second_drop, code_of(second_drop, 0))
    assert len((await cog._member_state(member)).collection) == 2


@pytest.mark.asyncio
async def test_lockout_on_a_test_drop_does_not_set_a_real_penalty(cog):
    """`.card testdrop` previews the guess cap, but must never leave an admin
    with a real penalty that blocks them from winning actual drops."""
    guild, admin, (member,), channel, drop, _ = await ready_drop(
        cog, 206, n_members=1, is_test=True, max_wrong_guesses=1, wrong_guess_penalty_seconds=120
    )
    reply = await submit(cog, channel, member, drop, wrong_code_for(drop))

    assert drop.is_locked_out(member.id)
    assert member.id not in cog.wrong_guess_penalty_until
    assert "test" in reply.lower()


@pytest.mark.asyncio
async def test_failed_save_leaves_the_card_open_and_unannounced(cog, monkeypatch):
    guild, admin, (member,), channel, drop, drop_message = await ready_drop(cog, 209, n_members=1, n_cards=2)
    real_save = cog._save_member_state
    calls = []

    async def flaky_save(m, state):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("config backend down")
        return await real_save(m, state)

    monkeypatch.setattr(cog, "_save_member_state", flaky_save)

    first = await submit(cog, channel, member, drop, code_of(drop, 0))  # must not raise
    assert "still open" in first
    assert channel.sent[-1] is drop_message, "no result embed may be posted for a claim that wasn't saved"
    assert drop.claimed_positions == set() and drop.claimed_by == set()
    assert member.id not in cog.claim_cooldown_until
    assert (await cog._member_state(member)).collection == []

    await submit(cog, channel, member, drop, code_of(drop, 0))  # the card was never lost -- retry works
    assert len((await cog._member_state(member)).collection) == 1


@pytest.mark.asyncio
async def test_a_failed_result_announcement_does_not_undo_the_claim(cog, monkeypatch):
    guild, admin, (member,), channel, drop, _ = await ready_drop(cog, 218, n_members=1, n_cards=2)

    async def broken_send(*args, **kwargs):
        raise discord.HTTPException(FakeHTTPResponse(500, "Server Error"), "boom")

    monkeypatch.setattr(channel, "send", broken_send)
    reply = await submit(cog, channel, member, drop, code_of(drop, 0))  # must not raise

    assert "claimed" in reply.lower()
    assert (await cog._member_state(member)).collection == [drop.cards[0]["card_id"]]


@pytest.mark.asyncio
async def test_expired_drop_stops_accepting_codes_and_is_forgotten(cog):
    guild, admin, (member,), channel, drop, drop_message = await ready_drop(cog, 210, n_members=1, drop_expiry_seconds=60)
    assert drop.expires_at is not None and drop.expires_at > time.monotonic()

    drop.expires_at = time.monotonic() - 1
    reply = await submit(cog, channel, member, drop, code_of(drop))

    assert "over" in reply.lower()
    assert (await cog._member_state(member)).collection == []
    assert drop_message.id not in cog.active_drops


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["expires", "forgotten"])
async def test_a_claim_queued_behind_the_lock_rechecks_the_drop_when_it_gets_it(cog, how):
    """The drop can expire (or be released) while a claim is waiting its turn
    for the claim lock. Whatever was true when the code was submitted is not
    good enough -- the checks must run again once the lock is held."""
    guild, admin, (member,), channel, drop, drop_message = await ready_drop(cog, 215, n_members=1)
    lock = cog._get_claim_lock(guild.id)
    await lock.acquire()
    task = asyncio.ensure_future(submit(cog, channel, member, drop, code_of(drop)))
    await asyncio.sleep(0)  # let it run up to the lock and wait there
    assert not task.done()

    if how == "expires":
        drop.expires_at = time.monotonic() - 1
    else:
        cog._forget_drop(drop_message.id)
    lock.release()
    reply = await task

    assert "over" in reply.lower()
    assert (await cog._member_state(member)).collection == []


@pytest.mark.asyncio
async def test_a_locked_out_member_queued_behind_the_lock_still_cannot_win(cog):
    """Locked out *while* their correct code waits for the lock (a wrong
    guess landing in between) -- the lockout must be honored at the lock."""
    guild, admin, (member,), channel, drop, _ = await ready_drop(cog, 216, n_members=1, max_wrong_guesses=1)
    lock = cog._get_claim_lock(guild.id)
    await lock.acquire()
    task = asyncio.ensure_future(submit(cog, channel, member, drop, code_of(drop)))
    await asyncio.sleep(0)
    assert not task.done()

    drop.wrong_guesses[member.id] = 1  # used up their guesses in the meantime
    lock.release()
    reply = await task

    assert "locked out" in reply
    assert (await cog._member_state(member)).collection == []


@pytest.mark.asyncio
async def test_card_with_missing_art_is_dropped_from_the_claimable_set(cog, monkeypatch):
    """A card whose art can't be read has no visible code, so it must not
    linger in drop.cards -- it could never be claimed, and the drop would
    never count as fully claimed."""
    import random as random_mod

    guild = FakeGuild(213)
    admin = FakeMember(2130, guild)
    guild.members = {2130: admin}
    channel = FakeChannel(21300, guild)
    cog.bot.register_channel(channel)
    for i in range(2):
        ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes((i * 60, 40, 40)))])
        await cog.card.commands["addcard"].callback(cog, ctx, "common", name_and_series=f"M{i} | Anime")
    pool = await cog.config.guild(guild).pool()
    missing_id = sorted(int(k) for k in pool)[0]
    storage.card_image_path(cog.data_path, guild.id, missing_id).unlink()
    await cog.config.guild(guild).drop_size.set(6)

    rolled = {}
    real_build = cc_module.engine.build_drop

    def seeded_build(*args, **kwargs):
        drop = real_build(*args, rng=random_mod.Random(5), **kwargs)
        rolled["ids"] = {c["card_id"] for c in drop.cards}
        return drop

    monkeypatch.setattr(cc_module.engine, "build_drop", seeded_build)
    assert await cog._post_drop(channel, guild, is_test=False) is None

    assert missing_id in rolled["ids"], "test setup: the seeded roll must include the card with missing art"
    drop = cog.active_drops[channel.sent[-1].id]
    assert drop.cards and all(c["card_id"] != missing_id for c in drop.cards)


# -- the button and the pop-up ------------------------------------------------


@pytest.mark.asyncio
async def test_pressing_claim_opens_the_code_popup(cog):
    guild, admin, (member,), channel, drop, drop_message = await ready_drop(cog, 219, n_members=1)
    interaction = FakeInteraction(member, channel, message=drop_message)

    await drop_message.view.claim.callback(interaction)

    modal = interaction.response.modal
    assert isinstance(modal, views.CodeModal)
    assert modal.drop_message_id == drop_message.id
    assert interaction.response.messages == []


@pytest.mark.asyncio
@pytest.mark.parametrize("why", ["over", "already", "locked", "cooldown", "penalty"])
async def test_pressing_claim_when_it_cannot_work_explains_privately_instead_of_opening_the_popup(cog, why):
    guild, admin, (member, other), channel, drop, drop_message = await ready_drop(
        cog, 220, n_members=2, n_cards=2, max_wrong_guesses=1
    )
    if why == "over":
        cog._forget_drop(drop_message.id)
    elif why == "already":
        await submit(cog, channel, member, drop, code_of(drop, 0))
    elif why == "locked":
        drop.wrong_guesses[member.id] = 1
    elif why == "cooldown":
        cog.claim_cooldown_until[member.id] = time.monotonic() + 100
    elif why == "penalty":
        cog.wrong_guess_penalty_until[member.id] = time.monotonic() + 100
    interaction = FakeInteraction(member, channel, message=drop_message)

    await drop_message.view.claim.callback(interaction)

    assert interaction.response.modal is None
    (text, ephemeral), = interaction.response.messages
    assert ephemeral is True and text


@pytest.mark.asyncio
async def test_a_button_press_after_a_restart_gets_a_friendly_reply(cog):
    """After a restart the in-memory drop is gone, but the persistent button
    still fires -- it must answer 'over', not raise or open a dead pop-up."""
    guild, admin, (member,), channel, drop, drop_message = await ready_drop(cog, 221, n_members=1)
    cog.active_drops.clear()  # what a restart does
    interaction = FakeInteraction(member, channel, message=drop_message)

    await views.ClaimView(cog).claim.callback(interaction)

    assert interaction.response.modal is None
    assert "over" in interaction.response.messages[0][0].lower()


@pytest.mark.asyncio
async def test_cog_load_registers_one_persistent_claim_view(cog):
    await cog.cog_load()
    try:
        assert len(cog.bot.added_views) == 1
        view = cog.bot.added_views[0]
        assert isinstance(view, views.ClaimView)
        assert view.timeout is None, "persistent views must never time out"
        assert view.claim.custom_id == views.CLAIM_BUTTON_ID
    finally:
        await cog.cog_unload()


@pytest.mark.asyncio
async def test_popup_submission_defers_privately_then_replies_privately(cog):
    guild, admin, (member,), channel, drop, drop_message = await ready_drop(cog, 222, n_members=1)
    interaction = FakeInteraction(member, channel, message=drop_message)
    modal = views.CodeModal(cog, drop_message.id)

    await modal.handle_submit(interaction, wrong_code_for(drop))

    assert interaction.response.deferred is True
    (text, ephemeral), = interaction.followup.messages
    assert ephemeral is True and "Wrong code" in text


@pytest.mark.asyncio
async def test_a_slow_acknowledgement_cannot_lose_a_race_the_member_won(cog):
    """Both members submit the same code, first one first -- but the first
    member's acknowledgement is much slower than the second's. The claim must
    be decided by submission order, not by whose acknowledgement finished
    first, so the first submitter still wins."""
    guild, admin, (first, second), channel, drop, drop_message = await ready_drop(cog, 223, n_members=2, n_cards=2)
    slow = FakeInteraction(first, channel, message=drop_message, defer_delay=25)
    fast = FakeInteraction(second, channel, message=drop_message, defer_delay=0)
    modal_a = views.CodeModal(cog, drop_message.id)
    modal_b = views.CodeModal(cog, drop_message.id)

    await asyncio.gather(
        modal_a.handle_submit(slow, code_of(drop, 0)),
        modal_b.handle_submit(fast, code_of(drop, 0)),
    )

    assert (await cog._member_state(first)).collection == [drop.cards[0]["card_id"]]
    assert (await cog._member_state(second)).collection == []
    assert "claimed" in slow.followup.messages[0][0].lower()
    assert "too slow" in fast.followup.messages[0][0].lower()


@pytest.mark.asyncio
async def test_popup_survives_a_crash_in_claim_handling(cog, monkeypatch):
    guild, admin, (member,), channel, drop, drop_message = await ready_drop(cog, 224, n_members=1)

    async def boom(*args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(cog, "submit_code", boom)
    interaction = FakeInteraction(member, channel, message=drop_message)

    await views.CodeModal(cog, drop_message.id).handle_submit(interaction, "K3+9T")  # must not raise

    (text, ephemeral), = interaction.followup.messages
    assert ephemeral is True and "try again" in text.lower()


@pytest.mark.asyncio
async def test_settings_commands_for_the_captcha_mechanic(cog):
    guild = FakeGuild(214)
    admin = FakeMember(2140, guild)
    channel = FakeChannel(21400, guild)
    set_cmds = cog.card.commands["set"].commands

    for removed in ("decoys", "decoycount"):
        assert removed not in set_cmds, f"`.card set {removed}` belonged to the emoji mechanic and should be gone"

    ctx = FakeCtx(admin, guild, channel)
    await set_cmds["wrongguesses"].callback(cog, ctx, 5)
    assert await cog.config.guild(guild).max_wrong_guesses() == 5
    await set_cmds["wrongguesses"].callback(cog, ctx, -1)
    assert "negative" in ctx.sent[-1].content.lower()
    assert await cog.config.guild(guild).max_wrong_guesses() == 5
    await set_cmds["wrongguesses"].callback(cog, ctx, 0)
    assert "unlimited" in ctx.sent[-1].content.lower()

    await set_cmds["dropexpiry"].callback(cog, ctx, 120)
    assert await cog.config.guild(guild).drop_expiry_seconds() == 120
    await set_cmds["dropexpiry"].callback(cog, ctx, 5)
    assert "30" in ctx.sent[-1].content
    assert await cog.config.guild(guild).drop_expiry_seconds() == 120

    await set_cmds["wrongguesspenalty"].callback(cog, ctx, 90)
    assert await cog.config.guild(guild).wrong_guess_penalty_seconds() == 90
    await set_cmds["wrongguesspenalty"].callback(cog, ctx, -5)
    assert "negative" in ctx.sent[-1].content.lower()
    assert await cog.config.guild(guild).wrong_guess_penalty_seconds() == 90

    await cog.card.commands["settings"].callback(cog, ctx)
    field_names = [f.name for f in ctx.sent[-1].embeds[0].fields]
    assert "Wrong guesses / drop" in field_names and "Drop expiry" in field_names
    assert not any("decoy" in n.lower() or "claim window" in n.lower() for n in field_names)


@pytest.mark.asyncio
async def test_defaults_give_one_retry_then_lock_the_member_out(cog):
    """Out of the box: a first wrong code says 'one guess left', and the
    second wrong code triggers the lockout message."""
    guild, admin, (member,), channel, drop, _ = await ready_drop(cog, 225, n_members=1)
    assert drop.max_wrong_guesses == 2
    wrong = wrong_code_for(drop)

    assert "1 guess left" in await submit(cog, channel, member, drop, wrong)
    assert not drop.is_locked_out(member.id)
    assert "locked out" in await submit(cog, channel, member, drop, wrong)
    assert drop.is_locked_out(member.id)
    assert member.dms == [], "nothing is DM'd any more -- replies are ephemeral"
