import discord
from redbot.core import commands


class RoleWipe(commands.Cog):
    """One-off utility: mass-strip all roles (except one target role and @everyone) from every member holding that role."""

    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="rolewipe")
    @commands.admin_or_permissions(manage_roles=True)
    @commands.guild_only()
    async def rolewipe(self, ctx: commands.Context, role: discord.Role, confirm: bool = False):
        """
        Strip ALL other roles from every member who has `role`.

        The target role itself and @everyone are never touched.

        Usage:
          [p]rolewipe @13-17          -> dry run, shows who/what would be removed
          [p]rolewipe @13-17 yes      -> actually performs the removal
        """
        members = list(role.members)
        if not members:
            return await ctx.send(f"No members currently have {role.mention}.")

        preview = []
        for m in members:
            others = [r for r in m.roles if r != ctx.guild.default_role and r != role]
            if others:
                preview.append((m, others))

        if not preview:
            return await ctx.send(f"All {len(members)} member(s) with {role.mention} already have no other roles.")

        if not confirm:
            lines = [f"**Dry run** — {len(preview)} of {len(members)} member(s) with {role.mention} have other roles to strip:\n"]
            for m, others in preview[:25]:
                lines.append(f"• {m} ({m.id}): {', '.join(r.name for r in others)}")
            if len(preview) > 25:
                lines.append(f"...and {len(preview) - 25} more")
            lines.append(f"\nRun `{ctx.prefix}rolewipe {role.id} yes` to actually remove these roles.")
            out = "\n".join(lines)
            if len(out) > 1900:
                out = out[:1900] + "\n...(truncated)"
            return await ctx.send(out)

        removed_count = 0
        failed = []
        async with ctx.typing():
            for m, others in preview:
                try:
                    await m.remove_roles(*others, reason=f"Mass role wipe by {ctx.author} via rolewipe cog")
                    removed_count += 1
                except discord.Forbidden:
                    failed.append(f"{m} (forbidden - check role hierarchy/perms)")
                except discord.HTTPException as e:
                    failed.append(f"{m} ({e})")

        msg = f"✅ Stripped extra roles from {removed_count}/{len(preview)} member(s) with {role.mention}."
        if failed:
            msg += f"\n⚠️ Failed for {len(failed)}: " + ", ".join(failed[:10])
            if len(failed) > 10:
                msg += f" ...and {len(failed) - 10} more"
        if len(msg) > 1900:
            msg = msg[:1900] + "\n...(truncated)"
        await ctx.send(msg)
