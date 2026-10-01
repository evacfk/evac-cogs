"""Dev-only test shim that extends the repo-root conftest.py stubs with the few
extra discord.py / Red names IntroForm uses (modals, text inputs, listeners).
Every block is a no-op when the real library is installed (i.e. on the host).
"""
import sys
import types

_discord = sys.modules.get("discord")
if _discord is not None and not hasattr(_discord.ui, "Modal"):

    class _Modal:
        def __init__(self, *args, **kwargs):
            self.children = []

        def add_item(self, item):
            self.children.append(item)

    class _TextInput:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            self.value = kwargs.get("default")

    class _AllowedMentions:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

        @classmethod
        def none(cls):
            return cls()

    _discord.Message = object
    _discord.ui.Modal = _Modal
    _discord.ui.TextInput = _TextInput
    _discord.TextStyle = types.SimpleNamespace(short="short", paragraph="paragraph")
    _discord.AllowedMentions = _AllowedMentions
    _discord.MessageType = types.SimpleNamespace(default=0, reply=19)
    _discord.Forbidden = type("Forbidden", (Exception,), {})
    _discord.HTTPException = type("HTTPException", (Exception,), {})

    def _set_author(self, name=None, icon_url=None, **kwargs):
        self.author_name = name
        return self

    def _set_thumbnail(self, url=None):
        self.thumbnail_url = url
        return self

    if not hasattr(_discord.Embed, "set_author"):
        _discord.Embed.set_author = _set_author
    if not hasattr(_discord.Embed, "set_thumbnail"):
        _discord.Embed.set_thumbnail = _set_thumbnail

_commands = sys.modules.get("redbot.core.commands")
if _commands is not None and not hasattr(_commands, "admin_or_permissions"):

    def _passthrough(*args, **kwargs):
        return lambda f: f

    _commands.admin_or_permissions = _passthrough
    _commands.Cog.listener = staticmethod(_passthrough)
