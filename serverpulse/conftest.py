"""Dev-only test shim, layered on the repo-root conftest.py (which stubs discord/redbot).

The root stub has no `Cog.listener`; the serverpulse cog class needs it to
import. Inert on the real host (never imported by Red) and a no-op when real
redbot is installed.
"""
from redbot.core import commands

if not hasattr(commands.Cog, "listener"):
    commands.Cog.listener = staticmethod(lambda *a, **k: (lambda f: f))

import discord

for _name in ("Forbidden", "HTTPException"):
    if not hasattr(discord, _name):
        setattr(discord, _name, type(_name, (Exception,), {}))
