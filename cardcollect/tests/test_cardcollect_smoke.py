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
from types import SimpleNamespace

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

    async def edit(self, content=None, embed=None, file=None, view=None, attachments=None, **kwargs):
        if content is not None:
            self.content = content
        if embed is not None:
            self.embeds = [embed]
        if attachments is not None:
            self.files = list(attachments)
        if view is not None:
            self.view = view
        self.edits = getattr(self, "edits", 0) + 1

    async def reply(self, content=None, **kwargs):
        sent = await self.channel.send(content, **kwargs)
        sent.replied_to = self
        return sent


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

    async def send(self, content=None, embed=None, file=None, view=None, files=None, **kwargs):
        msg = FakeMessage(self._next_id, self, content=content, embed=embed, file=file, view=view)
        if files:
            msg.files.extend(files)
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


def _admin(id_, guild):
    member = FakeMember(id_, guild)
    member.guild_permissions = SimpleNamespace(manage_guild=True, administrator=False)
    return member


async def _seed_search_pool(cog, guild, admin, channel):
    """Add Lucy / Lucy Heartfilia / Rebecca / Nami cards; return ids by name."""
    ids = {}
    for rarity, entry in [
        ("rare", "Lucy | Cyberpunk: Edgerunners"),
        ("common", "Lucy Heartfilia | Fairy Tail"),
        ("epic", "Rebecca | Cyberpunk: Edgerunners"),
        ("common", "Nami | One Piece"),
    ]:
        ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes())])
        await cog.card.commands["addcard"].callback(cog, ctx, rarity, name_and_series=entry)
    pool = await cog.config.guild(guild).pool()
    for cid, data in pool.items():
        ids[data["name"]] = int(cid)
    return ids


@pytest.fixture
def cog():
    bot = FakeBot()
    c = cc_module.CardCollect(bot)
    return c


@pytest.mark.asyncio
async def test_settings_summary_and_channel_setting(cog):
    guild = FakeGuild(1)
    author = FakeMember(10, guild)
    channel = FakeChannel(100, guild)
    cog.bot.register_channel(channel)
    ctx = FakeCtx(author, guild, channel)

    assert "will post" in await cog._apply_channel(guild, author, channel)

    await cog.card.commands["settings"].callback(cog, ctx)  # a member without Manage Server gets the read-only summary
    embed = ctx.sent[-1].embeds[0]
    assert embed.title.startswith("cardcollect settings")












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

    monkeypatch.setattr(cc_module.import_characters, "fetch_top_characters", fake_fetch)

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
    await cog._apply_channel(guild, ctx.author, channel)
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
    await cog._apply_channel(guild, ctx.author, channel)
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

from cardcollect import views  # noqa: E402
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
        self.edited = None

    async def defer(self, ephemeral=False):
        for _ in range(self.defer_delay):
            await asyncio.sleep(0)
        self.deferred = ephemeral

    async def send_message(self, content=None, ephemeral=False):
        self.messages.append((content, ephemeral))

    async def send_modal(self, modal):
        self.modal = modal

    async def edit_message(self, content=None, attachments=None, view=None):
        self.edited = {"content": content, "attachments": attachments, "view": view}


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
        self.original_edit = None

    async def edit_original_response(self, content=None, attachments=None, view=None):
        self.original_edit = {"content": content, "attachments": attachments, "view": view}
































































# -- the button and the pop-up ------------------------------------------------




















# -- "all cards claimed" announcement ------------------------------------------
















# ---------------------------------------------------------------------------
# .card gallery pagination (views.GalleryView)
# ---------------------------------------------------------------------------

from cardcollect import imagegen  # noqa: E402
from cardcollect.constants import GALLERY_PAGE_SIZE  # noqa: E402


async def _collection_of_n_unique_cards(cog, guild, admin, channel, owner, n):
    """Add `n` pool cards and give `owner` exactly one of each, distinct
    card_ids -- what drives .card's page count."""
    card_ids = []
    for i in range(n):
        ctx = FakeCtx(admin, guild, channel, attachments=[FakeAttachment(fake_art_bytes((i, i, i)))])
        await cog.card.commands["addcard"].callback(cog, ctx, "common", name_and_series=f"Card{i} | S")
        pool = await cog.config.guild(guild).pool()
        card_ids = [int(cid) for cid in pool]  # pool grows in insertion order
    await cog._save_member_state(owner, MemberState(collection=card_ids))
    return card_ids


















# ---------------------------------------------------------------------------
# Gallery responsiveness: never shadow discord.py internals, ack before the
# render, and keep Pillow off the event loop (live "interaction failed" reports)
# ---------------------------------------------------------------------------

import threading  # noqa: E402












# --- activity tracking is buffered, not written per message -----------------


@pytest.mark.asyncio
async def test_track_activity_does_not_touch_config_per_message(cog):
    """REGRESSION: every message in the drop channel rewrote the cog's entire
    settings file. Counting must stay in memory until a flush."""
    guild = FakeGuild(7)
    for _ in range(50):
        await cog._track_activity(guild)
    assert cog.config._guild_data.get(7, {}).get("activity_tracking") is None
    assert sum(cog._activity_pending[7].values()) == 50


@pytest.mark.asyncio
async def test_flush_activity_merges_counts_into_config_once(cog):
    guild = FakeGuild(8)
    for _ in range(5):
        await cog._track_activity(guild)
    await cog._flush_activity()
    tracking = await cog.config.guild(guild).activity_tracking()
    assert sum(tracking["hourly_buckets"].values()) == 5
    assert tracking["sampling_since"] > 0
    assert cog._activity_pending == {}

    for _ in range(3):
        await cog._track_activity(guild)
    await cog._flush_activity()
    tracking = await cog.config.guild(guild).activity_tracking()
    assert sum(tracking["hourly_buckets"].values()) == 8  # adds, never overwrites


