"""Discord views for hungergames: the persistent buttons, the short-lived menus and the four training
challenges. Game rules live in engine.py and the cog; this file only builds UI and forwards clicks.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time

import discord

from . import engine
from .constants import (
    AGILITY_LIMIT, AGILITY_STEPS, BTN, CHALLENGE_LABEL, ENDURANCE_IDLE_LIMIT, ENDURANCE_MAX, ENDURANCE_SHOW_SECONDS,
    ENDURANCE_START, INTELLECT_ATTEMPTS, INTELLECT_LIMIT, INTERVIEW_MAX_CHARS, ITEMS, STRATEGIES, STRENGTH_LIMIT,
    STRENGTH_ROUNDS,
)
from .content import MARCH, SPRINT, TELLS

log = logging.getLogger("red.evac-cogs.hungergames")
_DISCORD_ERRORS = (discord.Forbidden, discord.NotFound, discord.HTTPException)


async def say(interaction, text, *, view=None, embed=None):
    """Reply ephemerally whether or not the interaction was already answered."""
    kwargs = {"content": text or None, "ephemeral": True}
    if view is not None:
        kwargs["view"] = view
    if embed is not None:
        kwargs["embed"] = embed
    try:
        if interaction.response.is_done():
            await interaction.followup.send(**kwargs)
        else:
            await interaction.response.send_message(**kwargs)
    except _DISCORD_ERRORS:
        log.debug("hungergames: could not reply to an interaction", exc_info=True)


# =====================================================================================================
# persistent views (fixed custom ids, registered once with bot.add_view)
# =====================================================================================================

class ReapingView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="I Volunteer", emoji="🙋", style=discord.ButtonStyle.success, custom_id=BTN["volunteer"], row=0)
    async def volunteer(self, interaction, button):
        await self.cog.on_volunteer(interaction)

    @discord.ui.button(label="Withdraw", emoji="🏳️", style=discord.ButtonStyle.secondary, custom_id=BTN["withdraw"], row=0)
    async def withdraw(self, interaction, button):
        await self.cog.on_withdraw(interaction)

    @discord.ui.button(label="Keep my spot", emoji="✊", style=discord.ButtonStyle.secondary, custom_id=BTN["keep"], row=0)
    async def keep(self, interaction, button):
        await self.cog.on_keep(interaction)

    @discord.ui.button(label="Never draft me / Draft me again", style=discord.ButtonStyle.secondary,
                       custom_id=BTN["optout"], row=1)
    async def optout(self, interaction, button):
        await self.cog.on_optout(interaction)

    @discord.ui.button(label="Hunger Games pings", emoji="🔔", style=discord.ButtonStyle.secondary,
                       custom_id=BTN["pings"], row=1)
    async def pings(self, interaction, button):
        await self.cog.on_pings(interaction)


class SwapView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Accept a volunteer", emoji="🤝", style=discord.ButtonStyle.success, custom_id=BTN["swap_accept"])
    async def accept(self, interaction, button):
        await self.cog.on_swap_accept(interaction)

    @discord.ui.button(label="Keep my spot", emoji="✊", style=discord.ButtonStyle.secondary, custom_id=BTN["swap_keep"])
    async def keep(self, interaction, button):
        await self.cog.on_keep(interaction)


class TrainingView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog
        for kind, (label, emoji) in CHALLENGE_LABEL.items():
            self.add_item(_RouteButton(cog, label=label, emoji=emoji, custom_id=BTN["train"] + kind, kind=kind))
        self.add_item(_RouteButton(cog, label="Interview", emoji="🎤", custom_id=BTN["train"] + "interview",
                                   kind="interview", style=discord.ButtonStyle.success))


class _RouteButton(discord.ui.Button):
    def __init__(self, cog, *, kind, style=discord.ButtonStyle.primary, **kw):
        super().__init__(style=style, **kw)
        self.cog, self.kind = cog, kind

    async def callback(self, interaction):
        await self.cog.on_train(interaction, self.kind)


class CheerView(discord.ui.View):
    def __init__(self, cog, count: int = 0):
        super().__init__(timeout=None)
        self.cog = cog
        btn = discord.ui.Button(label=f"Cheer ({count})" if count else "Cheer", emoji="👏",
                                style=discord.ButtonStyle.success, custom_id=BTN["cheer"])
        btn.callback = self._cheer
        self.add_item(btn)

    async def _cheer(self, interaction):
        await self.cog.on_cheer(interaction)


class ArenaView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Sponsor", emoji="🪂", style=discord.ButtonStyle.success, custom_id=BTN["sponsor"])
    async def sponsor(self, interaction, button):
        await self.cog.on_sponsor(interaction)

    @discord.ui.button(label="My plan (tributes)", emoji="📋", style=discord.ButtonStyle.secondary, custom_id=BTN["plan"])
    async def plan(self, interaction, button):
        await self.cog.on_plan(interaction)


class AllyView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Accept alliance", emoji="🤝", style=discord.ButtonStyle.success, custom_id=BTN["ally_yes"])
    async def yes(self, interaction, button):
        await self.cog.on_ally_answer(interaction, True)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.secondary, custom_id=BTN["ally_no"])
    async def no(self, interaction, button):
        await self.cog.on_ally_answer(interaction, False)


def persistent_views(cog) -> list:
    return [ReapingView(cog), SwapView(cog), TrainingView(cog), CheerView(cog), ArenaView(cog), AllyView(cog)]


# =====================================================================================================
# short-lived menus
# =====================================================================================================

class PickView(discord.ui.View):
    """One select; calls `on_pick(interaction, value)`. Only `owner` may use it."""

    def __init__(self, owner_id: int, options: list, on_pick, placeholder: str, timeout: int = 120):
        super().__init__(timeout=timeout)
        self.owner_id, self.on_pick = owner_id, on_pick
        select = discord.ui.Select(placeholder=placeholder, options=options[:25], min_values=1, max_values=1)
        select.callback = self._picked
        self.select = select
        self.add_item(select)

    async def _picked(self, interaction):
        if interaction.user.id != self.owner_id:
            await say(interaction, "That menu isn't yours.")
            return
        self.stop()
        await self.on_pick(interaction, self.select.values[0])


class ConfirmView(discord.ui.View):
    """Buttons from [(label, style, value)]; calls `on_choice(interaction, value)`."""

    def __init__(self, owner_id: int, choices: list, on_choice, timeout: int = 120):
        super().__init__(timeout=timeout)
        self.owner_id, self.on_choice = owner_id, on_choice
        for label, style, value in choices:
            btn = discord.ui.Button(label=label, style=getattr(discord.ButtonStyle, style))
            btn.callback = self._make(value)
            self.add_item(btn)

    def _make(self, value):
        async def callback(interaction):
            if interaction.user.id != self.owner_id:
                await say(interaction, "That isn't yours.")
                return
            self.stop()
            await self.on_choice(interaction, value)
        return callback


class ShopView(discord.ui.View):
    """Pick a tribute, then an item, then Buy."""

    def __init__(self, cog, owner_id: int, tributes: list, items: list, tribute: str | None, item: str | None):
        super().__init__(timeout=180)
        self.cog, self.owner_id, self.tribute, self.item = cog, owner_id, tribute, item
        t_opts = [discord.SelectOption(label=name[:100], value=uid, description=desc[:100], default=uid == tribute)
                  for uid, name, desc in tributes[:25]]
        if t_opts:
            sel = discord.ui.Select(placeholder="1. Choose a tribute", options=t_opts, row=0)
            sel.callback = self._pick_tribute
            self.add_item(sel)
        if tribute and items:
            i_opts = [discord.SelectOption(label=label[:100], value=key, emoji=ITEMS[key][1], description=desc[:100],
                                           default=key == item) for key, label, desc in items[:25]]
            sel2 = discord.ui.Select(placeholder="2. Choose an item", options=i_opts, row=1)
            sel2.callback = self._pick_item
            self.add_item(sel2)
        buy = discord.ui.Button(label="Send parachute", emoji="🪂", style=discord.ButtonStyle.success, row=2,
                                disabled=not (tribute and item))
        buy.callback = self._buy
        self.add_item(buy)

    async def _guard(self, interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await say(interaction, "Open your own shop with the Sponsor button.")
            return False
        return True

    async def _pick_tribute(self, interaction):
        if await self._guard(interaction):
            await self.cog.render_shop(interaction, interaction.data["values"][0], None)

    async def _pick_item(self, interaction):
        if await self._guard(interaction):
            await self.cog.render_shop(interaction, self.tribute, interaction.data["values"][0])

    async def _buy(self, interaction):
        if await self._guard(interaction):
            await self.cog.buy(interaction, self.tribute, self.item)


class PlayerView(discord.ui.View):
    """Buttons under `.hg`."""

    def __init__(self, cog, owner_id: int, shop: bool):
        super().__init__(timeout=300)
        self.cog, self.owner_id = cog, owner_id
        specs = [("Sponsor", "🪂", "success", cog.on_sponsor, not shop), ("My stats", "📈", "secondary", cog.on_my_stats, False),
                 ("Leaderboard", "🏅", "secondary", cog.on_leaderboard, False),
                 ("Hall of Victors", "🏛️", "secondary", cog.on_hall, False),
                 ("Draft opt-out", "🚫", "secondary", cog.on_optout, False),
                 ("Pings", "🔔", "secondary", cog.on_pings, False)]
        for label, emoji, style, handler, disabled in specs:
            btn = discord.ui.Button(label=label, emoji=emoji, style=getattr(discord.ButtonStyle, style), disabled=disabled)
            btn.callback = self._wrap(handler)
            self.add_item(btn)

    @staticmethod
    def _wrap(handler):
        async def callback(interaction):
            await handler(interaction)
        return callback


def strategy_options(current: str, allies_possible: bool) -> list:
    opts = [discord.SelectOption(label=f"{label}", value=key, emoji=emoji, description=effect[:100], default=key == current)
            for key, (label, emoji, effect) in STRATEGIES.items()]
    if allies_possible:
        opts.append(discord.SelectOption(label="Seek an alliance", value="ally", emoji="🤝",
                                         description="Ask another tribute to team up (they must accept)"))
    return opts


class AnswerModal(discord.ui.Modal):
    def __init__(self, title: str, label: str, on_submit, *, max_length: int = 100, long: bool = False,
                 placeholder: str = ""):
        super().__init__(title=title[:45], timeout=600)
        self.box = discord.ui.TextInput(label=label[:45], max_length=max_length, placeholder=placeholder[:100],
                                        style=discord.TextStyle.paragraph if long else discord.TextStyle.short)
        self.add_item(self.box)
        self._submit = on_submit

    async def on_submit(self, interaction):
        await self._submit(interaction, str(self.box.value))


# =====================================================================================================
# the live play-by-play line in a tribute's training thread
# =====================================================================================================

class LiveLine:
    """One message in the training thread, edited at most every ~1.5s so a fast tribute can't flood the API."""

    def __init__(self, thread, header: str):
        self.thread, self.header = thread, header
        self.parts: list = []
        self.footer = ""
        self.message = None
        self._task = None
        self._dirty = False

    def text(self) -> str:
        body = " ".join(self.parts) or "starting…"
        return f"{self.header}\n{body}" + (f"\n{self.footer}" if self.footer else "")

    def add(self, part: str):
        self.parts.append(part)
        self._schedule()

    def _schedule(self):
        self._dirty = True
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._flush_later())

    async def _flush_later(self):
        await asyncio.sleep(1.5)
        await self.flush()

    async def flush(self):
        if self.thread is None or not self._dirty:
            return
        self._dirty = False
        try:
            if self.message is None:
                self.message = await self.thread.send(self.text(), allowed_mentions=discord.AllowedMentions.none())
            else:
                await self.message.edit(content=self.text())
        except _DISCORD_ERRORS:
            log.debug("hungergames: live line update failed", exc_info=True)

    async def finish(self, footer: str):
        self.footer = footer
        self._dirty = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
        await self.flush()


