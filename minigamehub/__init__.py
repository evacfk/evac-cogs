async def setup(bot):
    # Imported lazily so pure-logic modules (offerings.py) stay importable and
    # testable without discord.py or redbot installed.
    from .minigamehub import MinigameHub

    await bot.add_cog(MinigameHub(bot))
