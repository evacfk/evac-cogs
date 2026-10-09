"""Dev-only test shim, layered on the repo-root conftest.py (which stubs
discord/redbot). Every block is a no-op when the real library is installed
(i.e. on the host).
"""
import sys

_commands = sys.modules.get("redbot.core.commands")
if _commands is not None and not hasattr(_commands.Cog, "listener"):
    _commands.Cog.listener = staticmethod(lambda *a, **k: (lambda f: f))

_discord = sys.modules.get("discord")
if _discord is not None:
    for _name in ("Forbidden", "HTTPException"):
        if not hasattr(_discord, _name):
            setattr(_discord, _name, type(_name, (Exception,), {}))
    if not hasattr(_discord, "AllowedMentions"):
        import types as _types
        _discord.AllowedMentions = _types.SimpleNamespace(none=lambda: None)
