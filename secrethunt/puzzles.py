"""Puzzle definitions. Add a new dict here (and a new id) for each weekly puzzle."""

LATE_RABBIT = {
    "id": "late_rabbit",
    "title": "The Late Rabbit",
    "tier": "easy",
    "trigger": "tick",           # the word the poem's first letters spell
    "poem_title": "Pinned to the Mad Hatter's table",
    "poem": [
        "Tea's gone cold, and nobody came.",
        "Idle clocks all read the same.",
        "Cheshire grins but won't say why.",
        "Kettle's still whistling. So am I.",
    ],
    # variant i hides its object in pool[i] and ends on answers[i]; the order of players is hashed, not chosen
    "answers": ["hatter", "dormouse", "flamingo", "jabberwock", "walrus"],
    "object_title": "A pocket watch",
    "object_text": "Stopped at six o'clock. Cold tea beside it. It only ticks when somebody notices.",
    "riddle": (
        "\N{RABBIT FACE} *The Rabbit's ears twitch. You said the word.*\n\n"
        "I am late, I am late, and I have lost my watch. It is **held in place** in a room whose name I have "
        "shaken loose: **{hint}** ({shape} letters).\n\n"
        "Find it, and give it a wind with \N{MANTELPIECE CLOCK}."
    ),
    "riddle_no_hint": (
        "\N{RABBIT FACE} *The Rabbit's ears twitch. You said the word.*\n\n"
        "I am late, I am late, and I have lost my watch. It is **held in place** in {channel}.\n\n"
        "Find it, and give it a wind with \N{MANTELPIECE CLOCK}."
    ),
    "found": (
        "\N{MANTELPIECE CLOCK} *Tick.*\n\nThe watch starts. The Rabbit yawns and curls up for a nap. "
        "Let him sleep. He will send word when he wakes."
    ),
    "waiting": "\N{SLEEPING SYMBOL} *The Rabbit is fast asleep. Let him rest.*",
    "final": (
        "\N{RABBIT FACE} *The Rabbit wakes, muttering one word over and over. It comes out all jumbled:*\n\n"
        "**{anagram}**\n\nSay it back to me here, in this chat."
    ),
    "decoy": (
        "\N{MANTELPIECE CLOCK} *Tick... tock.* It is a watch, but not the Rabbit's.\n\n"
        "*The Rabbit's voice drifts over, thin and sharp:* One more wrong watch, and I will stop listening."
    ),
    "lockout": (
        "\N{RABBIT FACE} *That is not my watch either.* I have stopped listening. Come back tomorrow and "
        "look again, and mind which watch you wind."
    ),
    "wrong": [
        "\N{RABBIT FACE} *The Rabbit tilts his head.* Not quite.",
        "\N{RABBIT FACE} *He shakes his ears.* That is not what he said.",
        "\N{RABBIT FACE} *A long, patient blink.* Try again.",
    ],
    "locked": "\N{RABBIT FACE} *The Rabbit has stopped listening for today.* Come back tomorrow.",
    "solved": (
        "\N{TOP HAT} *Yes! That is it! That is exactly it!*\n\n"
        "You caught the Late Rabbit. Your name is on the **Hall of Secrets** now.{rank}"
    ),
    "already": "\N{RABBIT FACE} *You have already caught the Rabbit.* He is very pleased with you.",
}

