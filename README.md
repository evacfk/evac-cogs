# evac-cogs

Custom Red-DiscordBot cogs for the Wonderland server, running on **Hana** (Red 3.5, prefix `.`).
22 cogs, each a folder with its own `info.json`. Install name = folder name.

Most cogs are set up through an interactive **settings panel**: dropdowns for channels and roles, buttons for on/off,
and pop-up forms for numbers and text. Open it with the command in the *Settings* column (needs Manage Server;
mods without it see a read-only summary or help). Changes save as you choose them. Day-to-day commands (leaderboards,
reports, previews, testing, member commands) stay typed.

## Cogs

### Community & access
| Cog | Ver | What it does | Settings panel | Version probe |
|---|---|---|---|---|
| `afterdark` | 1.8.1 | Gated Rabbit Hole access: rules panel with interest buttons, mod-approved invitations, random drops, inactivity cleanup, exclude list. | `.afterdark` | `.afterdark version` |
| `secrethunt` | 1.8.0 | WoW-style secret hunts: personalised puzzles in forum posts, Hall of Secrets, tier badge roles. Members use `.secrets`. | `.secrethunt` | `.secrethunt version` |
| `lurker` | 2.5.0 | Auto-hides inactive members behind a Lurker role; mod reports; year-club roles (`.yearclub`). | `.lurkerset` | `.lurkerversion` |
| `rejoinwatch` | 1.1.0 | Tracks voluntary leaves, warns rejoiners, flags repeat leavers to mods with a ban button. | `.rejoinwatch` | `.rejoinwatch settings` |
| `celebrations` | 1.1.0 | Birthdays (role, coin gift, posts) and join-anniversary shout-outs. Members use `.birthday`. | `.celebrations` | `.celebrations version` |
| `contest` | 1.0.0 | Wonderween 2026 costume contest: DM entries, mod approval, hidden-then-public channel, daily anonymous votes, prizes, auto-purge. | `.contest` | `.contest version` |
| `wonderevents` | 1.4.0 | Movie and game nights: RSVP buttons, scheduled events, votes, reminders, attendance. | `.event settings` | `.event version` |
| `verdict` | 1.4.0 | Daily crowd question: pick an answer, then guess what everyone else picks. | `.verdict settings` | `.verdict version` |
| `bumpreward` | 1.1.0 | Coin rewards, streaks, reminders and a daily board for Disboard bumps. | `.bumpreward settings` | `.bumpreward version` |
| `wonderland` | 1.4.1 | The `.wld` guidebook (command guide, server map, mod cheat sheet) plus IntroForm, IntroCleanup and MyStats. | `.introform`, `.introcleanup` | `.wld version` |

### Feeds & analytics
| Cog | Ver | What it does | Settings panel | Version probe |
|---|---|---|---|---|
| `redditfeed` | source-v9 | Subreddit-to-channel image feed (Arctic Shift), manual approval queue, RedGifs upload, discovery. `.rf` is an alias. | `.redditfeed` | `.redditfeed version` |
| `tgfeed` | tg-v4 | Telegram forum group photos/videos to Discord channels (media only, throttled). Needs `telethon`. | `.tgfeed` | `.tgfeed version` |
| `serverpulse` | 1.5.0 | Activity analytics: busy/quiet hours, reports, heatmap, live board, digests, approval-gated weekly recap. Needs `Pillow`. | `.pulse settings` | `.pulse version` |
| `photodrop` | 1.2.0 | Daily photo drop with streaks, strikes, PTO, weekly poll and owner ratings. `.pp` is an alias. | `.pp set` | `.pp version` |

### Games & economy
| Cog | Ver | What it does | Settings panel | Version probe |
|---|---|---|---|---|
| `gamble` | 1.1.0 | Per-user gambling threads plus the Wondercasino hub menu (`.gamblehub`). | `.gambleset` | `.gambleset version` |
| `blackjacktable` | 1.1.0 | Multiplayer blackjack table with a persistent shoe and card counting. | `.wonderjackset` | `.wonderjackset version` |
| `duel` | 1.1.0 | Best-of-3 wagered duels between members. | `.duelset` | `.duelset version` |
| `minigamehub` | 1.1.0 | Config-driven minigame spawner: pet, mathdrop, hunt, lootdrop, reacttowin, boss. `.mgh` is an alias. | `.minigamehub` | `.mgh version` |
| `puzzle` | 1.7.0 | Image-reveal puzzle game with an auto-rotating image pool. | `.puzzle` | `.puzzle version` |
| `cardcollect` | 2.0.2 | Ambient anime-character card collecting: pick the card you want from a drop, the draw picks winners. Wishlist, series sets, card requests. Needs `Pillow`. | `.card settings` | `.card settings` (version in the panel title) |
| `wonderpet` | 1.3.0 | One server pet raised together: daily care, coin treats, weekly top carers, evolution. Needs `Pillow`. | `.wonderpet settings` | `.wonderpet version` |

### Utility
| Cog | What it does | Main command |
|---|---|---|
| `rolewipe` | Mass role strip by target role (`confirm` required). | `.rolewipe <role> [confirm]` |

## Installing

```
.repo add evac-cogs https://github.com/evacfk/evac-cogs
.cog install evac-cogs <cog>
.load <cog>
```

Then open the cog's settings panel (table above) and work through its tabs.

## Updating a cog after a push

```
.repo update evac-cogs
.cog update true <cog>
<version probe from the table above>
```

`.cog update true <cog>` updates and reloads in one go. After reloading `gamble`, run `.gamblehub setup` once (or press *Post / refresh hub menu* in `.gambleset`) to re-render the hub menu.

## How the settings panels work

- Every panel is built from one shared file, `_panel/settings_panel.py`, copied identically into each cog (and once into `wonderland/`) with `python3 _panel/sync.py [cog ...]`. Edit the master, then sync. `test_panel_sync.py` fails if a copy drifts.
- Each cog has a small `panel.py` that declares its settings (channel, role, toggle, choice, form, button, or list of records). Saving goes through the cog's own `_apply_*` method, which validates the change and returns a message or raises a user-visible error.
- Config keys did not change, so existing settings carry over with no migration.
- Only the member who opened a panel can use it, and only while they still have Manage Server (or Administrator).

## Tests

Each cog has its own pytest suite. Run them per cog, not repo-wide: the cogs expect different environments (some use the root `conftest.py` stub when `discord`/`redbot` are not installed, `blackjacktable`, `puzzle`, `duel` and `gamble` want a real Red install, `bumpreward` uses its own stub).

```
pytest <cog>/ -q
```

Panel widget tests (`test_<cog>_panel.py`) need real discord.py and skip themselves otherwise. They drive the real components with fake interactions; a live click-through in Discord is still the final check after a deploy.

## Maintainer notes

- All currency work goes through `redbot.core.bank`; there are no separate ledgers.
- `info.json` must sit directly inside each cog folder.
- Cog READMEs and this table can go stale; confirm against the live `info.json` and each cog's version constant.
- Deployed cog path on the bot: `/data/red_data/cogs/CogManager/cogs/`.
