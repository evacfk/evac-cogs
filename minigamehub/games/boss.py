"""boss -- new game type, replacing the earlier vague "steal" idea.

Design doc: extends lootdrop's scenario shape (start/good/bad/button_text/
button_emoji + hp) rather than being a separate mechanic. Tiered difficulty
(weak/medium/strong, weighted), unlimited attacks per user for the fight
duration with a short per-user attack_cooldown (anti-macro fix caught in
design review), every successful hit paid immediately -- no pooled split on
kill, and an escape (timer runs out) is just an ending since outcomes were
already settled per-hit. HP-bar embed updates are batched on a fixed
interval rather than per-reaction, to stay clear of Discord's edit rate
limit during a very active fight.
"""
import asyncio
import logging
import random
import time

import discord
from redbot.core import bank

from .. import pacing, stats
from ..payout_format import format_payout_lines
from .base import register

log = logging.getLogger("red.minigamehub.boss")


class _MentionStub:
    """Stand-in for an attacker who's since left the guild -- format_payout_lines
    just needs something with a .mention."""
    def __init__(self, user_id: int):
        self.mention = f"<@{user_id}>"


def _pick_tier(tiers: dict) -> tuple:
    keys = list(tiers.keys())
    weights = [tiers[k]["weight"] for k in keys]
    key = random.choices(keys, weights=weights, k=1)[0]
    return key, tiers[key]


def _hp_bar(current: int, maximum: int, width: int = 10) -> str:
    """Colored square emoji instead of Unicode block-drawing characters --
    `█`/`░` render almost identically at embed-description size in Discord's
    default font (a full bar and an empty bar looked the same), emoji squares
    don't have that problem and also give a free color cue on HP remaining."""
    if maximum <= 0:
        return "[error]"
    filled = max(0, min(width, round(width * current / maximum)))
    pct = current / maximum
    fill = "\U0001F7E9" if pct > 0.5 else ("\U0001F7E8" if pct > 0.2 else "\U0001F7E5")  # 🟩/🟨/🟥
    return fill * filled + "⬛" * (width - filled)  # ⬛


