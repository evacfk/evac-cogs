from .bumpreward import BumpReward


async def setup(bot):
    await bot.add_cog(BumpReward(bot))
