"""Discord-facing rendering for secrethunt, kept apart from the engine."""
import discord


def poem_embed(puzzle: dict) -> discord.Embed:
    body = "*" + "\n".join(puzzle["poem"]) + "*"
    if puzzle.get("nudge"):  # a trial's poem hides its words in the verses; this line says what to do with them
        body += "\n\n**" + puzzle["nudge"] + "**"
    return discord.Embed(title=puzzle["poem_title"], description=body, color=discord.Color.gold())


def witness_embed(puzzle: dict, label: str = "") -> discord.Embed:
    """The pinned object of a trial puzzle: identical in every room, because every room holds a witness."""
    title = puzzle["object_title"] + (f" \N{EM DASH} {label}" if label else "")
    return discord.Embed(title=title, description=puzzle["object_text"], color=discord.Color.dark_grey())


def object_embed(puzzle: dict, label: str = "") -> discord.Embed:
    # Identical in every channel on purpose: a decoy must look exactly like the real object. `label` is only used by the
    # trail sandbox, where one channel holds every room's object and each must say which room it stands in for.
    title = puzzle["object_title"] + (f" \N{EM DASH} {label}" if label else "")
    return discord.Embed(title=title, description=puzzle["object_text"], color=discord.Color.dark_grey())


def status_embed(version: str, lines: dict) -> discord.Embed:
    text = "\n".join(f"**{k}:** {v}" for k, v in lines.items())
    if len(text) > 4000:  # an embed description holds 4096; never let a long status fail to send
        text = text[:3990].rsplit("\n", 1)[0] + "\n\N{HORIZONTAL ELLIPSIS}"
    embed = discord.Embed(title="Secrets of Wonderland", description=text, color=discord.Color.gold())
    embed.set_footer(text=f"SecretHunt v{version}")
    return embed


TIER_COLORS = {"easy": "green", "medium": "orange", "hard": "red", "expert": "gold"}


def hall_embed(tier: str, title: str, text: str, hidden: int, total: int) -> discord.Embed:
    color = getattr(discord.Color, TIER_COLORS.get(tier, "gold"))()
    embed = discord.Embed(title=f"{tier.title()} \N{MIDDLE DOT} {title}", description=text or "*Nobody yet.*", color=color)
    note = f"{total} solver{'' if total == 1 else 's'}"
    if hidden:
        note += f" \N{MIDDLE DOT} {hidden} more not shown"
    embed.set_footer(text=note)
    return embed


def intro_embed(poem_channel_ids) -> discord.Embed:
    """The "what is this?" post at the top of the Hall of Secrets."""
    text = (
        "There are secrets hidden around this server.\n\n"
        "**How it works:** a poem appears. If you figure out what it's hiding, the secret begins, and the Rabbit "
        "comes to find you. Everything happens in Discord, and your path is your own, so sharing answers with a "
        "friend won't help them.\n\n"
        "**Tiers:**\n"
        "\N{LARGE GREEN CIRCLE} **Easy:** a few days. A new one most weeks.\n"
        "\N{LARGE ORANGE CIRCLE} **Medium:** about a week.\n"
        "\N{LARGE RED CIRCLE} **Hard:** weeks.\n"
        "\N{LARGE PURPLE CIRCLE} **Expert:** months.\n\n"
        "Everyone who solves a secret is listed below. Hard and Expert solvers also earn a badge role. "
        "Some secrets let you ask for a hint once you have been stuck a while, and the list shows how many hints each "
        "solver used. Check yours any time with `.secrets`."
    )
    if poem_channel_ids:
        text += "\n\n**The current poem is pinned in " + ", ".join(f"<#{c}>" for c in poem_channel_ids) + ".**"
    return discord.Embed(title="\N{OLD KEY} Secrets of Wonderland", description=text, color=discord.Color.gold())