@pytest.mark.asyncio
async def test_failed_flush_keeps_counts_for_retry(cog):
    guild = FakeGuild(9)
    for _ in range(4):
        await cog._track_activity(guild)

    real = cog.config.guild_from_id

    def boom(_gid):
        raise RuntimeError("disk full")

    cog.config.guild_from_id = boom
    await cog._flush_activity()
    assert sum(cog._activity_pending[9].values()) == 4

    cog.config.guild_from_id = real
    await cog._flush_activity()
    tracking = await cog.config.guild(guild).activity_tracking()
    assert sum(tracking["hourly_buckets"].values()) == 4




# --- search / show commands ---------------------------------------------------














# ---------------------------------------------------------------------------
# .card daily
# ---------------------------------------------------------------------------


async def _pool_with(cog, guild, channel, author, entries):
    for rarity, name in entries:
        ctx = FakeCtx(author, guild, channel, attachments=[FakeAttachment(fake_art_bytes())])
        await cog.card.commands["addcard"].callback(cog, ctx, rarity, name_and_series=f"{name} | Show")


@pytest.mark.asyncio
async def test_daily_pull_once_per_day_and_streak(cog, monkeypatch):
    from cardcollect import engine
    guild = FakeGuild(70)
    author = FakeMember(71, guild)
    channel = FakeChannel(700, guild)
    await _pool_with(cog, guild, channel, author, [("common", "A"), ("rare", "B")])

    days = iter(["2026-10-01", "2026-10-01", "2026-10-02", "2026-10-04"])
    monkeypatch.setattr(engine, "today_str", lambda tz, now=None: next(days))

    first = await cog.daily_pull(guild, author)
    assert not isinstance(first, str) and first[3] == 1
    again = await cog.daily_pull(guild, author)
    assert isinstance(again, str) and "already pulled" in again
    second = await cog.daily_pull(guild, author)
    assert second[3] == 2
    after_gap = await cog.daily_pull(guild, author)
    assert after_gap[3] == 1  # missed Oct 3 -> streak restarts

    state = await cog._member_state(author)
    assert len(state.collection) + len(state.sell_tokens) == 3
    assert state.daily_pull_date == "2026-10-04" and state.daily_streak == 1




@pytest.mark.asyncio
async def test_daily_pull_empty_pool(cog):
    guild = FakeGuild(74)
    author = FakeMember(75, guild)
    assert "empty" in await cog.daily_pull(guild, author)






# --- .card pool (member-facing whole-pool search) -----------------------------












# ===========================================================================
# 1.7.0: importpool gender + failed-art reporting + hand-added linking,
# `.card importseries` (search -> pick -> preview -> confirm), and the admin
# "Change rarity" dropdown on `.card info`
# ===========================================================================

from types import SimpleNamespace  # noqa: E402

import aiohttp  # noqa: E402

from cardcollect import views as cc_views  # noqa: E402




class _AniListResp:
    def __init__(self, payload=None, image=None, error=None):
        self._payload, self._image, self._error = payload, image, error

    async def __aenter__(self):
        if self._error is not None:
            raise self._error
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    async def json(self):
        return self._payload

    async def read(self):
        return self._image


class FakeAniList:
    """post() replays JSON payloads in order (the last repeats); get() serves
    card art, failing for chosen URLs the way a flaky image host would."""

    def __init__(self, posts=(), fail_urls=(), post_error=None, small_urls=()):
        self._posts = list(posts)
        self.post_calls = 0
        self.fail_urls = set(fail_urls)
        self.small_urls = set(small_urls)
        self.get_calls = []
        self.post_error = post_error

    def post(self, url, json=None):
        if self.post_error is not None:
            return _AniListResp(error=self.post_error)
        payload = self._posts[min(self.post_calls, len(self._posts) - 1)]
        self.post_calls += 1
        return _AniListResp(payload)

    def get(self, url):
        self.get_calls.append(url)
        if url in self.fail_urls:
            return _AniListResp(error=aiohttp.ClientError("image host down"))
        if url in self.small_urls:
            buf = io.BytesIO()
            Image.new("RGB", (90, 120), (9, 9, 9)).save(buf, format="PNG")
            return _AniListResp(image=buf.getvalue())
        return _AniListResp(image=fake_art_bytes())


def _bulk_char(id_, name, favourites=100, gender="Female", series="Some Show", english=None):
    return {
        "id": id_,
        "name": {"full": name, "native": name},
        "image": {"large": f"https://art.invalid/{id_}.png"},
        "favourites": favourites,
        "gender": gender,
        "media": {"nodes": [{"title": {"romaji": series, "english": english}}]},
    }


def _bulk_page(*chars):
    return {"data": {"Page": {"pageInfo": {"hasNextPage": False}, "characters": list(chars)}}}


def _series_char(id_, name, favourites, gender="Female"):
    return {
        "id": id_,
        "name": {"full": name, "native": name},
        "image": {"large": f"https://art.invalid/{id_}.png"},
        "favourites": favourites,
        "gender": gender,
    }


def _media(id_, romaji, english=None):
    return {
        "id": id_, "type": "ANIME", "format": "TV", "seasonYear": 2023,
        "startDate": {"year": 2023}, "title": {"romaji": romaji, "english": english},
    }


