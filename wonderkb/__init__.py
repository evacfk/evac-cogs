async def setup(bot):
    # Imported lazily so engine.py stays importable/testable without discord.py or redbot.
    from .wonderkb import WonderKB

    await bot.add_cog(WonderKB(bot))
