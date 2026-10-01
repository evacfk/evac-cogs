async def setup(bot):
    # Imported lazily so the package (and its pure-logic modules) can be
    # imported in tests without discord.py / redbot installed.
    from .introform import IntroForm

    await bot.add_cog(IntroForm(bot))
