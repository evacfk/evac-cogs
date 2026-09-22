"""Stub out `redbot` so cardcollect.py (and anything importing it) can be
imported and exercised under plain pytest, without installing the actual
Red-DiscordBot package. Same rationale as photodrop's conftest.py: the real
dependency is heavy and only meaningful inside a running bot process, but
the cog's own import correctness -- and a good chunk of its Config-driven
behavior -- is still worth testing standalone.

This stub emulates enough of Red's real `Config` semantics (Value/Group
call-vs-context-manager duality, per-guild/per-member scoping,
`all_members`) to let command callbacks be exercised directly against it,
not just imported.

discord.py itself is a real pip dependency here (installed for real), so
only `redbot` needs faking.
"""

import asyncio
import copy
import sys
import tempfile
import types
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Config emulation
# ---------------------------------------------------------------------------


class _Value:
    def __init__(self, store: dict, key: str, default):
        self._store = store
        self._key = key
        self._default = default
        self._cm_target = None

    def __call__(self, default=...):
        return self

    def __await__(self):
        return self._get().__await__()

    async def _get(self):
        # a real suspension point, not just an `async def` -- Red's actual
        # Config backend does real I/O, so code relying on a lock around a
        # check-then-set spanning Config awaits (e.g. on_message's drop
        # cooldown) needs its test fakes to genuinely yield here too, or two
        # "concurrent" callers would just run sequentially to completion and
        # the test could pass even against unlocked, racy code
        await asyncio.sleep(0)
        if self._key not in self._store:
            return copy.deepcopy(self._default)
        return copy.deepcopy(self._store[self._key])

    async def set(self, value):
        self._store[self._key] = copy.deepcopy(value)

    async def __aenter__(self):
        if self._key not in self._store:
            self._store[self._key] = copy.deepcopy(self._default)
        self._cm_target = self._store[self._key]
        return self._cm_target

    async def __aexit__(self, exc_type, exc, tb):
        self._store[self._key] = self._cm_target
        return False


class _AllValue:
    def __init__(self, store: dict, defaults: dict):
        self._store = store
        self._defaults = defaults

    def __call__(self, default=...):
        return self

    def __await__(self):
        return self._get().__await__()

    async def _get(self):
        await asyncio.sleep(0)  # real suspension point, see _Value._get
        merged = copy.deepcopy(self._defaults)
        merged.update(copy.deepcopy(self._store))
        return merged

    async def set(self, value):
        self._store.clear()
        self._store.update(copy.deepcopy(value))

    async def __aenter__(self):
        for k, v in self._defaults.items():
            if k not in self._store:
                self._store[k] = copy.deepcopy(v)
        return self._store

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _ScopedConfig:
    def __init__(self, store: dict, defaults: dict):
        object.__setattr__(self, "_store", store)
        object.__setattr__(self, "_defaults", defaults)

    def __getattr__(self, name):
        if name == "all":
            return _AllValue(self._store, self._defaults)
        if name not in self._defaults:
            raise AttributeError(name)
        return _Value(self._store, name, self._defaults[name])

    async def set(self, value):
        # real Red's Group.set() replaces the entire scope in one shot,
        # same as .all.set(value) -- cardcollect.py relies on this for
        # _save_member_state
        self._store.clear()
        self._store.update(copy.deepcopy(value))


class FakeConfig:
    def __init__(self):
        self._guild_defaults = {}
        self._member_defaults = {}
        self._guild_data = {}
        self._member_data = {}

    @classmethod
    def get_conf(cls, cog_instance, identifier, force_registration=True):
        return cls()

    def register_guild(self, **kwargs):
        self._guild_defaults.update(kwargs)

    def register_member(self, **kwargs):
        self._member_defaults.update(kwargs)

    def guild_from_id(self, guild_id):
        store = self._guild_data.setdefault(guild_id, {})
        return _ScopedConfig(store, self._guild_defaults)

    def guild(self, guild_obj):
        return self.guild_from_id(guild_obj.id)

    def member_from_ids(self, guild_id, member_id):
        key = (guild_id, member_id)
        store = self._member_data.setdefault(key, {})
        return _ScopedConfig(store, self._member_defaults)

    def member(self, member_obj):
        return self.member_from_ids(member_obj.guild.id, member_obj.id)

    async def all_members(self, guild_obj):
        result = {}
        for (gid, mid), store in self._member_data.items():
            if gid == guild_obj.id:
                merged = copy.deepcopy(self._member_defaults)
                merged.update(copy.deepcopy(store))
                result[mid] = merged
        return result


