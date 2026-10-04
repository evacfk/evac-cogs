async def setup(bot):
    # Imported lazily so engine.py / sources.py stay importable without discord.py or redbot.
    from .mystats import MyStats

    await bot.add_cog(MyStats(bot))
