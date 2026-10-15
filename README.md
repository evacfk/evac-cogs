# wonderland-cogs

Custom Red-DiscordBot cogs for the Wonderland server, running on **Hana**.

## Cogs

| Cog | Description |
|---|---|
| `duel` | Challenge another member to a best-of-3 wagered duel using wondercoin. |
| `gamblethreads` | Opens a per-user thread for gambling commands to keep the main bot channel clean. |
| `wonderland` | The `.wld` guidebook (command guide, server map, mod cheat sheet) plus IntroForm, IntroCleanup and MyStats in one package. |

## Installing on Hana

```
[p]repo add wonderland-cogs https://github.com/<your-username>/wonderland-cogs
[p]cog install wonderland-cogs duel
[p]cog install wonderland-cogs gamblethreads
[p]load duel gamblethreads
```

Replace `[p]` with your actual prefix (`.`).

## Updating a cog after pushing new code

```
[p]repo update wonderland-cogs
[p]cog update
[p]reload <cogname>
```

## Maintainer notes

- All currency work goes through `redbot.core.bank` — no separate ledgers.
- `gamblethreads` should be kept in sync with whatever is actually running on
  evacOVH (`/data/red_data/cogs/CogManager/cogs/`), since it has had live
  hand-tuning outside of chat-generated code.
