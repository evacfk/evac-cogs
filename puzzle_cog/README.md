# Puzzle cog

A Red-DiscordBot cog for an image-reveal puzzle game. Load it with a pool of
images and it runs itself: it slices each image into a grid, posts one piece
at a time on a timer, lets members race to claim pieces with a reaction, and
automatically starts a new puzzle from a random image in the pool once one is
completed.

## Install

Drop the `puzzle/` folder into wherever your other local Red cogs live (or
add this repo's folder as a local cogs path), then in Discord:

```
[p]load puzzle
```

(Requires Pillow, which Red already depends on, so nothing extra to
`pip install`.)

## Setup

1. `[p]puzzle setchannel #your-channel` — where pieces get posted.
2. `[p]puzzle addimage 4x4` with an image attached — slices and adds it to
   the pool (repeat for as many images as you want, ~10 is a good starting
   pool; grid defaults to `3x3` if you omit it).
3. `[p]puzzle setinterval 6` — hours between piece postings (optional,
   defaults to 6).
4. `[p]puzzle setrole @Puzzle Champ` — role given to whoever completes a
   puzzle (optional).
5. `[p]puzzle start` — starts the rotation. From here it runs itself:
   finishing a puzzle automatically kicks off the next random one from the
   pool.

## Commands

- `[p]puzzle addimage <grid>` (admin, attach image) — add an image to the pool.
- `[p]puzzle delimage <id>` (admin) — remove an image from the pool by ID.
- `[p]puzzle images` — list the pool with IDs and grid sizes.
- `[p]puzzle start` (admin) — start the rotation.
- `[p]puzzle stop` (admin) — stop the current round (pool is untouched).
- `[p]puzzle status` — show progress and per-player standings on the current puzzle.
- `[p]puzzle setchannel <#channel>` (admin)
- `[p]puzzle setinterval <hours>` (admin)
- `[p]puzzle setrole [role]` (admin) — omit role to clear it.
- `[p]puzzle setemoji <emoji>` (admin) — defaults to 🧩.
- `[p]puzzle settings` — show current configuration.

## How it behaves right now (v1, tuned for "just let it run")

- **Claiming**: first reaction with the claim emoji on a piece wins that
  piece, permanently. No undo.
- **Winning**: a single person has to claim *every* piece of the current
  image to win. If all pieces get claimed but end up split across multiple
  people, the round ends with no winner and the cog just moves on to the
  next puzzle.
- **Rotation**: images are picked at random from the pool without repeats
  until the whole pool has been used once, then the cycle resets.
- **Deleting**: you can't delete the image that's the currently active
  puzzle — stop the round first.
- **Grid size**: set per image when you add it, so different images in the
  pool can have different grid sizes.
- **Restart-safe**: piece timing is based on a stored timestamp, not an
  in-memory countdown, so restarting the bot doesn't reset or double up the
  posting schedule. Active round state (claims, pool, etc.) persists across
  restarts via Red's Config.

These are the defaults from where we left the design — all easy to change
later (e.g. switching claiming to a button, changing what counts as a win,
or letting completed images repeat sooner).
