async def setup(bot):
    # Imported lazily so the pure-logic modules (tracker, storage, engine, ...)
    # stay importable and unit-testable without discord.py / redbot installed.
    # Red calls setup() on load, so behaviour on the real bot is identical.
    from .serverpulse import ServerPulse

    await bot.add_cog(ServerPulse(bot))