def _search_payload(*media):
    return {"data": {"Page": {"media": list(media)}}}


def _series_payload(nodes, total=None, romaji="Sousou no Frieren", english="Frieren: Beyond Journey's End"):
    return {
        "data": {
            "Media": {
                "id": 1,
                "title": {"romaji": romaji, "english": english},
                "characters": {"pageInfo": {"total": total or len(nodes), "hasNextPage": False}, "nodes": nodes},
            }
        }
    }


def _seventeen_characters():
    # favourites fall with the rank, so rank order == favourites order, as AniList returns them
    return [_series_char(100 + i, f"Char{i:02d}", 30000 - i * 1500) for i in range(17)]


class AdminInteraction:
    """Just enough of discord.Interaction for the admin views."""

    def __init__(self, user, guild, message=None):
        self.user = user
        self.guild = guild
        self.message = message or FakeMessage(900, None)
        self.edits, self.sent, self.followups, self.original_edits = [], [], [], []
        self.modal = None
        self.deferred = False
        outer = self

        class _Response:
            def is_done(self):
                return outer.deferred or bool(outer.edits or outer.sent or outer.modal)

            async def defer(self, **kwargs):
                outer.deferred = True

            async def edit_message(self, **kwargs):
                outer.edits.append(kwargs)

            async def send_message(self, content=None, **kwargs):
                outer.sent.append((content, kwargs))

            async def send_modal(self, modal):
                outer.modal = modal

        class _Followup:
            async def send(self, content=None, **kwargs):
                outer.followups.append((content, kwargs))

        self.response = _Response()
        self.followup = _Followup()

    async def edit_original_response(self, **kwargs):
        self.original_edits.append(kwargs)
        return self.message


# --- importpool -------------------------------------------------------------


@pytest.mark.asyncio
async def test_importpool_defaults_to_every_gender_and_can_be_narrowed(cog):
    guild = FakeGuild(70)
    admin = _admin(700, guild)
    ctx = FakeCtx(admin, guild, FakeChannel(7000, guild))
    cog._session = FakeAniList([_bulk_page(_bulk_char(1, "Alice"), _bulk_char(2, "Bob", gender="Male"))])

    await cog.card.commands["importpool"].callback(cog, ctx, count=10)
    names = sorted(e["name"] for e in (await cog.config.guild(guild).pool()).values())
    assert names == ["Alice", "Bob"]

    guild2 = FakeGuild(71)
    ctx2 = FakeCtx(_admin(710, guild2), guild2, FakeChannel(7100, guild2))
    cog._session = FakeAniList([_bulk_page(_bulk_char(1, "Alice"), _bulk_char(2, "Bob", gender="Male"))])
    await cog.card.commands["importpool"].callback(cog, ctx2, count=10, gender="male")
    assert [e["name"] for e in (await cog.config.guild(guild2).pool()).values()] == ["Bob"]


@pytest.mark.asyncio
async def test_importpool_rejects_an_unknown_gender_before_calling_anilist(cog):
    guild = FakeGuild(72)
    ctx = FakeCtx(_admin(720, guild), guild, FakeChannel(7200, guild))
    cog._session = FakeAniList(post_error=AssertionError("AniList must not be called"))
    await cog.card.commands["importpool"].callback(cog, ctx, count=10, gender="robots")
    assert "Gender must be one of" in ctx.sent[-1].content


@pytest.mark.asyncio
async def test_importpool_reports_characters_whose_art_failed_to_download(cog):
    guild = FakeGuild(73)
    ctx = FakeCtx(_admin(730, guild), guild, FakeChannel(7300, guild))
    cog._session = FakeAniList(
        [_bulk_page(_bulk_char(1, "Alice"), _bulk_char(2, "Broken"))], fail_urls={"https://art.invalid/2.png"}
    )
    await cog.card.commands["importpool"].callback(cog, ctx, count=10)
    assert [e["name"] for e in (await cog.config.guild(guild).pool()).values()] == ["Alice"]
    assert "1 image download(s) failed" in ctx.sent[-1].content
    assert "Broken" in ctx.sent[-1].content


@pytest.mark.asyncio
async def test_importpool_does_not_bring_back_a_character_that_was_retired(cog):
    """Pruning a character with `.card removecard` retires it; the next import must not re-add it."""
    guild = FakeGuild(74)
    ctx = FakeCtx(_admin(740, guild), guild, FakeChannel(7400, guild))
    async with cog.config.guild(guild).all() as data:
        data["pool"]["1"] = Card(
            card_id=1, name="Pruned", series="S", rarity="common", image_path="1.png", anilist_id=42, retired=True
        ).to_dict()
        data["next_id"] = 2
    cog._session = FakeAniList([_bulk_page(_bulk_char(42, "Pruned"), _bulk_char(43, "Fresh"))])

    await cog.card.commands["importpool"].callback(cog, ctx, count=10)
    pool = await cog.config.guild(guild).pool()
    assert sorted(e["anilist_id"] for e in pool.values()) == [42, 43]
    assert pool["1"]["retired"] is True  # still retired, not duplicated or revived


