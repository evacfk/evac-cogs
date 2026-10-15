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
    # The dev stub's _StubCommand doesn't expose a children registry, so this
    # just confirms each subcommand attribute exists on the class and is a
    # callable/stub command object, not that dispatch actually routes to it.
    assert hasattr(RedditFeed, "redditfeed_version")
    assert hasattr(RedditFeed, "redditfeed_status")
    for name in ("redditfeed_queue", "redditfeed_map", "redditfeed_check", "redditfeed_backfill", "redditfeed_source"):
        assert hasattr(RedditFeed, name), name
    # the old typed setters live in the panel now
    for name in ("redditfeed_add", "redditfeed_addmany", "redditfeed_remove", "redditfeed_removechannel", "redditfeed_pause",
                 "redditfeed_resume", "redditfeed_mode", "redditfeed_modeall", "redditfeed_interval", "redditfeed_stagger",
                 "redditfeed_list", "redditfeed_resetall", "redditfeed_xbutton", "redditfeed_redgifs", "redditfeed_logchannel",
                 "redditfeed_category", "redditfeed_prefix", "redditfeed_denied", "redditfeed_undeny", "redditfeed_keyword",
                 "redditfeed_queue_channel", "redditfeed_queue_clear"):
        assert not hasattr(RedditFeed, name), name
    assert group is not None


def test_cog_inherits_dashboard_mixin_ahead_of_cog():
    """Documented pattern: `class Cog(DashboardIntegration, commands.Cog)`."""
    from redbot.core import commands

    from redditfeed.dashboard_integration import DashboardIntegration
    from redditfeed.redditfeed import RedditFeed

    mro = RedditFeed.__mro__
    assert mro.index(DashboardIntegration) < mro.index(commands.Cog)
    assert hasattr(RedditFeed, "on_dashboard_cog_add")


def test_dashboard_page_decorator_actually_attaches_its_params():
    """Regression: the old cog tried to import the decorator from
    dashboard.rpc.thirdparties (a module that doesn't exist; it is
    third_parties) and silently fell back to a no-op, so no params were ever
    attached and the page could never register."""
    from redditfeed.redditfeed import RedditFeed

    args, kwargs = RedditFeed.dashboard_redditfeed.__dashboard_decorator_params__
    assert kwargs["name"] == "feeds"  # an explicit lowercase name; name=None registered as "None" and its link 404s
    assert kwargs["methods"] == ("GET", "POST")


def test_cog_module_no_longer_imports_from_the_dashboard_cog():
    import inspect

    from redditfeed import redditfeed

    source = inspect.getsource(redditfeed)
    assert "dashboard.rpc" not in source
    assert "DASHBOARD_INTEGRATION_AVAILABLE" not in source


def test_version_probe_text_is_the_new_build():
    import inspect

    from redditfeed import redditfeed

    assert "redditfeed build: source-v9" in inspect.getsource(redditfeed)


def test_setup_function_exists():
    from redditfeed import setup

    assert callable(setup)


def test_discovery_commands_exist():
    from redditfeed.redditfeed import RedditFeed

    assert hasattr(RedditFeed, "redditfeed_discover")
    for name in ("redditfeed_denied", "redditfeed_undeny"):
        assert not hasattr(RedditFeed, name), name
