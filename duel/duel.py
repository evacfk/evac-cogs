import asyncio
import random
from typing import Optional

import discord
from redbot.core import Config, bank, commands, checks
from redbot.core.utils.chat_formatting import humanize_number, bold


WEAPONS = [
    ("🍭", "Candy Cane Saber"),
    ("🎮", "Pixel Blaster"),
    ("🫧", "Bubble Bomb"),
    ("⭐", "Star Beam"),
    ("🍬", "Gumdrop Grenade"),
    ("🌈", "Rainbow Ray"),
    ("🍫", "Choco Cannon"),
    ("🎯", "Arcade Dart"),
]

CLASH_LINES = [
    "{a} and {b} circle each other, weapons raised...",
    "{a} and {b} charge in at full speed...",
    "{a} and {b} lock eyes across the arena...",
    "The crowd goes quiet as {a} and {b} size each other up...",
]

HIT_LINES = [
    "{winner}'s {weapon} lands clean! 💥",
    "{winner} strikes first with the {weapon}! 💥",
    "{winner}'s {weapon} finds its mark! 💥",
    "A direct hit — {winner}'s {weapon} connects! 💥",
]


class DuelView(discord.ui.View):
    """Accept/Decline confirmation for an incoming duel challenge."""

    def __init__(self, cog: "Duel", challenger: discord.Member, opponent: discord.Member, amount: int):
        super().__init__(timeout=60)
        self.cog = cog
        self.challenger = challenger
        self.opponent = opponent
        self.amount = amount
        self.responded = False
        self.accepted = False
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.opponent.id:
            await interaction.response.send_message(
                "This challenge isn't addressed to you.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self):
        if self.responded:
            return
        self.responded = True
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(
                    content=f"⌛ {self.opponent.mention} didn't respond in time. Duel cancelled.",
                    view=self,
                )
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.green)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.responded:
            return
        self.responded = True
        self.accepted = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content=f"⚔️ {self.opponent.mention} accepted! Duel starting...", view=self
        )
        self.stop()
        await self.cog.run_duel(interaction.channel, self.challenger, self.opponent, self.amount)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.red)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.responded:
            return
        self.responded = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content=f"🚫 {self.opponent.mention} declined the duel.", view=self
        )
        self.stop()


