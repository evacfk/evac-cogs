import asyncio
import random
import time

import discord
from redbot.core import Config, bank, commands
from redbot.core.utils.chat_formatting import humanize_number

# ---------------------------------------------------------------------------
# Default balance settings. These live in per-guild Config now (see
# DEFAULT_BALANCE below) and can be tuned live with `heistset`, no redeploy
# needed. Phase keys are strings ("1"/"2"/"3") because Config stores dict
# keys as JSON, which only supports string keys.
# ---------------------------------------------------------------------------

DEFAULT_BALANCE = {
    "phase_chances": {
        "1": {"safe": 0.90, "risky": 0.55},
        "2": {"safe": 0.85, "risky": 0.50},
        "3": {"safe": 0.90, "risky": 0.55},
    },
    "jail_base_hours": {"1": 4, "2": 8, "3": 12},
    "phase2_loot": {"safe": [1.3, 1.6], "risky": [2.0, 3.5]},
    "phase3_escape_bonus": [1.1, 1.3],
    "phase3_partial_fraction": [0.4, 0.6],
    "crew_bonus_per_extra": 0.05,
    "crew_bonus_cap": 1.5,
}

PHASE_INFO = {
    1: {
        "title": "Get In",
        "safe_label": "Sneak",
        "risky_label": "Force your way in",
        "description": (
            "How are you getting into the vault?\n"
            "*(Forcing your way in and winning guarantees a big Phase 2 score, "
            "plus a cut of whatever the rest of the crew loses along the way.)*"
        ),
    },
    2: {
        "title": "Grab the Cash",
        "safe_label": "Grab-and-go",
        "risky_label": "Go for the big score",
        "description": "You're inside. How much are you taking?",
    },
    3: {
        "title": "Escape",
        "safe_label": "Lay low",
        "risky_label": "Shoot your way out",
        "description": "Time to get out. How are you playing it?",
    },
}


class WagerModal(discord.ui.Modal, title="Join the Heist"):
    def __init__(self, cog: "Heist", guild: discord.Guild, user: discord.Member):
        super().__init__()
        self.cog = cog
        self.guild = guild
        self.user = user
        self.wager = discord.ui.TextInput(
            label="Wager (wondercoin)",
            placeholder="e.g. 500",
            required=True,
            max_length=12,
        )
        self.add_item(self.wager)

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.wager.value.strip().replace(",", "")
        if not raw.isdigit():
            await interaction.response.send_message(
                "Enter a whole number, no symbols.", ephemeral=True
            )
            return
        amount = int(raw)
        if amount <= 0:
            await interaction.response.send_message("Wager must be positive.", ephemeral=True)
            return

        session = self.cog.active_heists.get(self.guild.id)
        if session is None or session.phase != "signup":
            await interaction.response.send_message("Signup has closed.", ephemeral=True)
            return

        if self.user.id in session.participants:
            await interaction.response.send_message(
                "You've already joined this heist.", ephemeral=True
            )
            return

        jailed_until = await self.cog.config.member(self.user).jailed_until()
        now = time.time()
        if jailed_until and jailed_until > now:
            remaining = jailed_until - now
            await interaction.response.send_message(
                f"You're still in jail for {self.cog.fmt_duration(remaining)}. Sit tight.",
                ephemeral=True,
            )
            return

        if not await bank.can_spend(self.user, amount):
            await interaction.response.send_message(
                "You don't have that much wondercoin.", ephemeral=True
            )
            return

        await bank.withdraw_credits(self.user, amount)
        session.participants[self.user.id] = {
            "member": self.user,
            "wager": amount,
            "pot": amount,
            "status": "active",
            "payout": 0,
            "summary": "",
        }

        # Track this as money-in-play so a crash/restart mid-heist can refund
        # it instead of leaving it vanished from the economy.
        async with self.cog.config.guild(self.guild).pending_escrow() as escrow:
            escrow[str(self.user.id)] = amount

        await interaction.response.send_message(
            f"You're in for **{humanize_number(amount)}** wondercoin. Good luck out there.",
            ephemeral=True,
        )
        await session.refresh_embed()


