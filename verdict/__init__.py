async def setup(bot):
    # Imported lazily so engine.py stays importable/testable without discord.py or redbot.
    from .verdict import Verdict

    await bot.add_cog(Verdict(bot))
