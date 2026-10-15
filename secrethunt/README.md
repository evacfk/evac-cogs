# secrethunt - Secrets of Wonderland

WoW-style secret hunts. Each puzzle starts from a public poem that hides a word (spelled by its first letters, or the
answer to its riddle). Saying the word starts a personal chain (every member gets their own variant). Solvers are listed on
the **Hall of Secrets**.

Tiers: **Easy** (days, no role or badge, name on the Hall only), **Medium**, **Hard**, **Expert** (each with its own role
and icon). Easy so far: **The Late Rabbit** (`late_rabbit`: acrostic poem, clock reaction, unscramble) and **The Looking Glass**
(`looking_glass`: riddle poem whose answer is `.mirror`, a room named in words, a pinned glass with Drink / Eat / Knock / Look
buttons answered privately, an emoji rebus). A puzzle with `"kind": "button"` gets the buttons from `views.py`; the pool and
setup steps are the same for both. For The Looking Glass use `setup looking_glass` and `start looking_glass`; `preview looking_glass`
DMs you every variant's pointer, right button, rebus and answer (spoilers).

## Setup (once)
```
.cog install evac-cogs secrethunt
.load secrethunt
.secrethunt channels #Gaming #Music #Memes #Movies #Artwork   # 2-5 forum posts (threads) or channels
.secrethunt hall #hall-of-secrets     # one post per puzzle, edited in place
.secrethunt setup                     # posts and pins the hunt object in each pool spot (reopens archived posts)
.secrethunt start late_rabbit
.secrethunt testmode on               # clue waits become 30 seconds; run it yourself, then turn it off
.secrethunt enable
.secrethunt poem #where               # when you are ready to go live
```
Roles for harder tiers (not needed for Easy):
```
.secrethunt role medium               # creates "Secret Finder"; hard = "Timekeeper"; expert = "Eye of Wonder"; or pass a role
.secrethunt roleicon medium           # attach a PNG/JPG under 256 KB (needs boost level 2+); roles have no colour on purpose
```

Badge roles per puzzle (Medium and up; Easy never):
```
.secrethunt puzzlerole <puzzle_id>    # creates a role named for the puzzle (not hoisted), or pass an existing one
.secrethunt roleicon <puzzle_id>      # attach an icon; works for tier roles too
```
Solvers get the tier role (if set) and the puzzle's role (if set); only future solvers, not earlier ones.
Members can run `.secrets [member]` to see which secrets someone has found (solved ones only).

Bot permissions: Send Messages (and Send Messages in Threads), Embed Links, Manage Messages / Pin Messages (pin, delete the
trigger and pin notice, remove reactions), Read Message History, Manage Roles. Keep tier roles below the bot's top role.

## Admin commands
`status`, `version`, `channels`, `setup [id] [force]`, `start <id>`, `poem [channel]`, `enable`, `disable`,
`testmode <on|off>`, `announce [channel]`, `hall [channel]`, `hallrefresh`, `role <tier> [role]`, `puzzlerole <puzzle> [role]`, `roleicon <tier or puzzle>`, 
`who <member>`, `preview [id]` (DMs you every variant's clues and answers: spoilers), `reset <member> [id]`.

## Adding a weekly puzzle
Add a dict to `puzzles.py` (see `LATE_RABBIT`) and register it in `PUZZLES`. A test checks that the poem's
acrostic spells the trigger and that no answer appears in the public poem. Then `.secrethunt setup <id>` and
`.secrethunt start <id>`. Earlier puzzles stay live: they never expire.

## Notes
- The trigger word is not a command: it is caught by a listener, the message is deleted at once, and nothing about
  it shows in `.help`. It also works as a plain DM to the bot.
- The Hall of Secrets starts with a "what is this?" post (tiers, how it works, where the current poem is pinned). `start` and `hallrefresh` create it if missing, at the top; the `poem` command keeps its poem-channel line up to date. Its text lives in `embeds.intro_embed`.
- `announce #channel` posts a one-line, non-pinging "X has solved <puzzle> (Tier) · finder #N" there for every solve. Off by default.
- The bot deletes the "pinned a message" notice so it cannot point at the object.
- `poem` pins the poem it posts and unpins the previous poem in that channel. `disable` silences triggers, reactions and clue delivery; `enable` resumes.
- Two wrong hunt objects lock a member out of the riddle for 24 hours; then they get two fresh tries on the same riddle.
- `start` posts the puzzle's (empty) Hall entry at once, so the Hall reads in release order.
- The jumbled-word clue arrives at the next 9:00 Pacific, at least 4 hours after the object is found.
- Progress lives in Red's Config, so a restart loses nothing.
