from cardcollect import embeds, imagegen
from cardcollect.models import Card


class FakeUser:
    def __init__(self, mention="<@123>"):
        self.mention = mention


def test_claim_result_embed_color_matches_the_card_rarity():
    # regression test: live feedback asked for the claim embed's accent bar
    # to match the card's rarity color (same as the border imagegen.py
    # draws on the card art itself), instead of always being flat purple.
    # Check all four tiers so a future rarity/color mapping change can't
    # silently drift the two apart.
    for rarity in ("common", "rare", "epic", "legendary"):
        card = Card(1, "A", "S", rarity, "1.png")
        embed = embeds.claim_result_embed(card, FakeUser(), outcome="new")
        expected = imagegen.RARITY_COLORS[rarity]
        assert (embed.colour.r, embed.colour.g, embed.colour.b) == expected


def test_claim_result_embed_color_still_varies_by_rarity_for_duplicate_and_sell_token_outcomes():
    # the rarity-color fix must apply to every non-test outcome, not just
    # the "new card" path -- these two were flat purple before as well
    common_card = Card(1, "A", "S", "common", "1.png")
    legendary_card = Card(2, "B", "S", "legendary", "2.png")

    dup_common = embeds.claim_result_embed(common_card, FakeUser(), outcome="duplicate")
    dup_legendary = embeds.claim_result_embed(legendary_card, FakeUser(), outcome="duplicate")
    assert dup_common.colour != dup_legendary.colour

    token_common = embeds.claim_result_embed(common_card, FakeUser(), outcome="sell_token", price=25)
    token_legendary = embeds.claim_result_embed(legendary_card, FakeUser(), outcome="sell_token", price=1500)
    assert token_common.colour != token_legendary.colour


def test_claim_result_embed_test_mode_stays_red_regardless_of_rarity():
    # test claims must keep their own distinct color (not the rarity color)
    # so they're never visually mistaken for a real claim, whatever rarity
    # the test drop happened to roll
    legendary_card = Card(1, "A", "S", "legendary", "1.png")
    embed = embeds.claim_result_embed(legendary_card, FakeUser(), outcome="new", is_test=True)
    assert embed.colour == embeds.TEST_COLOR
