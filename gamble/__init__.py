from .gamblethreads import GambleThreads


async def setup(bot):
    await bot.add_cog(GambleThreads(bot))
