from .dailyquests import DailyQuests


async def setup(bot):
    await bot.add_cog(DailyQuests(bot))
