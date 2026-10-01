from serverpulse import models
from serverpulse._testdata import local_ts
from serverpulse.tracker import GuildTracker, is_command_message, should_count

T0 = local_ts(2026, 9, 29, 20, 0, 0)  # a Tuesday, 8pm


def count(**kw):
    base = dict(is_bot=False, webhook_id=None, type_name="default", content="hello", channel_key=1,
                ignored=set(), exclude_commands=True, prefixes=["."])
    base.update(kw)
    return should_count(**base)


class TestShouldCount:
    def test_plain_message_counts(self):
        assert count()

    def test_bots_and_webhooks_do_not(self):
        assert not count(is_bot=True)
        assert not count(webhook_id=123)

    def test_system_messages_do_not(self):
        assert not count(type_name="new_member")
        assert not count(type_name="premium_guild_subscription")
        assert count(type_name="reply")

    def test_ignored_channel(self):
        assert not count(channel_key=5, ignored={5})
        assert count(channel_key=6, ignored={5})

    def test_bot_commands_excluded_by_default(self):
        assert not count(content=".gamble 100")
        assert not count(content=".bj")

    def test_commands_counted_when_toggle_off(self):
        assert count(content=".gamble 100", exclude_commands=False)

    def test_ellipsis_and_lone_dot_are_chat_not_commands(self):
        assert count(content="...what")
        assert count(content=". hello")
        assert count(content=".")
        assert not is_command_message("", ["."])

    def test_attachment_only_message_counts(self):
        assert count(content="")

    def test_any_configured_prefix(self):
        assert not count(content="!ping", prefixes=[".", "!"])


class TestRecording:
    def test_counts_messages_and_distinct_users(self):
        tr = GuildTracker(floor_ts=T0)
        for i, uid in enumerate([1, 2, 1, 3]):
            tr.record(T0 + 10 * i, uid, "111")
        acc = tr.open_acc(T0 + 60)
        assert acc.msgs == 4 and len(acc.users) == 3
        assert acc.channels["111"].msgs == 4

    def test_peak_concurrency_uses_rolling_5_minute_window(self):
        tr = GuildTracker(floor_ts=T0)
        for i, uid in enumerate([1, 2, 3]):
            tr.record(T0 + i, uid, "c")
        tr.record(T0 + 400, 4, "c")  # users 1-3 aged out of the window
        acc = tr.open_acc(T0 + 400)
        assert acc.peak == 3
        assert tr.concurrency_now(T0 + 400) == 1  # only user 4 is still inside 5 minutes
        assert tr.concurrency_now(T0 + 800) == 0

    def test_inner_gap_and_offsets(self):
        tr = GuildTracker(floor_ts=T0)
        tr.record(T0 + 100, 1, "c")
        tr.record(T0 + 160, 1, "c")
        tr.record(T0 + 1000, 2, "c")  # 840s gap, starting at offset 160
        rec = tr.open_acc(T0 + 1000).to_record()
        assert rec["f"] == 100 and rec["l"] == 1000 and rec["gi"] == 840 and rec["gs"] == 160

    def test_messages_land_in_their_own_hour(self):
        tr = GuildTracker(floor_ts=T0)
        tr.record(T0 + 10, 1, "c")
        tr.record(T0 + 3700, 1, "c")
        assert len(tr.accs) == 2

    def test_time_never_goes_backwards(self):
        tr = GuildTracker(floor_ts=T0)
        tr.record(T0 + 100, 1, "c")
        tr.record(T0 + 90, 2, "c")  # late-processed event
        assert tr.open_acc(T0 + 100).last_ts >= T0 + 100

    def test_user_deltas_joins_leaves(self):
        tr = GuildTracker(floor_ts=T0)
        tr.record(T0 + 1, 7, "c")
        tr.record(T0 + 2, 7, "c")
        tr.record_join(T0 + 3)
        tr.record_leave(T0 + 4)
        snap = tr.snapshot(T0 + 10)
        assert snap.user_deltas["2026-09-29"] == {7: 2}
        assert snap.joins == {"2026-09-29": 1} and snap.leaves == {"2026-09-29": 1}


