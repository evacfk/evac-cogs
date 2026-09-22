from .cardcollect import CardCollect


async def setup(bot):
    cog = CardCollect(bot)
    await bot.add_cog(cog)
