async def setup(bot):
    # Imported lazily so engine.py stays importable/testable without discord.py or redbot.
    from .hungergames import HungerGames

    await bot.add_cog(HungerGames(bot))
