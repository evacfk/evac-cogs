async def setup(bot):
    # Imported lazily so `rejoinwatch.engine` stays importable (and unit-testable) on a
    # machine without redbot installed. Red calls setup() on load, so behaviour on the
    # real bot is identical.
    from .rejoinwatch import RejoinWatch

    await bot.add_cog(RejoinWatch(bot))
