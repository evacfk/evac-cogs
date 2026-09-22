"""Dev-only test shim. Stubs `discord` (just enough for embeds.py) when it
isn't installed -- a no-op on the real host, which has real discord.py
installed via the redbot image. Pure-logic tests (models/storage/collage)
don't need discord.py at all and are unaffected either way.
"""
import sys
import types

try:
    import discord  # noqa: F401
except ImportError:
    discord_stub = types.ModuleType("discord")

    class _Color:
        def __init__(self, name):
            self.name = name

        @classmethod
        def gold(cls):
            return cls("gold")

        @classmethod
        def orange(cls):
            return cls("orange")

        @classmethod
        def red(cls):
            return cls("red")

        @classmethod
        def green(cls):
            return cls("green")

        @classmethod
        def dark_grey(cls):
            return cls("dark_grey")

    class _Embed:
        def __init__(self, title=None, description=None, color=None):
            self.title = title
            self.description = description
            self.color = color
            self.fields = []
            self.footer_text = None
            self.image_url = None

        def add_field(self, name, value, inline=False):
            self.fields.append(types.SimpleNamespace(name=name, value=value, inline=inline))
            return self

        def set_footer(self, text=None, **kwargs):
            self.footer_text = text
            return self

        def set_image(self, url=None):
            self.image_url = url
            return self

    class _File:
        def __init__(self, fp, filename=None):
            self.fp = fp
            self.filename = filename

    class _NotFound(Exception):
        pass

    class _View:
        """Enough of discord.ui.View for a class body defining buttons to
        construct cleanly -- never actually dispatches interactions here.
        """

        def __init__(self, *args, **kwargs):
            self.children = []

    def _button_decorator(*args, **kwargs):
        def deco(f):
            return f

        return deco

    discord_stub.Embed = _Embed
    discord_stub.Color = _Color
    discord_stub.File = _File
    discord_stub.NotFound = _NotFound
    discord_stub.Poll = object
    discord_stub.PollMedia = object
    discord_stub.Guild = object
    discord_stub.Member = object
    discord_stub.Role = object
    discord_stub.TextChannel = object
    discord_stub.Interaction = object
    discord_stub.ButtonStyle = types.SimpleNamespace(success="success", primary="primary", secondary="secondary", danger="danger")
    discord_stub.ui = types.SimpleNamespace(View=_View, Button=object, button=_button_decorator)
    sys.modules["discord"] = discord_stub

try:
    import redbot  # noqa: F401
except ImportError:
    redbot_stub = types.ModuleType("redbot")
    redbot_core_stub = types.ModuleType("redbot.core")
    redbot_bot_stub = types.ModuleType("redbot.core.bot")
    redbot_data_stub = types.ModuleType("redbot.core.data_manager")

    class _StubCommand:
        """Enough of a Red command/group object for photodrop.py's decorator
        chains (`@commands.group()` then `@pp.command()`) to build cleanly.
        Never invoked -- these tests exercise models/storage/collage/embeds
        directly, not the cog's live command dispatch.
        """

        def __init__(self, func, **kwargs):
            self.func = func
            self.name = kwargs.get("name", getattr(func, "__name__", None))
            self.__name__ = self.name or "command"

        def command(self, *args, **kwargs):
            def deco(f):
                return _StubCommand(f, **kwargs)

            return deco

        def group(self, *args, **kwargs):
            return self.command(*args, **kwargs)

        def before_loop(self, f):
            return f

        def __call__(self, *args, **kwargs):
            return self.func(*args, **kwargs)

    def _passthrough_decorator(*args, **kwargs):
        def deco(f):
            return f

        return deco

    def _group(*args, **kwargs):
        def deco(f):
            return _StubCommand(f, **kwargs)

        return deco

    class _Cog:
        pass

    class _Context:
        pass

    class _Config:
        @classmethod
        def get_conf(cls, *args, **kwargs):
            return cls()

        def register_guild(self, **kwargs):
            pass

        def register_member(self, **kwargs):
            pass

    commands_stub = types.SimpleNamespace(
        Cog=_Cog,
        Context=_Context,
        Config=_Config,
        group=_group,
        command=_group,
        guild_only=_passthrough_decorator,
        mod_or_permissions=_passthrough_decorator,
        has_permissions=_passthrough_decorator,
    )

    redbot_core_stub.Config = _Config
    redbot_core_stub.commands = commands_stub
    redbot_bot_stub.Red = type("Red", (), {})
    redbot_data_stub.cog_data_path = lambda cog: None

    sys.modules["redbot"] = redbot_stub
    sys.modules["redbot.core"] = redbot_core_stub
    sys.modules["redbot.core.commands"] = commands_stub
    sys.modules["redbot.core.bot"] = redbot_bot_stub
    sys.modules["redbot.core.data_manager"] = redbot_data_stub
