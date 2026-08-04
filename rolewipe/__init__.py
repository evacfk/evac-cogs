from .rolewipe import RoleWipe


async def setup(bot):
    await bot.add_cog(RoleWipe(bot))
