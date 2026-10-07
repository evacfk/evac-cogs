"""Discord-facing rendering for wonderpet: the home card and the pet's announcements."""
from __future__ import annotations

import discord

from . import engine
from .constants import (
    COLOR_BAD, COLOR_EGG, COLOR_OK, COLOR_WARN, FORMS, GONE_HOURS, LEVEL_HOURS, STAGE_EMOJI, STAGE_LABEL,
)


def pet_emoji(pet: dict) -> str:
    if pet["stage"] == "adult":
        return FORMS[pet["form"] or "scruffy"][1]
    return STAGE_EMOJI[pet["stage"]]


def stage_text(pet: dict) -> str:
    if pet["stage"] == "adult":
        return f"{FORMS[pet['form'] or 'scruffy'][0]} {STAGE_LABEL['adult']}"
    return STAGE_LABEL[pet["stage"]]


def art_slot(pet: dict) -> str:
    """Which uploaded picture (if any) shows right now."""
    if pet["stage"] != "egg" and engine.level_for(pet) >= 2:
        return "sick"
    if pet["stage"] == "adult":
        return pet["form"] or "scruffy"
    return pet["stage"]


def mood_line(pet: dict) -> str:
    name = pet["name"]
    if pet["stage"] == "egg":
        return f"{name}'s egg is warm and wiggling. Keep showing it some love!"
    level = engine.level_for(pet)
    if level >= 4:
        return f"**{name} is fading. This is the last chance, please help right now!**"
    if level == 3:
        return f"**{name} is in a really bad way.** It needs care soon or it will be gone."
    if level == 2:
        return f"{name} is sick and has stopped growing. Please look after it."
    if level == 1:
        low = engine.lowest_meter(pet)
        need = {"hunger": "hungry", "happy": "bored", "clean": "dirty"}[low]
        return f"{name} is feeling {need} and could use some attention."
    return f"{name} is doing great. Thanks, everyone!"


def card_embed(pet: dict, *, now: float, this_week: list, last_week: list, art_file: str | None = None) -> discord.Embed:
    level = engine.level_for(pet)
    color = COLOR_EGG if pet["stage"] == "egg" else COLOR_BAD if level >= 3 else COLOR_WARN if level >= 1 else COLOR_OK
    title = f"{pet_emoji(pet)} {pet['name']} \N{EM DASH} {stage_text(pet)}"
    e = discord.Embed(title=title, description=mood_line(pet), color=discord.Color(color))
    if pet["stage"] != "egg":
        e.add_field(
            name="Wellbeing",
            value="\n".join(f"{label} {engine.bar(pet[m])} {int(pet[m])}%" for label, m in
                            (("\N{CUT OF MEAT} Hunger", "hunger"), ("\N{SLIGHTLY SMILING FACE} Happy ", "happy"),
                             ("\N{BUBBLES} Clean ", "clean"))),
            inline=False,
        )
    prog = engine.growth_progress(pet, now)
    if prog is not None:
        nxt = STAGE_LABEL[prog["next"]] if prog["next"] in STAGE_LABEL else prog["next"]
        e.add_field(
            name=f"Growing into: {nxt}",
            value=(f"Time {min(prog['hours'], prog['hours_needed']) / 24:.1f} / {prog['hours_needed'] / 24:.1f} days\n"
                   f"Care {int(min(prog['care'], prog['care_needed']))} / {prog['care_needed']}"),
            inline=True,
        )
    e.add_field(name="Age", value=engine.age_text(now - pet["born_ts"]), inline=True)
    if level >= 2:
        e.add_field(name="\N{WARNING SIGN} Time left", value=f"~{int(engine.hours_left(pet))}h if nothing changes",
                    inline=True)
    e.add_field(name="Top carers this week", value=_people(this_week), inline=False)
    if last_week:
        e.add_field(name="Last week", value=_people(last_week[:3]), inline=False)
    e.set_footer(text="One free care a day: pick Feed, Play or Clean for whichever meter is lowest. Treats cost wondercoins.")
    if art_file:
        e.set_thumbnail(url=f"attachment://{art_file}")
    return e


