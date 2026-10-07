"""Dashboard page + permission tests, run against fakes (no Red, no dashboard
cog). They prove the logic and that the module wires up; they do NOT prove the
live dashboard renders/accepts the page -- that is verified on the host.
"""
import copy
from types import SimpleNamespace

import pytest

from redditfeed.dashboard_view import PAGE_TEMPLATE
from redditfeed.models import SubredditMapping
from redditfeed.redditfeed import RedditFeed

GUILD_CHANNEL = 10
OTHER_GUILD_CHANNEL = 99


# -- fakes ---------------------------------------------------------------------------

class _Ctx:
    """Like Red's Value call result: awaitable AND an async context manager."""

    def __init__(self, data):
        self.data = data

    def __await__(self):
        async def _get():
            return copy.deepcopy(self.data)

        return _get().__await__()

    async def __aenter__(self):
        return self.data

    async def __aexit__(self, *exc):
        return False


class FakeMappings:
    def __init__(self, data):
        self.data = data

    def __call__(self):
        return _Ctx(self.data)


class FakeBot:
    def __init__(self, owners=(), mods=()):
        self.owners, self.mods = set(owners), set(mods)

    async def is_owner(self, user):
        return user.id in self.owners

    async def is_mod(self, member):
        return member.id in self.mods


def make_user(uid):
    return SimpleNamespace(id=uid)


def make_guild(member_ids_with_manage=None, member_ids_plain=()):
    members = {}
    for uid, manage in (member_ids_with_manage or {}).items():
        members[uid] = SimpleNamespace(id=uid, guild_permissions=SimpleNamespace(manage_guild=manage))
    for uid in member_ids_plain:
        members[uid] = SimpleNamespace(id=uid, guild_permissions=SimpleNamespace(manage_guild=False))
    return SimpleNamespace(
        id=1,
        name="Wonderland",
        text_channels=[SimpleNamespace(id=GUILD_CHANNEL, name="feet")],
        get_member=lambda uid: members.get(uid),
    )


def make_cog(bot=None, mappings=None):
    data = mappings if mappings is not None else {
        "feet": SubredditMapping(subreddit="feet", channel_ids=[GUILD_CHANNEL]).to_dict(),
        "elsewhere": SubredditMapping(subreddit="elsewhere", channel_ids=[OTHER_GUILD_CHANNEL]).to_dict(),
    }
    cog = object.__new__(RedditFeed)
    cog.bot = bot or FakeBot()
    cog.config = SimpleNamespace(mappings=FakeMappings(data))
    return cog, data


# -- registration -----------------------------------------------------------------------

async def test_on_dashboard_cog_add_registers_the_cog():
    cog, _ = make_cog()
    registered = []
    dashboard_cog = SimpleNamespace(
        rpc=SimpleNamespace(third_parties_handler=SimpleNamespace(add_third_party=registered.append))
    )
    await cog.on_dashboard_cog_add(dashboard_cog)
    assert registered == [cog]


# -- shared setter ------------------------------------------------------------------------

async def test_apply_paused_writes_flag_and_reports_unknown():
    cog, data = make_cog()
    assert await cog._apply_paused("feet", True) is True
    assert data["feet"]["paused"] is True
    assert await cog._apply_paused("feet", False) is True
    assert data["feet"]["paused"] is False
    assert await cog._apply_paused("nope", True) is False


# -- permission-checked write path ---------------------------------------------------------

async def test_mod_can_pause_a_feed_in_their_guild():
    cog, data = make_cog(bot=FakeBot(mods={5}))
    guild = make_guild(member_ids_plain=[5])
    result = await cog._dashboard_apply_pause(make_user(5), guild, "feet", "pause")
    assert result[0] == "success"
    assert data["feet"]["paused"] is True


async def test_manage_guild_member_can_resume():
    cog, data = make_cog()
    data["feet"]["paused"] = True
    guild = make_guild(member_ids_with_manage={6: True})
    assert (await cog._dashboard_apply_pause(make_user(6), guild, "feet", "resume"))[0] == "success"
    assert data["feet"]["paused"] is False


async def test_bot_owner_allowed_even_if_not_a_member():
    cog, data = make_cog(bot=FakeBot(owners={7}))
    guild = make_guild()
    assert (await cog._dashboard_apply_pause(make_user(7), guild, "feet", "pause"))[0] == "success"
    assert data["feet"]["paused"] is True


@pytest.mark.parametrize("uid", [8, 404])  # plain member / not in the guild at all
async def test_non_mod_is_denied_and_nothing_changes(uid):
    cog, data = make_cog()
    guild = make_guild(member_ids_plain=[8])
    category, _ = await cog._dashboard_apply_pause(make_user(uid), guild, "feet", "pause")
    assert category == "error"
    assert data["feet"]["paused"] is False


async def test_cannot_touch_a_feed_that_does_not_post_into_this_guild():
    cog, data = make_cog(bot=FakeBot(owners={7}))
    category, _ = await cog._dashboard_apply_pause(make_user(7), make_guild(), "elsewhere", "pause")
    assert category == "error"
    assert data["elsewhere"]["paused"] is False


