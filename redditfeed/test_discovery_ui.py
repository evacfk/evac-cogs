"""Embed + Approve/Deny view tests against fakes (no Discord). They prove the
logic and the one-decision-wins race handling; they do NOT prove how Discord
renders the image grid or button clicks -- that is verified on the live bot.
"""
import asyncio
from types import SimpleNamespace

from redditfeed import discovery_ui
from redditfeed.discovery import PreviewResult, SubredditCandidate
from redditfeed.models import MediaItem

CHANNEL_ID = 555


def candidate(**kw):
    base = dict(name="feetinyourface", display_name="FeetInYourFace", subscribers=433657,
                description="A subreddit about feet", created_utc=1)
    base.update(kw)
    return SubredditCandidate(**base)


def images(n):
    return [MediaItem(kind="image", url=f"https://i.redd.it/{i}.jpg") for i in range(n)]


def field_names(embed):
    return [f.name for f in embed.fields]


def field_value(embed, name):
    return next(f.value for f in embed.fields if f.name == name)


# -- embeds ----------------------------------------------------------------------------------

class TestBuildSuggestionEmbeds:
    def test_details_embed_has_link_subscribers_and_target(self):
        preview = PreviewResult(images=images(1), post_links=["https://www.reddit.com/r/x/comments/a/t/"])
        embeds = discovery_ui.build_suggestion_embeds(candidate(), preview, CHANNEL_ID)
        first = embeds[0]
        assert first.title == "r/FeetInYourFace"
        assert first.url == "https://www.reddit.com/r/FeetInYourFace"
        assert first.description == "A subreddit about feet"
        assert field_value(first, "Subscribers") == "433,657"
        assert field_value(first, "Would post to") == f"<#{CHANNEL_ID}>"
        assert "[post 1](https://www.reddit.com/r/x/comments/a/t/)" in field_value(first, "Top recent posts")

    def test_images_ride_as_extra_embeds_sharing_the_url(self):
        """Discord folds embeds with an identical url into one card with an image
        grid -- the first embed keeps the details, the rest only carry images."""
        embeds = discovery_ui.build_suggestion_embeds(candidate(), PreviewResult(images=images(4)), CHANNEL_ID)
        assert len(embeds) == 4
        assert embeds[0].image_url == "https://i.redd.it/0.jpg"
        assert [e.image_url for e in embeds[1:]] == ["https://i.redd.it/1.jpg", "https://i.redd.it/2.jpg", "https://i.redd.it/3.jpg"]
        assert len({e.url for e in embeds}) == 1
        assert all(e.title is None for e in embeds[1:])

    def test_no_images_says_so_and_points_at_the_link(self):
        embeds = discovery_ui.build_suggestion_embeds(candidate(), PreviewResult(), CHANNEL_ID)
        assert len(embeds) == 1
        assert "No recent image posts" in field_value(embeds[0], "Preview")

    def test_preview_failure_is_distinct_from_no_images(self):
        embeds = discovery_ui.build_suggestion_embeds(candidate(), PreviewResult(), CHANNEL_ID, preview_failed=True)
        assert "timed out" in field_value(embeds[0], "Preview")

    def test_flagged_titles_raise_a_warning(self):
        preview = PreviewResult(images=images(1), flagged_titles=2)
        embeds = discovery_ui.build_suggestion_embeds(candidate(), preview, CHANNEL_ID)
        warning = next(n for n in field_names(embeds[0]) if "Safety screen" in n)
        assert "2 recent post title(s)" in field_value(embeds[0], warning)

    def test_no_warning_when_nothing_was_flagged(self):
        embeds = discovery_ui.build_suggestion_embeds(candidate(), PreviewResult(images=images(1)), CHANNEL_ID)
        assert not any("Safety screen" in n for n in field_names(embeds[0]))

    def test_long_description_is_truncated(self):
        embeds = discovery_ui.build_suggestion_embeds(candidate(description="x" * 900), PreviewResult(), CHANNEL_ID)
        assert len(embeds[0].description) <= 300


# -- view fakes ----------------------------------------------------------------------------------

class FakeBot:
    def __init__(self, mods=()):
        self.mods = set(mods)

    async def is_mod(self, member):
        return member.id in self.mods


class FakeCog:
    def __init__(self, mods=(), fail_approve=False):
        self.bot = FakeBot(mods)
        self.approved, self.denied = [], []
        self.fail_approve = fail_approve

    async def _approve_suggestion(self, name, channel_id=None, new_name=None, guild=None, display_name=""):
        await asyncio.sleep(0)          # yield, so concurrent clicks can interleave
        if self.fail_approve:
            raise RuntimeError("disk full")
        if channel_id is None:
            channel_id = 4242           # what creating the proposed channel would return
        self.approved.append((name, channel_id))
        return True, channel_id

    async def _deny_suggestion(self, name):
        await asyncio.sleep(0)
        self.denied.append(name)