# ---------------------------------------------------------------------------
# commands / checks / bank / data_manager emulation
# ---------------------------------------------------------------------------


class _FakeCommand:
    def __init__(self, func, **kwargs):
        self.callback = func
        self.name = kwargs.get("name", func.__name__)
        self.__name__ = func.__name__

    def __get__(self, obj, objtype=None):
        # not used directly -- cog methods are invoked via .callback
        return self


class _FakeGroup(_FakeCommand):
    def __init__(self, func, **kwargs):
        super().__init__(func, **kwargs)
        self.commands = {}

    def command(self, *dargs, **dkwargs):
        def deco(f):
            cmd = _FakeCommand(f, **dkwargs)
            self.commands[cmd.name] = cmd
            return cmd

        return deco

    def group(self, *dargs, **dkwargs):
        def deco(f):
            grp = _FakeGroup(f, **dkwargs)
            self.commands[grp.name] = grp
            return grp

        return deco


def _group(*dargs, **dkwargs):
    def deco(f):
        return _FakeGroup(f, **dkwargs)

    return deco


def _command(*dargs, **dkwargs):
    def deco(f):
        return _FakeCommand(f, **dkwargs)

    return deco


def _identity_decorator_factory(*dargs, **dkwargs):
    def deco(f):
        return f

    return deco


class _FakeCog:
    @staticmethod
    def listener():
        def deco(f):
            f.__cog_listener__ = True
            return f

        return deco


class _FakeContext:
    pass


def build_fake_redbot_modules():
    redbot = types.ModuleType("redbot")
    core = types.ModuleType("redbot.core")
    data_manager = types.ModuleType("redbot.core.data_manager")
    bank_mod = types.ModuleType("redbot.core.bank")
    checks_mod = types.ModuleType("redbot.core.checks")
    commands_mod = types.ModuleType("redbot.core.commands")

    commands_mod.Cog = _FakeCog
    commands_mod.Context = _FakeContext
    commands_mod.group = _group
    commands_mod.command = _command
    commands_mod.guild_only = _identity_decorator_factory
    # current Red (3.5+) exposes these on redbot.core.commands, not the
    # legacy redbot.core.checks module -- see cardcollect.py's import
    commands_mod.admin_or_permissions = _identity_decorator_factory
    commands_mod.mod_or_permissions = _identity_decorator_factory

    checks_mod.admin_or_permissions = _identity_decorator_factory
    checks_mod.mod_or_permissions = _identity_decorator_factory

    async def _deposit_credits(member, amount):
        _deposit_credits.calls.append((member, amount))

    _deposit_credits.calls = []
    bank_mod.deposit_credits = _deposit_credits

    _data_dirs = {}

    def _cog_data_path(cog_instance):
        cls_name = type(cog_instance).__name__
        if cls_name not in _data_dirs:
            _data_dirs[cls_name] = Path(tempfile.mkdtemp(prefix=f"{cls_name}_data_"))
        return _data_dirs[cls_name]

    data_manager.cog_data_path = _cog_data_path

    core.Config = FakeConfig
    core.bank = bank_mod
    core.checks = checks_mod
    core.commands = commands_mod
    core.data_manager = data_manager

    redbot.core = core

    sys.modules["redbot"] = redbot
    sys.modules["redbot.core"] = core
    sys.modules["redbot.core.data_manager"] = data_manager
    sys.modules["redbot.core.bank"] = bank_mod
    sys.modules["redbot.core.checks"] = checks_mod
    sys.modules["redbot.core.commands"] = commands_mod

    return redbot


build_fake_redbot_modules()


@pytest.fixture
def fake_bot():
    class FakeUser:
        id = 999999

    class FakeBot:
        user = FakeUser()

        def get_channel(self, channel_id):
            return None

    return FakeBot()
