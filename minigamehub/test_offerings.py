import copy

from minigamehub import offerings
from minigamehub.config_schema import DEFAULT_GUILD


def _hunt():
    return copy.deepcopy(DEFAULT_GUILD["games"]["hunt"])


def test_render_fills_placeholders_and_formats_amount():
    out = offerings.render(
        "{user} fed the {animal} for {amount} {currency}",
        user="@Ann", animal="mouse", amount=1234, currency="wondercoins",
    )
    assert out == "@Ann fed the mouse for 1,234 wondercoins"


def test_render_tolerates_stray_braces_and_unknown_placeholders():
    out = offerings.render("{oops} {user} }{", user="@Ann", animal="crow", amount=5, currency="c")
    assert out == "{oops} @Ann }{"


def test_shipped_texts_have_no_salute_wording():
    for text in (offerings.CROW_TEXT, offerings.MOUSE_TEXT, offerings.ZERO_REWARD_TEXT):
        assert "salut" not in text.lower()
    assert "{amount}" in offerings.CROW_TEXT and "{amount}" in offerings.MOUSE_TEXT


def test_offering_text_only_for_safe_animals_with_text():
    assert offerings.offering_text(None) == ""
    assert offerings.offering_text({"safe": True}) == ""
    assert offerings.offering_text({"safe": True, "success_text": "  hi  "}) == "hi"
    assert offerings.offering_text({"safe": False, "success_text": "hi"}) == ""
    # entries predating the "safe" key count as safe
    assert offerings.offering_text({"success_text": "hi"}) == "hi"


def test_seed_adds_mouse_with_cheese_word_and_text():
    hunt = _hunt()
    assert offerings.seed_hunt_offerings(hunt) is True
    assert "mouse" in hunt["animals"]
    entry = hunt["safe_animals"]["mouse"]
    assert entry["safe"] is True and entry["safe_word"] == "cheese"
    assert entry["success_text"] == offerings.MOUSE_TEXT
    assert hunt["offerings_seeded"] is True
    # eagle (existing safe animal) untouched
    assert "success_text" not in hunt["safe_animals"]["eagle"]


def test_seed_gives_existing_crow_its_message_without_touching_numbers():
    hunt = _hunt()
    hunt["animals"]["crow"] = {"emoji": "x", "text": "caw"}
    hunt["safe_animals"]["crow"] = {"safe": True, "penalty_pct": 10, "reward_range": [100, 300], "safe_word": "shiny coin"}
    offerings.seed_hunt_offerings(hunt)
    crow = hunt["safe_animals"]["crow"]
    assert crow["success_text"] == offerings.CROW_TEXT
    assert crow["safe_word"] == "shiny coin" and crow["penalty_pct"] == 10 and crow["reward_range"] == [100, 300]


def test_seed_keeps_a_custom_crow_message():
    hunt = _hunt()
    hunt["safe_animals"]["crow"] = {"safe": True, "success_text": "custom"}
    offerings.seed_hunt_offerings(hunt)
    assert hunt["safe_animals"]["crow"]["success_text"] == "custom"


def test_seed_runs_once_so_admin_removal_sticks():
    hunt = _hunt()
    offerings.seed_hunt_offerings(hunt)
    del hunt["animals"]["mouse"]
    del hunt["safe_animals"]["mouse"]
    assert offerings.seed_hunt_offerings(hunt) is False
    assert "mouse" not in hunt["animals"]


def test_seed_does_not_overwrite_an_existing_mouse():
    hunt = _hunt()
    hunt["animals"]["mouse"] = {"emoji": "m", "text": "mine"}
    offerings.seed_hunt_offerings(hunt)
    assert hunt["animals"]["mouse"]["text"] == "mine"
    assert "mouse" not in hunt["safe_animals"]


def test_default_schema_flag_starts_false():
    assert DEFAULT_GUILD["games"]["hunt"]["offerings_seeded"] is False
