async def setup(bot):
    # Imported lazily so the package (and its pure-logic modules) can be imported
    # in tests without discord.py / redbot installed.
    #
    # One package, one install, one `.load wonderland`. The sub-features keep their
    # own Cog classes (IntroForm, IntroCleanup, MyStats) on purpose: Red keys saved
    # Config data by the cog class name, so keeping the names means every existing
    # setting and every member's saved intro carries over untouched.
    from .guide import Wonderland
    from .introcleanup.introcleanup import IntroCleanup
    from .introform.introform import IntroForm
    from .mystats.mystats import MyStats

    await bot.add_cog(MyStats(bot))
    await bot.add_cog(IntroForm(bot))
    await bot.add_cog(IntroCleanup(bot))
    await bot.add_cog(Wonderland(bot))
