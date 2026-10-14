"""Pure guide logic: live-command filtering, search, channel matching, layout."""
from wonderland import engine
from wonderland.content import MAP, PAGES, Entry, Page


def ex(*live):
    return lambda name: name in live


def test_entry_hidden_when_a_required_command_is_missing():
    e = Entry(".card daily", "daily pull", requires=("card daily",))
    assert engine.resolve_entry(e, ex("card daily")) == (".card daily", "daily pull")
    assert engine.resolve_entry(e, ex("card")) is None


def test_entry_with_all_required_needs_every_one():
    e = Entry(".star / .unstar", "x", requires=("star", "unstar"))
    assert engine.resolve_entry(e, ex("star")) is None
    assert engine.resolve_entry(e, ex("star", "unstar")) is not None


def test_expand_lists_only_live_commands_and_hides_when_none():
    e = Entry("{cmds}", "games", expand=("slot", "coin", "war"))
    assert engine.resolve_entry(e, ex("slot", "war")) == (".slot  .war", "games")
    assert engine.resolve_entry(e, ex()) is None


def test_entry_without_requirements_always_shows():
    assert engine.resolve_entry(Entry(".wld stats", "x"), ex()) is not None


def test_page_lines_formats_usage_and_description():
    page = Page("p", "x", "P", "b", (Entry(".a", "does a", requires=("a",)), Entry(".b", "does b", requires=("b",))))
    assert engine.page_lines(page, ex("a")) == ["`.a`: does a"]


def test_mod_page_is_hidden_from_non_mods():
    assert all(not p.mod_only for p in engine.visible_pages(False))
    assert any(p.mod_only for p in engine.visible_pages(True))
    assert engine.get_page("mod", False) is None
    assert engine.get_page("mod", True) is not None


def test_search_matches_command_and_description_case_insensitively():
    live = lambda name: True
    hits = engine.search("CARD", live, False)
    assert hits and all("card" in line.lower() for _, line in hits)
    assert engine.search(".leaderboard", live, False)


def test_search_ignores_too_short_queries_and_caps_results():
    live = lambda name: True
    assert engine.search("a", live, False) == []
    assert engine.search("  ", live, False) == []
    assert len(engine.search("the", live, True)) <= 15


def test_search_never_returns_mod_entries_to_members():
    live = lambda name: True
    member = [line for _, line in engine.search("quarantine", live, False)]
    mod = [line for _, line in engine.search("quarantine", live, True)]
    assert member == [] and mod


def test_search_skips_commands_that_do_not_exist():
    assert engine.search("fishing", ex(), False) == []


def test_match_channel_handles_decorated_names_and_hyphens():
    names = {1: "🧸・cuddle", 2: "📢 • server-updates", 3: "🛍️・wondershop", 4: "🏪 • shop", 5: "✅・verified-chat", 6: "🔐・verify"}
    assert engine.match_channel("cuddle", names) == 1
    assert engine.match_channel("server-updates", names) == 2
    assert engine.match_channel("shop", names) == 4          # not "wondershop"
    assert engine.match_channel("verify", names) == 6        # not "verified-chat"
    assert engine.match_channel("counting", names) is None


def test_match_channel_is_case_insensitive_for_spaces():
    assert engine.match_channel("room creator", {9: "🎮 • Room Creator"}) == 9


def test_map_lines_use_mentions_when_found_and_bold_names_otherwise():
    blocks = dict(engine.map_blocks(lambda slug: "<#7>" if slug == "cuddle" else None))
    chat = blocks["chat"]
    assert any(line.startswith("🧸 <#7>:") for line in chat)
    assert any(line.startswith("🌙 **pillow-talk**:") for line in chat)


def test_clamp_truncates_with_ellipsis():
    assert engine.clamp("x" * 10, 20) == "x" * 10
    out = engine.clamp("x" * 50, 20)
    assert len(out) == 20 and out.endswith("…")


def test_content_integrity():
    keys = [p.key for p in PAGES]
    assert len(keys) == len(set(keys))
    for p in PAGES:
        assert p.entries, p.key
        for e in p.entries:
            assert e.desc and e.usage
            assert not (e.expand and e.requires)
            if e.expand:
                assert "{cmds}" in e.usage
    slugs = [c.slug for s in MAP for c in s.channels]
    assert len(slugs) == len(set(slugs))
    assert "rabbit hole" not in " ".join(slugs) and "modmail" not in " ".join(slugs)


def test_every_page_fits_in_one_embed_even_with_every_command_live():
    live = lambda name: True
    for p in PAGES:
        text = p.blurb + "\n\n" + "\n".join(engine.page_lines(p, live))
        assert len(text) < 3900, (p.key, len(text))
    mapped = "\n\n".join(t + "\n" + "\n".join(lines) for t, lines in engine.map_blocks(lambda s: "<#123456789012345678>"))
    assert len(mapped) < 3900


def test_no_removed_commands_are_advertised():
    text = " ".join(e.usage + e.desc for p in PAGES for e in p.entries)
    assert "gamblehub" not in text
