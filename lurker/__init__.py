async def setup(bot):
    # Imported lazily so `lurker.engine` stays importable (and unit-testable) on a
    # machine without redbot installed. Red calls setup() on load, so behaviour
    # on the real bot is identical.
    from .lurker import Lurker

    await bot.add_cog(Lurker(bot))