# =====================================================================================================
# training challenges (ephemeral, one attempt each)
# =====================================================================================================

class Challenge(discord.ui.View):
    """Base: owner check, a monotonic clock, a public live line and one call to cog.finish_challenge."""

    kind = ""
    limit = 60

    def __init__(self, cog, guild, uid: int, thread, rng: random.Random):
        super().__init__(timeout=self.limit)
        self.cog, self.guild, self.uid, self.rng = cog, guild, uid, rng
        label, emoji = CHALLENGE_LABEL[self.kind]
        self.live = LiveLine(thread, f"{emoji} **{label}**")
        self.t0 = time.monotonic()
        self.done = False
        self.message_interaction = None

    def elapsed(self) -> float:
        return time.monotonic() - self.t0

    async def interaction_check(self, interaction) -> bool:
        if interaction.user.id != self.uid:
            await say(interaction, "Only the tribute can do their own training.")
            return False
        return True

    def result(self, finished: bool) -> dict:
        raise NotImplementedError

    async def end(self, interaction=None, *, finished: bool, note: str = ""):
        if self.done:
            return
        self.done = True
        self.stop()
        res = self.result(finished)
        res["finished"] = finished
        score_note = f"Accuracy {res['acc']:.0f}/70" + (f" · {res['time']:.1f}s" if finished else " · ran out of time")
        await self.live.finish(f"**Done.** {score_note}" + (f" · {note}" if note else ""))
        await self.cog.finish_challenge(self.guild, self.uid, self.kind, res)
        text = f"Challenge complete! {score_note}. Your speed bonus is worked out against the other tributes tonight."
        if interaction is not None:
            try:
                if interaction.response.is_done():
                    await interaction.edit_original_response(content=text, embed=None, view=None)
                else:
                    await interaction.response.edit_message(content=text, embed=None, view=None)
            except _DISCORD_ERRORS:
                pass

    async def on_timeout(self):
        if not self.done:
            await self.end(None, finished=False)

    def buttons(self, labels: list, callback_for) -> None:
        self.clear_items()
        for i, label in enumerate(labels):
            btn = discord.ui.Button(label=label[:80], style=discord.ButtonStyle.secondary, row=i // 3)
            btn.callback = callback_for(label)
            self.add_item(btn)


class SprintChallenge(Challenge):
    kind = "agility"
    limit = AGILITY_LIMIT

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.steps = self.rng.sample(list(SPRINT), AGILITY_STEPS)
        self.i = 0
        self.misses = 0
        self.step_t = time.monotonic()
        self._build()

    def _build(self):
        obstacle, right = self.steps[self.i]
        decoys = self.rng.sample([a for _o, a in SPRINT if a != right], 5)
        labels = decoys + [right]
        self.rng.shuffle(labels)
        self.buttons(labels, self._click)

    def prompt(self) -> str:
        return (f"🏃 **Sprint to the Cornucopia** — obstacle {self.i + 1}/{len(self.steps)}\n"
                f"## {self.steps[self.i][0]}\nPick the right move, fast!")

    def _click(self, label):
        async def callback(interaction):
            if self.done:
                return
            ok = label == self.steps[self.i][1]
            took = time.monotonic() - self.step_t
            if not ok:
                self.misses += 1
            self.live.add(f"{'✅' if ok else '❌'}{took:.1f}s")
            self.i += 1
            self.step_t = time.monotonic()
            if self.i >= len(self.steps):
                await self.end(interaction, finished=True)
                return
            self._build()
            await interaction.response.edit_message(content=self.prompt(), view=self)
        return callback

    def result(self, finished):
        missed = self.misses + (len(self.steps) - self.i)
        return {"acc": engine.agility_accuracy(missed), "time": min(self.elapsed(), self.limit)}


class CombatChallenge(Challenge):
    kind = "strength"
    limit = STRENGTH_LIMIT

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.rounds = self.rng.sample(list(TELLS), STRENGTH_ROUNDS)
        self.i = 0
        self.correct = 0
        self._build()

    def _build(self):
        _tell, right = self.rounds[self.i]
        decoys = self.rng.sample([c for _t, c in TELLS if c != right], 3)
        labels = decoys + [right]
        self.rng.shuffle(labels)
        self.buttons(labels, self._click)

    def prompt(self) -> str:
        return (f"🥊 **Combat Trainer** — round {self.i + 1}/{len(self.rounds)}\n"
                f"## The trainer {self.rounds[self.i][0]}!\nCounter it.")

    def _click(self, label):
        async def callback(interaction):
            if self.done:
                return
            ok = label == self.rounds[self.i][1]
            self.correct += ok
            self.live.add("✅" if ok else "❌")
            self.i += 1
            if self.i >= len(self.rounds):
                await self.end(interaction, finished=True)
                return
            self._build()
            await interaction.response.edit_message(content=self.prompt(), view=self)
        return callback

    def result(self, finished):
        return {"acc": engine.strength_accuracy(self.correct), "time": min(self.elapsed(), self.limit)}


class MarchChallenge(Challenge):
    kind = "endurance"
    limit = ENDURANCE_IDLE_LIMIT     # resets on every click; the show phase is short

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.length = ENDURANCE_START
        self.cleared = 2
        self.seq: list = []
        self.pos = 0
        self.input_time = 0.0
        self.inputs = 0
        self.shown_at = 0.0
        self.last_t = 0.0

    def new_sequence(self):
        self.seq = [self.rng.choice(MARCH) for _ in range(self.length)]
        self.pos = 0

    def show_text(self) -> str:
        return (f"🧠 **Memory March** — length {self.length}\nMemorize this:\n# {' '.join(self.seq)}\n"
                f"*(it disappears in {ENDURANCE_SHOW_SECONDS} seconds)*")

    def input_text(self) -> str:
        return f"🧠 **Memory March** — length {self.length}\nNow repeat it! ({self.pos}/{self.length})"

    async def start(self, interaction):
        self.new_sequence()
        self.clear_items()
        await interaction.response.send_message(self.show_text(), ephemeral=True, view=self)
        self.message_interaction = interaction
        await self._reveal_buttons(interaction)

    async def _reveal_buttons(self, interaction):
        await asyncio.sleep(ENDURANCE_SHOW_SECONDS)
        if self.done:
            return
        self.buttons(list(MARCH), self._click)
        self.last_t = time.monotonic()
        try:
            await interaction.edit_original_response(content=self.input_text(), view=self)
        except _DISCORD_ERRORS:
            pass

    def _click(self, emoji):
        async def callback(interaction):
            if self.done or not self.seq:
                return
            now = time.monotonic()
            self.input_time += now - self.last_t
            self.inputs += 1
            self.last_t = now
            if emoji != self.seq[self.pos]:
                self.live.add(f"❌ at {self.length}")
                await self.end(interaction, finished=True, note=f"cleared length {self.cleared}")
                return
            self.pos += 1
            if self.pos < self.length:
                await interaction.response.edit_message(content=self.input_text(), view=self)
                return
            self.cleared = self.length
            self.live.add(f"✅{self.length}")
            if self.length >= ENDURANCE_MAX:
                await self.end(interaction, finished=True, note="perfect memory!")
                return
            self.length += 1
            self.new_sequence()
            self.clear_items()
            await interaction.response.edit_message(content=self.show_text(), view=self)
            await self._reveal_buttons(interaction)
        return callback

    def result(self, finished):
        per_input = self.input_time / self.inputs if self.inputs else ENDURANCE_IDLE_LIMIT
        return {"acc": engine.endurance_accuracy(self.cleared), "time": per_input}


class CipherChallenge(Challenge):
    kind = "intellect"
    limit = INTELLECT_LIMIT

    def __init__(self, cog, guild, uid, thread, rng, puzzles: list):
        super().__init__(cog, guild, uid, thread, rng)
        self.puzzles = puzzles
        self.i = 0
        self.tries = 0
        self.first = 0
        self.second = 0
        btn = discord.ui.Button(label="Answer", emoji="✍️", style=discord.ButtonStyle.primary)
        btn.callback = self._open
        self.add_item(btn)
        skip = discord.ui.Button(label="Skip this one", style=discord.ButtonStyle.secondary)
        skip.callback = self._skip
        self.add_item(skip)

    def prompt(self, note: str = "") -> str:
        p = self.puzzles[self.i]
        left = max(0, int(self.limit - self.elapsed()))
        return (f"📡 **Capitol Broadcast** — puzzle {self.i + 1}/{len(self.puzzles)} · {p['kind']}\n"
                f"## {p['prompt']}\n"
                f"Attempts left: {INTELLECT_ATTEMPTS - self.tries} · about {left}s left on the clock"
                + (f"\n{note}" if note else ""))

    async def _open(self, interaction):
        if self.done:
            return
        await interaction.response.send_modal(AnswerModal("Capitol Broadcast", "Your answer", self._submit))

    async def _skip(self, interaction):
        if self.done:
            return
        self.live.add("⏭️")
        await self._next(interaction, "Skipped.")

    async def _next(self, interaction, note: str):
        self.i += 1
        self.tries = 0
        if self.i >= len(self.puzzles):
            await self.end(interaction, finished=True)
            return
        await interaction.response.edit_message(content=self.prompt(note), view=self)

    async def _submit(self, interaction, guess: str):
        if self.done:
            await say(interaction, "This challenge is already over.")
            return
        if self.elapsed() > self.limit:
            self.live.add("⌛")
            await self.end(interaction, finished=False)
            return
        if engine.check_answer(self.puzzles[self.i], guess):
            if self.tries == 0:
                self.first += 1
            else:
                self.second += 1
            self.live.add("✅" if self.tries == 0 else "☑️")
            await self._next(interaction, "Correct!")
            return
        self.tries += 1
        if self.tries >= INTELLECT_ATTEMPTS:
            self.live.add("❌")
            await self._next(interaction, "Out of attempts on that one.")
            return
        await interaction.response.edit_message(content=self.prompt("Not quite. One more try."), view=self)

    def result(self, finished):
        return {"acc": engine.intellect_accuracy(self.first, self.second), "time": min(self.elapsed(), self.limit)}


class InterviewView(discord.ui.View):
    def __init__(self, cog, uid: int, question: str):
        super().__init__(timeout=600)
        self.cog, self.uid, self.question = cog, uid, question
        btn = discord.ui.Button(label="Answer Caesar", emoji="🎤", style=discord.ButtonStyle.success)
        btn.callback = self._open
        self.add_item(btn)

    async def _open(self, interaction):
        if interaction.user.id != self.uid:
            await say(interaction, "Only the tribute can answer.")
            return
        await interaction.response.send_modal(AnswerModal(
            "Caesar's Interview", "Your answer", self._submit, max_length=INTERVIEW_MAX_CHARS, long=True,
            placeholder=self.question))

    async def _submit(self, interaction, answer: str):
        self.stop()
        await self.cog.submit_interview(interaction, self.question, answer.strip())
