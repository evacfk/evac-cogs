async def setup(bot):
    # Imported lazily so the pure-logic modules (engine, models, arctic_shift,
    # constants) stay importable and unit-testable without discord.py / redbot
    # installed. Red calls setup() on load, so behaviour on the real bot is
    # identical.
    from .redditfeed import RedditFeed

    await bot.add_cog(RedditFeed(bot))
