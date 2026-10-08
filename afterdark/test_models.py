from afterdark.models import ExcludeEntry, Interest, InterestMembership, Invite


def test_interest_roundtrip_with_and_without_role():
    a = Interest(key="feet", name="Feet", emoji="x", channel_id=776525958675955733, role_id=5)
    assert Interest.from_dict(a.to_dict()) == a
    b = Interest(key="bdsm", name="BDSM", emoji="", channel_id=9)
    assert Interest.from_dict(b.to_dict()) == b
    assert Interest.from_dict(b.to_dict()).role_id is None


def test_interest_from_dict_coerces_and_defaults():
    it = Interest.from_dict({"key": "feet", "channel_id": "12", "role_id": 0})
    assert it.channel_id == 12 and it.role_id is None and it.name == "feet"


def test_invite_roundtrip():
    inv = Invite(ts=123.5, channel_id=7, message_id=8)
    assert Invite.from_dict(inv.to_dict()) == inv
    assert Invite.from_dict({"ts": 1}).message_id is None


def test_exclude_entry_roundtrip():
    e = ExcludeEntry(by=1, reason="r", ts=2.0)
    assert ExcludeEntry.from_dict(e.to_dict()) == e
    assert ExcludeEntry.from_dict({}).reason == ""


def test_membership_roundtrip():
    m = InterestMembership(since=1.0, last=2.0, warned=3.0)
    assert InterestMembership.from_dict(m.to_dict()) == m
