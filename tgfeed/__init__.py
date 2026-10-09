async def setup(bot):
    # Imported lazily so the pure-logic modules (engine, models, pipeline, transcode,
    # store, source) stay importable and unit-testable without discord.py / redbot
    # installed. Red calls setup() on load, so behaviour on the real bot is identical.
    from .tgfeed import TGFeed

    await bot.add_cog(TGFeed(bot))