class FakeInteraction:
    def __init__(self, uid, manage_guild=False):
        self.user = SimpleNamespace(
            id=uid, mention=f"<@{uid}>", guild_permissions=SimpleNamespace(manage_guild=manage_guild)
        )
        self.sent, self.followups, self.edited, self.deferred = [], [], [], 0
        self.response = SimpleNamespace(defer=self._defer, send_message=self._send)
        self.followup = SimpleNamespace(send=self._followup)

    async def _defer(self):
        self.deferred += 1

    async def _send(self, content=None, ephemeral=False):
        self.sent.append(content)

    async def _followup(self, content=None, ephemeral=False):
        self.followups.append(content)

    async def edit_original_response(self, **kwargs):
        self.edited.append(kwargs)


def make_view(cog=None, owner_id=1):
    embeds = discovery_ui.build_suggestion_embeds(candidate(), PreviewResult(images=images(2)), CHANNEL_ID)
    return discovery_ui.SuggestionView(cog or FakeCog(), "feetinyourface", CHANNEL_ID, embeds, owner_id), embeds


# -- view behaviour --------------------------------------------------------------------------------

async def test_owner_can_approve_and_the_card_records_the_result():
    cog = FakeCog()
    view, embeds = make_view(cog, owner_id=1)
    click = FakeInteraction(1)
    await view._resolve(click, approve=True)

    assert cog.approved == [("feetinyourface", CHANNEL_ID)]
    assert click.deferred == 1
    assert "Approved by <@1>" in field_value(embeds[0], "Result")
    assert click.edited and click.edited[0]["embeds"] is embeds


async def test_deny_records_and_does_not_map():
    cog = FakeCog()
    view, embeds = make_view(cog)
    await view._resolve(FakeInteraction(1), approve=False)

    assert cog.denied == ["feetinyourface"] and cog.approved == []
    assert "Denied by" in field_value(embeds[0], "Result")


async def test_manage_guild_member_and_red_mod_are_allowed():
    for click in (FakeInteraction(2, manage_guild=True), FakeInteraction(3)):
        cog = FakeCog(mods={3})
        view, _ = make_view(cog, owner_id=1)
        await view._resolve(click, approve=True)
        assert cog.approved, f"user {click.user.id} should be allowed"


async def test_non_mod_is_refused_and_nothing_changes():
    cog = FakeCog()
    view, embeds = make_view(cog, owner_id=1)
    click = FakeInteraction(9)
    await view._resolve(click, approve=True)

    assert cog.approved == [] and click.deferred == 0
    assert click.sent == ["Only moderators can approve or deny suggestions."]
    assert "Result" not in field_names(embeds[0])
    # and the claim was not burned: a real mod can still decide
    await view._resolve(FakeInteraction(1), approve=True)
    assert cog.approved


async def test_two_simultaneous_clicks_only_decide_once():
    """REGRESSION guard: Approve and Deny clicked at the same moment must not
    both act (map AND deny the same subreddit)."""
    cog = FakeCog()
    view, _ = make_view(cog, owner_id=1)
    a, b = FakeInteraction(1), FakeInteraction(1)
    await asyncio.gather(view._resolve(a, approve=True), view._resolve(b, approve=False))

    assert len(cog.approved) + len(cog.denied) == 1
    loser = a if not a.deferred else b
    assert loser.sent == ["Someone already decided this one."]


async def test_second_click_after_a_decision_is_told_so():
    cog = FakeCog()
    view, _ = make_view(cog)
    await view._resolve(FakeInteraction(1), approve=True)
    late = FakeInteraction(1)
    await view._resolve(late, approve=False)

    assert cog.denied == [] and late.sent == ["Someone already decided this one."]


async def test_a_failed_save_releases_the_claim_so_it_can_be_retried():
    cog = FakeCog(fail_approve=True)
    view, embeds = make_view(cog)
    first = FakeInteraction(1)
    await view._resolve(first, approve=True)

    assert first.followups and "Something went wrong" in first.followups[0]
    assert "Result" not in field_names(embeds[0])

    cog.fail_approve = False
    await view._resolve(FakeInteraction(1), approve=True)
    assert cog.approved == [("feetinyourface", CHANNEL_ID)]


async def test_timeout_expires_an_undecided_card_but_leaves_a_decided_one():
    edits = []

    async def edit(**kwargs):
        edits.append(kwargs)

    undecided, embeds = make_view()
    undecided.message = SimpleNamespace(edit=edit)
    await undecided.on_timeout()
    assert len(edits) == 1 and "Expired" in embeds[0].footer_text

    decided, _ = make_view()
    decided.message = SimpleNamespace(edit=edit)
    await decided._resolve(FakeInteraction(1), approve=True)
    await decided.on_timeout()
    assert len(edits) == 1      # unchanged: the timeout did not touch a decided card
