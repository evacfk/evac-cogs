# Running the tests

Suites are per cog (`pytest <cog>/ -q`), not repo-wide. The cogs were written against different
test setups, so no single environment runs every suite green. Use the one listed for the cog.

Always also run `pytest test_index.py test_panel_sync.py` (green in A, B and C) and
`python build_index.py` before a push.

## The environments

| Env | What it is | Setup |
|---|---|---|
| **A. Stub** | Python 3.13, no discord.py, no Red. `conftest.py` stubs both. | `pip install --break-system-packages pytest pytest-asyncio pillow jinja2 aiohttp` in a venv that has no discord.py |
| **B. discord.py + stubbed Red** | Python 3.13, real discord.py, Red still stubbed. The default sandbox setup. | `pip install --break-system-packages discord.py pytest pytest-asyncio` |
| **C. Real Red** | Python 3.11 with Red-DiscordBot installed (Red does not install on 3.13). | `uv venv --python 3.11 /tmp/redvenv`, then `uv pip install --python /tmp/redvenv/bin/python Red-DiscordBot pytest pytest-asyncio pillow jinja2 pip` |

## Which env for which cog

Green means the whole suite passes. Checked on 2026-11-27.

| Cog | Use | Notes |
|---|---|---|
| afterdark | A or B | Fails in C (tests call stub command objects via `.func`) |
| bumpreward | `bash bumpreward/run_tests.sh` | Own runner loads `_redbot_stub`; plain `pytest bumpreward` errors |
| blackjacktable | C | Needs real `redbot.core.bank` |
| cardcollect | B | Errors in A and C |
| celebrations | A, B or C | |
| dailyquests | C | Needs real `commands.Cog.listener` |
| duel | C | Needs real `redbot.core.bank` |
| gamble | C | Needs real `redbot.core.bank` |
| hungergames | B or C | 38 failures in A |
| lurker | B or C | |
| minigamehub | B or C | Some tests skip in A |
| puzzle | C | Needs real `redbot.core.bank` |
| redditfeed | A | 3 tests fail in B and C: they read stub-only attributes (`embed.image_url`, `embed.footer_text`) |
| rejoinwatch | A, B or C | |
| serverpulse | A | 9 tests fail in B and C: they read stub-only attributes (`embed.footer_text`, `modal.box`, `.callback`). `test_dashboard.py` also needs `jinja2` |
| tgfeed | A or B | |
| verdict | B or C | Errors in A |
| wonderevents | A, B or C | |
| wonderland | B or C | Failures and errors in A |
| wonderpet | B or C | Errors in A |

## Reading a failure

- `cannot import name 'bank' from 'redbot.core'` or `has no attribute 'listener'`: you are in a
  stubbed-Red env but the cog needs real Red. Use C.
- `HTTPException.__init__() missing ... 'message'`: a test built a discord exception with one argument.
  discord.py 2.x wants `(response, message)`; use `discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "msg")`.
- `'Embed' object has no attribute 'footer_text'` (or `image_url`, `box`, `.callback`): the test is
  written for the stub and the env has real discord.py. Run it in A.
- Many errors at setup in cardcollect under C: it needs the stubbed Red data manager. Use B.

A failure that appears in the listed env for a cog is a real failure. A failure in any other env is
most likely this table, not the code.
