from .introcleanup import IntroCleanup


async def setup(bot):
    await bot.add_cog(IntroCleanup(bot))
