from .lurker import Lurker


async def setup(bot):
    await bot.add_cog(Lurker(bot))
