import random

import pytest

from cardcollect import captcha
from cardcollect.constants import CODE_DIGITS, CODE_LENGTH, CODE_LETTERS, CODE_SYMBOLS

CONFUSABLE = set("0OQD1IL5S2Z8BUVG6")


def test_alphabet_excludes_every_confusable_glyph():
    chars = set(CODE_LETTERS + CODE_DIGITS + CODE_SYMBOLS)
    assert chars.isdisjoint(CONFUSABLE)
    assert captcha.ALPHABET == frozenset(chars)


def test_generated_codes_always_have_the_documented_shape():
    rng = random.Random(1234)
    for _ in range(2000):
        code = captcha.generate_code(rng)
        assert len(code) == CODE_LENGTH
        assert captcha.looks_like_code(code)
        assert sum(ch in CODE_SYMBOLS for ch in code) == 1, "exactly one symbol"
        assert any(ch in CODE_DIGITS for ch in code) and any(ch in CODE_LETTERS for ch in code)


def test_generate_code_is_deterministic_for_a_seed():
    assert captcha.generate_code(random.Random(7)) == captcha.generate_code(random.Random(7))
    assert captcha.generate_codes(3, random.Random(7)) == captcha.generate_codes(3, random.Random(7))


def test_normalize_uppercases_and_strips():
    assert captcha.normalize("  k3+9t \n") == "K3+9T"
    assert captcha.normalize("") == ""
    assert captcha.normalize(None) == ""


@pytest.mark.parametrize("text", ["K3+9T", "M7#4E", "9E@AN", "A?3CE", "#K3T9", "K3T9+"])
def test_looks_like_code_accepts_well_formed_codes(text):
    assert captcha.looks_like_code(text)


@pytest.mark.parametrize(
    "text",
    [
        "",  # empty
        "K3+9",  # too short
        "K3+9TA",  # too long
        "K3+9T".lower(),  # not normalized -- looks_like_code expects normalize() first
        "K3T9E",  # no symbol
        "K+3#T",  # two symbols
        "A+CEF",  # no digit
        "3+479",  # no letter
        "K3+0T",  # confusable glyph outside the alphabet
        "K3 +9",  # whitespace inside
        "HAHAH",  # ordinary chat that happens to use only alphabet letters
    ],
)
def test_looks_like_code_rejects_everything_else(text):
    assert not captcha.looks_like_code(text)


def test_edit_distance_basics():
    assert captcha.edit_distance("ABC", "ABC") == 0
    assert captcha.edit_distance("ABC", "ABD") == 1  # substitution
    assert captcha.edit_distance("ABC", "AB") == 1  # deletion
    assert captcha.edit_distance("AB", "ABC") == 1  # insertion
    assert captcha.edit_distance("", "ABC") == 3
    assert captcha.edit_distance("ABC", "") == 3
    assert captcha.edit_distance("KITTEN", "SITTING") == 3


def test_generate_codes_are_distinct_and_pairwise_far_apart():
    rng = random.Random(99)
    for _ in range(300):
        codes = captcha.generate_codes(3, rng)
        assert len(set(codes)) == 3
        for i, a in enumerate(codes):
            for b in codes[i + 1 :]:
                assert captcha.edit_distance(a, b) >= 2, "a single typo must never land on a neighboring card"


def test_generate_codes_gives_up_loudly_instead_of_looping_forever():
    with pytest.raises(ValueError):
        captcha.generate_codes(2, random.Random(1), min_distance=99)