class _BossView(discord.ui.View):
    def __init__(self, cog, scenario: dict, tier_key: str, tier: dict, max_hp: int, game_conf: dict, guild: discord.Guild, dry_run: bool = False):
        super().__init__(timeout=game_conf["fight_duration"])
        self.cog = cog
        self.scenario = scenario
        self.tier_key = tier_key
        self.tier = tier
        self.max_hp = max_hp
        self.hp = max_hp
        self.game_conf = game_conf
        self.guild = guild
        self.dry_run = dry_run
        self.message: discord.Message = None
        self.last_attack: dict = {}   # user_id -> timestamp, for attack_cooldown
        self.damage_dealt: dict = {}  # user_id -> total damage, for MVP (in-memory only, fine for test runs)
        self.reward_dealt: dict = {}  # user_id -> total currency earned (good hits only), for the public payout summary
        self.attackers: set = set()   # user_ids who landed >=1 good hit
        self.ended = False
        self._dirty = False

    @discord.ui.button(label="Attack!", style=discord.ButtonStyle.danger, emoji="⚔️")
    async def attack(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.ended:
            await interaction.response.send_message("The fight's already over!", ephemeral=True)
            return

        now = time.time()
        last = self.last_attack.get(interaction.user.id, 0)
        cooldown = self.game_conf["attack_cooldown"]
        if now - last < cooldown:
            await interaction.response.send_message(
                f"Still on cooldown -- wait {cooldown - (now - last):.1f}s.", ephemeral=True
            )
            return
        self.last_attack[interaction.user.id] = now

        # Silently acknowledge the click -- no per-attack ephemeral popup to
        # dismiss anymore. The HP bar (render_loop, batched on
        # hp_update_interval) is the only visible feedback on a hit/miss now.
        # Deferring immediately, before the Config writes below, also means
        # a slow attack can't miss Discord's 3-second ack window.
        await interaction.response.defer()

        member = self.guild.get_member(interaction.user.id) or interaction.user
        landed = random.randint(1, 100) <= self.game_conf["hit_chance"]

        if landed:
            dmg = random.randint(*self.game_conf["damage_per_hit"])
            self.hp = max(0, self.hp - dmg)
            self.damage_dealt[member.id] = self.damage_dealt.get(member.id, 0) + dmg
            self.attackers.add(member.id)
            if not self.dry_run:
                await stats.add_boss_damage(self.cog.config, member, dmg)

            base = random.randint(*self.tier["reward"])
            actual = await pacing.settle_reward(self.cog.config, member, base, dry_run=self.dry_run)
            self.reward_dealt[member.id] = self.reward_dealt.get(member.id, 0) + actual
            if not self.dry_run:
                await stats.record_result(self.cog.config, member, "boss", good=True)
        else:
            raw_penalty = random.randint(*self.tier["penalty"])
            # Return value (actual amount deducted) only fed the ephemeral
            # message text before; the deduction itself happens as a side
            # effect of this call, so the await still has to happen.
            await pacing.settle_penalty(member, raw_penalty, dry_run=self.dry_run)
            if not self.dry_run:
                await stats.record_result(self.cog.config, member, "boss", good=False)

        self._dirty = True
        if self.hp <= 0:
            self.ended = True
            self.stop()

    def build_embed(self, status: str) -> discord.Embed:
        title = self.scenario["start"]
        if self.dry_run:
            title = "\U0001F9EA [TEST] " + title
        embed = discord.Embed(
            title=title,
            description=f"{_hp_bar(self.hp, self.max_hp)}\n{self.hp}/{self.max_hp} HP\n\n{status}",
            color=discord.Color.red() if self.hp > 0 else discord.Color.green(),
        )
        embed.set_footer(text=f"Tier: {self.tier_key} | Attackers: {len(self.attackers)}")
        return embed

    async def render_loop(self):
        interval = self.game_conf["hp_update_interval"]
        while not self.ended:
            await asyncio.sleep(interval)
            if self.ended:
                break
            if self._dirty and self.message:
                self._dirty = False
                try:
                    await self.message.edit(embed=self.build_embed("Fight in progress -- click Attack!"))
                except discord.HTTPException:
                    pass


@register("boss")
async def spawn(cog, channel: discord.TextChannel, game_conf: dict, dry_run: bool = False) -> None:
    guild = channel.guild
    cog.active_game[guild.id] = "boss"
    try:
        scenarios = game_conf.get("scenarios") or []
        if not scenarios:
            return
        scenario = random.choice(scenarios)
        tier_key, tier = _pick_tier(game_conf["tiers"])
        max_hp = random.randint(*tier["hp"])

        view = _BossView(cog, scenario, tier_key, tier, max_hp, game_conf, guild, dry_run=dry_run)
        message = await channel.send(embed=view.build_embed("Fight starting -- click Attack!"), view=view)
        view.message = message

        render_task = asyncio.create_task(view.render_loop())
        await view.wait()
        view.ended = True
        render_task.cancel()

        for child in view.children:
            child.disabled = True

        if view.hp <= 0:
            status = f"\U0001F480 **Defeated!** {len(view.attackers)} attacker(s) shared in the spoils."
        else:
            status = "\U0001F4A8 **The boss escaped!** Outcomes from every attack already settled."

        if view.damage_dealt:
            mvp_id = max(view.damage_dealt, key=view.damage_dealt.get)
            mvp = guild.get_member(mvp_id)
            if mvp:
                status += f"\nMVP: {mvp.mention} ({view.damage_dealt[mvp_id]} total damage)"

        final_embed = view.build_embed(status)
        if view.reward_dealt:
            currency = await bank.get_currency_name(guild)
            paid = [
                (guild.get_member(uid) or _MentionStub(uid), amount)
                for uid, amount in view.reward_dealt.items()
            ]
            # Grouped by identical amount (format_payout_lines) rather than
            # one line per attacker -- a busy fight has a lot of attackers
            # landing on the same reward roll, and listing each separately
            # got spammy fast. Amounts vary a lot less than attackers do, so
            # a flat cap on the number of grouped lines is enough to stay
            # under Discord's 1024-char field limit (same cap that bit the
            # diagnostics embed) without needing to count characters.
            lines = format_payout_lines(paid, currency, verb="earned")
            shown, cut = lines[:5], len(lines) - 5
            payouts_text = "\n".join(shown) + (f"\n...and {cut} more" if cut > 0 else "")
            final_embed.add_field(name="Payouts", value=payouts_text, inline=False)

        try:
            await message.edit(embed=final_embed, view=view)
        except discord.HTTPException:
            pass
    finally:
        cog.active_game.pop(guild.id, None)