class TestSnapshotCommit:
    def test_commit_drops_closed_hours_only(self):
        tr = GuildTracker(floor_ts=T0)
        tr.record(T0 + 10, 1, "c")
        tr.record(T0 + 3700, 2, "c")
        snap = tr.snapshot(T0 + 3800)  # first hour closed, second open
        assert [h[4] for h in snap.hours] == [True, False]
        tr.commit(snap)
        assert len(tr.accs) == 1
        assert not tr.user_deltas  # all deltas were flushed

    def test_commit_subtracts_only_what_was_flushed(self):
        tr = GuildTracker(floor_ts=T0)
        tr.record(T0 + 1, 1, "c")
        snap = tr.snapshot(T0 + 10)
        tr.record(T0 + 20, 1, "c")  # arrives while the write is in flight
        tr.commit(snap)
        assert tr.user_deltas["2026-09-29"][1] == 1

    def test_straggler_into_a_flushed_hour_never_recreates_it(self):
        """Regression: a late event for an already-finalised hour must not overwrite that hour's record."""
        tr = GuildTracker(floor_ts=T0)
        tr.record(T0 + 10, 1, "c")
        snap = tr.snapshot(T0 + 3700)
        tr.commit(snap)
        assert not tr.accs
        tr.record(T0 + 3599, 2, "c")  # claims to be from the closed hour
        old_slot = models.slot_for_ts(T0 + 10)
        assert (old_slot.date, old_slot.key) not in tr.accs
        assert sum(a.msgs for a in tr.accs.values()) == 1

    def test_event_during_write_keeps_the_closed_hour_for_the_next_flush(self):
        """Regression: a message that lands in a closed hour while its write is in flight must not be lost."""
        tr = GuildTracker(floor_ts=T0)
        tr.record(T0 + 10, 1, "c")
        snap = tr.snapshot(T0 + 3700)  # hour is closed
        tr.record(T0 + 3599, 2, "c")  # bumps the version while the 'write' happens
        tr.commit(snap)
        slot = models.slot_for_ts(T0 + 10)
        acc = tr.accs[(slot.date, slot.key)]
        assert acc.msgs == 2
        again = tr.snapshot(T0 + 3800)
        assert again.hours[0][2]["m"] == 2

    def test_failed_write_loses_nothing(self):
        tr = GuildTracker(floor_ts=T0)
        tr.record(T0 + 10, 1, "c")
        tr.snapshot(T0 + 20)  # never committed (write failed)
        again = tr.snapshot(T0 + 30)
        assert again.hours[0][2]["m"] == 1 and again.user_deltas["2026-09-29"] == {1: 1}


class TestHydration:
    def test_restart_mid_hour_continues_the_same_hour(self):
        tr = GuildTracker(floor_ts=T0)
        for i, uid in enumerate([1, 2, 3]):
            tr.record(T0 + 100 + i, uid, "111")
        snap = tr.snapshot(T0 + 200)
        rec = snap.hours[0][2]
        state = snap.open_state["slots"][0]

        fresh = GuildTracker(floor_ts=T0)
        slot = models.slot_for_ts(T0 + 100)
        fresh.hydrate_slot(slot, rec, state["users"], state["ch_users"])
        fresh.record(T0 + 300, 2, "111")  # a returning chatter
        fresh.record(T0 + 310, 9, "111")  # a new chatter
        out = fresh.open_acc(T0 + 300).to_record()
        assert out["m"] == 5 and out["u"] == 4
        assert out["f"] == 100  # first message of the hour survives the restart
        assert out["c"]["111"] == [5, 4]

    def test_hydrate_without_user_sets_still_keeps_the_distinct_floor(self):
        tr = GuildTracker(floor_ts=T0)
        for uid in (1, 2, 3):
            tr.record(T0 + uid, uid, "c")
        rec = tr.snapshot(T0 + 10).hours[0][2]
        fresh = GuildTracker(floor_ts=T0)
        fresh.hydrate_slot(models.slot_for_ts(T0), rec)  # crash: sets lost
        assert fresh.open_acc(T0).to_record()["u"] == 3

    def test_hydrate_never_overwrites_a_live_hour(self):
        tr = GuildTracker(floor_ts=T0)
        tr.record(T0 + 1, 1, "c")
        before = tr.open_acc(T0).msgs
        tr.hydrate_slot(models.slot_for_ts(T0), {"m": 99, "u": 9})
        assert tr.open_acc(T0).msgs == before