class Duel(commands.Cog):
    """Challenge another member to a best-of-3 wagered duel."""

    __version__ = "1.0.0"

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x57ADE04E1, force_registration=True)
        default_guild = {
            "min_bet": 0,
            "max_bet": 0,  # 0 = no max
            "cooldown": 0,  # seconds, 0 = disabled
        }
        default_member = {
            "wins": 0,
            "losses": 0,
            "coins_won": 0,
            "coins_lost": 0,
            "last_duel": 0,
        }
        self.config.register_guild(**default_guild)
        self.config.register_member(**default_member)
        # in-memory lock so a user can't be in two duels at once
        self._active: set = set()

    async def red_delete_data_for_user(self, **kwargs):
        return

    # ---------- helpers ----------

    def _in_duel(self, *user_ids: int) -> bool:
        return any(uid in self._active for uid in user_ids)

    def _lock(self, *user_ids: int):
        for uid in user_ids:
            self._active.add(uid)

    def _unlock(self, *user_ids: int):
        for uid in user_ids:
            self._active.discard(uid)

    async def _check_cooldown(self, guild: discord.Guild, member: discord.Member):
        cooldown = await self.config.guild(guild).cooldown()
        if not cooldown:
            return None
        last = await self.config.member(member).last_duel()
        now = discord.utils.utcnow().timestamp()
        remaining = cooldown - (now - last)
        if remaining > 0:
            return int(remaining)
        return None

    async def _set_cooldown(self, member: discord.Member):
        now = discord.utils.utcnow().timestamp()
        await self.config.member(member).last_duel.set(now)

    # ---------- commands ----------

    @commands.guild_only()
    @commands.command(name="duel")
    async def duel(self, ctx: commands.Context, opponent: discord.Member, amount: int):
        """Challenge another member to a best-of-3 duel for wondercoin."""
        challenger = ctx.author
        currency = await bank.get_currency_name(ctx.guild)

        if opponent.bot:
            return await ctx.send("You can't duel a bot.")
        if opponent.id == challenger.id:
            return await ctx.send("You can't duel yourself.")
        if amount <= 0:
            return await ctx.send("Wager must be a positive amount.")

        min_bet = await self.config.guild(ctx.guild).min_bet()
        max_bet = await self.config.guild(ctx.guild).max_bet()
        if amount < min_bet:
            return await ctx.send(f"Minimum wager is {humanize_number(min_bet)} {currency}.")
        if max_bet and amount > max_bet:
            return await ctx.send(f"Maximum wager is {humanize_number(max_bet)} {currency}.")

        if self._in_duel(challenger.id, opponent.id):
            return await ctx.send("One of you is already in an active duel.")

        remaining = await self._check_cooldown(ctx.guild, challenger)
        if remaining:
            return await ctx.send(f"You're on duel cooldown for another {remaining}s.")

        if not await bank.can_spend(challenger, amount):
            return await ctx.send(f"You don't have {humanize_number(amount)} {currency}.")
        if not await bank.can_spend(opponent, amount):
            return await ctx.send(
                f"{opponent.display_name} doesn't have enough {currency} to cover that wager."
            )

        self._lock(challenger.id, opponent.id)
        view = DuelView(self, challenger, opponent, amount)
        msg = await ctx.send(
            content=(
                f"🎲 {challenger.mention} challenges {opponent.mention} to a duel "
                f"for **{humanize_number(amount)} {currency}**!\n"
                f"{opponent.mention}, do you accept?"
            ),
            view=view,
        )
        view.message = msg
        await view.wait()
        # On accept, the button callback hands off to run_duel (still running in the
        # background when view.wait() returns), which owns unlocking when it finishes.
        # On decline or timeout, nothing else will run this duel, so release now.
        if not view.accepted:
            self._unlock(challenger.id, opponent.id)

    async def run_duel(
        self, channel: discord.abc.Messageable, challenger: discord.Member, opponent: discord.Member, amount: int
    ):
        guild = channel.guild
        currency = await bank.get_currency_name(guild)

        # re-check funds right before escrow (balances may have changed since challenge)
        if not (await bank.can_spend(challenger, amount) and await bank.can_spend(opponent, amount)):
            self._unlock(challenger.id, opponent.id)
            return await channel.send("One of you no longer has enough to cover the wager. Duel cancelled.")

        try:
            await bank.withdraw_credits(challenger, amount)
            await bank.withdraw_credits(opponent, amount)
        except ValueError:
            self._unlock(challenger.id, opponent.id)
            return await channel.send("Wager withdrawal failed. Duel cancelled.")

        try:
            weapon_a, weapon_b = random.sample(WEAPONS, 2)
            weapons = {challenger.id: weapon_a, opponent.id: weapon_b}
            hearts = {challenger.id: 2, opponent.id: 2}
            wins = {challenger.id: 0, opponent.id: 0}

            def weapons_line():
                return (
                    f"{challenger.mention} {weapon_a[0]} **{weapon_a[1]}**\n"
                    f"{opponent.mention} {weapon_b[0]} **{weapon_b[1]}**"
                )

            def hearts_field():
                return (
                    f"{challenger.display_name}: {'❤️' * hearts[challenger.id]}"
                    f"{'🖤' * (2 - hearts[challenger.id])}\n"
                    f"{opponent.display_name}: {'❤️' * hearts[opponent.id]}"
                    f"{'🖤' * (2 - hearts[opponent.id])}"
                )

            def base_embed(title, color, description):
                e = discord.Embed(title=title, description=description, color=color)
                e.add_field(name="Wager", value=f"{humanize_number(amount)} {currency} each", inline=False)
                e.add_field(name="Hearts", value=hearts_field(), inline=False)
                return e

            embed = base_embed(
                "⚔️ Duel!",
                discord.Color.blurple(),
                weapons_line() + "\n\nBest of 3 — first to land 2 hits takes the pot.",
            )
            msg = await channel.send(embed=embed)
            await asyncio.sleep(1.2)

            round_num = 0
            while max(wins.values()) < 2:
                round_num += 1

                # phase 1: the clash (tension beat, no result yet)
                clash = random.choice(CLASH_LINES).format(a=challenger.mention, b=opponent.mention)
                embed = base_embed(
                    f"⚔️ Round {round_num}",
                    discord.Color.orange(),
                    weapons_line() + f"\n\n{clash} 🎲",
                )
                await msg.edit(embed=embed)
                await asyncio.sleep(1.4)

                # phase 2: the hit (result beat)
                winner_id = random.choice([challenger.id, opponent.id])
                loser_id = opponent.id if winner_id == challenger.id else challenger.id
                wins[winner_id] += 1
                hearts[loser_id] -= 1
                winner_member = challenger if winner_id == challenger.id else opponent
                hit = random.choice(HIT_LINES).format(
                    winner=winner_member.mention, weapon=weapons[winner_id][1]
                )
                color = discord.Color.green() if winner_id == challenger.id else discord.Color.blue()
                embed = base_embed(f"⚔️ Round {round_num}", color, weapons_line() + f"\n\n{hit}")
                await msg.edit(embed=embed)
                await asyncio.sleep(1.6)

            winner = challenger if wins[challenger.id] == 2 else opponent
            loser = opponent if winner.id == challenger.id else challenger
            payout = amount * 2

            await bank.deposit_credits(winner, payout)

            async with self.config.member(winner).all() as data:
                data["wins"] += 1
                data["coins_won"] += amount
            async with self.config.member(loser).all() as data:
                data["losses"] += 1
                data["coins_lost"] += amount

            await self._set_cooldown(challenger)
            await self._set_cooldown(opponent)

            embed = base_embed(
                "🏆 Duel over!",
                discord.Color.gold(),
                weapons_line() + f"\n\n{winner.mention} takes the win!",
            )
            embed.add_field(
                name="Payout",
                value=f"🏆 {winner.mention} wins **{humanize_number(payout)} {currency}**!",
                inline=False,
            )
            embed.set_thumbnail(url=winner.display_avatar.url)
            await msg.edit(embed=embed)
        finally:
            self._unlock(challenger.id, opponent.id)

    @commands.guild_only()
    @commands.command(name="duelstats")
    async def duelstats(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        """Show a member's duel record."""
        member = member or ctx.author
        currency = await bank.get_currency_name(ctx.guild)
        data = await self.config.member(member).all()
        total = data["wins"] + data["losses"]
        winrate = f"{(data['wins'] / total * 100):.0f}%" if total else "—"
        net = data["coins_won"] - data["coins_lost"]
        embed = discord.Embed(
            title=f"Duel record — {member.display_name}",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Wins", value=str(data["wins"]))
        embed.add_field(name="Losses", value=str(data["losses"]))
        embed.add_field(name="Win rate", value=winrate)
        embed.add_field(
            name=f"Net {currency}",
            value=f"{'+' if net >= 0 else ''}{humanize_number(net)}",
            inline=False,
        )
        await ctx.send(embed=embed)

    @commands.guild_only()
    @checks.admin_or_permissions(manage_guild=True)
    @commands.group(name="duelset")
    async def duelset(self, ctx: commands.Context):
        """Configure the duel cog."""

    @duelset.command(name="mincost")
    async def duelset_mincost(self, ctx: commands.Context, amount: int):
        """Set the minimum wager. 0 disables the minimum."""
        if amount < 0:
            return await ctx.send("Must be 0 or higher.")
        await self.config.guild(ctx.guild).min_bet.set(amount)
        await ctx.send(f"Minimum wager set to {humanize_number(amount)}.")

    @duelset.command(name="maxcost")
    async def duelset_maxcost(self, ctx: commands.Context, amount: int):
        """Set the maximum wager. 0 disables the cap."""
        if amount < 0:
            return await ctx.send("Must be 0 or higher.")
        await self.config.guild(ctx.guild).max_bet.set(amount)
        await ctx.send(f"Maximum wager set to {'no cap' if amount == 0 else humanize_number(amount)}.")

    @duelset.command(name="cooldown")
    async def duelset_cooldown(self, ctx: commands.Context, seconds: int):
        """Set the per-user cooldown between duels, in seconds. 0 disables it."""
        if seconds < 0:
            return await ctx.send("Must be 0 or higher.")
        await self.config.guild(ctx.guild).cooldown.set(seconds)
        await ctx.send(f"Duel cooldown set to {seconds}s.")

    @duelset.command(name="settings")
    async def duelset_settings(self, ctx: commands.Context):
        """Show current duel settings."""
        data = await self.config.guild(ctx.guild).all()
        embed = discord.Embed(title="Duel settings", color=discord.Color.blurple())
        embed.add_field(name="Min wager", value=humanize_number(data["min_bet"]))
        embed.add_field(
            name="Max wager", value="no cap" if not data["max_bet"] else humanize_number(data["max_bet"])
        )
        embed.add_field(name="Cooldown", value=f"{data['cooldown']}s")
        await ctx.send(embed=embed)
