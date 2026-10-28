"""Red-Web-Dashboard third-party integration, following the documented pattern
(https://red-web-dashboard.readthedocs.io/en/latest/third_parties.html).

Nothing is imported from the dashboard cog: the decorator only stores its
arguments on the function, and the dashboard reads them when the cog is
registered. That is deliberate -- the Dashboard cog may load after this one, so
the decorator must exist without it.
"""
from __future__ import annotations

import typing

from redbot.core import commands


def dashboard_page(*args, **kwargs):
    def decorator(func: typing.Callable):
        func.__dashboard_decorator_params__ = (args, kwargs)
        return func

    return decorator


class DashboardIntegration:
    """Mixin: registers this cog's `@dashboard_page` methods with the Dashboard
    cog whenever it (re)loads. The cog class must list this BEFORE commands.Cog.
    """

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog: commands.Cog) -> None:
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)
