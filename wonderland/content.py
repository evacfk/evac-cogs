"""Everything the guide says. Plain data, no discord/redbot imports.

An Entry is one command line. `requires` lists commands that must exist on the live bot
(every one) and `expand` lists commands where `{cmds}` in `usage` is replaced by the ones that
exist (the entry is hidden if none do). An entry with neither is always shown. That way a
renamed or unloaded cog quietly drops out of the guide instead of showing a broken line.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Entry:
    usage: str
    desc: str
    requires: tuple = ()
    expand: tuple = ()


@dataclass(frozen=True)
class Page:
    key: str
    emoji: str
    title: str
    blurb: str
    entries: tuple
    mod_only: bool = False
    body: str = ""   # optional text between the blurb and the command lines; `{#slug}` becomes a channel link


@dataclass(frozen=True)
class Channel:
    slug: str       # the channel name without its emoji prefix, used to find the live channel
    emoji: str
    desc: str
    # Channels whose access changes with something the member does (verifying, picking a role):
    public_link: bool = True   # False: the published copy shows the name only, never a link
    unlock: str = ""           # how to get in. A member who can't open it sees the name + this hint,
                               # instead of the channel vanishing ("" = hide it from them)


@dataclass(frozen=True)
class Section:
    title: str
    channels: tuple


PAGES = (
    Page("start", "🌱", "Start here", "New here? Do these three first. Nothing is required.", (
        Entry(".birthday set <date>", "add your birthday", requires=("birthday set",)),
        Entry(".payday", "free coins, any time", requires=("payday",)),
        Entry(".card daily", "a free card, once a day", requires=("card daily",)),
        Entry(".wld search <word>", "find any command by what it does"),
    ), body="**1.** Introduce yourself in {#intro}\n**2.** Pick your roles in {#role-select}\n"
            "**3.** Say hi in {#cuddle}\n\n**When you have a minute**"),
    Page("economy", "💰", "Economy & Shop", "Earn, spend and send wondercoin.", (
        Entry(".bank balance", "check your coins", requires=("bank balance",)),
        Entry(".bank transfer @user <amount>", "send coins to someone", requires=("bank transfer",)),
        Entry(".payday", "claim your regular coins", requires=("payday",)),
        Entry(".leaderboard", "the richest members", requires=("leaderboard",)),
        Entry(".shop", "browse and buy items", requires=("shop",)),
    )),
    Page("casino", "🎰", "Casino",
         "Games happen in {#wondercasino}. Tap **Open a Table** on its menu for your own private thread, "
         "or pick a game from its dropdown. The commands below also work.", (
        Entry(".gamble", "same as the Open a Table button", requires=("gamble",)),
        Entry(".endgambling", "close your thread (run it inside the thread)", requires=("endgambling",)),
        Entry(".wonderjack table", "sit at the multiplayer blackjack table", requires=("wonderjack table",)),
        Entry("{cmds}", "table games: pick one and place your bet",
              expand=("slot", "blackjack", "coin", "dice", "craps", "cups", "hilo", "war", "double", "allin")),
        Entry(".payouts", "what the slot machine pays", requires=("payouts",)),
        Entry(".duel @user <bet>", "challenge someone to a best-of-3 duel", requires=("duel",)),
        Entry(".duelstats [@user]", "a duel record", requires=("duelstats",)),
    )),
    Page("games", "🎮", "Games & Activities", "Things to do when you want to play.", (
        Entry(".fish", "go fishing", requires=("fish",)),
        Entry(".fishinfo [@user]", "your catches and fishing stats", requires=("fishinfo",)),
        Entry(".card daily", "your free card for today", requires=("card daily",)),
        Entry(".card show <name>", "show one of your cards full size", requires=("card show",)),
        Entry(".card find <name>", "search your cards by character or series", requires=("card find",)),
        Entry(".card give @user <card>", "give a card to someone", requires=("card give",)),
        Entry(".card leaderboard", "top collectors", requires=("card leaderboard",)),
        Entry(".puzzle status", "progress on the current puzzle", requires=("puzzle status",)),
        Entry(".puzzle mypieces [@user]", "the pieces you've collected, with a preview", requires=("puzzle mypieces",)),
        Entry(".puzzle leaderboard", "all-time puzzle winners", requires=("puzzle leaderboard",)),
        Entry(".puzzle gallery", "hall of fame: finished puzzles", requires=("puzzle gallery",)),
        Entry(".wonderpet me", "what you can still do for the pet today", requires=("wonderpet me",)),
        Entry(".wonderpet card", "bring the pet's card to the bottom of the chat", requires=("wonderpet card",)),
        Entry(".wonderpet carers", "this week's top carers", requires=("wonderpet carers",)),
        Entry(".connect4 [@user]", "play Connect 4 against the bot or a member", requires=("connect4",)),
        Entry(".tictactoe [@user]", "play Tic-Tac-Toe against the bot or a member", requires=("tictactoe",)),
    )),
    Page("profile", "🌟", "Profile & Levels", "Your progress, your stats and your milestones.", (
        Entry(".wld stats [@user]", "your record across every game"),
        Entry(".profile", "view your profile", requires=("profile",)),
        Entry(".setprofile", "customize your profile", requires=("setprofile",)),
        Entry(".leveltop", "the level leaderboard", requires=("leveltop",)),
        Entry(".weekly", "this week's leaderboard", requires=("weekly",)),
        Entry(".lastweekly", "last week's leaderboard", requires=("lastweekly",)),
        Entry(".roletop", "leaderboard by role", requires=("roletop",)),
        Entry(".prestige", "reset your rank for a prestige", requires=("prestige",)),
        Entry(".stars @user", "reward a good noodle", requires=("stars",)),
        Entry(".startop", "the star leaderboard", requires=("startop",)),
        Entry(".birthday set <date>", "save your birthday (the year is optional)", requires=("birthday set",)),
        Entry(".birthday upcoming", "birthdays in the next 30 days", requires=("birthday upcoming",)),
        Entry(".yearclub check [@user]", "your years here and your club role", requires=("yearclub check",)),
        Entry(".userinfo [@user]", "details about a member", requires=("userinfo",)),
    )),
    Page("community", "🎉", "Community & Events", "What's happening in the server.", (
        Entry(".event list", "upcoming and live events", requires=("event list",)),
        Entry(".event mytz", "set your timezone for event times", requires=("event mytz",)),
        Entry(".verdict top", "this month's best crowd-readers", requires=("verdict top",)),
        Entry(".verdict suggest <question> | <a> | <b>", "suggest a daily question (2-4 options)", requires=("verdict suggest",)),
        Entry(".secrets [@user]", "the secrets someone has found", requires=("secrets",)),
        Entry(".bumpreward top", "top server bumpers", requires=("bumpreward top",)),
        Entry(".star / .unstar", "add or remove a message from the starboard", requires=("star", "unstar")),
    )),
    Page("music", "🎵", "Music & Fun", "Voice-channel music and small time-wasters.", (
        Entry(".play <song or link>", "play or queue a track", requires=("play",)),
        Entry("{cmds}", "control playback",
              expand=("pause", "skip", "stop", "queue", "now", "volume", "search", "shuffle", "repeat")),
        Entry("{cmds}", "just for fun", expand=("8", "choose", "flip", "roll", "rps", "urban")),
    )),
    Page("mod", "🛡️", "Mod Cheat Sheet", "Mods only. Which bot does what.", (
        Entry("Hana (Red)", "games, economy, roles, logging and the cogs listed here"),
        Entry("Alice (YAGPDB)", "automod (accounts under a day old, bad words, invite links) and kick, ban and mute"),
        Entry(".case  .casesfor  .softban  .slowmode  .rename  .names  .voicekick", "moderation on Hana",
              expand=()),
        Entry(".quarantine  .unquarantine  .lastactions", "Security cog tools", requires=("quarantine",)),
        Entry(".introform panel / enforce / prune", "the intro form", requires=("introform",)),
        Entry(".introcleanup sweep", "clean intros left by members who left", requires=("introcleanup",)),
        Entry(".pulse", "server activity: day, week, month, heatmap", requires=("pulse",)),
        Entry(".afterdark", "Rabbit Hole access", requires=("afterdark",)),
        Entry(".secrethunt", "secret hunt setup and status", requires=("secrethunt",)),
        Entry(".event create <kind>", "schedule a movie or game night", requires=("event create",)),
        Entry(".lurkerset", "inactivity system settings", requires=("lurkerset",)),
        Entry(".minigamehub", "minigame spawner settings", requires=("minigamehub",)),
        Entry(".wld publish #channel", "post or refresh the guide in a channel"),
        Entry(".wld version", "version probe for this cog"),
    ), mod_only=True),
)

MAP = (
    Section("important", (
        Channel("announcements", "📣", "official news"),
        Channel("server-updates", "🛠️", "what changed on the server"),
        Channel("server-guide", "📖", "the full guide to features"),
        Channel("rules", "📜", "read before chatting"),
        Channel("role-select", "🌈", "pick your roles"),
        Channel("intro", "💌", "introduce yourself"),
        Channel("shop", "🏪", "browse and buy items"),
        Channel("starboard", "⭐", "the best posts, voted by members"),
        Channel("boosties", "💜", "for server boosters"),
    )),
    Section("chat", (
        Channel("cuddle", "🧸", "main chat"),
        Channel("pillow-talk", "🌙", "an adult version of cuddle where NSFW conversations are permitted",
                public_link=False, unlock="pick the role in role-select"),
        Channel("verified-chat", "✅", "for verified members", public_link=False, unlock="verify first"),
        Channel("bump-us", "🚀", "bump the server and earn rewards"),
        Channel("chat-topics", "🧵", "specific conversation topics"),
    )),
    Section("other", (
        Channel("verify", "🔐", "become a verified Wonderlandian!", public_link=False),
        Channel("qotd", "❓", "question of the day"),
        Channel("hall-of-secrets", "🗝️", "secret hunting"),
        Channel("selfies", "📸", "selfie sharing"),
        Channel("share-your-pics", "🖼️", "photo sharing"),
        Channel("gm-and-gn-only", "🌗", "good morning and good night only"),
        Channel("counting", "🔢", "count together"),
    )),
    Section("wonderland arcade", (
        Channel("room creator", "🎮", "join it to create your own room"),
    )),
    Section("bots", (
        Channel("wondercasino", "🎰", "casino tables and games"),
        Channel("bot-commands", "🤖", "run bot commands here"),
    )),
)

MAP_KEY = "map"
HOME_KEY = "home"
