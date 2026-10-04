async def setup(bot):
    # Imported lazily so engine.py stays importable/testable without discord.py or redbot.
    from .wonderevents import WonderEvents

    await bot.add_cog(WonderEvents(bot))
