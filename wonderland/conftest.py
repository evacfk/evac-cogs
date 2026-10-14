"""Dev-only test shim extending the repo-root stubs with the Red/discord names the guide uses
(hybrid groups, cooldowns, select menus). Every block is a no-op when the real libraries are
installed, i.e. on the host.
"""
import sys
import types

_commands = sys.modules.get("redbot.core.commands")
if _commands is not None and not hasattr(_commands, "hybrid_group"):

    class _Cmd:
        def __init__(self, func, **kw):
            self.callback, self.kw, self.subs = func, kw, {}
            self.name = kw.get("name", getattr(func, "__name__", "cmd"))

        def command(self, *a, **kw):
            def deco(f):
                c = _Cmd(f, **kw)
                self.subs[kw.get("name", f.__name__)] = c
                return c
            return deco

        def __call__(self, *a, **kw):
            return self.callback(*a, **kw)

    def _group(*a, **kw):
        return lambda f: _Cmd(f, **kw)

    def _passthrough(*a, **kw):
        return lambda f: f

    # Only ever ADD missing names; never replace what the repo-level stub already provides.
    _commands.hybrid_group = _group
    for _name in ("cooldown", "mod_or_permissions", "guild_only"):
        if not hasattr(_commands, _name):
            setattr(_commands, _name, _passthrough)
    for _name in ("admin_or_permissions", "has_permissions", "max_concurrency"):
        if not hasattr(_commands, _name):
            setattr(_commands, _name, _passthrough)
    if not hasattr(_commands.Cog, "listener"):
        _commands.Cog.listener = staticmethod(lambda *a, **kw: (lambda f: f))
    if not hasattr(_commands, "BucketType"):
        _commands.BucketType = types.SimpleNamespace(user=1, guild=2, channel=3)
    else:
        for _attr, _val in (("guild", 2), ("channel", 3), ("user", 1)):
            if not hasattr(_commands.BucketType, _attr):
                setattr(_commands.BucketType, _attr, _val)

_discord = sys.modules.get("discord")
if _discord is not None and not hasattr(_discord.ui, "Select"):
    class _Select:
        def __init__(self, *a, **kw):
            self.values = []
            self.__dict__.update(kw)

    _discord.ui.Select = _Select
    _discord.SelectOption = lambda **kw: types.SimpleNamespace(**kw)
    _discord.Interaction = object
    _discord.AllowedMentions = types.SimpleNamespace(none=lambda: None)
    _discord.utils = types.SimpleNamespace(escape_markdown=lambda s: s)
    _discord.Embed.set_footer  # exists on the stub