async def test_invalid_action_and_unknown_subreddit_are_errors():
    cog, data = make_cog(bot=FakeBot(owners={7}))
    guild = make_guild()
    assert (await cog._dashboard_apply_pause(make_user(7), guild, "feet", "delete"))[0] == "error"
    assert (await cog._dashboard_apply_pause(make_user(7), guild, "ghost", "pause"))[0] == "error"
    assert data["feet"]["paused"] is False


async def test_subreddit_input_is_normalized():
    cog, data = make_cog(bot=FakeBot(owners={7}))
    assert (await cog._dashboard_apply_pause(make_user(7), make_guild(), " R/Feet ", "pause"))[0] == "success"
    assert data["feet"]["paused"] is True


# -- the page callback -----------------------------------------------------------------------

async def test_get_page_without_form_shows_only_this_guilds_rows():
    cog, _ = make_cog(bot=FakeBot(mods={5}))
    guild = make_guild(member_ids_plain=[5])
    result = await cog.dashboard_redditfeed(make_user(5), guild, method="GET")
    assert result["status"] == 0
    content = result["web_content"]
    assert content["source"] == PAGE_TEMPLATE
    assert [r["subreddit"] for r in content["rows"]] == ["feet"]
    assert content["rows"][0]["channels"] == ["#feet"]
    assert content["form"] is None
    assert content["can_edit"] is True
    assert "notifications" not in result


async def test_non_mod_gets_read_only_page_even_when_form_class_is_supplied():
    wtforms = pytest.importorskip("wtforms")

    class Base(wtforms.Form):
        def validate_on_submit(self):  # a submit that WOULD succeed
            return True

    cog, data = make_cog()
    guild = make_guild(member_ids_plain=[8])
    result = await cog.dashboard_redditfeed(make_user(8), guild, method="POST", Form=Base)
    assert result["web_content"]["form"] is None
    assert result["web_content"]["can_edit"] is False
    assert data["feet"]["paused"] is False


async def test_post_through_a_real_wtforms_form_pauses_and_notifies():
    wtforms = pytest.importorskip("wtforms")
    MultiDict = pytest.importorskip("werkzeug.datastructures").MultiDict

    class Base(wtforms.Form):
        def __init__(self, prefix=""):
            formdata = MultiDict(
                {
                    f"{prefix}subreddit": "feet",
                    f"{prefix}action": "pause",
                    f"{prefix}submit": "Apply",
                }
            )
            super().__init__(formdata=formdata, prefix=prefix)

        def validate_on_submit(self):
            return self.validate()

    cog, data = make_cog(bot=FakeBot(mods={5}))
    guild = make_guild(member_ids_plain=[5])
    result = await cog.dashboard_redditfeed(make_user(5), guild, method="POST", Form=Base)
    assert data["feet"]["paused"] is True
    assert result["notifications"] == [{"message": "r/feet is now paused.", "category": "success"}]
    assert result["web_content"]["rows"][0]["state"] == "paused"  # table re-read after the write


async def test_post_for_a_feed_not_in_the_dropdown_is_rejected_by_the_form():
    wtforms = pytest.importorskip("wtforms")
    MultiDict = pytest.importorskip("werkzeug.datastructures").MultiDict

    class Base(wtforms.Form):
        def __init__(self, prefix=""):
            formdata = MultiDict({f"{prefix}subreddit": "elsewhere", f"{prefix}action": "pause"})
            super().__init__(formdata=formdata, prefix=prefix)

        def validate_on_submit(self):
            return self.validate()

    cog, data = make_cog(bot=FakeBot(owners={7}))
    result = await cog.dashboard_redditfeed(make_user(7), make_guild(), method="POST", Form=Base)
    assert data["elsewhere"]["paused"] is False
    assert "notifications" not in result


# -- the template ----------------------------------------------------------------------------

@pytest.mark.parametrize("autoescape", [True, False])
def test_template_escapes_hostile_values_exactly_once(autoescape):
    jinja2 = pytest.importorskip("jinja2")
    env = jinja2.Environment(autoescape=autoescape)
    rows = [
        {
            "subreddit": "x",
            "state": "active",
            "channels": ["#a<b>"],
            "last_poll": "never",
            "last_post": "never",
            "last_error": "<script>alert(1)</script> & more",
            "require": "",
            "block": "",
        }
    ]
    html = env.from_string(PAGE_TEMPLATE).render(rows=rows, form=None, can_edit=True, guild_name="<G>")
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; more" in html
    assert "&amp;lt;" not in html  # not double-escaped
    assert "&lt;G&gt;" in html and "#a&lt;b&gt;" in html


def test_template_empty_and_read_only_states_render():
    jinja2 = pytest.importorskip("jinja2")
    env = jinja2.Environment(autoescape=True)
    empty = env.from_string(PAGE_TEMPLATE).render(rows=[], form=None, can_edit=False, guild_name="G")
    assert "No subreddits are mapped" in empty
    row = {"subreddit": "x", "state": "active", "channels": [], "last_poll": "never",
           "last_post": "never", "last_error": "", "require": "", "block": ""}
    read_only = env.from_string(PAGE_TEMPLATE).render(rows=[row], form=None, can_edit=False, guild_name="G")
    assert "needs the mod role" in read_only
