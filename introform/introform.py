import asyncio
import logging
import time
from collections import defaultdict

import discord
from redbot.core import commands, Config
from redbot.core.bot import Red

from . import embeds, engine
from .constants import (
    BTN_DELETE_ID,
    BTN_OPEN_ID,
    GUILD_DEFAULTS,
    NOTICE_COOLDOWN,
    NOTICE_DELETE_AFTER,
    QUESTIONS,
)

log = logging.getLogger("red.evaccogs.introform")

_DISCORD_ERRORS = (discord.NotFound, discord.Forbidden, discord.HTTPException)


class IntroModal(discord.ui.Modal):
    """The pre-filled-question form. Prefilled with the member's old answers when editing."""

    def __init__(self, cog, existing_answers: dict):
        super().__init__(title="Your Intro", timeout=900)
        self.cog = cog
        self.inputs = {}
        for q in QUESTIONS:
            style = discord.TextStyle.paragraph if q["style"] == "paragraph" else discord.TextStyle.short
            item = discord.ui.TextInput(
                label=q["label"],
                style=style,
                placeholder=q["placeholder"],
                default=engine.prefill_value(existing_answers, q["key"], q["max_length"]),
                required=q["required"],
                max_length=q["max_length"],
            )
            self.inputs[q["key"]] = item
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        raw = {key: item.value for key, item in self.inputs.items()}
        await self.cog.handle_submit(interaction, raw)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        log.exception("IntroForm: modal error", exc_info=error)
        msg = "❌ Something went wrong saving your intro. Please try again."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except _DISCORD_ERRORS:
            pass


class IntroPanelView(discord.ui.View):
    """Persistent button panel. Buttons are matched by custom_id, so any old panel keeps working."""

    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Create / Edit My Intro", emoji="📝", style=discord.ButtonStyle.primary, custom_id=BTN_OPEN_ID)
    async def open_button(self, interaction: discord.Interaction, button):
        if interaction.guild is None:
            return
        entry = (await self.cog.config.guild(interaction.guild).intros()).get(str(interaction.user.id))
        answers = entry["answers"] if entry else {}
        await interaction.response.send_modal(IntroModal(self.cog, answers))

    @discord.ui.button(label="Delete My Intro", emoji="🗑️", style=discord.ButtonStyle.secondary, custom_id=BTN_DELETE_ID)
    async def delete_button(self, interaction: discord.Interaction, button):
        if interaction.guild is None:
            return
        removed = await self.cog.remove_intro(interaction.guild, interaction.user.id)
        text = "🗑️ Your intro was deleted." if removed else "You don't have an intro posted through the form."
        await interaction.response.send_message(text, ephemeral=True)


