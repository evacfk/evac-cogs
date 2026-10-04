async def setup(bot):
    # Imported lazily so engine.py stays importable/testable without discord.py or redbot.
    from .celebrations import Celebrations

    await bot.add_cog(Celebrations(bot))
