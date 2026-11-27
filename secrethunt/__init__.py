async def setup(bot):
    # Imported lazily so cleanup.py stays importable/testable without redbot.
    from .secrethunt import SecretHunt

    await bot.add_cog(SecretHunt(bot))
