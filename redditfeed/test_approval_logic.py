"""Pure logic for the approval queue and destination proposals."""
from redditfeed import constants, discovery, engine
from redditfeed.models import QueueEntry, SubredditMapping

NOW = 1_000_000.0


# -- mapping defaults / migration ------------------------------------------------------------

def test_new_and_old_mappings_default_to_manual():
    assert SubredditMapping(subreddit="x").approval == "manual"
    old = SubredditMapping.from_dict({"subreddit": "feet", "channel_ids": [1], "last_poll_ts": 123.0})
    assert old.approval == "manual" and old.added_ts == 123.0          # old data: manual, no backfill before its cursor


def test_bad_approval_value_falls_back_to_manual_and_roundtrips():
    m = SubredditMapping.from_dict({"subreddit": "x", "approval": "nonsense"})
    assert m.approval == "manual"
    m.approval = "auto"
    assert SubredditMapping.from_dict(m.to_dict()).approval == "auto"


def test_queue_entry_roundtrip():
    e = QueueEntry(subreddit="feet", post={"id": "a"}, media=[{"kind": "image", "url": "u", "is_link_only": False}],
                   channel_ids=[5], created_ts=NOW)
    back = QueueEntry.from_dict(e.to_dict())
    assert back.channel_ids == [5] and back.media_items()[0].url == "u" and back.status == "pending"


def test_parse_approval():
    assert engine.parse_approval(" AUTO ") == "auto" and engine.parse_approval("manual") == "manual"
    assert engine.parse_approval("maybe") is None


# -- when may a post be queued ----------------------------------------------------------------

def post(age_min, score, **kw):
    return {"id": "a", "created_utc": NOW - age_min * 60, "score": score, **kw}


def test_queue_skip_reasons():
    skip = lambda p, added=None: engine.queue_skip_reason(p, NOW, added, 3600, 3)
    assert skip(post(120, 10)) is None
    assert skip(post(10, 10)) == "too_new"
    assert skip(post(120, 1)) == "low_score"
    assert skip(post(120, 10), added=NOW - 60) == "before_added"          # no backfill
    assert skip({"id": "a", "score": 9}) == "no_timestamp"


def test_boundaries_are_inclusive():
    assert engine.queue_skip_reason(post(60, 3), NOW, None, 3600, 3) is None


def test_fetch_window_never_reaches_before_the_mapping_was_added():
    assert engine.queue_fetch_after(NOW, None) == NOW - constants.QUEUE_LOOKBACK_HOURS * 3600
    assert engine.queue_fetch_after(NOW, NOW - 100) == NOW - 100


# -- queue upkeep -------------------------------------------------------------------------------

def test_expiry_only_touches_old_pending_items():
    q = {
        "1": {"status": "pending", "created_ts": NOW - 25 * 3600},
        "2": {"status": "pending", "created_ts": NOW - 23 * 3600},
        "3": {"status": "approved", "created_ts": NOW - 90 * 3600},
    }
    assert engine.expired_pending_ids(q, NOW) == ["1"]
    assert engine.pending_ids(q) == ["1", "2"]


def test_prune_keeps_pending_and_recent_decisions_only():
    q = {
        "p": {"status": "pending", "created_ts": NOW - 99 * 3600},
        "old": {"status": "rejected", "created_ts": NOW - 99 * 3600, "resolved_ts": NOW - 60 * 3600},
        "new": {"status": "approved", "created_ts": NOW - 99 * 3600, "resolved_ts": NOW - 1 * 3600},
    }
    assert set(engine.prune_queue(q, NOW)) == {"p", "new"}


def test_posted_map_prunes_by_age_and_size():
    posted = {str(i): {"ts": NOW - i} for i in range(10)}
    posted["ancient"] = {"ts": NOW - 30 * 86400}
    out = engine.prune_posted_map(posted, NOW, max_entries=5)
    assert "ancient" not in out and len(out) == 5 and "0" in out


def test_trim_post_keeps_only_what_the_queue_needs():
    t = engine.trim_post({"id": 1, "title": " hi ", "score": None, "permalink": "/r/x/1", "author": "secret"}, "x")
    assert t == {"id": "1", "title": "hi", "score": 0, "permalink": "/r/x/1", "created_utc": None, "subreddit": "x"}


# -- destination proposals ------------------------------------------------------------------------

CHANNELS = [(1, "🦶・feet"), (2, "2d"), (3, "3d"), (4, "pillow-talk")]


def test_topic_extraction():
    assert discovery.topic_of("FeetInYourFace") == "feet"
    assert discovery.topic_of("FootFetish") == "feet"            # alias
    assert discovery.topic_of("2DHentai") == "2d"
    assert discovery.topic_of("GoneWild") is None                  # pure noise
    assert discovery.topic_of("nsfw_gifs") is None


def test_matches_an_existing_channel_ignoring_emoji_and_dots():
    assert discovery.propose_destination("FeetInYourFace", CHANNELS).channel_id == 1
    assert discovery.propose_destination("HentaiBeast", CHANNELS).channel_id == 2


def test_stem_match_feetish_to_feet():
    assert discovery.propose_destination("Feetish", [(9, "feet-pics")]).channel_id == 9


def test_learned_topic_wins_over_name_matching():
    dest = discovery.propose_destination("FeetInYourFace", CHANNELS, learned={"feet": 4})
    assert dest.channel_id == 4


def test_learned_channel_that_no_longer_exists_is_ignored():
    dest = discovery.propose_destination("FeetInYourFace", CHANNELS, learned={"feet": 999})
    assert dest.channel_id == 1


def test_new_channel_only_proposed_when_creation_is_possible():
    none = discovery.propose_destination("bdsm", CHANNELS, can_create=False)
    assert none.channel_id is None and none.new_name is None and "pick one" in none.reason
    new = discovery.propose_destination("bdsm", CHANNELS, can_create=True, prefix="🔞・")
    assert new.new_name == "🔞・bdsm" and new.channel_id is None


def test_noise_only_names_propose_nothing_even_when_creation_is_allowed():
    dest = discovery.propose_destination("GoneWild", CHANNELS, can_create=True)
    assert dest.channel_id is None and dest.new_name is None
