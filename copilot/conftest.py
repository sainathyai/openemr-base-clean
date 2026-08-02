"""Test-session setup, loaded by pytest before any test or app module.

Observability and the LLM are neutralized for the whole test run so the suite is
hermetic, free, and quiet:

  - Langfuse keys are blanked, so `trace.enabled()` is False and StubChat / the
    agent graph do not flush synthetic traces into the live Langfuse project.
  - The Anthropic key is blanked, so `get_narrator()` / `get_chat()` fall back to
    the deterministic stubs and no test can ever spend credits.

These are set at import time, before `app.config.load_dotenv()` runs. python-dotenv
loads with override=False, so it will not clobber a key that already exists in the
environment (even an empty one), and the real values in copilot/.env stay dormant
for tests while remaining available to the CLIs and the live agent.
"""
import os

_SILENCED = (
    "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST",
    "LANGFUSE_BASE_URL", "ANTHROPIC_API_KEY",
)

for _k in _SILENCED:
    os.environ[_k] = ""
