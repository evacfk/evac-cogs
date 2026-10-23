# secrethunt - Secrets of Wonderland

WoW-style secret hunts. Each puzzle starts from a public poem that hides a word (spelled by its first letters, or the
answer to its riddle). Saying the word starts a personal chain (every member gets their own variant). Solvers are listed on
the **Hall of Secrets**.

Tiers: **Easy** (days) and **Medium** (about a week) have no role or badge: solvers are only listed on the Hall.
**Hard** and **Expert** each have their own role and icon. Medium so far: **The Knave's Trial** and **The Treacle Well** (see below). Easy so far: **The Late Rabbit** (`late_rabbit`: acrostic poem, clock reaction, unscramble) and **The Looking Glass**
(`looking_glass`: riddle poem whose answer is `.mirror`, a room named in words, a pinned glass with Drink / Eat / Knock / Look
buttons answered privately, an emoji rebus). A puzzle with `"kind": "button"` gets the buttons from `views.py`; the pool and
setup steps are the same for both. For The Looking Glass use the same panel buttons; `.secrethunt preview looking_glass`
DMs you every variant's pointer, right button, rebus and answer (spoilers).

## The Knave's Trial (Medium, `knaves_trial`)
A logic puzzle instead of a chain of finds. The poem's rhyme-breakers (the last word of each verse that fails to rhyme),
read in order, are the trigger (a multi-word phrase; it lives in `puzzles.py` and is not written here). It works with the
prefix in chat (deleted at once) or as a plain DM.

Needs exactly **5** hiding places: each pool room holds one pinned witness with a "Hear the witness" button. Every member
hears their own private testimony (generated from a private seed, never a fixed list, and checked by a brute-force solver to
have exactly one valid answer).
- **Round 1:** exactly two of five witnesses lie. The answer is the thief's name only. A wrong answer locks the member out
  for **4 hours**, then the testimony re-rolls.
- The court adjourns until the next 9:00 Pacific (at least 4 hours).
- **Round 2:** harder testimony that also talks about the hour, exactly three liars. The answer is the thief **and** the hour
  (1 to 12), for example `Hatter 9`; the summons says so. A wrong answer re-rolls the testimony after a **4 hour** cooldown.
  Answers missing the hour or naming several suspects cost nothing.
- **Hints:** DM the bot `hint`. First one 48 hours after a round opens, then a day apart, at most 3 per round. They are always
  true (an honest witness, a liar, then an innocent suspect or a 4-hour window for the hour). The Hall and `.secrets` show
  how many hints each solver used.
- Medium has no role. `preview knaves_trial` DMs you your own cases for both rounds with the answers (spoilers).
- Set it up in the same panel: pool (exactly 5), *Set up objects*, *Go live*, post the poem. Test mode shrinks the lockout,
  cooldown, adjournment and hint waits to 30 seconds.

## The Treacle Well (Medium, `treacle_well`, six stages)
A six-stage trail of different tasks played in the server and in DMs. Everything personal (which rooms, which numbers, words,
buttons and the final answer) is generated per member from a private seed, so one member's route is useless to another. There
is no typed trigger: the pinned poem is a riddle, and the member answers it by reacting to the poem (the bot removes every
reaction at once, so the poem always looks untouched).
- **Stage 1** the poem. **Stage 2** three rooms found one after another, each with a pinned tea table and a **Look** button
  (replies are private). **Stage 3** a word test played by DM: the member asks the bot about single words, then answers one long
  message. **Stage 4** a pour-and-toast task using the **Pour** and **Toast** buttons on two of the tea tables. **Stage 5** four
  ordered "this button in that room" steps given as short verses. **Stage 6** a six-letter answer, DM'd to the bot.
- Members DM the bot `recap` for the current clue and `hint` when stuck: a stage's first hint comes 24 hours after it opened, the
  second 12 hours later, at most 2 per stage (Test mode: 30 seconds). While a Knave's Trial round is open `hint` belongs to it.
- Penalties: three wrong tables in a row rest the tea tables 30 minutes; a failed word test rests 15 minutes; three wrong toasts
  rest the cups an hour; five slips in a day close the tables for two hours; a wrong final answer costs an hour.
- The Hall shows each solver's hints and how many days it took. `.secrethunt status` shows a funnel (members at each stage,
  median minutes per finished stage, hints taken). `.secrethunt who <member>` shows one member's counters.
- Needs **trail rooms** (public forum posts or channels, 12 planned, at least 6), set under Hunt with *Trail rooms*. *Set up
  objects* pins a tea table in each. It does not use the 2-5 hiding place pool.
- While marked unfinished it is playable by admins and Manage Server members only; everyone else's reactions are just removed.
- The delivery loop reopens trail rooms that auto-archived. `preview treacle_well` DMs you your own trail (spoilers).

## Rehearsing (Test channel)
Under Hunt, set a private **Test channel**. While **Test mode** is on, *Set up objects* and the poem for ANY puzzle post
there instead of the real rooms: one object per room, each labelled with the room it stands in for, and the poem pinned in the
same channel. Nothing public changes, and a puzzle plays normally from that channel. Turning Test mode off asks first, then posts
every rehearsed puzzle for real: its objects in the real rooms and its poem in the regular channel (the one last chosen
under *Post the newest poem in...*). Test mode also shortens the waits to 30 seconds. With no Test channel, setup posts for real.

