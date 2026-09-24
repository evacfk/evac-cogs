# Puzzle cog

A Red-DiscordBot cog for an image-reveal puzzle game. Load it with a pool of
images and it runs itself: it slices each image into pieces, posts random
pieces to a channel on a timer, lets members race to claim them with a
reaction and build up their own collection, and automatically starts a new
puzzle from a random image in the pool once enough people have completed the
current one.

## Install

Install it through Red's Downloader from this repo, or drop the `puzzle/`
folder into wherever your other local Red cogs live, then in Discord:

```
[p]load puzzle
```

(Requires Pillow, which Red already depends on, so nothing extra to
`pip install`.)

## Setup

1. `[p]puzzle setchannel #your-channel` — where pieces get posted.
2. `[p]puzzle addimage 4x4` with one or more images attached — slices each
   and adds it to the pool (repeat for as many images as you want, ~10 is a
   good starting pool). See *Sizing* below for the size options.
3. `[p]puzzle setinterval 4 8` — random range (in hours) between piece
   postings, re-rolled after every post so the schedule can't be camped
   (optional, defaults to 4-8).
4. `[p]puzzle setwinners 3` — how many people need to complete the full set
   before the round ends and moves on (optional, defaults to 1).
5. `[p]puzzle setrole @Puzzle Champ` — rotating "current champion" role: given
   to each puzzle's winner(s) and automatically taken away from whoever held
   it before, so it always reflects the most recent winner(s) (optional).
6. `[p]puzzle start` — starts the rotation. From here it runs itself:
   finishing a puzzle automatically kicks off the next random one from the
   pool.

### Sizing

`[p]puzzle addimage [size]` takes either:

- a **piece count**, e.g. `7` — 2 to 25 pieces, auto-arranged into rows, or
- an explicit **grid**, e.g. `4x4` — each side 2 to 10, an exact rectangle.

Leave it out and the server default is used: whatever `[p]puzzle setpieces`
set, or 9 pieces if that was never set. All images attached to one
`addimage` command share the same size, and different images in the pool can
have different sizes. Byte-for-byte duplicates of an image already in the
pool are skipped automatically.

## Commands

Everything is under `[p]puzzle`. "Admin" means admin or Manage Server.

**Playing**

- `[p]puzzle status` — progress and per-player standings on the current puzzle.
- `[p]puzzle mypieces [member]` (aliases: `mine`, `collection`) — how many
  distinct pieces you (or someone else) have collected, with an image preview
  showing your claimed pieces in place and the rest blanked out.
- `[p]puzzle leaderboard` (alias: `lb`) — all-time top 10: most puzzles won,
  then most pieces collected, across every round ever played on the server.

**Managing the pool**

- `[p]puzzle addimage [size]` (admin, attach image(s)) — add images to the pool.
- `[p]puzzle delimage <id>` (admin) — remove an image from the pool by ID.
- `[p]puzzle images` — list the pool with IDs, piece counts and which one is
  active, plus a labeled thumbnail grid so you can tell images apart at a
  glance instead of relying on filenames.
- `[p]puzzle migratepool` (admin) — one-time cleanup for images added by an
  older version of the cog (converts the old grid format, backfills
  duplicate-detection hashes). Safe to run any time; it only touches entries
  that need it, and `[p]puzzle images` tells you when it's needed.

**Running a round**

- `[p]puzzle start` (admin) — start the rotation.
- `[p]puzzle stop` (admin) — stop and reset the current round (pool and
  settings are untouched).
- `[p]puzzle skip` (bot owner only) — immediately end the current puzzle with
  no winners declared and no full image posted, and move straight on to a
  new random image from the pool. A running test run keeps going against
  the new puzzle without needing to be restarted.