class IntroForm(commands.Cog):
    """Turns the intros channel into a fill-in form with a consistent look."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x696E74726F666F726D, force_registration=True)
        self.config.register_guild(**GUILD_DEFAULTS)
        self._user_locks = defaultdict(asyncio.Lock)    # (guild_id, user_id) -> Lock
        self._panel_locks = defaultdict(asyncio.Lock)   # guild_id -> Lock
        self._notice_at = {}                            # (guild_id, user_id) -> monotonic time

    async def cog_load(self):
        # Re-attach the persistent panel buttons after a restart/reload.
        self.bot.add_view(IntroPanelView(self))

    async def cog_unload(self):
        pass

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    async def _get_channel(self, guild: discord.Guild):
        channel_id = await self.config.guild(guild).channel_id()
        return guild.get_channel(channel_id) if channel_id else None

    async def handle_submit(self, interaction: discord.Interaction, raw: dict):
        """Validate the modal answers, then post (or edit) the member's intro embed."""
        guild = interaction.guild
        member = interaction.user
        await interaction.response.defer(ephemeral=True)

        answers = engine.normalize_answers(raw)
        problems = engine.validate_answers(answers)
        if problems:
            return await interaction.followup.send("❌ " + "\n".join(problems), ephemeral=True)

        is_new = True
        try:
            async with self._user_locks[(guild.id, member.id)]:
                channel = await self._get_channel(guild)
                if channel is None:
                    return await interaction.followup.send(
                        "❌ The intros channel isn't set up. Ask a mod to run `introform setchannel`.",
                        ephemeral=True,
                    )

                embed = embeds.build_intro_embed(member, answers)
                entry = (await self.config.guild(guild).intros()).get(str(member.id))

                msg = None
                if entry:
                    try:
                        msg = await channel.fetch_message(entry["message_id"])
                        await msg.edit(embed=embed)
                        is_new = False
                    except discord.NotFound:
                        msg = None  # old post was deleted; fall through and post a fresh one
                if msg is None:
                    msg = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                    is_new = True

                async with self.config.guild(guild).intros() as intros:
                    intros[str(member.id)] = {"message_id": msg.id, "answers": answers}
        except (discord.Forbidden, discord.HTTPException):
            log.exception("IntroForm: failed posting intro for %s in %s", member.id, guild.id)
            return await interaction.followup.send(
                "❌ I couldn't post your intro (missing permissions?). Ask a mod to check my access to the intros channel.",
                ephemeral=True,
            )

        if is_new:
            await self.refresh_panel(guild)
        verb = "posted" if is_new else "updated"
        await interaction.followup.send(f"✅ Your intro was {verb}: {msg.jump_url}", ephemeral=True)

    async def remove_intro(self, guild: discord.Guild, user_id: int) -> bool:
        """Delete a member's form-posted intro and forget it. Returns True if one existed."""
        async with self._user_locks[(guild.id, user_id)]:
            async with self.config.guild(guild).intros() as intros:
                entry = intros.pop(str(user_id), None)
            if not entry:
                return False
            channel = await self._get_channel(guild)
            if channel is not None:
                try:
                    await channel.get_partial_message(entry["message_id"]).delete()
                except _DISCORD_ERRORS:
                    pass
            return True

    async def refresh_panel(self, guild: discord.Guild):
        """Re-post the button panel at the bottom of the channel, removing the old one."""
        async with self._panel_locks[guild.id]:
            conf = self.config.guild(guild)
            channel = await self._get_channel(guild)
            if channel is None:
                return None
            old_id = await conf.panel_message_id()
            if old_id:
                try:
                    await channel.get_partial_message(old_id).delete()
                except _DISCORD_ERRORS:
                    pass
            msg = await channel.send(
                embed=embeds.build_panel_embed(await conf.enforce()),
                view=IntroPanelView(self),
            )
            await conf.panel_message_id.set(msg.id)
            return msg

    # ------------------------------------------------------------------
    # Listeners
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        guild = message.guild
        if guild is None or message.author.bot or message.webhook_id:
            return
        try:
            conf = self.config.guild(guild)
            channel_id = await conf.channel_id()
            if not channel_id or message.channel.id != channel_id:
                return
            if not await conf.enforce():
                return
            if message.type not in (discord.MessageType.default, discord.MessageType.reply):
                return
            perms = message.author.guild_permissions
            if perms.manage_messages or perms.administrator:
                return

            await message.delete()

            key = (guild.id, message.author.id)
            now = time.monotonic()
            if now - self._notice_at.get(key, -NOTICE_COOLDOWN) >= NOTICE_COOLDOWN:
                self._notice_at[key] = now
                await message.channel.send(
                    f"{message.author.mention} intros use the form now. "
                    "Press **Create / Edit My Intro** on the panel below!",
                    delete_after=NOTICE_DELETE_AFTER,
                    allowed_mentions=discord.AllowedMentions(users=[message.author]),
                )
        except discord.NotFound:
            pass
        except Exception:
            log.exception("IntroForm: error handling message %s", message.id)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        try:
            await self.remove_intro(member.guild, member.id)
        except Exception:
            log.exception("IntroForm: error cleaning intro for %s (%s)", member, member.id)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    @commands.group(name="introform", invoke_without_command=True)
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def introform(self, ctx: commands.Context):
        """Manage the IntroForm cog."""
        await ctx.send_help(ctx.command)

    @introform.command(name="setchannel")
    async def setchannel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Set the intros channel and post the form panel there."""
        await self.config.guild(ctx.guild).channel_id.set(channel.id)
        await self.config.guild(ctx.guild).panel_message_id.set(None)
        try:
            msg = await self.refresh_panel(ctx.guild)
        except _DISCORD_ERRORS:
            return await ctx.send(
                f"⚠️ Channel set to {channel.mention}, but I couldn't post the panel there. "
                "Check that I can view, send messages, embed links and manage messages in it."
            )
        await ctx.send(f"✅ Intros channel set to {channel.mention}. Panel posted: {msg.jump_url}")

    @introform.command(name="panel")
    async def panel(self, ctx: commands.Context):
        """Re-post the form panel at the bottom of the intros channel."""
        try:
            msg = await self.refresh_panel(ctx.guild)
        except _DISCORD_ERRORS:
            return await ctx.send("❌ I couldn't post the panel. Check my permissions in the intros channel.")
        if msg is None:
            return await ctx.send("❌ No intros channel set. Use `introform setchannel #channel` first.")
        await ctx.send(f"✅ Panel posted: {msg.jump_url}")

    @introform.command(name="enforce")
    async def enforce(self, ctx: commands.Context, on_off: bool):
        """Turn deletion of free-form messages (from non-mods) in the intros channel on or off."""
        await self.config.guild(ctx.guild).enforce.set(on_off)
        await self.refresh_panel(ctx.guild)
        await ctx.send(f"✅ Free-form message removal is now **{'on' if on_off else 'off'}**.")

    @introform.command(name="settings")
    async def settings(self, ctx: commands.Context):
        """Show current IntroForm configuration."""
        conf = self.config.guild(ctx.guild)
        channel_id = await conf.channel_id()
        enforce = await conf.enforce()
        count = len(await conf.intros())
        embed = discord.Embed(title="IntroForm Settings", color=discord.Color(0xFF8FB1))
        embed.add_field(name="Intros Channel", value=f"<#{channel_id}>" if channel_id else "not set", inline=True)
        embed.add_field(name="Remove Free-form Messages", value="on" if enforce else "off", inline=True)
        embed.add_field(name="Form Intros Stored", value=str(count), inline=True)
        await ctx.send(embed=embed)
