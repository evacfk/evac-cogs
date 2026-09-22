from .photodrop import PhotoDrop


async def setup(bot):
    cog = PhotoDrop(bot)
    await bot.add_cog(cog)