@pytest.mark.asyncio
async def test_importpool_links_a_hand_added_card_instead_of_duplicating_it(cog):
    guild = FakeGuild(75)
    ctx = FakeCtx(_admin(750, guild), guild, FakeChannel(7500, guild))
    async with cog.config.guild(guild).all() as data:
        data["pool"]["1"] = Card(
            card_id=1, name="Alice", series="Some Show", rarity="rare", image_path="1.png", added_by=5
        ).to_dict()
        data["next_id"] = 2
    cog._session = FakeAniList([_bulk_page(_bulk_char(7, "Alice"), _bulk_char(8, "Bob"))])

    await cog.card.commands["importpool"].callback(cog, ctx, count=10)
    pool = await cog.config.guild(guild).pool()
    assert sorted(e["name"] for e in pool.values()) == ["Alice", "Bob"]  # Alice not duplicated
    assert pool["1"]["anilist_id"] == 7  # the hand-added card learned its AniList id
    assert "linked" in ctx.sent[-1].content

    cog._session = FakeAniList([_bulk_page(_bulk_char(7, "Alice"), _bulk_char(8, "Bob"))])
    await cog.card.commands["importpool"].callback(cog, ctx, count=10)
    assert len(await cog.config.guild(guild).pool()) == 2  # and a re-run adds nothing


# --- importseries -----------------------------------------------------------


async def _open_series_preview(cog, guild, admin, channel, characters, query="frieren"):
    cog._session = FakeAniList(
        [_search_payload(_media(1, "Sousou no Frieren", "Frieren: Beyond Journey's End")), _series_payload(characters)]
    )
    ctx = FakeCtx(admin, guild, channel)
    await cog.card.commands["importseries"].callback(cog, ctx, series=query)
    return ctx, ctx.sent[-1]


@pytest.mark.asyncio
async def test_importseries_previews_without_importing_anything(cog):
    guild = FakeGuild(80)
    admin = _admin(800, guild)
    ctx, msg = await _open_series_preview(cog, guild, admin, FakeChannel(8000, guild), _seventeen_characters())

    assert isinstance(msg.view, cc_views.SeriesPreviewView)
    embed = msg.embeds[0]
    assert embed.title == "Import series: Sousou no Frieren"
    fields = {f.name: f.value for f in embed.fields}
    assert "**15** new" in fields["Top 15"]
    assert "Import everything checked" in fields  # 17 characters were checked, only 15 are in the default slice
    assert await cog.config.guild(guild).pool() == {}  # a preview must never write to the pool


@pytest.mark.asyncio
async def test_importseries_import_top_adds_the_top_characters_with_rarity_and_titles(cog):
    guild = FakeGuild(81)
    admin = _admin(810, guild)
    ctx, msg = await _open_series_preview(cog, guild, admin, FakeChannel(8100, guild), _seventeen_characters())
    inter = AdminInteraction(admin, guild, msg)

    await msg.view.import_top.callback(inter)

    pool = await cog.config.guild(guild).pool()
    assert len(pool) == 15
    by_name = {e["name"]: e for e in pool.values()}
    assert "Char15" not in by_name and "Char16" not in by_name  # ranks 15 and 16 were outside the top 15
    assert by_name["Char00"]["rarity"] == "legendary"  # 30,000 favourites
    assert by_name["Char14"]["rarity"] == "epic"  # 9,000 favourites
    assert all(e["series"] == "Sousou no Frieren" for e in pool.values())
    assert all(e["series_alt"] == "Frieren: Beyond Journey's End" for e in pool.values())
    assert all(e["anilist_id"] is not None and e["added_by"] is None for e in pool.values())
    assert inter.original_edits[-1]["embed"].title == "Imported: Sousou no Frieren"
    assert (await cog._session.get("x").__aenter__()) is not None  # (session still usable afterwards)


@pytest.mark.asyncio
async def test_importseries_import_all_and_a_rerun_never_duplicates(cog):
    guild = FakeGuild(82)
    admin = _admin(820, guild)
    chars = _seventeen_characters()
    ctx, msg = await _open_series_preview(cog, guild, admin, FakeChannel(8200, guild), chars)
    await msg.view.import_all.callback(AdminInteraction(admin, guild, msg))
    assert len(await cog.config.guild(guild).pool()) == 17

    # run it again: everything is already there, so the preview has nothing to import
    ctx2, msg2 = await _open_series_preview(cog, guild, admin, FakeChannel(8201, guild), chars)
    assert msg2.view.import_top.disabled is True
    assert "already" in {f.name: f.value for f in msg2.embeds[0].fields}["Top 15"]
    assert len(await cog.config.guild(guild).pool()) == 17


@pytest.mark.asyncio
async def test_importseries_a_stale_preview_cannot_add_a_character_twice(cog):
    """Two admins open the same preview, or one presses Import twice across messages."""
    guild = FakeGuild(83)
    admin = _admin(830, guild)
    ctx, msg = await _open_series_preview(cog, guild, admin, FakeChannel(8300, guild), _seventeen_characters())
    preview = msg.view.preview
    await cog.commit_series(guild, preview, 15)
    await cog.commit_series(guild, preview, 15)  # same stale preview again
    assert len(await cog.config.guild(guild).pool()) == 15


@pytest.mark.asyncio
async def test_importseries_never_resurrects_a_retired_series_character(cog):
    guild = FakeGuild(84)
    admin = _admin(840, guild)
    async with cog.config.guild(guild).all() as data:
        data["pool"]["1"] = Card(
            card_id=1, name="Char00", series="Sousou no Frieren", rarity="legendary",
            image_path="1.png", anilist_id=100, retired=True,
        ).to_dict()
        data["next_id"] = 2
    ctx, msg = await _open_series_preview(cog, guild, admin, FakeChannel(8400, guild), _seventeen_characters())
    await msg.view.import_top.callback(AdminInteraction(admin, guild, msg))
    pool = await cog.config.guild(guild).pool()
    assert sorted(e["name"] for e in pool.values()).count("Char00") == 1
    assert pool["1"]["retired"] is True


