"""Billed live check: direct spawn with an EMPTY roster, two resumes, then a digest control.

Optional args: child_model child_provider parent_provider parent_model. The parent makes no inference calls.
Plugin settings and session/ledger DBs are temporary; the user's Hermes config is never changed.
"""
import importlib.util
import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
MODEL = sys.argv[1] if len(sys.argv) > 1 else 'claude-haiku-5-5'
PROVIDER = sys.argv[2] if len(sys.argv) > 2 else 'anthropic'
PARENT_PROVIDER = sys.argv[3] if len(sys.argv) > 3 else PROVIDER
PARENT_MODEL = sys.argv[4] if len(sys.argv) > 4 else MODEL
FILE = ROOT / 'teammates_config.py'
tmp = Path(tempfile.mkdtemp(prefix='hermes-subagents-e2e-'))

spec = importlib.util.spec_from_file_location('hermes_subagents', ROOT / '__init__.py',
                                              submodule_search_locations=[str(ROOT)])
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)

from hermes_cli.plugins import discover_plugins, get_plugin_manager  # noqa: E402
from hermes_cli.runtime_provider import resolve_runtime_provider  # noqa: E402
from hermes_state_registry import acquire, release_or_close  # noqa: E402
from run_agent import AIAgent  # noqa: E402
from agent.subagent_lifecycle import SubagentLifecycleService, bind_subagent_parent, get_active_subagent_parent  # noqa: E402

discover_plugins()
manager = get_plugin_manager()
settings = {'followup_mode': 'auto'}  # deliberately no models/teammates/roster_file
tools = {}
ctx = SimpleNamespace(
    get_config=lambda key, default=None: settings.get(key, default),
    register_tool=lambda **kw: tools.__setitem__(kw['name'], kw['handler']),
    register_command=lambda *a, **k: None,
    register_hook=lambda name, callback: manager._hooks.setdefault(name, []).append(callback),
    subagent_lifecycle=SubagentLifecycleService(get_active_subagent_parent),
)
plugin.register(ctx)
ledger = tmp / 'runs.db'
from hermes_subagents.teammates_store import Store  # noqa: E402
plugin.HermesHost.store_factory = staticmethod(lambda: Store(sqlite3.connect(ledger, timeout=10)))

db = acquire(tmp / 'state.db')
rt = resolve_runtime_provider(requested=PARENT_PROVIDER, target_model=PARENT_MODEL)
parent = AIAgent(base_url=rt.get('base_url'), api_key=rt.get('api_key'), provider=rt.get('provider'),
                 api_mode=rt.get('api_mode'), model=PARENT_MODEL, quiet_mode=True, skip_memory=True,
                 skip_context_files=True, enabled_toolsets=['file'], platform='cli',
                 credential_pool=rt.get('credential_pool'), session_db=db)
sid = parent.session_id
print(f'parent: provider={parent.provider} model={parent.model} (no inference calls)')
print(f'child: provider={PROVIDER} model={MODEL}; empty roster; temporary DBs in {tmp}')


def call(name, args):
    with bind_subagent_parent(parent):
        reply = json.loads(tools[name](args, session_id=sid, parent_agent=parent))
        if reply.get('timed_out') and reply.get('run_id'):
            tools['subagent_stop']({'run_id': reply['run_id'], 'reason': 'live verification deadline'},
                                    session_id=sid, parent_agent=parent)
    assert reply.get('ok') and reply.get('status') == 'succeeded', reply
    assert reply['provider'] == PROVIDER and reply['model'] == MODEL, reply
    return reply


def show(label, reply):
    keys = ('ok', 'run_id', 'status', 'provider', 'model', 'followup_mode', 'resume_skipped')
    print(f'{label}: ' + json.dumps({k: reply[k] for k in keys if k in reply}))
    if reply.get('summary'):
        print('   answer: ' + reply['summary'][:160].replace('\n', ' '))


t = time.time()
first = call('subagent_spawn', {'model': MODEL, 'provider': PROVIDER, 'toolsets': ['file'], 'wait_seconds': 180,
                                'goal': f'Read {FILE} with your file tools and list, in 5 bullets, the settings it '
                                        'validates and their allowed ranges.'})
show('spawn', first)
second = call('subagent_followup', {'run_id': first['run_id'], 'wait_seconds': 180,
                                   'message': 'Quote verbatim the source line(s) implementing the fallback when '
                                              'followup_context_chars is out of range. Use tools only if needed.'})
assert second['followup_mode'] == 'resume', second
show('follow-up 1', second)
third = call('subagent_followup', {'run_id': second['run_id'], 'wait_seconds': 180,
                                  'message': 'In one line: which setting has the widest allowed range?'})
assert third['followup_mode'] == 'resume', third
show('follow-up 2', third)
settings['followup_mode'] = 'digest'
fourth = call('subagent_followup', {'run_id': third['run_id'], 'wait_seconds': 180,
                                   'message': 'Quote verbatim the line that validates the lane setting. '
                                              'Use tools only if needed.'})
assert fourth['followup_mode'] == 'digest', fourth
show('follow-up 3 (digest control)', fourth)
print(f'elapsed {time.time() - t:.1f}s\n')

with Store(sqlite3.connect(ledger)) as store:
    rows = list(reversed(store.list(sid)))
assert len(rows) == 4, rows
assert all(row['status'] == 'succeeded' and row['model'] == MODEL and row['provider'] == PROVIDER for row in rows)
assert all((row['tokens_cache_read'] or 0) > 0 for row in rows if row['followup_mode'] == 'resume')
print(f"{'run':<16} {'mode':<8} {'status':<10} {'calls':>5} {'input':>7} {'cache_read':>10} {'cache_write':>11} "
      f"{'output':>7} {'read%':>6}")
for row in rows:
    read, write, plain = (row.get('tokens_cache_read') or 0), (row.get('tokens_cache_write') or 0), (row.get('tokens_input') or 0)
    total = read + write + plain
    print(f"{row['run_id']:<16} {row.get('followup_mode') or 'spawn':<8} {row['status']:<10} {row.get('api_calls') or 0:>5} "
          f"{plain:>7} {read:>10} {write:>11} {row.get('tokens_output') or 0:>7} {100 * read / total if total else 0:>5.1f}%")
parent.close()
release_or_close(db)
print('PASS: empty-roster direct routing, pinned follow-ups and cache reads verified.')
