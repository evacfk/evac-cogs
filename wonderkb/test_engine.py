"""Pure logic: no discord, no redbot."""
import types

from wonderkb import engine, seed
from wonderkb.constants import AUTO_PREFIX, MANUAL_PREFIX, MAX_CHUNK_CHARS


def test_match_channel_ignores_decoration_and_lookalikes():
    names = {1: "📖・server-guide", 2: "wondershop", 3: "🏪・shop", 4: "rules"}
    assert engine.match_channel("server-guide", names) == 1
    assert engine.match_channel("shop", names) == 3          # never the "wondershop" lookalike
    assert engine.match_channel("rules", names) == 4
    assert engine.match_channel("nope", names) is None
    assert engine.match_channel("", names) is None


def test_display_name_strips_the_emoji_prefix():
    assert engine.display_name("📖・server-guide") == "server-guide"
    assert engine.display_name("rules") == "rules"


def test_clean_text_drops_mentions_and_blank_runs():
    raw = "Hi <@123> and <@&9> @everyone\n\n\n\nRead this   \nline"
    assert engine.clean_text(raw) == "Hi  and\n\nRead this\nline"


def test_message_text_includes_embed_title_description_and_fields():
    text = engine.message_text("plain", [{"title": "T", "description": "D", "fields": [{"name": "n", "value": "v"}]}])
    assert "plain" in text and "T" in text and "D" in text and "n: v" in text


def test_chunk_text_respects_the_limit_and_never_cuts_mid_word():
    long = " ".join(["word"] * 900)
    chunks = engine.chunk_text(long, limit=200)
    assert len(chunks) > 1
    assert all(len(c) <= 200 for c in chunks)
    assert all(not c.startswith("ord") and not c.endswith("wor") for c in chunks)
    assert " ".join(chunks).split() == long.split()


def test_chunk_text_packs_short_paragraphs_together():
    assert engine.chunk_text("a\n\nb\n\nc", limit=100) == ["a\n\nb\n\nc"]


def test_channel_entries_are_stable_and_labelled():
    out = engine.channel_entries(7, "rules", [(55, "Be kind."), (56, "")])
    assert [e.name for e in out] == [f"{AUTO_PREFIX}ch:7:55:0"]
    assert out[0].text == "From #rules: Be kind."


def _live_shop():
    """The shape `.kb shopdump` printed on the live bot."""
    item = lambda price, desc="": {"price": price, "amount": None, "description": desc, "role_id": 1}
    return {"wondershop - colors": {
        "description": "", "thumbnail": "", "giftable": True,
        "stock": {"Cloud": item(20000), "Pink": item(20000), "Rose": item(20000), "Void": item(50000, "A rare dark color")},
    }}


def test_shop_entries_read_the_live_format_without_one_entry_per_color():
    out = engine.shop_entries(_live_shop())
    texts = {e.name: e.text for e in out}
    howto = texts[f"{AUTO_PREFIX}shop:wondershop - colors:__howto__"]
    assert ".shop buy" in howto and "#bot-commands" in howto and ".shop gift" in howto and ".shop inventory" in howto
    cheap = texts[f"{AUTO_PREFIX}shop:wondershop - colors:price:20000:0"]
    assert "cost 20,000 wondercoins each" in cheap and "Cloud, Pink, Rose" in cheap
    assert "Void" not in cheap                                      # different price, different line
    assert "50,000 wondercoins" in texts[f"{AUTO_PREFIX}shop:wondershop - colors:price:50000:0"]
    assert texts[f"{AUTO_PREFIX}shop:wondershop - colors:item:Void"].endswith("A rare dark color")
    assert not any(n.endswith(":item:Cloud") for n in texts)       # a plain color has no entry of its own
    assert len(out) == 4


def test_shop_entries_chunk_a_long_price_group_and_name_the_shop():
    big = {"s": {"stock": {f"Color {i}": {"price": 5} for i in range(300)}}}
    out = [e for e in engine.shop_entries(big) if ":price:5:" in e.name]
    assert len(out) > 1 and all(len(e.text) <= MAX_CHUNK_CHARS for e in out)
    assert all("In the shop 's'" in e.text and "cost 5 wondercoins each" in e.text for e in out)
    assert "Color 0" in " ".join(e.text for e in out) and "Color 299" in " ".join(e.text for e in out)


def test_shop_entries_still_read_the_older_layout_and_skip_junk():
    old = {"old": {"Items": {"Rose": {"Cost": 100, "Info": "pink"}}}, "bad": "x", "empty": {"stock": {}}}
    texts = [e.text for e in engine.shop_entries(old)]
    assert any("100 wondercoins" in t for t in texts) and any("pink" in t for t in texts)
    assert engine.shop_entries(None) == [] and engine.shop_entries([1]) == []


def _cmd(name, level="NONE", perms=0, hidden=False, parents=()):
    return types.SimpleNamespace(
        qualified_name=name, hidden=hidden, parents=list(parents),
        requires=types.SimpleNamespace(privilege_level=types.SimpleNamespace(name=level),
                                       user_perms=types.SimpleNamespace(value=perms) if perms else None))


def test_is_public_command_rejects_anything_not_for_everyone():
    assert engine.is_public_command(_cmd("heist"))
    assert not engine.is_public_command(_cmd("heist reset", level="ADMIN"))
    assert not engine.is_public_command(_cmd("x", level="MOD"))
    assert not engine.is_public_command(_cmd("x", perms=8))
    assert not engine.is_public_command(_cmd("x", hidden=True))