@pytest.mark.asyncio
async def test_importseries_links_a_hand_added_card_of_the_same_character(cog):
    guild = FakeGuild(85)
    admin = _admin(850, guild)
    async with cog.config.guild(guild).all() as data:
        data["pool"]["1"] = Card(
            card_id=1, name="Char01", series="Sousou no Frieren", rarity="epic", image_path="1.png", added_by=9
        ).to_dict()
        data["next_id"] = 2
    ctx, msg = await _open_series_preview(cog, guild, admin, FakeChannel(8500, guild), _seventeen_characters())
    assert "will be linked" in {f.name: f.value for f in msg.embeds[0].fields}["Top 15"]

    await msg.view.import_top.callback(AdminInteraction(admin, guild, msg))
    pool = await cog.config.guild(guild).pool()
    assert sorted(e["name"] for e in pool.values()).count("Char01") == 1  # not duplicated
    assert pool["1"]["anilist_id"] == 101
    assert pool["1"]["rarity"] == "epic"  # the hand-added card keeps the rarity you gave it


@pytest.mark.asyncio
async def test_importseries_with_several_matches_asks_which_one(cog):
    guild = FakeGuild(86)
    admin = _admin(860, guild)
    cog._session = FakeAniList(
        [
            _search_payload(_media(1, "Sousou no Frieren", "Frieren"), _media(2, "Sousou no Frieren (Manga)")),
            _series_payload(_seventeen_characters()),
        ]
    )
    ctx = FakeCtx(admin, guild, FakeChannel(8600, guild))
    await cog.card.commands["importseries"].callback(cog, ctx, series="frieren")
    pick = ctx.sent[-1]
    assert isinstance(pick.view, cc_views.SeriesPickView)
    assert pick.embeds[0].title == "Import series: which one?"
    assert await cog.config.guild(guild).pool() == {}

    inter = AdminInteraction(admin, guild, pick)
    pick.view.select._values = ["1"]
    await pick.view._picked(inter)
    assert inter.deferred
    shown = inter.original_edits[-1]
    assert shown["embed"].title == "Import series: Sousou no Frieren"
    assert isinstance(shown["view"], cc_views.SeriesPreviewView)


@pytest.mark.asyncio
async def test_importseries_reports_no_match_and_anilist_failures_without_crashing(cog):
    guild = FakeGuild(87)
    admin = _admin(870, guild)
    ctx = FakeCtx(admin, guild, FakeChannel(8700, guild))

    cog._session = FakeAniList([_search_payload()])
    await cog.card.commands["importseries"].callback(cog, ctx, series="zzzz")
    assert "no series matching" in ctx.sent[-1].content.lower()

    request_info = SimpleNamespace(real_url="https://graphql.anilist.co")
    cog._session = FakeAniList(post_error=aiohttp.ClientResponseError(request_info, (), status=500, message="boom"))
    await cog.card.commands["importseries"].callback(cog, ctx, series="frieren")
    assert "AniList request failed" in ctx.sent[-1].content

    cog._session = FakeAniList([{"data": None}])
    await cog.card.commands["importseries"].callback(cog, ctx, series="frieren")
    assert "wasn't in the shape" in ctx.sent[-1].content
    assert await cog.config.guild(guild).pool() == {}


@pytest.mark.asyncio
async def test_importseries_views_refuse_anyone_but_the_admin_who_ran_it(cog):
    guild = FakeGuild(88)
    admin = _admin(880, guild)
    ctx, msg = await _open_series_preview(cog, guild, admin, FakeChannel(8800, guild), _seventeen_characters())
    stranger_inter = AdminInteraction(FakeMember(881, guild), guild, msg)
    assert await msg.view.interaction_check(stranger_inter) is False
    assert "Only the admin" in stranger_inter.sent[-1][0]
    assert await msg.view.interaction_check(AdminInteraction(admin, guild, msg)) is True


@pytest.mark.asyncio
async def test_importseries_cancel_adds_nothing(cog):
    guild = FakeGuild(89)
    admin = _admin(890, guild)
    ctx, msg = await _open_series_preview(cog, guild, admin, FakeChannel(8900, guild), _seventeen_characters())
    inter = AdminInteraction(admin, guild, msg)
    await msg.view.cancel.callback(inter)
    assert "cancelled" in inter.edits[-1]["content"].lower()
    assert await cog.config.guild(guild).pool() == {}


@pytest.mark.asyncio
async def test_importseries_rarity_edits_change_what_gets_imported(cog):
    guild = FakeGuild(90)
    admin = _admin(900, guild)
    ctx, msg = await _open_series_preview(cog, guild, admin, FakeChannel(9000, guild), _seventeen_characters())
    view = msg.view

    inter = AdminInteraction(admin, guild, msg)
    await view.adjust.callback(inter)
    modal = inter.modal
    assert "Char00: legendary" in modal.changes.default  # prefilled with the current tiers, ready to edit

    modal.changes._value = "Char08: legendary\nNobody: rare"
    submit = AdminInteraction(admin, guild, msg)
    await modal.on_submit(submit)
    assert view.preview.overrides == {8: "legendary"}
    assert "Your edits" in {f.name for f in submit.edits[-1]["embed"].fields}
    assert "No new character matches" in submit.followups[-1][0]  # the bad line is reported, not guessed at

    await view.import_top.callback(AdminInteraction(admin, guild, msg))
    pool = await cog.config.guild(guild).pool()
    assert {e["name"]: e["rarity"] for e in pool.values()}["Char08"] == "legendary"

    # putting a tier back to what its favourites say drops the override again
    ctx2, msg2 = await _open_series_preview(cog, guild, admin, FakeChannel(9001, guild), _seventeen_characters())
    modal2 = (await _press(msg2.view.adjust, admin, guild, msg2)).modal
    modal2.changes._value = "Char16: legendary"
    await modal2.on_submit(AdminInteraction(admin, guild, msg2))
    assert msg2.view.preview.overrides == {16: "legendary"}
    modal2.changes._value = "Char16: rare"
    await modal2.on_submit(AdminInteraction(admin, guild, msg2))
    assert msg2.view.preview.overrides == {}


