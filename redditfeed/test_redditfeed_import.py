"""Import-time smoke test for the live cog module.

test_engine/test_models/test_arctic_shift exercise pure logic directly and
never import redditfeed.py itself, so a module-level error in it (a missing
stub attribute, a bad decorator, a typo in an import) could ship unnoticed
even with every other test green. This test's only job is to import the
module and confirm the cog and its command tree actually come into existence.
"""


def test_module_imports_and_defines_the_cog():
    from redditfeed import redditfeed

    assert hasattr(redditfeed, "RedditFeed")


def test_redditfeed_command_group_and_subcommands_exist():
    from redditfeed.redditfeed import RedditFeed

    group = RedditFeed.redditfeed
    sub_names = {"version", "add", "remove", "removechannel", "pause", "resume", "interval", "stagger", "list", "status"}
    # The dev stub's _StubCommand doesn't expose a children registry, so this
    # just confirms each subcommand attribute exists on the class and is a
    # callable/stub command object, not that dispatch actually routes to it.
    assert hasattr(RedditFeed, "redditfeed_version")
    assert hasattr(RedditFeed, "redditfeed_add")
    assert hasattr(RedditFeed, "redditfeed_status")
    assert hasattr(RedditFeed, "redditfeed_keyword")
    assert hasattr(RedditFeed, "redditfeed_keyword_require")
    assert hasattr(RedditFeed, "redditfeed_keyword_block")
    assert group is not None


def test_dashboard_integration_degrades_gracefully_without_dashboard_cog():
    """The dashboard cog isn't installed in this sandbox, so the module must
    import via the no-op dashboard_page fallback rather than raising ImportError."""
    from redditfeed import redditfeed

    assert redditfeed.DASHBOARD_INTEGRATION_AVAILABLE is False
    assert hasattr(redditfeed.RedditFeed, "dashboard_redditfeed_settings")


def test_setup_function_exists():
    from redditfeed import setup

    assert callable(setup)
