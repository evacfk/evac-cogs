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
