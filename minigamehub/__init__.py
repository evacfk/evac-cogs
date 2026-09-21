from .minigamehub import MinigameHub


async def setup(bot):
    cog = MinigameHub(bot)
    await bot.add_cog(cog)
