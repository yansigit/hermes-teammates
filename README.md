# hermes-subagents

Subagents for [Hermes Agent](https://github.com/NousResearch/hermes-agent) on any model/provider your Hermes can
access, with follow-ups that resume the same child conversation and reuse its prompt cache. No roster registration
is required. Named presets remain optional for instructions, descriptions and toolset preferences.

A fork of [hermes-teammates](https://github.com/100yenadmin/hermes-teammates) 1.1.0 (MIT). Spawning, waiting,
steering, stopping and the run ledger build on upstream; this fork adds direct model/provider selection,
on-demand discovery and cache-preserving follow-ups.

## Tools

| Tool | What it does |
|---|---|
| `subagent_models(provider?, query?, limit?, offset?)` | Discover Hermes providers, search model IDs and get brief catalog metadata when known. No inference. |
| `subagent_spawn(model, goal, provider?, context?, toolsets?, wait_seconds?)` | Start a subagent with an exact model ID and optional provider, or an optional named preset. |
| `subagent_followup(run_id, message, wait_seconds?)` | Continue a finished run on its recorded model/provider. Returns a new `run_id`. |
| `subagent_wait(run_ids?, mode?, wait_seconds?)` | Wait for results; `wait_seconds: 0` checks; no `run_ids` lists recent runs. |
| `subagent_steer(run_id, message)` | Course-correct a running subagent. |
| `subagent_stop(run_id, reason?)` | Cancel a running subagent. |

`/subagents` shows optional presets, direct-routing guidance, settings and host capabilities. Model-selection
preferences can live in your prompt or `AGENTS.md`; they do not need to be duplicated in a roster.

## Use without configuration

Authenticate/configure providers in Hermes normally. Then the agent can call `subagent_spawn` with arguments like:

```json
{"model":"claude-haiku-5-5","provider":"anthropic","goal":"Read the specified file and summarize it.","toolsets":["file"]}
```

- With `provider`, `model` is always an exact model ID, even if an optional preset has the same name.
- Without `provider`, a matching preset is used; otherwise the exact model ID uses normal Hermes delegation/parent
  routing. Explicit model + provider is least ambiguous.
- There is no catalog-membership gate on spawning. A new model can work before discovery learns its ID. Hermes
  resolves credentials and the actual provider decides whether a model is available and supports agent tools.
- No tool argument can supply an API key, base URL or arbitrary endpoint. Use configured Hermes provider IDs.
- `toolsets` narrows the parent's tools. A preset can narrow further when that preset is used; direct spawns are
  not restricted by unrelated presets. Global policy belongs in Hermes, not in an optional preset.

For discovery, call `subagent_models` with `{}` for providers, `{"provider":"anthropic"}` for that provider's
models, or `{"query":"haiku"}` to search IDs across providers. Responses are deduplicated, sorted and bounded to
16,000 characters, with exact `total`, `has_more` and `next_offset` values. `limit` defaults to 25 (maximum 100).

Discovery reuses Hermes's authenticated-provider inventory and cached/curated catalogs. It does not probe every
custom endpoint synchronously or dump all models into the system prompt. Native caches may refresh in the
background. Context windows and tool/vision/reasoning flags are included only when Hermes has metadata; missing
metadata and catalog entries are not proof that a model is unavailable. Catalogs are not an access or billing
entitlement guarantee. Spawning remains independent of discovery outages.

## Install

Requires Hermes **v0.22.2** or newer; private resume seams are tested against v0.22.2 and CI also checks main.
The fork's work is on `feat/model-subagents`, not its default branch. Install an immutable commit from that branch:

```bash
COMMIT=$(gh api repos/yansigit/hermes-teammates/commits/feat/model-subagents --jq .sha)
hermes plugins install yansigit/hermes-teammates --ref "$COMMIT" --enable
```

The plugin installs as `hermes-subagents`. Start a new session (`/reset`) so its tools appear. To update an existing
installation to another reviewed commit, use the same command with `--force`.

## Optional presets and settings

No plugin configuration is needed for direct model/provider spawning. To add a convenience preset:

```bash
hermes config set plugins.entries.hermes-subagents.settings.models.haiku '{"model":"claude-haiku-5-5","route":"anthropic","description":"Fast worker for file reading, summaries and small edits."}'
```

Then `subagent_spawn` may use `{"model":"haiku","goal":"..."}` without `provider`. The preset's `route` field
is the provider; the direct-spawn argument is named `provider`. Preset names start with a lowercase letter and may
contain lowercase letters, digits, `-` and `_`, up to 32 characters. Exact model IDs are not subject to that naming
rule and can include `/`, `:`, `.` and longer IDs.

Optional preset fields: `model`, `route`, `description`, `instructions` (standing brief), `toolsets` (narrowing) and
`reasoning_effort` (reported unsupported if the host cannot honor it). Upstream's `teammates` key is still read when
`models` is unset. `roster_file` can hold optional presets separately (see `teammates.example.yaml`). A malformed
preset refuses use of that preset; it does not block unrelated direct model/provider calls.

Other settings: `max_live_runs` (default 4 per conversation), `followup_context_chars` (digest budget, default 8000),
and the upstream Kanban lane (`kanban: true`, off by default; then `lane` and `claim_ttl_seconds`). Settings live under
`plugins.entries.hermes-subagents.settings`.

## Follow-ups and caching

A follow-up such as "now fix what you found" usually needs what the child already read. Resuming sends the prefix
already cached by the provider, rather than starting a new child that must re-read files.

`followup_mode` controls when the plugin resumes:

- `auto` (default): resume while the child's prompt cache is likely warm; otherwise send a digest to a fresh child.
  Hermes clamps delegated children to the 5-minute cache tier, so "warm" means the run finished less than 300 s ago.
  With prompt caching off, `auto` always uses a digest.
- `resume`: always resume when possible, even after cache expiry. A cold resume pays cache-write cost again but
  keeps the earlier conversation.
- `digest`: always start a fresh child with a bounded digest of this run's ancestor results.

Both routes keep the recorded model/provider. Preset edits/removal cannot redirect a follow-up, and digest context
is scoped to the requested run's chain, not unrelated jobs that happen to use the same model. The initial preset
brief is retained and toolset narrowing is preserved (including a later narrowing of a used preset).

If an implicit route only recorded Hermes's generic `custom` runtime label (for example, an unnamed
`delegation.base_url` endpoint), a digest refuses with `followup_route_unavailable` rather than reinterpret that
label as the main custom endpoint. This also applies to legacy runs without an explicit launch provider record.
Start a new run with a configured named provider ID to make its route reproducible. An explicitly launched
`provider: custom` remains supported and intentionally uses Hermes's main custom route.

If a follow-up cannot resume, it falls back to a digest and explains why in `resume_skipped`: for example, unavailable
Hermes internals/transcript, a provider resolving to a different model, or a child that rotated to another session
when compressing context. In-place compression keeps current session data and remains resumable. Replies carry
`followup_mode` and `parent_run_id`; finished runs carry input, output and cache token receipts when Hermes supplies
them. The provider/endpoint configuration itself must remain consistent for cache reuse.

Historical measurement on Hermes v0.22.2 with `claude-haiku-5-5` via Anthropic (one run per row, not a benchmark):

| Run | Mode | API calls | Cache read | Cache write | Read share of input |
|---|---|---:|---:|---:|---:|
| spawn | | 2 | 4,010 | 7,020 | 36% |
| follow-up 1 | resume (session DB) | 1 | 7,020 | 463 | 94% |
| follow-up 2 | resume (memory) | 1 | 7,483 | 339 | 96% |
| follow-up 3 | digest control | 2 | 7,874 | 1,912 | 80% |

## Limits and verification

- **Internal APIs.** Resume uses private Hermes builder/runner/session seams isolated in `teammates_resume.py`.
  Discovery's inventory adapter is in `teammates_models.py`. Missing discovery seams return `catalog_unavailable`
  without blocking direct spawns; missing resume seams degrade to digest. Re-run checks after upgrading Hermes.
- **Retention.** First follow-ups read Hermes's own session DB. Resumed chains retain conversations in process
  memory (64 records plus live/recently finished runs). After restart or eviction, chained follow-ups use digests.
- **In-process plugins only** for spawning/resuming (`plugins.isolation: in_process`).
- **Costs.** No model allowlist or price ceiling is enforced by this plugin. Choosing a provider/model authorizes
  its normal inference charges; use prompts/preferences and Hermes policy to control usage. Presets are not a
  security boundary.
- **Results.** Get completed results with `subagent_wait`; session-bound push notices are not supported by the host.

```bash
scripts/test.sh
# Billed: direct spawn (empty roster), two resumed follow-ups, one digest control:
scripts/hermes-python.sh scripts/live_e2e.py claude-haiku-5-5 anthropic
# Same child route, with an unbilled parent on a different provider/model:
scripts/hermes-python.sh scripts/live_e2e.py claude-haiku-5-5 anthropic openai-codex gpt-6.1-sol
```

The live check loads the plugin from the checkout with temporary session/ledger databases and never changes your
Hermes configuration. It asserts success, route stability, resume modes and nonzero cache reads. Test setup is in
`AGENTS.md`.

## License

MIT, as upstream. Original notices are preserved in `LICENSE`.
