async def setup(bot):
    # Imported lazily so engine.py stays importable/testable without discord.py or redbot.
    from .wonderpet import WonderPet

    await bot.add_cog(WonderPet(bot))
