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

PUZZLES = {LATE_RABBIT["id"]: LATE_RABBIT}
