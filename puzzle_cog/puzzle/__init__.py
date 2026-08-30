from .puzzle import Puzzle


async def setup(bot):
    await bot.add_cog(Puzzle(bot))