async def _press(button, user, guild, message):
    inter = AdminInteraction(user, guild, message)
    await button.callback(inter)
    return inter


# --- `.card info` admin rarity dropdown ----------------------------------------


async def _seed_one_card(cog, guild, rarity="common"):
    async with cog.config.guild(guild).all() as data:
        card_id = data["next_id"]
        data["next_id"] += 1
        storage.save_card_image(cog.data_path, guild.id, card_id, fake_art_bytes())
        data["pool"][str(card_id)] = Card(
            card_id=card_id, name="Rem", series="Re:Zero", rarity=rarity,
            image_path=str(storage.card_image_path(cog.data_path, guild.id, card_id)),
        ).to_dict()
    return card_id




@pytest.mark.asyncio
async def test_change_rarity_keeps_existing_sell_tokens_at_the_value_they_were_issued_with(cog):
    guild = FakeGuild(92)
    card_id = await _seed_one_card(cog, guild, "common")
    from cardcollect import engine as cc_engine
    from cardcollect.models import MemberState

    owner = FakeMember(920, guild)
    token = cc_engine.make_sell_token(card_id, "common")
    await cog._save_member_state(owner, MemberState(collection=[card_id], sell_tokens=[token]))

    card = await cog.apply_card_rarity(guild, card_id, "legendary")
    assert card.rarity == "legendary"
    state = await cog._member_state(owner)
    assert state.sell_tokens[0].rarity == "common"  # captured at claim time, so no retroactive windfall


@pytest.mark.asyncio
async def test_apply_card_rarity_rejects_a_bad_tier_and_a_missing_card(cog):
    guild = FakeGuild(93)
    card_id = await _seed_one_card(cog, guild)
    with pytest.raises(ValueError):
        await cog.apply_card_rarity(guild, card_id, "shiny")
    with pytest.raises(KeyError):
        await cog.apply_card_rarity(guild, 999, "epic")


# ===========================================================================
# 1.7.1: contact sheet, skip, art upload, poor-art flags, Replace art on info
# ===========================================================================

import asyncio  # noqa: E402


class FakeUpload:
    """An admin's message carrying an image (or not) in reply to the bot's prompt."""

    def __init__(self, author, channel, content="", data=None):
        self.author = author
        self.channel = channel
        self.content = content
        self.attachments = [FakeAttachment(data)] if data is not None else []
        self.deleted = False

    async def delete(self):
        self.deleted = True


def _queue_uploads(cog, *messages):
    queue = list(messages)

    async def wait_for(event, check=None, timeout=None):
        assert event == "message"
        while queue:
            message = queue.pop(0)
            if check is None or check(message):
                return message
        raise asyncio.TimeoutError

    cog.bot.wait_for = wait_for


def _inter(admin, guild, channel, msg):
    inter = AdminInteraction(admin, guild, msg)
    inter.channel = channel
    return inter


@pytest.mark.asyncio
async def test_series_preview_shows_a_numbered_contact_sheet_without_writing(cog):
    guild = FakeGuild(100)
    admin = _admin(1000, guild)
    chan = FakeChannel(10000, guild)
    ctx, msg = await _open_series_preview(cog, guild, admin, chan, _seventeen_characters())
    assert [f.filename for f in msg.files] == ["contact_sheet.png"]
    assert msg.embeds[0].image.url == "attachment://contact_sheet.png"
    lines = {f.name: f.value for f in msg.embeds[0].fields}["New characters"].splitlines()
    assert lines[0].startswith("`1`") and "Char00" in lines[0]
    assert len(cog._session.get_calls) == 15  # one download per card in the grid, none for the other two
    assert await cog.config.guild(guild).pool() == {}


@pytest.mark.asyncio
async def test_poor_anilist_art_is_flagged_in_the_preview(cog):
    guild = FakeGuild(101)
    admin = _admin(1010, guild)
    chars = _seventeen_characters()
    chars[2]["image"] = {"large": "https://s4.anilist.co/file/anilistcdn/character/large/default.jpg"}
    cog._session = FakeAniList(
        [_search_payload(_media(1, "Sousou no Frieren")), _series_payload(chars)], small_urls={"https://art.invalid/101.png"}
    )
    ctx = FakeCtx(admin, guild, FakeChannel(10100, guild))
    await cog.card.commands["importseries"].callback(cog, ctx, series="frieren")
    text = {f.name: f.value for f in ctx.sent[-1].embeds[0].fields}["New characters"]
    assert "⚠ small (90x120)" in text.splitlines()[1]
    assert "⚠ AniList placeholder" in text.splitlines()[2]
    assert "⚠" not in text.splitlines()[0]


