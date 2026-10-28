from cardcollect import embeds, imagegen
from cardcollect.models import Card


class FakeUser:
    def __init__(self, mention="<@123>"):
        self.mention = mention


def test_draw_results_embed_shows_winner_picker_count_and_outcome():
    cards = {10: Card(10, "Frieren", "Sousou no Frieren", "legendary", "10.png"), 11: Card(11, "Fern", "Sousou no Frieren", "rare", "11.png")}
    results = [
        {"position": 0, "card_id": 10, "pickers": 3, "winner": 5, "outcome": "new", "contested": True, "boosted": False},
        {"position": 1, "card_id": 11, "pickers": 0, "winner": None, "outcome": None, "contested": False, "boosted": False},
    ]
    embed = embeds.draw_results_embed(results, cards, 3, [], is_test=False)
    text = embed.title + " " + (embed.description or "") + " ".join(f.name + f.value for f in embed.fields)
    assert "Frieren" in text and "<@5>" in text and "3 picked" in text
    assert "Fern" in text


def test_draw_results_embed_test_mode_is_marked_and_red():
    cards = {10: Card(10, "A", "S", "common", "10.png")}
    results = [{"position": 0, "card_id": 10, "pickers": 1, "winner": 5, "outcome": "new", "contested": False, "boosted": False}]
    embed = embeds.draw_results_embed(results, cards, 1, [], is_test=True)
    assert embed.colour == embeds.TEST_COLOR
    assert "would be" not in embed.description and "NEW" not in embed.description


def test_sets_and_wishlist_embeds_render_empty_and_full_states():
    assert embeds.sets_embed([], set(), "Your", 0).description
    full = embeds.sets_embed([("Frieren", 3, 3), ("Fairy Tail", 1, 4)], {"Frieren"}, "Your", 2)
    assert "Frieren" in full.description and "Fairy Tail" in full.description
    assert embeds.wishlist_embed([], False).description
    cards = [Card(1, "Fern", "Frieren", "rare", "1.png")]
    assert "Fern" in embeds.wishlist_embed(cards, True).description