## Scheduling a launch
Under Puzzles, *Schedule a launch*: a puzzle id, a Pacific time (`2026-10-24 09:00` or `2026-10-24 9:00am`) and the channel for
the poem. At that time (within a minute) the puzzle goes live and its poem is posted and pinned there. The hunt must be on, Test
mode off and the objects set up; turning Test mode off leaves a scheduled puzzle's poem for its time. An hour before, the person
who scheduled it gets a DM if something is missing, and another when it has run (or why it did not). Leave the time empty to
cancel. A puzzle flagged `wip` cannot be scheduled. `.secrethunt status` lists every trail room by name (and flags a missing one)
and any scheduled launch.

## Closed DMs
A member whose DMs are closed is told privately when a button needed to DM them (tea table buttons: the notice is in the private
reply, and the DM is sent again at their next press). A typed trigger or a poem reaction has no private reply, so it posts a
short notice that deletes itself after 15 seconds, and a Treacle Well start is undone so reacting again works.

## Who can play
Under Hunt, *Roles that cannot play* (the Lurker role) makes every secret ignore those members: no clues, no clicks, no answers.
A typed trigger word is still deleted from chat, and their progress waits for when they lose the role.

## Setup (once)
```
.cog install evac-cogs secrethunt
.load secrethunt
.secrethunt
```
`.secrethunt` opens the control panel (admins only; only the person who opened it can use it). Three tabs:

- **Hunt**: pick 2-5 hiding places (forum posts or channels; use *Paste hiding places* if a forum post is missing from the dropdown), the Hall of Secrets channel, the solve-announcement channel, *Test mode* (clue waits become 30 seconds; run it yourself, then turn it off), *Refresh Hall posts*, and the on/off switch.
- **Puzzles**: choose a puzzle, then *Set up objects* (posts and pins the hunt object in each hiding place, reopening archived posts), *Go live*, and *DM me answers* (spoilers). *Post the newest poem in...* posts and pins the public poem.
- **Roles**: the Hard / Expert reward roles (or *Create the missing tier roles*) and per-puzzle badge roles (Hard and up; Easy and Medium never have one).

Order: pool, Hall, set up objects, go live, post the poem, then turn the hunt on.
Icons for roles stay a command (they need an attachment): `.secrethunt roleicon <tier or puzzle id>` with a PNG/JPG under 256 KB (needs boost level 2+). Roles have no colour on purpose.

Solvers get the tier role (if set) and the puzzle's role (if set); only future solvers, not earlier ones.
Members can run `.secrets [member]` to see which secrets someone has found (solved ones only).

Bot permissions: Send Messages (and Send Messages in Threads), Embed Links, Manage Messages / Pin Messages (pin, delete the
trigger and pin notice, remove reactions), Read Message History, Manage Roles. Keep tier roles below the bot's top role.

## Commands
`.secrethunt` (the panel), `status`, `version`, `roleicon <tier or puzzle>`, `who <member>`, `preview [id]` (DMs you every variant's clues and answers: spoilers), `reset <member> [id]`.
Everything else (pool, Hall, announcements, setup, go live, poem, roles, enable/disable, test mode) is in the panel.

## Easy vs Medium and up
Players kept looking for something to react to, so every Easy poem works a playful "you have to type" hint into the verse
itself (Late Rabbit: "Keys, not shouting, will catch his eye."; Looking Glass: "use your fingers; I am hard of hearing").
Each Easy puzzle names that phrase in its `typing_hint` field and a test checks it is really in the poem and never names the
word. Medium and up leave the hint out on purpose. There is no footer or banner.

## Adding a weekly puzzle
Add a dict to `puzzles.py` (see `LATE_RABBIT`) and register it in `PUZZLES`. (`kind: "trial"` puzzles are generated by
`trial.py` instead of having an `answers` list; see `KNAVE_TRIAL`.) Easy puzzles need a `typing_hint` phrase that appears in the poem. A test checks that an acrostic poem
acrostic spells the trigger and that no answer appears in the public poem. Then open `.secrethunt`, Puzzles tab: *Set up objects*, then *Go live*. Earlier puzzles stay live: they never expire.

## Notes
- The trigger word is not a command: it is caught by a listener, the message is deleted at once, and nothing about
  it shows in `.help`. It also works as a plain DM to the bot.
- The Hall of Secrets starts with a "what is this?" post (tiers, how it works, where the current poem is pinned). *Go live* and *Refresh Hall posts* create it if missing, at the top; posting the poem keeps its poem-channel line up to date. Its text lives in `embeds.intro_embed`.
- The announcement channel gets a one-line, non-pinging "X has solved <puzzle> (Tier) · finder #N" for every solve. Off by default.
- The bot deletes the "pinned a message" notice so it cannot point at the object.
- A multi-word trigger can be up to 40 characters and 4 spaces; spaces and punctuation are ignored.
- Posting the poem pins it and unpins the previous poem in that channel. Turning the hunt off silences triggers, reactions and clue delivery; turning it on resumes.
- Two wrong hunt objects lock a member out of the riddle for 24 hours; then they get two fresh tries on the same riddle.
- *Go live* posts the puzzle's (empty) Hall entry at once, so the Hall reads in release order.
- The jumbled-word clue arrives at the next 9:00 Pacific, at least 4 hours after the object is found.
- Progress lives in Red's Config, so a restart loses nothing.
