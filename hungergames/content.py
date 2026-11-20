"""Training content and the event pool loader. Plain data, no discord/redbot imports."""
from __future__ import annotations

import json
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"

# Sprint to the Cornucopia: obstacle -> the one right move (the other moves are the decoys)
SPRINT = (
    ("🪵 Fallen Cedar Log", "Jump Over"),
    ("🌿 Low-Hanging Vine", "Duck Under"),
    ("🪨 Loose Gravel Slope", "Slide Down"),
    ("📦 Supply Crate", "Grab Crate"),
    ("🐝 Swarm of Wasps", "Sprint"),
    ("💧 Shallow Creek", "Wade Through"),
    ("🌵 Thorny Brambles", "Sidestep"),
    ("⛰️ Elevated Ridge", "Climb Up"),
    ("🎳 Rolling Boulder", "Dodge Left"),
    ("🪤 Tripwire Snare", "Step Over"),
    ("🟫 Mud Pit", "Vault Across"),
    ("🗡️ Weapons Rack", "Snatch Blade"),
)

# Combat Trainer: the trainer's tell -> the right counter
TELLS = (
    ("raises a shield high", "Strike Low"),
    ("winds up a huge overhead swing", "Roll Aside"),
    ("lunges forward with a spear", "Parry"),
    ("draws back a bowstring", "Take Cover"),
    ("charges head-down like a bull", "Sidestep"),
    ("grabs for your collar", "Break Grip"),
    ("feints left, eyes glancing right", "Guard Right"),
    ("throws a handful of sand", "Shield Eyes"),
    ("sweeps a staff at your ankles", "Jump"),
    ("backs away toward a cliff edge", "Hold Ground"),
    ("drops their guard to catch their breath", "Press In"),
    ("swings a net over their head", "Duck"),
)

# Memory March buttons
MARCH = ("🏹", "🔥", "🌲", "💧", "🪨")

INTERVIEW_QUESTIONS = (
    "Caesar leans in: \"What's the first thing you'll do if you win?\"",
    "\"Tell the Capitol something nobody knows about you.\"",
    "\"Who back home are you fighting for tonight?\"",
    "\"What's your secret weapon in the arena?\"",
    "\"If you could bring one luxury item into the arena, what would it be?\"",
    "\"Which tribute worries you the most, and why?\"",
    "\"Describe your training score in three words.\"",
    "\"What would your district say about you right now?\"",
    "\"The crowd wants to know: are you a lover or a fighter?\"",
    "\"What's your strategy: hide, hunt, or charm the sponsors?\"",
    "\"Give the sponsors one reason to send YOU a parachute.\"",
    "\"What's the bravest thing you've ever done?\"",
    "\"If the arena had a snack bar, what would you order?\"",
    "\"What song is stuck in your head going into the Games?\"",
    "\"Finish this sentence: 'The Capitol will remember me as...'\"",
)


def load_events() -> dict:
    with open(DATA / "events.json", encoding="utf-8") as f:
        return json.load(f)


def load_ciphers() -> dict:
    with open(DATA / "ciphers.json", encoding="utf-8") as f:
        return json.load(f)