@pytest.mark.asyncio
async def test_import_reuses_the_art_it_already_downloaded(cog):
    guild = FakeGuild(102)
    admin = _admin(1020, guild)
    ctx, msg = await _open_series_preview(cog, guild, admin, FakeChannel(10200, guild), _seventeen_characters())
    before = len(cog._session.get_calls)
    await msg.view.import_top.callback(_inter(admin, guild, ctx.channel, msg))
    assert len(cog._session.get_calls) == before  # nothing fetched twice
    assert len(await cog.config.guild(guild).pool()) == 15
    # "import all" needs only the 2 it never showed
    ctx2, msg2 = await _open_series_preview(cog, guild, admin, FakeChannel(10201, guild), _seventeen_characters(), "x")
    before = len(cog._session.get_calls)
    await msg2.view.import_all.callback(_inter(admin, guild, ctx2.channel, msg2))
    assert len(cog._session.get_calls) - before == 2
    assert len(await cog.config.guild(guild).pool()) == 17


@pytest.mark.asyncio
async def test_adjust_can_skip_and_unskip_characters_by_number_or_name(cog):
    guild = FakeGuild(103)
    admin = _admin(1030, guild)
    chan = FakeChannel(10300, guild)
    ctx, msg = await _open_series_preview(cog, guild, admin, chan, _seventeen_characters())
    view = msg.view
    inter = _inter(admin, guild, chan, msg)
    await view.adjust.callback(inter)
    modal = inter.modal
    modal.changes._value = "skip 2, Char04\nskip 99"
    submit = _inter(admin, guild, chan, msg)
    await modal.on_submit(submit)
    assert view.preview.skips == {1, 4}
    assert "no number 99" in submit.followups[-1][0]
    text = {f.name: f.value for f in submit.edits[-1]["embed"].fields}["New characters"]
    assert "~~Char01~~ skipped" in text
    assert "**13** new" in {f.name: f.value for f in submit.edits[-1]["embed"].fields}["Top 15"]

    await view.import_top.callback(_inter(admin, guild, chan, msg))
    names = {e["name"] for e in (await cog.config.guild(guild).pool()).values()}
    assert "Char01" not in names and "Char04" not in names and len(names) == 13

    guild2 = FakeGuild(1031)  # a fresh pool, so the numbers start from the top again
    ctx2, msg2 = await _open_series_preview(cog, guild2, admin, FakeChannel(10301, guild2), _seventeen_characters(), "y")
    modal2 = (await _press(msg2.view.adjust, admin, guild2, msg2)).modal
    modal2.changes._value = "skip 1, 2\nunskip 2"
    await modal2.on_submit(_inter(admin, guild2, chan, msg2))
    assert msg2.view.preview.skips == {0}


@pytest.mark.asyncio
async def test_replace_art_by_upload_is_used_for_the_import(cog):
    guild = FakeGuild(104)
    admin = _admin(1040, guild)
    chan = FakeChannel(10400, guild)
    ctx, msg = await _open_series_preview(cog, guild, admin, chan, _seventeen_characters())
    mine = fake_art_bytes((1, 2, 3))
    good = FakeUpload(admin, chan, "3", mine)
    _queue_uploads(
        cog,
        FakeUpload(FakeMember(5, guild), chan, "4", fake_art_bytes()),  # someone else: ignored
        good,
        FakeUpload(admin, chan, "4", b"this is not an image"),
        FakeUpload(admin, chan, "", fake_art_bytes()),  # no number
        FakeUpload(admin, chan, "Nobody", fake_art_bytes()),
        FakeUpload(admin, chan, "done"),
    )
    inter = _inter(admin, guild, chan, msg)
    await msg.view.replace_art.callback(inter)

    assert msg.view.preview.art == {2: mine}
    assert good.deleted  # the upload is cleaned out of the channel
    said = [c[0] for c in inter.followups]
    assert any("Char02" in s and "#3" in s and "replaced" in s for s in said)
    assert any("isn't an image" in s for s in said)
    assert any("Name or number missing" in s for s in said)
    assert any('No new character matches "Nobody"' in s for s in said)
    assert msg.view._listening is False

    await msg.view.import_top.callback(_inter(admin, guild, chan, msg))
    pool = await cog.config.guild(guild).pool()
    id_of = {e["name"]: int(k) for k, e in pool.items()}
    assert storage.read_card_image(cog.data_path, guild.id, id_of["Char02"]) == mine
    assert storage.read_card_image(cog.data_path, guild.id, id_of["Char03"]) == fake_art_bytes()


@pytest.mark.asyncio
async def test_replace_art_stops_quietly_when_nothing_is_sent(cog):
    guild = FakeGuild(105)
    admin = _admin(1050, guild)
    chan = FakeChannel(10500, guild)
    ctx, msg = await _open_series_preview(cog, guild, admin, chan, _seventeen_characters())
    _queue_uploads(cog)
    inter = _inter(admin, guild, chan, msg)
    await msg.view.replace_art.callback(inter)
    assert msg.view.preview.art == {} and msg.view._listening is False




@pytest.mark.asyncio
async def test_replace_card_art_rejects_a_missing_card_and_a_non_image(cog):
    guild = FakeGuild(107)
    card_id = await _seed_one_card(cog, guild)
    with pytest.raises(KeyError):
        await cog.replace_card_art(guild, 999, fake_art_bytes())
    with pytest.raises(ValueError):
        await cog.replace_card_art(guild, card_id, b"nope")


