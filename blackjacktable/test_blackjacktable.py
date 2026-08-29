"""
Coverage for blackjacktable.py's own logic that isn't already exercised
by table.py/embeds.py's suites -- currently just the channel/thread gate,
since that's the one piece of behavior with actual branching worth
locking in outside of a live bot.
"""

from types import SimpleNamespace

from .blackjacktable import _channel_allowed
from .constants import DEFAULT_CHANNEL_ID


def test_home_channel_allowed():
    channel = SimpleNamespace(id=DEFAULT_CHANNEL_ID)
    assert _channel_allowed(channel) is True


def test_thread_under_home_channel_allowed():
    # Real discord.Thread instances expose parent_id -- a plain object
    # with the same attribute is enough to exercise the branch without
    # needing a live bot/guild.
    thread = SimpleNamespace(id=999, parent_id=DEFAULT_CHANNEL_ID)
    assert _channel_allowed(thread) is True


def test_unrelated_channel_rejected():
    channel = SimpleNamespace(id=123456789)
    assert _channel_allowed(channel) is False


def test_thread_under_unrelated_channel_rejected():
    thread = SimpleNamespace(id=999, parent_id=123456789)
    assert _channel_allowed(thread) is False


def test_channel_without_parent_id_attribute_rejected():
    # A plain text channel (not a thread) has no parent_id at all --
    # getattr's default keeps this from raising instead of just failing
    # the check.
    channel = SimpleNamespace(id=123456789)
    assert not hasattr(channel, "parent_id")
    assert _channel_allowed(channel) is False
