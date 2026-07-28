# wonderland-cogs

Custom Red-DiscordBot cogs for the Wonderland server, running on **Hana**.

## Cogs

| Cog | Description |
|---|---|
| `duel` | Challenge another member to a best-of-3 wagered duel using wondercoin. |
| `heist` | Cooperative bank-heist minigame — signup window, three-phase Safe/Risky engine, jail mechanic. |
| `gamblethreads` | Opens a per-user thread for gambling commands to keep the main bot channel clean. |

## Installing on Hana

```
[p]repo add wonderland-cogs https://github.com/<your-username>/wonderland-cogs
[p]cog install wonderland-cogs duel
[p]cog install wonderland-cogs heist
[p]cog install wonderland-cogs gamblethreads
[p]load duel heist gamblethreads
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
- `heist` and `gamblethreads` should be kept in sync with whatever is
  actually running on evacOVH (`/data/red_data/cogs/CogManager/cogs/`),
  since both have had live hand-tuning outside of chat-generated code.