# Easy #2. Different on purpose: a riddle poem (its answer is the trigger word, no first-letter trick), a room named in
# words instead of a jumble, a button object (Drink / Eat / Knock / Look) answered privately, and a picture to read
# instead of a word to unjumble.
LOOKING_GLASS = {
    "id": "looking_glass",
    "title": "The Looking Glass",
    "tier": "easy",
    "kind": "button",            # the object is pressed (views.py), not reacted to
    "trigger_kind": "riddle",    # the poem is a riddle whose answer is the trigger; every other puzzle spells it
    "trigger": "mirror",         # the answer to the poem's riddle
    "poem_title": "Hung where the Queen forgot it",
    "poem": [
        "I never speak, yet I answer every word.",
        "I hold your face but I cannot keep it.",
        "My left is your right, and my right is your left.",
        "Walk in, and all the writing runs backwards.",
        "Call me by my name, and I will let you through.",
    ],
    # variant i hides its object in pool[i]; the right button and the final answer also depend on the variant
    "answers": ["butterfly", "dragonfly", "horsefly", "firefly", "housefly"],
    "rebuses": [
        "\N{BUTTER}\N{FLY}",       # butter + fly
        "\N{DRAGON}\N{FLY}",                # dragon + fly
        "\N{HORSE FACE}\N{FLY}",            # horse + fly
        "\N{FIRE}\N{FLY}",                  # fire + fly
        "\N{HOUSE BUILDING}\N{FLY}",        # house + fly
    ],
    "buttons": ["look", "drink", "knock", "eat", "look"],
    "button_hints": {
        "look": "Gaze into it for a while.",
        "drink": "Mind the little bottle that says DRINK ME.",
        "eat": "Mind the little cake that says EAT ME.",
        "knock": "Mind the tiny door, and be polite.",
    },
    # how each room is named: by a written description when its title contains the keyword, and by a letter clue for the
    # variants in `letter_variants` (and for any room no keyword fits), so the riddles are not all one trick
    "pointers": {
        "gaming": "the room where games are argued over and nobody agrees on the best one",
        "music": "the room where songs are passed around like sweets",
        "memes": "the room where the jokes have been shared so often they have worn smooth",
        "movies": "the room where the lights go down and everyone has an opinion about the ending",
        "cuddle": "the room where the whole court gathers, always busy, always talking",
    },
    "letter_variants": [1, 3],
    "object_title": "A looking glass",
    "object_text": (
        "A looking glass hangs crooked on the wall. Under it sit a little bottle, a little cake and a very small door. "
        "Everything here looks like it wants something from you."
    ),
    "riddle": (
        "\N{MIRROR} *The glass ripples. You called its name.*\n\n"
        "The Queen's looking glass has slipped off its hook, and it is hiding in {pointer}. Go there. {object_hint}\n\n"
        "Press the right button, and only the right room will answer."
    ),
    "riddle_no_hint": (
        "\N{MIRROR} *The glass ripples. You called its name.*\n\n"
        "The Queen's looking glass has slipped off its hook, and it is hiding in {channel}. Go there. {object_hint}\n\n"
        "Press the right button, and only the right room will answer."
    ),
    "found": (
        "\N{MIRROR} *The glass fogs over from the inside, then goes quiet.*\n\n"
        "Let it settle. It will have something to show you when the morning comes."
    ),
    "waiting": "\N{MIRROR} *The glass is fogged over and still. Let it settle.*",
    "final": (
        "\N{MIRROR} *The glass clears. A little picture drifts across it, and a voice reads it out, backwards:*\n\n"
        "**{rebus}**\n\n({shape} letters) Say what it spells, here in this chat."
    ),
    "idle": "\N{MIRROR} *Your own face looks back at you, and nothing else.*",
    "decoy": (
        "\N{MIRROR} *Your own face looks back at you. This is not the Queen's glass.*\n\n"
        "*A cold voice whispers from behind you:* One more wrong press, and I will stop listening."
    ),
    "lockout": (
        "\N{MIRROR} *The glass turns its face to the wall.* I have stopped listening. Come back tomorrow, "
        "and mind which button you press."
    ),
    "wrong": [
        "\N{MIRROR} *The picture wobbles, then settles.* Not quite.",
        "\N{MIRROR} *The glass shows your reflection shaking its head.* That is not what it spells.",
        "\N{MIRROR} *A long, patient ripple.* Try again.",
    ],
    "locked": "\N{MIRROR} *The glass has turned its face to the wall for today.* Come back tomorrow.",
    "solved": (
        "\N{MIRROR} *The glass cracks into a hundred small mirrors, and every one of them is smiling at you.*\n\n"
        "You found your way through. Your name is on the **Hall of Secrets** now.{rank}"
    ),
    "already": "\N{MIRROR} *You have already stepped through.* The glass is rather fond of you.",
}

PUZZLES = {p["id"]: p for p in (LATE_RABBIT, LOOKING_GLASS)}
