# AGENTS.md

Guidance for AI coding agents, and for people, working in this repository.

## What this is

hermes-subagents is a fork of [hermes-teammates](https://github.com/100yenadmin/hermes-teammates) 1.1.0. It gives
the agent subagents on a model it picks by name from an operator-defined roster, and follow-ups that resume the same
child conversation on the same model, so the provider's prompt cache is reused. Spawns go through the public
subagent lifecycle API; resumed follow-ups use Hermes internals, all in `teammates_resume.py`. A per-profile SQLite
ledger records runs. The upstream Kanban lane is kept but off by default.

## Layout

- `plugin.yaml`: plugin metadata, settings schema and provided tools/hooks.
- `__init__.py`: lazy tool, hook and slash-command registration.
- `teammates_tools.py`: the five `subagent_*` tool schemas, handlers and `/subagents` rendering.
- `teammates_service.py`: spawns, follow-ups (resume or digest), monitoring, results and Kanban handoff.
- `teammates_resume.py`: every Hermes-internal call resume needs (`HermesInternals`), the lifecycle-shaped
  `ResumeRunner`, and `RoutingLifecycle`, which sends resume handles to the runner.
- `teammates_host.py`: Hermes lifecycle, session and host integration.
- `teammates_config.py` and `teammates_store.py`: settings validation and SQLite ledger.
- `tests/`: isolated fixtures and regression tests (`test_resume.py`, `test_integration_resume.py` and
  `test_subagents_tools.py` cover the fork's features).
- `.github/workflows/ci.yml`: pinned (Hermes v0.22.2) and main validation and tests.

## Setup and checks

Tests run inside a real Hermes checkout, never against the live install:

```bash
git clone --depth 1 --branch v0.22.2 https://github.com/NousResearch/hermes-agent.git ../hermes-test-0.22.2
(cd ../hermes-test-0.22.2 && uv sync --frozen --no-dev && uv pip install --python .venv/bin/python pytest pytest-timeout)
scripts/test.sh                      # HERMES_TEST_TREE=<checkout> to use another Hermes tree
```

`scripts/hermes-python.sh scripts/live_e2e.py <model> <provider>` is the billed end-to-end check (spawn, two resumed
follow-ups and a digest control, with token receipts). It loads the plugin from the checkout, uses a temporary
session DB and ledger, and changes nothing in the user's Hermes config.

## Rules

- Keep Hermes-internal calls in `teammates_resume.HermesInternals`. Anything missing there must raise
  `ResumeUnavailable`, so a Hermes update degrades follow-ups to digests instead of breaking the plugin.
- After a Hermes upgrade, re-run the suite against the new tag and the billed end-to-end check before relying on resume.
- Keep upstream's module names and layout so `git merge upstream/main` stays practical.
- Never add self-updating code or runtime dependency/code downloads.
- Tests must use fake lifecycle fixtures and temporary storage; do not make billed model calls in tests.
- Get the user's approval before starting a paid run.
- Preserve profile/conversation ownership and toolset narrowing.
- Never commit secrets, tokens, real user data, or machine-specific paths.
- Tests use a temporary `HERMES_HOME`, never a real one.
