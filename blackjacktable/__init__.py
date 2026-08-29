"""
Red's setup() entrypoint -- this is what was missing before. Without this
function, `bot.load_extension()` has nothing to call and raises exactly
the ClientException you hit ("does not have a setup function").
"""

from redbot.core.bot import Red

from .blackjacktable import BlackjackTable


async def setup(bot: Red) -> None:
    await bot.add_cog(BlackjackTable(bot))
