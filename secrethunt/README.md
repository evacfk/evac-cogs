# secrethunt - Secrets of Wonderland

WoW-style secret hunts. Each puzzle starts from a public poem whose first letters spell a hidden word. Saying the
word starts a personal chain (every member gets their own variant). Solvers are listed on the **Hall of Secrets**.

Tiers: **Easy** (days, no role or badge, name on the Hall only), **Medium**, **Hard**, **Expert** (each with its own role
and icon). Only Easy exists so far.

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

Bot permissions: Send Messages (and Send Messages in Threads), Embed Links, Manage Messages / Pin Messages (pin, delete the
trigger and pin notice, remove reactions), Read Message History, Manage Roles. Keep tier roles below the bot's top role.

## Admin commands
`status`, `version`, `channels`, `setup [id] [force]`, `start <id>`, `poem [channel]`, `enable`, `disable`,
`testmode <on|off>`, `hall [channel]`, `hallrefresh`, `role <tier> [role]`, `roleicon <tier>`, 
`who <member>`, `preview [id]` (DMs you every variant's clues and answers: spoilers), `reset <member> [id]`.

## Adding a weekly puzzle
Add a dict to `puzzles.py` (see `LATE_RABBIT`) and register it in `PUZZLES`. A test checks that the poem's
acrostic spells the trigger and that no answer appears in the public poem. Then `.secrethunt setup <id>` and
`.secrethunt start <id>`. Earlier puzzles stay live: they never expire.

## Notes
- The trigger word is not a command: it is caught by a listener, the message is deleted at once, and nothing about
  it shows in `.help`. It also works as a plain DM to the bot.
- The bot deletes the "pinned a message" notice so it cannot point at the object.
- `poem` pins the poem it posts and unpins the previous poem in that channel. `disable` silences triggers, reactions and clue delivery; `enable` resumes.
- Two wrong hunt objects lock a member out of the riddle for 24 hours; then they get two fresh tries on the same riddle.
- `start` posts the puzzle's (empty) Hall entry at once, so the Hall reads in release order.
- The jumbled-word clue arrives at the next 9:00 Pacific, at least 12 hours after the object is found.
- Progress lives in Red's Config, so a restart loses nothing.