class SignupView(discord.ui.View):
    def __init__(self, cog: "Heist", guild: discord.Guild):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild = guild

    @discord.ui.button(label="Join the Heist", style=discord.ButtonStyle.green, emoji="\U0001F3E6")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        session = self.cog.active_heists.get(self.guild.id)
        if session is None or session.phase != "signup":
            await interaction.response.send_message(
                "Signup isn't open right now.", ephemeral=True
            )
            return

        jailed_until = await self.cog.config.member(interaction.user).jailed_until()
        now = time.time()
        if jailed_until and jailed_until > now:
            remaining = jailed_until - now
            await interaction.response.send_message(
                f"You're in jail for another {self.cog.fmt_duration(remaining)}. Sit tight.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(WagerModal(self.cog, self.guild, interaction.user))


class PhaseChoiceView(discord.ui.View):
    def __init__(self, safe_label: str, risky_label: str, timeout: int):
        super().__init__(timeout=timeout)
        self.choice = None
        self.safe_button.label = safe_label
        self.risky_button.label = risky_label

    @discord.ui.button(style=discord.ButtonStyle.blurple)
    async def safe_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.choice = "safe"
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(style=discord.ButtonStyle.red)
    async def risky_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.choice = "risky"
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()


class HeistSession:
    """Tracks one heist from signup through resolution."""

    def __init__(
        self,
        cog: "Heist",
        guild: discord.Guild,
        channel: discord.abc.Messageable,
        conf: dict,
    ):
        self.cog = cog
        self.guild = guild
        self.channel = channel
        self.conf = conf
        self.min_players = conf["min_players"]
        self.phase_timeout = conf["phase_timeout"]
        self.afk_penalty_percent = conf["afk_penalty_percent"]
        self.phase = "signup"
        self.participants: dict[int, dict] = {}
        self.message: discord.Message | None = None
        self.view: SignupView | None = None

    # ---------- signup ----------

    async def start_signup(self, window_seconds: int):
        self.view = SignupView(self.cog, self.guild)
        embed = self._signup_embed(window_seconds)
        self.message = await self.channel.send(embed=embed, view=self.view)
        await asyncio.sleep(window_seconds)
        await self.close_signup()

    def _signup_embed(self, window_seconds: int) -> discord.Embed:
        minutes = max(1, window_seconds // 60)
        embed = discord.Embed(
            title="\U0001F3E6 A Heist Is Forming",
            description=(
                f"Click below to join and set your wager.\n"
                f"Signup closes in **{minutes} minute{'s' if minutes != 1 else ''}**.\n"
                f"Needs at least **{self.min_players}** crew members to go ahead."
            ),
            color=discord.Color.gold(),
        )
        embed.add_field(name="Crew So Far", value="_Nobody yet_", inline=False)
        return embed

    async def refresh_embed(self):
        if not self.message:
            return
        embed = self.message.embeds[0]
        if self.participants:
            lines = [
                f"\u2022 {data['member'].mention} \u2014 {humanize_number(data['wager'])} wondercoin"
                for data in self.participants.values()
            ]
            embed.set_field_at(
                0, name=f"Crew So Far ({len(self.participants)})", value="\n".join(lines), inline=False
            )
        else:
            embed.set_field_at(0, name="Crew So Far", value="_Nobody yet_", inline=False)
        await self.message.edit(embed=embed)

    async def _clear_escrow(self):
        await self.cog.config.guild(self.guild).pending_escrow.set({})

    async def close_signup(self):
        self.phase = "locked"
        if self.view:
            self.view.join.disabled = True
            if self.message:
                await self.message.edit(view=self.view)

        if len(self.participants) < self.min_players:
            for data in self.participants.values():
                await bank.deposit_credits(data["member"], data["wager"])
            await self._clear_escrow()
            await self.channel.send(
                f"Not enough crew showed up ({len(self.participants)}/{self.min_players}). "
                f"Heist's off \u2014 wagers refunded."
            )
            await self.cog.config.guild(self.guild).last_heist_end.set(time.time())
            self.cog.active_heists.pop(self.guild.id, None)
            return

        names = ", ".join(data["member"].mention for data in self.participants.values())
        crew_size = len(self.participants)
        await self.channel.send(
            f"Signup's closed with **{crew_size}** crew members: {names}\n"
            f"Check your DMs \u2014 Phase 1 starts now."
        )
        self.phase = "running"

        choices = await self.get_choices(1)
        await self.resolve_phase1(choices)
        await self.post_phase_update(1)
        if not self._any_active():
            await self.finalize()
            return

        forced_ids = {
            uid for uid, d in self.participants.items() if d["status"] == "active" and d.get("forced_in")
        }
        if forced_ids:
            self.resolve_phase2_forced(forced_ids, crew_size)
        choices = await self.get_choices(2, skip_ids=forced_ids)
        await self.resolve_phase2(choices, crew_size)
        await self.post_phase_update(2)
        if not self._any_active():
            await self.finalize()
            return

        choices = await self.get_choices(3)
        await self.resolve_phase3(choices)
        await self.finalize()

    # ---------- phase engine ----------

    def _any_active(self) -> bool:
        return any(d["status"] == "active" for d in self.participants.values())

    async def get_choices(self, phase_num: int, skip_ids: set[int] | None = None) -> dict[int, str | None]:
        info = PHASE_INFO[phase_num]
        skip_ids = skip_ids or set()
        active_ids = [
            uid for uid, d in self.participants.items() if d["status"] == "active" and uid not in skip_ids
        ]
        views: dict[int, PhaseChoiceView | None] = {}

        for uid in active_ids:
            member = self.participants[uid]["member"]
            view = PhaseChoiceView(info["safe_label"], info["risky_label"], self.phase_timeout)
            embed = discord.Embed(
                title=f"Phase {phase_num}: {info['title']}",
                description=(
                    f"{info['description']}\n\n"
                    f"You have {self.phase_timeout} seconds to choose. "
                    f"No response = you slip out safely, losing {self.afk_penalty_percent}% of your wager."
                ),
                color=discord.Color.orange(),
            )
            try:
                dm = await member.create_dm()
                await dm.send(embed=embed, view=view)
            except (discord.Forbidden, discord.HTTPException):
                view = None
            views[uid] = view

        waits = [v.wait() for v in views.values() if v is not None]
        if waits:
            await asyncio.gather(*waits)

        return {uid: (v.choice if v else None) for uid, v in views.items()}

    def _roll(self, phase_num: int, choice: str):
        chance = float(self.conf["phase_chances"][str(phase_num)][choice])
        roll = random.random()
        success = roll <= chance
        margin = 0.0
        if not success and chance < 1:
            margin = (roll - chance) / (1 - chance)
        return success, margin

    async def _jail(self, member: discord.Member, phase_num: int, margin: float) -> float:
        base_hours = self.conf["jail_base_hours"][str(phase_num)]
        multiplier = 0.5 + margin  # near-miss -> 0.5x, bad miss -> 1.5x
        hours = base_hours * multiplier
        until = time.time() + hours * 3600
        await self.cog.config.member(member).jailed_until.set(until)
        return hours

    def _afk_exit(self, data: dict):
        penalty = int(data["wager"] * self.afk_penalty_percent / 100)
        data["pot"] = max(0, data["pot"] - penalty)
        data["status"] = "exited"
        data["payout"] = data["pot"]
        data["summary"] = f"Didn't respond in time \u2014 slipped out safely (\u2212{humanize_number(penalty)} wondercoin)."

    async def resolve_phase1(self, choices: dict[int, str | None]):
        for uid, choice in choices.items():
            data = self.participants[uid]
            if choice is None:
                self._afk_exit(data)
                continue
            success, margin = self._roll(1, choice)
            if choice == "safe":
                if success:
                    data["summary"] = "Snuck in clean."
                else:
                    data["status"] = "exited"
                    data["pot"] = 0
                    data["payout"] = 0
                    data["summary"] = "Got spooked sneaking in \u2014 bailed, forfeited the wager."
            else:
                if success:
                    data["forced_in"] = True
                    data["summary"] = "Forced the door \u2014 in like a wrecking ball."
                else:
                    hours = await self._jail(data["member"], 1, margin)
                    data["status"] = "jailed"
                    data["pot"] = 0
                    data["payout"] = 0
                    data["summary"] = f"Forced the door and got caught. Jailed {self.cog.fmt_duration(hours * 3600)}."

    def resolve_phase2_forced(self, forced_ids: set[int], crew_size: int):
        """Force-winners from Phase 1 skip the Phase 2 gamble entirely and are
        auto-treated as a risky success, guaranteed."""
        per_extra = self.conf["crew_bonus_per_extra"]
        cap = self.conf["crew_bonus_cap"]
        crew_bonus = min(1 + per_extra * max(0, crew_size - self.min_players), cap)
        lo, hi = self.conf["phase2_loot"]["risky"]
        for uid in forced_ids:
            data = self.participants[uid]
            mult = random.uniform(lo, hi) * crew_bonus
            data["pot"] = data["wager"] * mult
            data["summary"] = "Forced the door earlier and it paid off \u2014 breezed through with a guaranteed big score."

    async def resolve_phase2(self, choices: dict[int, str | None], crew_size: int):
        per_extra = self.conf["crew_bonus_per_extra"]
        cap = self.conf["crew_bonus_cap"]
        crew_bonus = min(1 + per_extra * max(0, crew_size - self.min_players), cap)
        loot = self.conf["phase2_loot"]
        for uid, choice in choices.items():
            data = self.participants[uid]
            if data["status"] != "active":
                continue
            if choice is None:
                self._afk_exit(data)
                continue
            success, margin = self._roll(2, choice)
            if choice == "safe":
                if success:
                    lo, hi = loot["safe"]
                    mult = random.uniform(lo, hi) * crew_bonus
                    data["pot"] = data["wager"] * mult
                    data["summary"] = "Grabbed a modest haul and kept it quiet."
                else:
                    data["status"] = "exited"
                    data["pot"] = 0
                    data["payout"] = 0
                    data["summary"] = "Came up empty-handed \u2014 walked out safe, nothing to show for it."
            else:
                if success:
                    lo, hi = loot["risky"]
                    mult = random.uniform(lo, hi) * crew_bonus
                    data["pot"] = data["wager"] * mult
                    data["summary"] = "Went for the big score \u2014 and hit it big."
                else:
                    hours = await self._jail(data["member"], 2, margin)
                    data["status"] = "jailed"
                    data["pot"] = 0
                    data["payout"] = 0
                    data["summary"] = f"Went for the big score, got caught. Jailed {self.cog.fmt_duration(hours * 3600)}."

    async def resolve_phase3(self, choices: dict[int, str | None]):
        escape_lo, escape_hi = self.conf["phase3_escape_bonus"]
        partial_lo, partial_hi = self.conf["phase3_partial_fraction"]
        for uid, choice in choices.items():
            data = self.participants[uid]
            if data["status"] != "active":
                continue
            if choice is None:
                self._afk_exit(data)
                continue
            success, margin = self._roll(3, choice)
            if choice == "safe":
                if success:
                    data["status"] = "escaped"
                    data["payout"] = int(data["pot"])
                    data["summary"] = "Laid low and slipped away clean with the full haul."
                else:
                    fraction = random.uniform(partial_lo, partial_hi)
                    data["status"] = "escaped"
                    data["payout"] = int(data["pot"] * fraction)
                    data["summary"] = f"Got clipped on the way out \u2014 kept about {int(fraction * 100)}% of the haul."
            else:
                if success:
                    bonus = random.uniform(escape_lo, escape_hi)
                    data["status"] = "escaped"
                    data["payout"] = int(data["pot"] * bonus)
                    data["summary"] = "Shot the way out clean \u2014 full haul plus extra."
                else:
                    hours = await self._jail(data["member"], 3, margin)
                    data["status"] = "jailed"
                    data["payout"] = 0
                    data["summary"] = f"Tried to shoot the way out, got caught. Jailed {self.cog.fmt_duration(hours * 3600)}."

    async def post_phase_update(self, phase_num: int):
        still_active = sum(1 for d in self.participants.values() if d["status"] == "active")
        if still_active:
            await self.channel.send(
                f"Phase {phase_num} ({PHASE_INFO[phase_num]['title']}) done. "
                f"{still_active}/{len(self.participants)} still in it."
            )
        else:
            await self.channel.send(f"Phase {phase_num} ({PHASE_INFO[phase_num]['title']}) done. Nobody's left standing.")

    async def finalize(self):
        # Pool together what everyone else lost (forfeits, jail losses,
        # partial-cut shortfalls) and split it among the crew members who
        # forced their way in during Phase 1 and made it out alive.
        total_pool = sum(max(0, data["wager"] - data.get("payout", 0)) for data in self.participants.values())
        force_winners = [
            data
            for data in self.participants.values()
            if data.get("forced_in") and data["status"] == "escaped" and data.get("payout", 0) > 0
        ]
        if force_winners and total_pool > 0:
            total_wager = sum(d["wager"] for d in force_winners)
            for d in force_winners:
                share = int(total_pool * (d["wager"] / total_wager))
                d["payout"] += share
                d["pool_share"] = share

        lines = []
        for data in self.participants.values():
            payout = data.get("payout", 0)
            if payout > 0:
                await bank.deposit_credits(data["member"], payout)
            payout_text = f"**+{humanize_number(payout)}**" if payout else "**+0**"
            summary = data["summary"] or "Never got a choice in \u2014 heist ended early."
            pool_note = ""
            if data.get("pool_share"):
                pool_note = f" *(+{humanize_number(data['pool_share'])} cut of the crew's losses)*"
            lines.append(f"\u2022 {data['member'].mention}: {summary}{pool_note} {payout_text}")

        embed = discord.Embed(
            title="\U0001F3C1 Heist Results",
            description="\n".join(lines) if lines else "Nobody made it in.",
            color=discord.Color.green(),
        )
        await self.channel.send(embed=embed)
        await self._clear_escrow()
        await self.cog.config.guild(self.guild).last_heist_end.set(time.time())
        self.cog.active_heists.pop(self.guild.id, None)


class Heist(commands.Cog):
    """A cooperative bank heist minigame with wagering and jail penalties."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0xBA5EBA11, force_registration=True)
        self.config.register_guild(
            signup_window=300,
            cooldown=3600,
            min_players=3,
            phase_timeout=60,
            afk_penalty_percent=10,
            last_heist_end=0,
            pending_escrow={},
            last_active_channel=0,
            **DEFAULT_BALANCE,
        )
        self.config.register_member(jailed_until=0)
        self.active_heists: dict[int, HeistSession] = {}
        self.tasks: dict[int, asyncio.Task] = {}

    async def cog_load(self):
        # Recover from a crash/restart that happened mid-heist: anything
        # still sitting in pending_escrow was withdrawn from a player's
        # balance but never resolved, so refund it and clear the marker.
        for guild in self.bot.guilds:
            escrow = await self.config.guild(guild).pending_escrow()
            if not escrow:
                continue
            channel_id = await self.config.guild(guild).last_active_channel()
            channel = guild.get_channel(channel_id) if channel_id else None
            refunded = []
            for uid_str, amount in escrow.items():
                member = guild.get_member(int(uid_str))
                if member:
                    await bank.deposit_credits(member, amount)
                    refunded.append(f"{member.mention} (+{humanize_number(amount)})")
            await self.config.guild(guild).pending_escrow.set({})
            if channel and refunded:
                try:
                    await channel.send(
                        "\u26A0\uFE0F A heist was interrupted by a bot restart. Refunded: "
                        + ", ".join(refunded)
                    )
                except discord.HTTPException:
                    pass

    def cog_unload(self):
        for task in self.tasks.values():
            task.cancel()

    @staticmethod
    def fmt_duration(seconds: float) -> str:
        seconds = max(0, int(seconds))
        hours, remainder = divmod(seconds, 3600)
        minutes, _ = divmod(remainder, 60)
        if hours and minutes:
            return f"{hours}h {minutes}m"
        if hours:
            return f"{hours}h"
        return f"{minutes}m"

    async def _jailed_lines(self, guild: discord.Guild) -> list[str]:
        now = time.time()
        all_members = await self.config.all_members(guild)
        jailed = []
        for member_id, data in all_members.items():
            until = data.get("jailed_until", 0)
            if until and until > now:
                member = guild.get_member(member_id)
                label = member.mention if member else f"<@{member_id}>"
                jailed.append((until, label))
        jailed.sort()
        return [f"\u2022 {label} \u2014 out in {self.fmt_duration(until - now)}" for until, label in jailed]

    # ---------- player commands ----------

    @commands.guild_only()
    @commands.group(invoke_without_command=True)
    async def heist(self, ctx: commands.Context):
        """Start a bank heist."""
        guild = ctx.guild

        if guild.id in self.active_heists:
            await ctx.send("A heist is already in progress here.")
            return

        cooldown = await self.config.guild(guild).cooldown()
        last_end = await self.config.guild(guild).last_heist_end()
        now = time.time()
        if last_end and now - last_end < cooldown:
            remaining = cooldown - (now - last_end)
            await ctx.send(f"The heat's still on. Next heist can start in {self.fmt_duration(remaining)}.")
            return

        jailed_lines = await self._jailed_lines(guild)
        if jailed_lines:
            embed = discord.Embed(
                title="\U0001F693 Currently in Jail",
                description="\n".join(jailed_lines),
                color=discord.Color.red(),
            )
            await ctx.send(embed=embed)

        conf = await self.config.guild(guild).all()
        await self.config.guild(guild).last_active_channel.set(ctx.channel.id)

        session = HeistSession(self, guild, ctx.channel, conf)
        self.active_heists[guild.id] = session
        await ctx.send("Starting a heist! \U0001F3E6")
        task = asyncio.create_task(session.start_signup(conf["signup_window"]))
        self.tasks[guild.id] = task

    @heist.command(name="status")
    async def heist_status(self, ctx: commands.Context):
        """Check the current heist's progress."""
        session = self.active_heists.get(ctx.guild.id)
        if not session:
            await ctx.send("No heist in progress right now.")
            return

        stage_names = {"signup": "Signup open", "locked": "Signup closed, starting", "running": "In progress"}
        if not session.participants:
            desc = f"**Stage:** {stage_names.get(session.phase, session.phase)}\nNo crew has joined yet."
        else:
            lines = [
                f"\u2022 {data['member'].display_name} \u2014 {data['status']}"
                for data in session.participants.values()
            ]
            desc = f"**Stage:** {stage_names.get(session.phase, session.phase)}\n\n" + "\n".join(lines)

        embed = discord.Embed(title="\U0001F3E6 Heist Status", description=desc, color=discord.Color.blurple())
        await ctx.send(embed=embed)

    @heist.command(name="jailed")
    async def heist_jailed(self, ctx: commands.Context):
        """Show everyone currently in jail."""
        lines = await self._jailed_lines(ctx.guild)
        if not lines:
            await ctx.send("Nobody's in jail right now.")
            return
        embed = discord.Embed(
            title="\U0001F693 Currently in Jail", description="\n".join(lines), color=discord.Color.red()
        )
        await ctx.send(embed=embed)

    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    @heist.command(name="unjail")
    async def heist_unjail(self, ctx: commands.Context, member: discord.Member):
        """Clear jail time for a specific member."""
        until = await self.config.member(member).jailed_until()
        if not until or until <= time.time():
            await ctx.send(f"{member.display_name} isn't in jail.")
            return
        await self.config.member(member).jailed_until.set(0)
        await ctx.send(f"{member.mention} has been released from jail.")

    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    @heist.command(name="unjailall")
    async def heist_unjailall(self, ctx: commands.Context):
        """Clear jail time for everyone in this server."""
        now = time.time()
        all_members = await self.config.all_members(ctx.guild)
        released = 0
        for member_id, data in all_members.items():
            if data.get("jailed_until", 0) > now:
                await self.config.member_from_ids(ctx.guild.id, member_id).jailed_until.set(0)
                released += 1
        if released:
            await ctx.send(f"Released {released} member(s) from jail.")
        else:
            await ctx.send("Nobody was in jail.")

    # ---------- admin settings ----------

    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    @commands.group()
    async def heistset(self, ctx: commands.Context):
        """Configure the heist minigame."""

    @heistset.command(name="minplayers")
    async def heistset_minplayers(self, ctx: commands.Context, amount: int):
        """Minimum crew size for a heist to proceed."""
        if amount < 1:
            await ctx.send("Must be at least 1.")
            return
        await self.config.guild(ctx.guild).min_players.set(amount)
        await ctx.send(f"Minimum players set to {amount}.")

    @heistset.command(name="cooldown")
    async def heistset_cooldown(self, ctx: commands.Context, seconds: int):
        """Cooldown between heists, in seconds."""
        if seconds < 0:
            await ctx.send("Must be 0 or more.")
            return
        await self.config.guild(ctx.guild).cooldown.set(seconds)
        await ctx.send(f"Cooldown between heists set to {self.fmt_duration(seconds)}.")

    @heistset.command(name="window")
    async def heistset_window(self, ctx: commands.Context, seconds: int):
        """Signup window length, in seconds."""
        if seconds < 30:
            await ctx.send("Signup window should be at least 30 seconds.")
            return
        await self.config.guild(ctx.guild).signup_window.set(seconds)
        await ctx.send(f"Signup window set to {seconds} seconds.")

    @heistset.command(name="timeout")
    async def heistset_timeout(self, ctx: commands.Context, seconds: int):
        """How long each player has to choose Safe/Risky per phase."""
        if seconds < 15:
            await ctx.send("Phase timeout should be at least 15 seconds.")
            return
        await self.config.guild(ctx.guild).phase_timeout.set(seconds)
        await ctx.send(f"Per-phase decision timeout set to {seconds} seconds.")

    @heistset.command(name="afkpenalty")
    async def heistset_afkpenalty(self, ctx: commands.Context, percent: int):
        """Percent of wager lost when a player doesn't respond in time."""
        if not 0 <= percent <= 100:
            await ctx.send("Must be between 0 and 100.")
            return
        await self.config.guild(ctx.guild).afk_penalty_percent.set(percent)
        await ctx.send(f"AFK penalty set to {percent}% of wager.")

    @heistset.command(name="chance")
    async def heistset_chance(self, ctx: commands.Context, phase: int, choice: str, percent: int):
        """Set success odds for a phase/choice. e.g. heistset chance 1 risky 55"""
        choice = choice.lower()
        if phase not in (1, 2, 3) or choice not in ("safe", "risky"):
            await ctx.send("Phase must be 1-3 and choice must be `safe` or `risky`.")
            return
        if not 1 <= percent <= 100:
            await ctx.send("Percent must be between 1 and 100.")
            return
        async with self.config.guild(ctx.guild).phase_chances() as chances:
            chances[str(phase)][choice] = percent / 100
        await ctx.send(f"Phase {phase} {choice} success chance set to {percent}%.")

    @heistset.command(name="jailhours")
    async def heistset_jailhours(self, ctx: commands.Context, phase: int, hours: float):
        """Set base jail hours for a risky failure on a given phase."""
        if phase not in (1, 2, 3):
            await ctx.send("Phase must be 1-3.")
            return
        if hours < 0:
            await ctx.send("Hours must be 0 or more.")
            return
        async with self.config.guild(ctx.guild).jail_base_hours() as jail_hours:
            jail_hours[str(phase)] = hours
        await ctx.send(f"Phase {phase} base jail time set to {hours}h (before near-miss/bad-miss scaling).")

    @heistset.command(name="loot")
    async def heistset_loot(self, ctx: commands.Context, choice: str, min_mult: float, max_mult: float):
        """Set Phase 2 loot multiplier range for safe or risky. e.g. heistset loot risky 2.0 3.5"""
        choice = choice.lower()
        if choice not in ("safe", "risky"):
            await ctx.send("Choice must be `safe` or `risky`.")
            return
        if min_mult <= 0 or max_mult < min_mult:
            await ctx.send("Need 0 < min <= max.")
            return
        async with self.config.guild(ctx.guild).phase2_loot() as loot:
            loot[choice] = [min_mult, max_mult]
        await ctx.send(f"Phase 2 {choice} loot multiplier set to {min_mult}x\u2013{max_mult}x of wager.")

    @heistset.command(name="escapebonus")
    async def heistset_escapebonus(self, ctx: commands.Context, min_mult: float, max_mult: float):
        """Set Phase 3 risky-success bonus multiplier range."""
        if min_mult <= 0 or max_mult < min_mult:
            await ctx.send("Need 0 < min <= max.")
            return
        await self.config.guild(ctx.guild).phase3_escape_bonus.set([min_mult, max_mult])
        await ctx.send(f"Phase 3 escape bonus set to {min_mult}x\u2013{max_mult}x on top of the pot.")

    @heistset.command(name="partialfraction")
    async def heistset_partialfraction(self, ctx: commands.Context, min_frac: float, max_frac: float):
        """Set Phase 3 safe-fail kept-fraction range (0-1)."""
        if not (0 <= min_frac <= max_frac <= 1):
            await ctx.send("Need 0 <= min <= max <= 1.")
            return
        await self.config.guild(ctx.guild).phase3_partial_fraction.set([min_frac, max_frac])
        await ctx.send(f"Phase 3 safe-fail keeps {int(min_frac * 100)}%\u2013{int(max_frac * 100)}% of the pot.")

    @heistset.command(name="crewbonus")
    async def heistset_crewbonus(self, ctx: commands.Context, percent_per_extra: float, cap_percent: float):
        """Set the loot bonus per crew member beyond min_players, and its cap."""
        if percent_per_extra < 0 or cap_percent < 0:
            await ctx.send("Values must be 0 or more.")
            return
        await self.config.guild(ctx.guild).crew_bonus_per_extra.set(percent_per_extra / 100)
        await self.config.guild(ctx.guild).crew_bonus_cap.set(1 + cap_percent / 100)
        await ctx.send(
            f"Crew bonus set to +{percent_per_extra}% loot per member above min_players, capped at +{cap_percent}%."
        )

    @heistset.command(name="resetbalance")
    async def heistset_resetbalance(self, ctx: commands.Context):
        """Reset all balance numbers (odds, jail hours, loot, bonuses) to defaults."""
        for key, value in DEFAULT_BALANCE.items():
            await self.config.guild(ctx.guild).set_raw(key, value=value)
        await ctx.send("Heist balance settings reset to defaults.")

    @heistset.command(name="balance")
    async def heistset_balance(self, ctx: commands.Context):
        """Show current odds, jail hours, loot, and bonus settings."""
        conf = await self.config.guild(ctx.guild).all()
        embed = discord.Embed(title="Heist Balance Settings", color=discord.Color.blurple())
        for phase in (1, 2, 3):
            chances = conf["phase_chances"][str(phase)]
            jail = conf["jail_base_hours"][str(phase)]
            embed.add_field(
                name=f"Phase {phase}",
                value=(
                    f"Safe: {int(chances['safe'] * 100)}% | Risky: {int(chances['risky'] * 100)}%\n"
                    f"Jail (base): {jail}h"
                ),
                inline=True,
            )
        loot = conf["phase2_loot"]
        embed.add_field(
            name="Phase 2 Loot",
            value=f"Safe: {loot['safe'][0]}x\u2013{loot['safe'][1]}x\nRisky: {loot['risky'][0]}x\u2013{loot['risky'][1]}x",
            inline=True,
        )
        embed.add_field(
            name="Phase 3 Extras",
            value=(
                f"Escape bonus: {conf['phase3_escape_bonus'][0]}x\u2013{conf['phase3_escape_bonus'][1]}x\n"
                f"Safe-fail keeps: {int(conf['phase3_partial_fraction'][0] * 100)}%"
                f"\u2013{int(conf['phase3_partial_fraction'][1] * 100)}%"
            ),
            inline=True,
        )
        embed.add_field(
            name="Crew Bonus",
            value=f"+{conf['crew_bonus_per_extra'] * 100:.1f}% per extra member, capped at +{(conf['crew_bonus_cap'] - 1) * 100:.1f}%",
            inline=False,
        )
        await ctx.send(embed=embed)

    @heistset.command(name="settings")
    async def heistset_settings(self, ctx: commands.Context):
        """Show core (non-balance) heist settings."""
        conf = await self.config.guild(ctx.guild).all()
        embed = discord.Embed(title="Heist Settings", color=discord.Color.blurple())
        embed.add_field(name="Min Players", value=str(conf["min_players"]))
        embed.add_field(name="Cooldown", value=self.fmt_duration(conf["cooldown"]))
        embed.add_field(name="Signup Window", value=f"{conf['signup_window']}s")
        embed.add_field(name="Phase Timeout", value=f"{conf['phase_timeout']}s")
        embed.add_field(name="AFK Penalty", value=f"{conf['afk_penalty_percent']}%")
        await ctx.send(embed=embed)
