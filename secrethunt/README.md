# secrethunt - Secrets of Wonderland

WoW-style secret hunts. Each puzzle starts from a public poem that hides a word (spelled by its first letters, or the
answer to its riddle). Saying the word starts a personal chain (every member gets their own variant). Solvers are listed on
the **Hall of Secrets**.

Tiers: **Easy** (days, no role or badge, name on the Hall only), **Medium**, **Hard**, **Expert** (each with its own role
and icon). Easy so far: **The Late Rabbit** (`late_rabbit`: acrostic poem, clock reaction, unscramble) and **The Looking Glass**
(`looking_glass`: riddle poem whose answer is `.mirror`, a room named in words, a pinned glass with Drink / Eat / Knock / Look
buttons answered privately, an emoji rebus). A puzzle with `"kind": "button"` gets the buttons from `views.py`; the pool and
setup steps are the same for both. For The Looking Glass use the same panel buttons; `.secrethunt preview looking_glass`
DMs you every variant's pointer, right button, rebus and answer (spoilers).

## Setup (once)
```
.cog install evac-cogs secrethunt
.load secrethunt
.secrethunt
```
`.secrethunt` opens the control panel (admins only; only the person who opened it can use it). Three tabs:

- **Hunt**: pick 2-5 hiding places (forum posts or channels; use *Paste hiding places* if a forum post is missing from the dropdown), the Hall of Secrets channel, the solve-announcement channel, *Test mode* (clue waits become 30 seconds; run it yourself, then turn it off), *Refresh Hall posts*, and the on/off switch.
- **Puzzles**: choose a puzzle, then *Set up objects* (posts and pins the hunt object in each hiding place, reopening archived posts), *Go live*, and *DM me answers* (spoilers). *Post the newest poem in...* posts and pins the public poem.
- **Roles**: the Medium / Hard / Expert reward roles (or *Create the missing tier roles*) and per-puzzle badge roles (Medium and up; Easy never has one).

Order: pool, Hall, set up objects, go live, post the poem, then turn the hunt on.
Icons for roles stay a command (they need an attachment): `.secrethunt roleicon <tier or puzzle id>` with a PNG/JPG under 256 KB (needs boost level 2+). Roles have no colour on purpose.

Solvers get the tier role (if set) and the puzzle's role (if set); only future solvers, not earlier ones.
Members can run `.secrets [member]` to see which secrets someone has found (solved ones only).

Bot permissions: Send Messages (and Send Messages in Threads), Embed Links, Manage Messages / Pin Messages (pin, delete the
trigger and pin notice, remove reactions), Read Message History, Manage Roles. Keep tier roles below the bot's top role.

## Commands
`.secrethunt` (the panel), `status`, `version`, `roleicon <tier or puzzle>`, `who <member>`, `preview [id]` (DMs you every variant's clues and answers: spoilers), `reset <member> [id]`.
Everything else (pool, Hall, announcements, setup, go live, poem, roles, enable/disable, test mode) is in the panel.

## Adding a weekly puzzle
Add a dict to `puzzles.py` (see `LATE_RABBIT`) and register it in `PUZZLES`. A test checks that the poem's
acrostic spells the trigger and that no answer appears in the public poem. Then open `.secrethunt`, Puzzles tab: *Set up objects*, then *Go live*. Earlier puzzles stay live: they never expire.

## Notes
- The trigger word is not a command: it is caught by a listener, the message is deleted at once, and nothing about
  it shows in `.help`. It also works as a plain DM to the bot.
- The Hall of Secrets starts with a "what is this?" post (tiers, how it works, where the current poem is pinned). *Go live* and *Refresh Hall posts* create it if missing, at the top; posting the poem keeps its poem-channel line up to date. Its text lives in `embeds.intro_embed`.
- The announcement channel gets a one-line, non-pinging "X has solved <puzzle> (Tier) · finder #N" for every solve. Off by default.
- The bot deletes the "pinned a message" notice so it cannot point at the object.
- Posting the poem pins it and unpins the previous poem in that channel. Turning the hunt off silences triggers, reactions and clue delivery; turning it on resumes.
- Two wrong hunt objects lock a member out of the riddle for 24 hours; then they get two fresh tries on the same riddle.
- *Go live* posts the puzzle's (empty) Hall entry at once, so the Hall reads in release order.
- The jumbled-word clue arrives at the next 9:00 Pacific, at least 4 hours after the object is found.
- Progress lives in Red's Config, so a restart loses nothing.
