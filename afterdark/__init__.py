async def setup(bot):
    # Imported lazily so the pure-logic modules (engine, models, constants)
    # stay importable and unit-testable without discord.py / redbot installed.
    # Red calls setup() on load, so behaviour on the real bot is identical.
    from .afterdark import AfterDark

    await bot.add_cog(AfterDark(bot))