- `[p]puzzle testrun [seconds=10]` (admin) — fast-forward: posts a new piece
  every `seconds` seconds instead of waiting for the real interval, so you
  can watch the flow (pieces, claiming, win announcement) play out quickly.
  Uses the active puzzle if one's running, otherwise starts one from the
  pool. Runs indefinitely (auto-continuing through new puzzles) until you
  run `[p]puzzle teststop` — ignores `setinterval` the whole time.
- `[p]puzzle teststop` (admin) — cancel an in-progress test run.

**Settings** (all admin; `[p]puzzle settings` shows the current values)

- `[p]puzzle setchannel <#channel>` — where pieces are posted and claimed.
- `[p]puzzle setinterval <min_hours> <max_hours>` — random posting range.
- `[p]puzzle setwinners <count>` — how many completions end a round.
- `[p]puzzle setrole [role]` — rotating champion role; omit role to clear it.
- `[p]puzzle setemoji <emoji>` — the claim reaction, defaults to 🧩.
- `[p]puzzle setpieces <count>` — default piece count (2-25) for images added
  without an explicit size. Only affects images added afterwards.
- `[p]puzzle setsharedmode <on|off>` — shared-credit mode, see below.
- `[p]puzzle setsharedwindow <minutes>` — how long a piece stays open in
  shared mode (greater than 0, up to 60; defaults to 1).
- `[p]puzzle setannouncechannel [#channel]` — also post every puzzle
  completion in this channel, for when the collection channel is busy enough
  to bury it. Omit the channel to clear it.
- `[p]puzzle livestatus enable [#channel]` — post a status message (in the
  given channel, or the current one) that updates itself with live standings
  every time a piece is claimed, so nobody has to keep running
  `[p]puzzle status`.
- `[p]puzzle livestatus disable` — stop refreshing it; the last message is left
  in place as-is.

## How it behaves right now

- **Posting**: every distinct piece position posts exactly once first, in a
  random order (a "first pass" with no repeats), guaranteeing everyone sees
  every position at least once early on. Only after that first pass is done
  does it start posting positions randomly with repeats. The round never
  runs out of pieces to post on its own; it keeps going until enough people
  finish.
- **Timing**: the wait before each piece is a fresh random value within
  `[p]puzzle setinterval`'s range, re-rolled after every single post — not a
  fixed countdown — so nobody can predict the next spawn and camp the
  channel for it. The background loop checks its timers every 5 minutes, so
  posts can land up to about that much after their exact due time.
- **Claiming (default)**: first reaction with the claim emoji on a given posted
  piece wins that copy of it, permanently. Everyone builds their own
  collection independently — claiming a piece doesn't take it away from
  anyone else's future chances, since the same position can be posted again.
- **Claiming (shared mode)**: with `[p]puzzle setsharedmode on`, everyone who
  reacts within the claim window gets credit for the piece, not just the
  first person, so a round can end with several simultaneous winners more
  easily. Only affects pieces posted after the switch; a piece already up
  keeps the mode it was posted under.
- **Winning**: a person completes the puzzle once their collection contains
  every distinct piece position at least once. `[p]puzzle setwinners`
  controls how many different people need to finish before the round ends —
  default is 1 (first to finish ends it immediately). Once that many people
  have finished, the full image posts, all winners are announced together
  (and copied to the announcement channel if one is set), the champion role
  rotates, and a new puzzle starts automatically.
- **Rotation**: images are picked at random from the pool without repeats
  until the whole pool has been used once, then the cycle resets. The image
  that just finished is never picked again immediately, as long as the pool
  has another option.
- **Deleting**: you can't delete the image that's the currently active
  puzzle — stop the round first.
- **Restart-safe**: piece timing is based on a stored timestamp, not an
  in-memory countdown, so restarting the bot doesn't reset or double up the
  posting schedule. Active round state (collections, pool, etc.) persists
  across restarts via Red's Config.
- **Full image**: reconstructed from the individual pieces automatically if
  it's ever missing, so this works even for images added before this cog
  started saving the assembled version alongside the pieces.
