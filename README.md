# evac-cogs

Custom Red-DiscordBot cogs for the Wonderland server, running on **Hana** (Red 3.5, prefix `.`).
21 cogs, each a folder with its own `info.json`. Install name = folder name.

## Cogs

### Community & access
| Cog | What it does | Main command | Version probe |
|---|---|---|---|
| `afterdark` | Gated Rabbit Hole access: rules panel with interest buttons, mod-approved invitations, random drops, inactivity cleanup, exclude list. | `.afterdark` | `.afterdark version` |
| `secrethunt` | WoW-style secret hunts: personalised puzzles in forum posts, Hall of Secrets, tier badge roles. | `.secrethunt`, `.secrets` | `.secrethunt version` |
| `lurker` | Auto-hides inactive members behind a Lurker role; year-club roles. | `.lurkerset`, `.yearclub`, `.lurker*` | `.lurkerversion` |
| `rejoinwatch` | Tracks voluntary leaves, warns rejoiners, flags repeat leavers to mods with a ban button. | `.rejoinwatch` | `.rejoinwatch settings` |
| `celebrations` | Birthdays (role, coin gift, posts) and join-anniversary shout-outs. | `.birthday`, `.celebrations` | `.celebrations version` |
| `wonderevents` | Movie and game nights: RSVP buttons, scheduled events, votes, reminders, attendance. | `.event` | `.event version` |
| `verdict` | Daily crowd question: pick an answer, then guess what everyone else picks. | `.verdict` | `.verdict version` |
| `bumpreward` | Coin rewards, streaks, reminders and a daily board for Disboard bumps. | `.bumpreward` | `.bumpreward settings` |
| `wonderland` | The `.wld` guidebook (command guide, server map, mod cheat sheet) plus IntroForm, IntroCleanup and MyStats. | `.wld`, `.mystats`, `.introform`, `.introcleanup` | `.wld version` |

### Feeds & analytics
| Cog | What it does | Main command | Version probe |
|---|---|---|---|
| `redditfeed` | Subreddit-to-channel image feed (Arctic Shift), manual approval queue, RedGifs upload, discovery. | `.redditfeed` / `.rf` | `.redditfeed version` |
| `tgfeed` | Telegram forum group photos/videos to Discord channels (media only, throttled). Needs `telethon`. | `.tgfeed` | `.tgfeed version` |
| `serverpulse` | Activity analytics: busy/quiet hours, reports, heatmap, live board, digests, approval-gated weekly recap. Needs `Pillow`. | `.pulse` | `.pulse version` |
| `photodrop` | Daily photo drop with streaks, strikes, PTO, weekly poll and owner ratings. | `.pockypolice` / `.pp` | `.pp version` |

### Games & economy
| Cog | What it does | Main command | Version probe |
|---|---|---|---|
| `gamble` | Per-user gambling threads plus the Wondercasino hub menu. | `.gamble`, `.endgambling`, `.gamblehub`, `.gambleset` | `.gambleset settings` |
| `blackjacktable` | Multiplayer blackjack table with a persistent shoe and card counting. | `.wonderjack`, `.wonderjackset` | `.wonderjackset settings` |
| `duel` | Best-of-3 wagered duels between members. | `.duel`, `.duelstats`, `.duelset` | `.duelset settings` |
| `minigamehub` | Config-driven minigame spawner: pet, mathdrop, hunt, lootdrop, reacttowin, boss. | `.minigamehub` / `.mgh` | `.mgh settings` |
| `puzzle` | Image-reveal puzzle game with an auto-rotating image pool. | `.puzzle` | `.puzzle version` |
| `cardcollect` | Ambient anime-character card collecting with a button-and-code claim mechanic. Needs `Pillow`. | `.card` | `.card version` |
| `wonderpet` | One server pet raised together: daily care, coin treats, weekly top carers, evolution. Needs `Pillow`. | `.wonderpet` | `.wonderpet version` |

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

## Updating a cog after a push

```
.repo update evac-cogs
.cog update true <cog>
<version probe from the table above>
```

`.cog update true <cog>` updates and reloads in one go. After reloading `gamble`, run `.gamblehub setup` once to re-render the hub menu.

## Tests

Each cog has its own pytest suite. Run them per cog, not repo-wide: the cogs expect different environments (some use the root `conftest.py` stub when `discord`/`redbot` are not installed, `blackjacktable` and `puzzle` want a real Red install, `bumpreward` uses its own stub).

```
pytest <cog>/ -q
```

## Maintainer notes

- All currency work goes through `redbot.core.bank`; there are no separate ledgers.
- `info.json` must sit directly inside each cog folder.
- Cog READMEs and this table can go stale; confirm against the live `info.json` and `COG_VERSION` in each cog.
- Deployed cog path on the bot: `/data/red_data/cogs/CogManager/cogs/`.