def test_a_subcommand_of_a_mod_group_is_not_public_even_if_it_looks_open():
    group = _cmd("modgroup", level="MOD")
    assert not engine.is_public_command(_cmd("modgroup list", parents=[group]))


def test_cog_entries_describe_each_cog_and_skip_system_and_empty_ones():
    out = engine.cog_entries([
        engine.CogInfo("Heist", "Plan a group heist.\n\nLong details.", ("heist", "heist stats")),
        engine.CogInfo("Assistant", "ai", ("assistant",)),
        engine.CogInfo("Mod", "moderation", ()),
    ])
    assert [e.name for e in out] == ["kb:cog:Heist"]
    assert out[0].text == "The Heist cog. Plan a group heist. Long details. Commands anyone can run: .heist, .heist stats."


def test_cog_entries_cap_a_long_command_list():
    out = engine.cog_entries([engine.CogInfo("Big", "", tuple(f"c{i}" for i in range(40)))])
    assert ".c14" in out[0].text and ".c15" not in out[0].text and "and 25 more" in out[0].text


def test_seed_connects_wondercoins_to_gambling_and_heists():
    text = " ".join(e.text for e in engine.seed_entries(seed.ROWS))
    assert "What you can do with wondercoins" in text and "heist" in text.lower() and "#wondercasino" in text
    # Only say what the guide or the cog itself says: nothing invented about how heists or fishing work.
    assert "group" not in text.lower().split("heist", 1)[1][:80]
    assert "bait shop" not in text.split("What you can do with wondercoins", 1)[1].split("Run commands in", 1)[0]


def test_alias_entries_say_what_the_shortcut_runs():
    out = engine.alias_entries([("removemy", "selfrole remove"), ("", "x")])
    assert len(out) == 1 and ".removemy" in out[0].text and ".selfrole remove" in out[0].text


def test_guide_entries_never_include_mod_pages():
    entry = types.SimpleNamespace
    pages = [
        entry(key="economy", title="Economy", mod_only=False,
              entries=[entry(usage=".bank balance", desc="check your coins", expand=())]),
        entry(key="mod", title="Mod Cheat Sheet", mod_only=True,
              entries=[entry(usage=".case", desc="moderation", expand=())]),
    ]
    out = engine.guide_entries(pages)
    assert len(out) == 1 and ".bank balance" in out[0].text
    assert not any(".case" in e.text for e in out)


def test_seed_tells_members_where_to_run_commands():
    texts = " ".join(e.text for e in engine.seed_entries(seed.ROWS))
    assert "Run every bot command in #bot-commands" in texts


def test_seed_has_no_mod_only_commands():
    out = engine.seed_entries(seed.ROWS)
    assert len(out) == len(seed.ROWS) > 20
    assert len({e.name for e in out}) == len(out)                 # unique names
    for e in out:
        assert e.name.startswith(AUTO_PREFIX)
        assert ".secrethunt" not in e.text and ".case " not in e.text and ".quarantine" not in e.text


def test_plan_sync_only_reembeds_what_changed():
    wanted = [engine.Entry("kb:a", "same"), engine.Entry("kb:b", "new text"), engine.Entry("kb:c", "added")]
    changed, stale = engine.plan_sync({"kb:a": "same", "kb:b": "old text"}, wanted, {"kb:"})
    assert [e.name for e in changed] == ["kb:b", "kb:c"] and stale == []


def test_plan_sync_deletes_stale_only_from_sources_that_were_read():
    existing = {"kb:ch:1:9:0": "x", "kb:ch:2:9:0": "y", "kb:shop:s:i": "z"}
    changed, stale = engine.plan_sync(existing, [], {"kb:ch:1:"})
    assert changed == [] and stale == ["kb:ch:1:9:0"]            # channel 2 and the shop were NOT read, so kept


def test_unsure_detection_handles_curly_apostrophes_and_case():
    assert engine.is_unsure_reply("I’m not sure, try .wld search x or ask a mod.", "I'm not sure, try")
    assert engine.is_unsure_reply("  i'm NOT sure, try .help", "I'm not sure, try")
    assert not engine.is_unsure_reply("Use .puzzle mypieces", "I'm not sure, try")
    assert not engine.is_unsure_reply("anything", "")


def test_question_key_ignores_case_and_punctuation():
    assert engine.question_key("How do I buy a color?") == engine.question_key("how do i buy a COLOR")
    assert engine.question_key("a") != engine.question_key("b")


def test_parse_qa():
    assert engine.parse_qa("How? | Like this") == ("How?", "Like this")
    assert engine.parse_qa("a | b | c") == ("a", "b | c")
    assert engine.parse_qa("no split") is None and engine.parse_qa(" | x") is None and engine.parse_qa("x | ") is None


def test_manual_entries_use_their_own_prefix():
    assert engine.manual_name("Q?").startswith(MANUAL_PREFIX)
    assert not engine.manual_name("Q?").startswith(AUTO_PREFIX)    # sync must never own these


def test_prune_seen_drops_expired_and_caps():
    seen = {"old": 0.0, "new": 100.0, "newer": 101.0}
    assert engine.prune_seen(seen, now=100.0 + 10, ttl=50, cap=10) == {"new": 100.0, "newer": 101.0}
    assert list(engine.prune_seen({"a": 1.0, "b": 2.0, "c": 3.0}, now=4.0, ttl=100, cap=2)) == ["c", "b"]
