"""Dev-only stub of redbot so the cog can be imported/tested outside a Red install.

Loaded as a pytest plugin (see run_tests.sh) BEFORE the package is imported. Inert when real redbot is installed.
"""
import asyncio  # noqa: F401
import copy
import sys
import types

try:
    import redbot  # noqa: F401
except ImportError:
    class _Value:
        def __init__(self, store, key):
            self._store, self._key = store, key

        async def __call__(self):
            return copy.deepcopy(self._store[self._key])

        async def set(self, v):
            self._store[self._key] = copy.deepcopy(v)

    class _Group:
        def __init__(self, store):
            self._store = store

        async def all(self):
            return copy.deepcopy(self._store)

        def __getattr__(self, name):
            if name.startswith("_") or name not in self._store:
                # real Red with force_registration=True raises for unregistered keys
                raise AttributeError(name)
            return _Value(self._store, name)

    class Config:
        @classmethod
        def get_conf(cls, *a, **k):
            return cls()

        def __init__(self):
            self._defaults = {"guild": {}, "member": {}}
            self._data = {"guild": {}, "member": {}}

        def register_guild(self, **d):
            self._defaults["guild"] = d

        def register_member(self, **d):
            self._defaults["member"] = d

        def _get(self, kind, key):
            bucket = self._data[kind]
            if key not in bucket:
                bucket[key] = copy.deepcopy(self._defaults[kind])
            return _Group(bucket[key])

        def guild(self, g):
            return self._get("guild", g.id)

        def member(self, m):
            return self._get("member", m.id)

        async def all_members(self, guild):
            return {k: copy.deepcopy(v) for k, v in self._data["member"].items()}

    class BalanceTooHigh(Exception):
        max_balance = 10**9

    class _Bank:
        currency = "wondercoin"
        deposits = []
        raise_on_deposit = None

        async def get_currency_name(self, guild):
            return self.currency

        async def deposit_credits(self, member, amount):
            if self.raise_on_deposit:
                raise self.raise_on_deposit
            self.deposits.append((member.id, amount))

    def _identity(*a, **k):
        return lambda f: f

    def _group(*a, **k):
        def deco(f):
            f.command = lambda *a, **k: (lambda g: g)
            return f
        return deco

    class _Cog:
        listener = staticmethod(lambda *a, **k: (lambda f: f))

    commands = types.SimpleNamespace(
        Cog=_Cog, Context=object, group=_group, guild_only=_identity,
        mod_or_permissions=_identity,
    )
    errors = types.SimpleNamespace(BalanceTooHigh=BalanceTooHigh)
    core = types.ModuleType("redbot.core")
    core.Config, core.bank, core.commands, core.errors = Config, _Bank(), commands, errors
    root = types.ModuleType("redbot")
    root.core = core
    sys.modules["redbot"] = root
    sys.modules["redbot.core"] = core
