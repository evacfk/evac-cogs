"""DailyQuests: three small quests a day per active member, with XP, streaks, badges, weekly and streak prizes.

The rules live in quests.py (pure), the Discord side in quests_cog.py (QuestsMixin), the mods' panel in
quests_panel.py. This file is the cog itself: config, the one admin command and the lookups the mixin needs.
"""
from __future__ import annotations

import logging

from redbot.core import Config, commands

from . import quests_panel
from .constants import COG_VERSION, CONFIG_ID, CONFIG_NAME
from .quests_cog import DEFAULT_GUILD_QUESTS, QuestsMixin
from .settings_panel import open_panel

log = logging.getLogger("red.evac-cogs.dailyquests")


class DailyQuests(QuestsMixin, commands.Cog):
    """Daily quests with streaks, XP and prizes. Members open them from the Daily Quests button on the casino hub."""

    __version__ = COG_VERSION

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(None, identifier=CONFIG_ID, cog_name=CONFIG_NAME, force_registration=True)
        self.config.register_guild(quests=DEFAULT_GUILD_QUESTS)
        self.config.register_member(quests={})
        self._q_setup()

    async def red_delete_data_for_user(self, *, requester=None, user_id: int = 0):
        await self._q_delete_member(int(user_id))      # daily quest progress and streak

    # ------------------------------------------------------------------ helpers the quests need

    def _exists(self, name: str) -> bool:
        try:
            cmd = self.bot.get_command(name)
        except Exception:
            return False
        return cmd is not None and getattr(cmd, "enabled", True)

    async def _lurker_role_id(self, guild):
        """The Lurker cog's role id (the same lookup afterdark uses), or None when it can't be read."""
        cog = self.bot.get_cog("Lurker")
        if cog is None:
            return None
        try:
            return await cog.config.guild(guild).lurker_role_id()
        except Exception:
            return None

    # ------------------------------------------------------------------ admin

    @commands.group(name="dailyquests", invoke_without_command=True)
    @commands.guild_only()
    @commands.mod_or_permissions(manage_guild=True)
    async def dailyquests(self, ctx: commands.Context):
        """Mods: daily quests. On/off switch, rewards, quest pool, channels and usage counts."""
        await open_panel(ctx, self, quests_panel.build_specs(), title="Daily quests", intro=quests_panel.INTRO)

    @dailyquests.command(name="version")
    async def dailyquests_version(self, ctx: commands.Context):
        """Show the running version (use it to confirm a deploy)."""
        await ctx.send(f"DailyQuests v{self.__version__}")
