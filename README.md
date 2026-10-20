# hermes-subagents

Subagents for [Hermes Agent](https://github.com/NousResearch/hermes-agent) that run on a model your agent picks by
name, with follow-ups that resume the same child conversation on the same model, so the provider's prompt cache is
reused instead of paid for again.

A fork of [hermes-teammates](https://github.com/100yenadmin/hermes-teammates) 1.1.0 (MIT). Spawning, waiting,
steering, stopping and the run ledger are upstream's; this fork adds model-named spawns, cache-preserving
follow-ups and a smaller tool surface.

## What it gives your agent

| Tool | What it does |
|---|---|
| `subagent_spawn(model, goal, context?, toolsets?, wait_seconds?)` | Start a subagent on a roster model, chosen by name. `toolsets` can only narrow. |
| `subagent_followup(run_id, message, wait_seconds?)` | Continue a finished run: resumes the same child conversation and model. Returns a new `run_id`. |
| `subagent_wait(run_ids?, mode?, wait_seconds?)` | Wait for runs and get results; `wait_seconds: 0` just checks; no `run_ids` lists recent runs. |
| `subagent_steer(run_id, message)` | Course-correct a running subagent. |
| `subagent_stop(run_id, reason?)` | Cancel a running subagent. |

`/subagents` shows the roster, settings and what your Hermes build supports.

The agent only ever picks a name from your roster. You decide which model and provider each name means, so a
model cannot route work to anything you have not listed.

## Why resume matters

A follow-up such as "now fix what you found" usually needs what the subagent already read. Starting a fresh child
with a summary means it re-reads files and re-sends everything uncached. Resuming sends the same prefix the provider
has just cached.

Measured on Hermes v0.22.2 with `claude-haiku-5-5` via Anthropic (a child reads a 130-line file, then three
follow-ups):

| Run | Mode | API calls | Cache read | Cache write | Read share of input |
|---|---|---:|---:|---:|---:|
| spawn | | 2 | 4,010 | 7,020 | 36% |
| follow-up 1 | resume (from the session DB) | 1 | 7,020 | 463 | 94% |
| follow-up 2 | resume (from memory) | 1 | 7,483 | 339 | 96% |
| follow-up 3 | digest (control) | 2 | 7,874 | 1,912 | 80% |

Follow-up 1 read back exactly what the spawn had written, so the restored transcript is byte-identical to what the
provider saw. The digest control re-read the file (2 calls) and wrote about 4x more new cache. One run per row, so
treat these as a direction, not a benchmark.

## Install

Requires Hermes **v0.22.2** (resume uses Hermes internals verified on that version; see Limits).

```bash
hermes plugins install yansigit/hermes-teammates --enable
```

The plugin installs as `hermes-subagents`. Start a new session so the tools appear.

## Configure

Add a roster to `config.yaml`, or keep it in its own file with `roster_file` (see `teammates.example.yaml`):

```yaml
plugins:
  entries:
    hermes-subagents:
      settings:
        models:
          haiku:
            model: claude-haiku-5-5
            route: anthropic            # a configured Hermes provider
            description: Cheap and fast; file reading, summaries, small edits.
          sonnet:
            model: claude-sonnet-5-5
            route: anthropic
            description: Mid-tier; multi-file changes and reviews.
        followup_mode: auto             # auto | resume | digest
```

Per entry, everything is optional: `model` and `route` (set both, or neither to use your `delegation` settings),
`description` (what the agent sees when picking), `instructions` (a standing brief), `toolsets` (narrows the
parent's) and `reasoning_effort`. Upstream's `teammates` key is still read when `models` is unset.

Other settings: `max_live_runs` (default 4 per conversation), `followup_context_chars` (digest budget, default
8000), `roster_file`, and the upstream Kanban lane (`kanban: true`, off by default; then `lane` and
`claim_ttl_seconds`).

## Follow-up modes

- `auto` (default): resume while the child's prompt cache is still warm, otherwise send a digest of earlier results
  to a fresh child. Hermes clamps every delegated child to the 5-minute cache tier, so "warm" means the run finished
  less than 300 s ago. With prompt caching off, `auto` always sends a digest.
- `resume`: always resume when possible. After the cache expires this re-sends the whole transcript at cache-write
  price, but the child keeps its full context.
- `digest`: never resume (upstream's behavior).

If a follow-up cannot resume, it falls back to a digest and says why in `resume_skipped`. Replies carry
`followup_mode` and `parent_run_id`; finished rows carry `tokens_input`, `tokens_output`, `tokens_cache_read` and
`tokens_cache_write`.

## Limits

- **Resume uses Hermes internals.** The public subagent API drops a child when it finishes, so resume rebuilds it
  with private functions (`tools.delegate_tool._build_child_preserving_parent_tools`, `_run_child_lifecycle`,
  `_resolve_delegation_credentials`, `hermes_state_registry`). All of them are in `teammates_resume.py`. If a Hermes
  update changes them, follow-ups fall back to digests instead of failing; re-run the checks below after upgrading.
  Upstream Hermes is working on a native follow-up (NousResearch/hermes-agent#80488, #133212); once it ships, this
  part can switch to it.
- **Same session only.** The first follow-up reads the child's transcript from the session DB; later follow-ups in
  a chain keep the conversation in memory (up to 64 per process). After a Hermes restart, chained follow-ups fall
  back to digests.
- **In-process plugins only** (`plugins.isolation: in_process`, the default), as upstream.
- The agent can pick any roster model; costs are bounded by what you put in the roster.

## Verifying resume

```bash
scripts/test.sh                                                     # the suite, against a Hermes v0.22.2 checkout
scripts/hermes-python.sh scripts/live_e2e.py claude-haiku-5-5 anthropic   # billed: spawn, 2 resumes, 1 digest
```

The end-to-end script loads the plugin from this checkout and uses a temporary session DB and ledger; it changes
nothing in your Hermes config. Test setup is in AGENTS.md.

## License

MIT, as upstream. The original copyright notice is kept in `LICENSE`.
