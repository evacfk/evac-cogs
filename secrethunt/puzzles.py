"""Puzzle definitions. Add a new dict here (and a new id) for each weekly puzzle."""

LATE_RABBIT = {
    "id": "late_rabbit",
    "title": "The Late Rabbit",
    "tier": "easy",
    "trigger": "tick",           # the word the poem's first letters spell
    "poem_title": "Pinned to the Mad Hatter's table",
    "typing_hint": "keys, not shouting",   # Easy poems carry the "you have to type" hint inside the verse (a test checks)
    "poem": [
        "Tea's gone cold, and nobody came.",
        "Idle clocks all read the same.",
        "Cheshire grins but won't say why.",
        "Keys, not shouting, will catch his eye.",
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
    "typing_hint": "use your fingers",
    "poem": [
        "I never speak, yet I answer every word.",
        "I hold your face but I cannot keep it.",
        "My left is your right, and my right is your left.",
        "Walk in, and all the writing runs backwards.",
        "Call me by my name, but use your fingers; I am hard of hearing.",
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

# Medium #1. A different shape from both Easies: a logic trial, not a chain of finds. Five witnesses (the five hiding
# places) each give every member a private, generated statement; some of them lie; the member deduces the thief (round 1)
# and then the thief and the hour (round 2). trial.py generates and checks the cases, so there is no `answers` list here.
KNAVE_TRIAL = {
    "id": "knaves_trial",
    "title": "The Knave's Trial",
    "tier": "medium",
    "kind": "trial",             # witnesses are pressed (views.WitnessView) and the answers are generated (trial.py)
    "trigger_kind": "breaker",   # one end-word per verse breaks the rhyme; read in order those words are the trigger
    "trigger": "mock turtle soup",
    "breakers": ["mock", "turtle", "soup"],
    "needs_pool": 5,             # one witness per hiding place
    "poem_title": "Nailed to the courtroom door",
    "poem": [
        "The Queen of Hearts is in a rage,",
        "The tarts are gone, the Knave on stage,",
        "The jurors sit and turn the page,",
        "And copy down each word, then mock.",
        "A witness takes the stand and swears,",
        "The Hatter shakes, the Dormouse stares,",
        "Some lie, some don't, and no one cares,",
        "While far behind them creeps the turtle.",
        "The hour is late, the candles burn,",
        "The Queen awaits her turn to learn",
        "A fitting end the guilty earn,",
        "And who will end up in the soup.",
    ],
    # printed under the poem: the verses hide the words, this line says what to do with them (Easy poems carry theirs inside)
    "nudge": "Some words would not behave. The court will hear them in order, dot first.",
    "object_title": "A witness waits here",
    "object_text": "A witness sits in the dock with their hands folded, waiting to be asked.",
    "summons": (
        "\N{SCALES} *A bell rings somewhere, and a gavel falls.*\n\n"
        "The Knave of Hearts is on trial for stealing the Queen's tarts, and **you** have been called to the jury.\n\n"
        "**The rules of the court**\n"
        "\N{BULLET} Five witnesses have given their statements. **Exactly two of them are lying.** The rest tell the truth.\n"
        "\N{BULLET} Every juror hears their own version of the testimony, so do not expect anyone else's to match yours.\n"
        "\N{BULLET} The suspects: the Hatter, the March Hare, the Dormouse, the Duchess and the Cook.\n\n"
        "A witness waits in each of these rooms: {rooms}. Press the button on the pinned message in each one to hear "
        "them out. When you know who stole the tarts, tell me the name here, in this chat.\n\n"
        "*Stuck for two days or more? Whisper me* `hint`."
    ),
    "round1_done": (
        "\N{SCALES} *The gavel falls.* **Correct.** The court accepts your verdict and the Knave is let go, for now. "
        "The court adjourns until morning.\n\n"
        "The Queen has more questions, and she does not like to be wrong."
    ),
    "summons2": (
        "\N{SCALES} *The gavel falls again. The court reconvenes.*\n\n"
        "**Bad news.** Overnight the bailiff found out that one of the witnesses you trusted yesterday was lying all "
        "along. **Yesterday's verdict is void**, the Knave is back in the dock, and the thief may not be who you named.\n\n"
        "The Queen wants **the whole truth** this time: who stole the tarts **and the hour it happened**, from 1 to 12. "
        "Answer with both, for example `Hatter 9`.\n\n"
        "The witnesses have new things to say, and **exactly three of the five are lying** now. Hear them out again "
        "in {rooms}.\n\n"
        "*Stuck for two days or more? Whisper me* `hint`."
    ),
    "waiting": "\N{SCALES} *The court is adjourned.* The doors are shut and the witnesses have gone home. It reconvenes in the morning.",
    "idle": "\N{SCALES} *The witness looks straight through you.* Only the jury is heard here.",
    "closed": "\N{SCALES} *The witness has gone home.* The court is not in session.",
    "testimony": "\N{SCALES} *The witness in {room} clears their throat.*\n\n> {text}",
    "recess": "\N{SCALES} *The court is in recess.* It reconvenes {until}.",
    "wrong1": (
        "\N{SCALES} *Wrong.* That is not the thief. The court is in recess, and when it reconvenes the witnesses will "
        "remember things differently. It reconvenes {until}."
    ),
    "wrong2": (
        "\N{SCALES} *The Queen is displeased.* That is not the verdict. The court is in recess, and the witnesses "
        "will have a new story when it reconvenes {until}."
    ),
    "malformed_suspect": (
        "\N{SCALES} *The Queen frowns.* I need **one** suspect: the Hatter, the Hare, the Dormouse, the Duchess or the "
        "Cook. That one does not count against you."
    ),
    "malformed_hour": (
        "\N{SCALES} *The Queen frowns.* She wants the thief **and** the hour, from 1 to 12. For example `Hatter 9`. "
        "That one does not count against you."
    ),
    "hint": (
        "\N{ELECTRIC LIGHT BULB} *A bailiff leans in and whispers:*\n\n> {hint}\n\n"
        "(Hint {n} of {max}. Hints are listed beside your name in the Hall of Secrets.)"
    ),
    "hint_early": "\N{ELECTRIC LIGHT BULB} *Not yet.* The bailiff will have something for you {when}.",
    "hint_max": "\N{ELECTRIC LIGHT BULB} *The bailiff shrugs.* That is all the help you get this round.",
    "solved": (
        "\N{SCALES} *The courtroom erupts. Order! Order!* You found the truth, and the Knave walks free. "
        "Your name is on the **Hall of Secrets** now.{rank}"
    ),
    "already": "\N{SCALES} *The trial is over, and the Knave walks free.* The witness waves politely.",
}

# Medium #2. A six-stage trail of different tasks (act, search, experiment, count, navigate, assemble), played in the
# server and in DMs. Everything personal is generated (trail.py); the story and the kinds of stage are shared.
# `wip` keeps it playable by admins only until every stage is built.
TREACLE_WELL = {
    "id": "treacle_well",
    "title": "The Treacle Well",
    "tier": "medium",
    "kind": "trail",             # a pinned tea table in every trail room (views.TrailView); no typed trigger
    "trigger_kind": "reactions", # the poem is a riddle of three pictures; the answer is three emoji, in order
    "wip": True,                 # push 1 of 3: stages 1-2 only, admins only
    "needs_trail": 12,           # public rooms that each hold a tea table (12 planned; the panel allows fewer while wip)
    "trail_min": 6,
    "poem_title": "Written on the Dormouse's teacup",
    "poem": [
        "The Dormouse tells a tale, and every time",
        "he reaches the good part, he stops.",
        "First comes whatever pours his tea.",
        "Then the sweet, slow, bottomless thing the sisters drew from the well.",
        "Last comes what happens to him in the middle of every sentence.",
        "Wake him with all three, in order.",
    ],
    "nudge": "Show him. Don't tell him.",
    "object_title": "A tea table",
    "object_text": "A long table, laid for tea. Nobody is sitting at it, but the cups are still warm.",
    "clue1": (
        "\N{TEACUP WITHOUT HANDLE} *The Dormouse stirs, blinks, and begins his tale at once, as if he had never stopped.*\n\n"
        "There were three little sisters, and they lived at the bottom of a well. Elsie was the eldest. "
        "**Find her**: she sits at a tea table in {pointer}. Press **Look** on it.\n\n"
        "*Lost the thread? Whisper me* `recap`."
    ),
    "sisters": [
        # Elsie
        ("\N{TEACUP WITHOUT HANDLE} *Elsie stirs her tea and does not look up.*\n\n"
         "> \"I counted **{number}** lumps of sugar before I fell asleep, and I tucked a **{token}** under the saucer. "
         "My sister Lacie is the one who knows the way down. She is at a tea table in {pointer}.\""),
        # Lacie
        ("\N{TEACUP WITHOUT HANDLE} *Lacie turns her cup round and round.*\n\n"
         "> \"**{number}** spoonfuls, no more, no less. I kept a **{token}** in my pocket for later. "
         "Tillie is the youngest and the sleepiest. Look for her in {pointer}.\""),
        # Tillie
        ("\N{TEACUP WITHOUT HANDLE} *Tillie yawns, and it is a very long yawn.*\n\n"
         "> \"**{number}** is how many I poured. I hid a **{token}** in the sugar bowl, so mind the sugar. "
         "That is all of us.\"\n\n"
         "*The Dormouse is snoring. The next part of the tale is not written yet.*"),
    ],
    "sisters_done_dm": (
        "\N{TEACUP WITHOUT HANDLE} *All three sisters have spoken, and the Dormouse has fallen asleep on the teapot.* "
        "Keep what they told you safe. The tale goes on soon."
    ),
    # how each room is named in a sister's line: a written description when the room's title contains the keyword, the
    # letter clue otherwise (and always for the steps in `letter_variants`)
    "pointers": {
        "gaming": "the room where games are argued over and nobody agrees on the best one",
        "music": "the room where songs are passed around like sweets",
        "meme": "the room where the jokes have been shared so often they have worn smooth",
        "movie": "the room where the lights go down and everyone has an opinion about the ending",
        "anime": "the room where the cartoons are drawn far too well to be for children",
        "book": "the room where the shelves are crowded and nobody has finished the pile",
        "food": "the room where everyone is hungry and nobody agrees on dinner",
        "pets": "the room full of small creatures, none of whom are the ones who took the tarts",
        "drawing": "the room where the walls are never quite dry",
        "tech": "the room where something is always broken and someone always knows why",
        "sport": "the room where everyone shouts about a game they only watched",
        "travel": "the room where everyone has been somewhere else",
        "photo": "the room where the pictures are kept and the stories behind them are told",
        "game": "the room where games are argued over and nobody agrees on the best one",
        "cuddle": "the room where the whole court gathers, always busy, always talking",
        "animal": "the room where a different creature is introduced every morning",
        "witch": "the room where a bunny in a pointed hat keeps its spells",
        "dive": "the room where one question goes down and down and never quite reaches the bottom",
    },
    "letter_variants": [1],
    "wrong_look": [
        "\N{TEACUP WITHOUT HANDLE} *Only a tea table. The tea has gone cold and nobody has sat here.*",
        "\N{TEACUP WITHOUT HANDLE} *A teapot, a saucer, a crumb. Nothing that belongs to the tale.*",
        "\N{TEACUP WITHOUT HANDLE} *The cups are empty. This is not where the sisters sit.*",
    ],
    "rest": "\N{SLEEPING SYMBOL} *The tea tables have gone quiet.* They will wake {until}.",
    "rest_start": (
        "\N{SLEEPING SYMBOL} *The tea tables sigh, and one by one they go quiet.* Too many wrong tables in a row. "
        "They will wake {until}."
    ),
    "quiet": "\N{TEACUP WITHOUT HANDLE} *You pour, you bow, you drink to nobody. Nothing happens, yet.*",
    "idle": "\N{TEACUP WITHOUT HANDLE} *Only a tea table. The tea has gone cold.*",
    "already": "\N{TEACUP WITHOUT HANDLE} *The tale is over, and the well is quiet.*",
    "solved": (
        "\N{TEACUP WITHOUT HANDLE} *You reach the bottom of the well, and the sisters were drawing something after all.*\n\n"
        "Your name is on the **Hall of Secrets** now.{rank}"
    ),
}

PUZZLES = {p["id"]: p for p in (LATE_RABBIT, LOOKING_GLASS, KNAVE_TRIAL, TREACLE_WELL)}