def _people(rows: list) -> str:
    if not rows:
        return "Nobody yet. Be the first!"
    medals = ["\N{FIRST PLACE MEDAL}", "\N{SECOND PLACE MEDAL}", "\N{THIRD PLACE MEDAL}"]
    paw, times = "\N{PAW PRINTS}", "\N{MULTIPLICATION SIGN}"
    return "\n".join(f"{medals[i] if i < 3 else paw} <@{uid}> {times}{n}" for i, (uid, n) in enumerate(rows))


# --- announcements -----------------------------------------------------------------------------

def hatch_text(pet: dict) -> str:
    return (f"\N{HATCHING CHICK} **The egg hatched!** Say hello to **{pet['name']}**. "
            f"Everyone gets one free feed, play or clean a day, so pick the meter that needs it most.")


def grew_text(pet: dict, stage: str) -> str:
    if stage == "teen":
        return f"\N{FRONT-FACING BABY CHICK} **{pet['name']} grew into a teen!** Keep up the great care."
    label, emoji = FORMS[pet["form"] or "scruffy"]
    flavor = {"radiant": "All that love really shows. It's glowing!",
              "happy": "A happy, healthy grown-up.",
              "scruffy": "A bit scruffy, but loved all the same."}[pet["form"] or "scruffy"]
    return f"{emoji} **{pet['name']} is all grown up: a {label} adult!** {flavor}"


def warn_text(pet: dict, level: int) -> str:
    n = pet["name"]
    if level == 1:
        low = engine.lowest_meter(pet)
        need = {"hunger": "hungry", "happy": "bored", "clean": "dirty"}[low]
        return f"\N{SLIGHTLY FROWNING FACE} **{n} is feeling {need}.** A quick tap on the card would help."
    if level == 2:
        return (f"\N{FACE WITH THERMOMETER} **{n} has gotten sick and stopped growing.** "
                f"It needs care to get better. If nobody helps it could be gone in about "
                f"{GONE_HOURS - LEVEL_HOURS[2]} hours.")
    if level == 3:
        return (f"\N{WARNING SIGN} **{n} is in trouble!** About {GONE_HOURS - LEVEL_HOURS[3]} hours "
                f"before we lose it. Please feed, play and clean now.")
    return (f"\N{POLICE CARS REVOLVING LIGHT} **FINAL WARNING: {n} has about "
            f"{GONE_HOURS - LEVEL_HOURS[4]} hours left.** Please help, it's almost too late!")


def recovered_text(pet: dict) -> str:
    return f"\N{SPARKLES} **{pet['name']} is feeling better!** Thank you for looking after it."


def ended_text(pet: dict, entry: dict) -> str:
    n, days = pet["name"], engine.age_text(entry["end_ts"] - entry["born_ts"])
    top = ", ".join(f"<@{t['user']}>" for t in entry["top"]) or "nobody"
    kind = entry["end"]
    if kind == "died":
        head = f"\N{CANDLE} **{n} has passed away.** It went hungry for too long."
    elif kind == "ran_away":
        head = f"\N{FOOTPRINTS} **{n} ran away.** It felt too lonely and untended."
    else:
        head = f"\N{SPARKLES} **{n} has retired to the Pet Hall** after a long, happy life."
    return f"{head}\nLived {days}. Best carers: {top}.\nA new egg will arrive soon."


def new_egg_text(pet: dict) -> str:
    return f"\N{EGG} **A new egg appeared!** Keep it warm to help it hatch."


def flavor_text(pet: dict, kind: str, names: list[str]) -> str:
    who = ", ".join(names[:3]) + (" and others" if len(names) > 3 else "")
    n = pet["name"]
    if kind == "birthday":
        return f"\N{WRAPPED PRESENT} {n} made a little birthday card for {who}!"
    if kind == "anniversary":
        return f"\N{PARTY POPPER} {n} cheers for {who} on their server anniversary!"
    return f"\N{EYES} {n} is peeking out. {who} is about to start!"
