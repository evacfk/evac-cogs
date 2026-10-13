# secrethunt - Secrets of Wonderland

WoW-style secret hunts. Each puzzle starts from a public poem whose first letters spell a hidden word. Saying the
word starts a personal chain (every member gets their own variant), and solving it earns the **Secret Finder** role.

## Setup (once)
```
.cog install evac-cogs secrethunt
.load secrethunt
.secrethunt channels #cuddle #chan2 #chan3 #chan4 #chan5
.secrethunt role                      # creates "Secret Finder" (gold, shown separately), or pass an existing role
.secrethunt roleicon                  # attach a PNG/JPG under 256 KB (needs boost level 2+)
.secrethunt hall #hall-of-finders     # optional announcements
.secrethunt setup                     # posts and pins the hunt object in each pool channel
.secrethunt start late_rabbit
.secrethunt testmode on               # clue waits become 30 seconds; run it yourself, then turn it off
.secrethunt enable
.secrethunt poem #cuddle              # when you are ready to go live
```

Bot permissions: Send Messages, Embed Links, Manage Messages (pin, delete the trigger, remove reactions), Read Message
History, Manage Roles. Keep the Secret Finder role below the bot's top role.

## Admin commands
`status`, `version`, `channels`, `setup [id] [force]`, `start <id>`, `poem [channel]`, `enable`, `disable`,
`testmode <on|off>`, `hall [channel]`, `role [role]`, `roleicon`, `rolecolor <hex>`, `who <member>`,
`preview [id]` (DMs you every variant's clues and answers: spoilers), `reset <member> [id]`.

## Adding a weekly puzzle
Add a dict to `puzzles.py` (see `LATE_RABBIT`) and register it in `PUZZLES`. A test checks that the poem's
acrostic spells the trigger and that no answer appears in the public poem. Then `.secrethunt setup <id>` and
`.secrethunt start <id>`. Earlier puzzles stay live: they never expire.

## Notes
- The trigger word is not a command: it is caught by a listener, the message is deleted at once, and nothing about
  it shows in `.help`. It also works as a plain DM to the bot.
- The jumbled-word clue arrives at the next 9:00 Pacific, at least 12 hours after the object is found.
- Progress lives in Red's Config, so a restart loses nothing.
