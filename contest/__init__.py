async def setup(bot):
    # Imported lazily so engine.py stays importable/testable without discord.py or redbot.
    from .contest import Contest

    await bot.add_cog(Contest(bot))
