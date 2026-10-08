"""Dev-only test shim, layered on the repo-root conftest.py (which stubs
discord/redbot when they are not installed). The root stub lacks several things
afterdark needs: Cog.listener, a Button that accepts kwargs, View.add_item,
discord.Forbidden/HTTPException/AllowedMentions/PermissionOverwrite, and the
admin_or_permissions decorator. Every block is a no-op when the real library is
installed (i.e. on the host).
"""
import sys
import types

_commands = sys.modules.get("redbot.core.commands")
if _commands is not None:
    if not hasattr(_commands.Cog, "listener"):
        _commands.Cog.listener = staticmethod(lambda *a, **k: (lambda f: f))
    if not hasattr(_commands, "admin_or_permissions"):
        _commands.admin_or_permissions = lambda *a, **k: (lambda f: f)

_discord = sys.modules.get("discord")
if _discord is not None and not hasattr(_discord, "__version__"):
    for _name in ("Forbidden", "HTTPException", "NotFound"):
        if not hasattr(_discord, _name):
            setattr(_discord, _name, type(_name, (Exception,), {}))

    if not hasattr(_discord, "AllowedMentions"):
        class _AllowedMentions:
            @classmethod
            def none(cls):
                return cls()

        _discord.AllowedMentions = _AllowedMentions

    if not hasattr(_discord, "PermissionOverwrite"):
        class _PermissionOverwrite:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        _discord.PermissionOverwrite = _PermissionOverwrite

    class _Button:
        def __init__(self, *, label=None, emoji=None, style=None, custom_id=None, **kwargs):
            self.label = label
            self.emoji = emoji
            self.style = style
            self.custom_id = custom_id
            self.callback = None

    _discord.ui.Button = _Button
    _view = _discord.ui.View
    if not hasattr(_view, "add_item"):
        def _add_item(self, item):
            self.children.append(item)
            return self

        _view.add_item = _add_item
    _orig_init = _view.__init__

    def _init(self, *args, **kwargs):
        _orig_init(self, *args, **kwargs)

    _view.__init__ = _init

    # embeds.py uses Color.dark_grey/green; the root stub has both. Add any
    # colour the cog might need so a future tweak cannot break imports.
    for _color in ("blue", "blurple", "magenta", "purple"):
        if not hasattr(_discord.Color, _color):
            setattr(_discord.Color, _color, classmethod(lambda cls, _c=_color: cls(_c)))