@pytest.mark.asyncio
async def test_an_upload_arriving_after_the_import_started_does_not_touch_the_result(cog):
    guild = FakeGuild(108)
    admin = _admin(1080, guild)
    chan = FakeChannel(10800, guild)
    ctx, msg = await _open_series_preview(cog, guild, admin, chan, _seventeen_characters())
    view = msg.view
    edits = []
    original_edit = msg.edit

    async def spy(**kwargs):
        edits.append(kwargs)
        return await original_edit(**kwargs)

    msg.edit = spy

    async def wait_for(event, check=None, timeout=None):
        view._busy = True  # the admin pressed Import while we were waiting...
        return FakeUpload(admin, chan, "3", fake_art_bytes((5, 5, 5)))  # ...and an upload landed anyway

    cog.bot.wait_for = wait_for
    await view.replace_art.callback(_inter(admin, guild, chan, msg))
    assert view.preview.art == {} and edits == [] and view._listening is False


# --- 1.7.2: the Adjust box decides what gets imported ------------------------------


async def _open_adjust(cog, guild, admin, chan, msg):
    inter = _inter(admin, guild, chan, msg)
    await msg.view.adjust.callback(inter)
    return inter.modal


@pytest.mark.asyncio
async def test_deleting_lines_in_the_adjust_box_leaves_those_characters_out(cog):
    guild = FakeGuild(110)
    admin = _admin(1100, guild)
    chan = FakeChannel(11000, guild)
    ctx, msg = await _open_series_preview(cog, guild, admin, chan, _seventeen_characters())
    modal = await _open_adjust(cog, guild, admin, chan, msg)
    lines = modal.changes.default.splitlines()
    assert len(lines) == 15
    modal.changes._value = "\n".join(lines[:3])  # keep only the first three lines
    submit = _inter(admin, guild, chan, msg)
    await modal.on_submit(submit)

    assert msg.view.preview.skips == set(range(3, 15))
    fields = {f.name: f.value for f in submit.edits[-1]["embed"].fields}
    assert "**3** new" in fields["Top 15"]
    assert "~~Char03~~ skipped" in fields["New characters"]
    assert not submit.followups  # nothing to complain about

    await msg.view.import_top.callback(_inter(admin, guild, chan, msg))
    assert sorted(e["name"] for e in (await cog.config.guild(guild).pool()).values()) == ["Char00", "Char01", "Char02"]


@pytest.mark.asyncio
async def test_a_line_typed_back_into_the_box_brings_a_skipped_character_back(cog):
    guild = FakeGuild(111)
    admin = _admin(1110, guild)
    chan = FakeChannel(11100, guild)
    ctx, msg = await _open_series_preview(cog, guild, admin, chan, _seventeen_characters())
    modal = await _open_adjust(cog, guild, admin, chan, msg)
    modal.changes._value = "\n".join(modal.changes.default.splitlines()[:3])
    await modal.on_submit(_inter(admin, guild, chan, msg))

    modal2 = await _open_adjust(cog, guild, admin, chan, msg)
    assert len(modal2.changes.default.splitlines()) == 3  # the box now lists only what will be imported
    modal2.changes._value = modal2.changes.default + "\nChar05: epic"
    await modal2.on_submit(_inter(admin, guild, chan, msg))
    assert 5 not in msg.view.preview.skips and 3 in msg.view.preview.skips
    assert msg.view.preview.overrides.get(5) == "epic" or msg.view.preview.built["entries"][5]["rarity"] == "epic"


@pytest.mark.asyncio
async def test_a_blank_adjust_box_changes_nothing(cog):
    guild = FakeGuild(112)
    admin = _admin(1120, guild)
    chan = FakeChannel(11200, guild)
    ctx, msg = await _open_series_preview(cog, guild, admin, chan, _seventeen_characters())
    modal = await _open_adjust(cog, guild, admin, chan, msg)
    modal.changes._value = "   \n  "
    await modal.on_submit(_inter(admin, guild, chan, msg))
    assert msg.view.preview.skips == set() and msg.view.preview.overrides == {}
    assert msg.view.import_top.disabled is False


@pytest.mark.asyncio
async def test_an_unreadable_line_blocks_removals_but_still_applies_the_good_lines(cog):
    guild = FakeGuild(113)
    admin = _admin(1130, guild)
    chan = FakeChannel(11300, guild)
    ctx, msg = await _open_series_preview(cog, guild, admin, chan, _seventeen_characters())
    modal = await _open_adjust(cog, guild, admin, chan, msg)
    lines = modal.changes.default.splitlines()
    lines[0] = "Char00: common"  # a real edit
    lines[1] = "Chr01: legendary"  # a typo: it can't be matched, so Char01 must NOT silently vanish
    modal.changes._value = "\n".join(lines[:4])
    submit = _inter(admin, guild, chan, msg)
    await modal.on_submit(submit)

    assert msg.view.preview.skips == set()  # nothing was left out
    assert msg.view.preview.overrides == {0: "common"}  # the good line still applied
    said = submit.followups[-1][0]
    assert 'No new character matches "Chr01"' in said and "NOT treated as skips" in said


@pytest.mark.asyncio
async def test_skip_ranges_work_and_bad_ranges_are_reported(cog):
    guild = FakeGuild(114)
    admin = _admin(1140, guild)
    chan = FakeChannel(11400, guild)
    ctx, msg = await _open_series_preview(cog, guild, admin, chan, _seventeen_characters())
    modal = await _open_adjust(cog, guild, admin, chan, msg)
    modal.changes._value = "skip 4-6, 10\nskip 40-90"
    submit = _inter(admin, guild, chan, msg)
    await modal.on_submit(submit)
    assert msg.view.preview.skips == {3, 4, 5, 9}  # 4, 5, 6 and 10 are ranks 3, 4, 5 and 9
    assert "longer than the list" in submit.followups[-1][0]
