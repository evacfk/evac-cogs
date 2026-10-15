"""Discord-facing rendering for secrethunt, kept apart from the engine."""
import discord


def poem_embed(puzzle: dict) -> discord.Embed:
    embed = discord.Embed(
        title=puzzle["poem_title"], description="*" + "\n".join(puzzle["poem"]) + "*", color=discord.Color.gold())
    return embed


def object_embed(puzzle: dict) -> discord.Embed:
    # Identical in every channel on purpose: a decoy must look exactly like the real object.
    return discord.Embed(title=puzzle["object_title"], description=puzzle["object_text"], color=discord.Color.dark_grey())


def status_embed(version: str, lines: dict) -> discord.Embed:
    embed = discord.Embed(title="Secrets of Wonderland", description="\n".join(f"**{k}:** {v}" for k, v in lines.items()),
                          color=discord.Color.gold())
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
        "Check yours any time with `.secrets`."
    )
    if poem_channel_ids:
        text += "\n\n**The current poem is pinned in " + ", ".join(f"<#{c}>" for c in poem_channel_ids) + ".**"
    return discord.Embed(title="\N{OLD KEY} Secrets of Wonderland", description=text, color=discord.Color.gold())
