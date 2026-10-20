"""Model-facing schemas and total JSON handlers."""
import json

from .teammates_host import current_session_id
from .teammates_service import RELAY_INSTRUCTION, error


def _p(kind, description, **extra):
    return dict(type=kind, description=description, **extra)


_RUN_ID = _p('string', 'The run_id returned by subagent_spawn or subagent_followup.')
_WAIT = _p('number', 'Optional: wait up to this many seconds for the run to finish and return its result in '
                     'this reply (0 = return at once). If it is still running at the deadline, the reply says '
                     'timed_out; call subagent_wait later.', minimum=0, maximum=300, default=0)
_KANBAN_TASK = _p('string', 'Id of a Kanban card on the subagents lane (for example t_1234abcd). The card is claimed '
                            'while the subagent works and moved to review (or blocked) when the run ends.')

SPECS = {
    'subagent_spawn': (
        'Start a subagent on a model you pick by name, for a self-contained task. Pick the cheapest model that '
        'can do the task well; keep strong models for hard work. Returns a run_id at once while the subagent '
        'works in the background; get the result with subagent_wait, or pass wait_seconds.',
        {'model': _p('string', 'Model name, from the list in this description.'),
         'goal': _p('string', 'The complete task. The subagent does not see this conversation, so include what it needs.'),
         'context': _p('string', 'Optional extra facts, file paths or constraints.'),
         'toolsets': _p('array', 'Optional: limit the subagent to these toolsets (for example ["file"]). Fewer tools '
                                 'cost less. Cannot add toolsets the model entry does not allow.',
                        items={'type': 'string', 'minLength': 1}, minItems=1, maxItems=24, uniqueItems=True),
         'wait_seconds': _WAIT},
        ['model', 'goal']),
    'subagent_followup': (
        'Continue a finished subagent run with a new message, for example a fix after its first answer. The '
        'subagent resumes its earlier conversation on the same model, so it keeps what it already read and, '
        'soon after the run, reuses the prompt cache. Returns a new run_id. For a run still working, use '
        'subagent_steer.',
        {'run_id': _p('string', 'The run to continue; pass the latest run_id of that subagent.'),
         'message': _p('string', 'What to do next, at most 12000 characters.'),
         'wait_seconds': _WAIT},
        ['run_id', 'message']),
    'subagent_wait': (
        'Wait 0-300 s (default 30) for any or all of the given runs; returns finished results and pending '
        'statuses at the deadline, or promptly on a Hermes interrupt. wait_seconds 0 only checks status. '
        "Without run_ids, lists this conversation's recent runs.",
        {'run_ids': _p('array', 'Run IDs to wait for.', items={'type': 'string', 'minLength': 1}, minItems=1,
                       maxItems=16, uniqueItems=True),
         'mode': _p('string', 'Completion rule.', enum=['any', 'all'], default='any'),
         'wait_seconds': _p('number', 'Seconds to wait, 0-300.', minimum=0, maximum=300, default=30)},
        []),
    'subagent_steer': (
        'Send a short course correction to a running subagent. It arrives at its next step without stopping it.',
        {'run_id': _RUN_ID, 'message': _p('string', 'The correction, at most 4000 characters.')},
        ['run_id', 'message']),
    'subagent_stop': (
        'Cancel a running subagent.',
        {'run_id': _RUN_ID, 'reason': _p('string', 'Why it is being stopped.')},
        ['run_id']),
}

# Hermes catalogs only the first sentence (at most 60 characters) of deferred tools. Keep the
# conditional bridge flow there, and a valid single-call envelope in the schema.
_BRIDGE_EXAMPLES = {
    'subagent_spawn': ('Start a subagent', {'model': 'MODEL_NAME', 'goal': 'Summarize the requested file.'}),
    'subagent_followup': ('Continue a run', {'run_id': 'RUN_ID', 'message': 'Now apply the fix.'}),
    'subagent_wait': ('Wait for runs', {'run_ids': ['RUN_ID']}),
}
_RESULT_TOOLS = ('subagent_spawn', 'subagent_followup', 'subagent_wait')


def _roster_text(models):
    entries = []
    for entry in models:
        name, description = (entry, '') if isinstance(entry, str) else entry
        entries.append(f'{name} ({description})' if description else name)
    return ' Models: ' + '; '.join(entries) + '.' if entries else ' No models are configured yet.'


def schema(name, models=(), *, kanban=False):
    """``models``: names, or (name, description) pairs, from the roster at startup."""
    description, properties, required = SPECS[name]
    properties = dict(properties)
    if name in _RESULT_TOOLS:
        description += ' ' + RELAY_INSTRUCTION + ' Read relay_instruction and summary_clipped before relaying summary.'
    if name in _BRIDGE_EXAMPLES:
        action, arguments = _BRIDGE_EXAMPLES[name]
        envelope = json.dumps({'calls': [{'name': name, 'arguments': arguments}]}, separators=(',', ':'))
        description = (f'{action}; if deferred: tool_describe then tool_call. '
                       f'If deferred, invoke with tool_call: {envelope}. '
                       'Replace example values; use one entry per call. When directly listed, call normally. '
                       + description)
    if name == 'subagent_spawn':
        description += _roster_text(models) + ' Models added later also work by name.'
        if kanban:
            properties['kanban_task'] = _KANBAN_TASK
    return {'name': name, 'description': description,
            'parameters': {'type': 'object', 'properties': properties, 'required': required}}


def public_names(value):
    """Model-facing replies call a roster entry a 'subagent'; the ledger keeps upstream's 'teammate'."""
    if isinstance(value, dict):
        return {('subagent' if key == 'teammate' else key): public_names(item) for key, item in value.items()}
    if isinstance(value, list):
        return [public_names(item) for item in value]
    return value


def handler(name, get_service):
    def h(args: dict, **kwargs) -> str:
        try:
            service, session_id = get_service(), current_session_id(kwargs)
            parent_agent = kwargs.get('parent_agent')
            if name == 'subagent_spawn':
                value = service.assign(session_id, args.get('model'), args.get('goal'),
                                       context=args.get('context'), kanban_task=args.get('kanban_task'),
                                       parent_agent=parent_agent, wait_seconds=args.get('wait_seconds', 0),
                                       toolsets=args.get('toolsets'))
            elif name == 'subagent_followup':
                value = service.followup(session_id, args.get('run_id'), args.get('message'),
                                         wait_seconds=args.get('wait_seconds', 0), parent_agent=parent_agent)
            elif name == 'subagent_wait':
                if args.get('run_ids') is None:
                    value = service.list_runs(session_id)
                else:
                    value = service.wait(session_id, args.get('run_ids'), args.get('mode', 'any'),
                                         args.get('wait_seconds', 30))
            elif name == 'subagent_steer':
                value = service.message(session_id, args.get('run_id'), args.get('message'),
                                        parent_agent=parent_agent)
            else:
                value = service.stop(session_id, args.get('run_id'), args.get('reason', ''))
            return json.dumps(public_names(value), ensure_ascii=False)
        except Exception as exc:
            return json.dumps(error('internal_error', str(exc)[:2000]), ensure_ascii=False)
    return h


def render_roster(value):
    """Plain-text roster for the /subagents slash command."""
    if not value.get('ok'):
        return f"hermes-subagents: {value.get('message') or value.get('error')}"
    lines, teammates = [], value.get('teammates') or []
    if value.get('roster_file'):
        lines.append(f"Roster file: {value['roster_file']}")
    if teammates:
        lines.append(f'Models ({len(teammates)}):')
    elif value.get('roster_file'):
        lines.append('No models in the roster file.' +
                     (' See the config problems below.' if value.get('config_errors') else ''))
    else:
        lines.append('No models configured. Add them under '
                     'plugins.entries.hermes-subagents.settings.models in config.yaml.')
    for teammate in teammates:
        toolsets = ', '.join(teammate.get('toolsets') or []) or 'inherits parent'
        route = ' via '.join(part for part in (teammate.get('model'), teammate.get('route')) if part) or 'parent model'
        lines.append(f"  {teammate['name']} - {route}; {teammate.get('description') or '(no description)'} "
                     f"[toolsets: {toolsets}]")
        if teammate.get('unsupported'):
            lines.append(f"    not supported by this Hermes build: {', '.join(teammate['unsupported'])}")
    lane = value.get('lane') or {}
    if not lane.get('enabled'):
        lines.append('Kanban lane: off (set kanban: true to enable)')
    elif lane.get('available'):
        tasks = lane.get('open_tasks') or []
        lines.append(f"Kanban lane '{lane.get('name')}': {len(tasks)} open card(s)" +
                     (' - WARNING: this lane name is also a profile, so lane work is refused' if lane.get('is_profile') else ''))
        lines.extend(f"  {task['id']} [{task['status']}] {task['title']}" for task in tasks[:10])
    else:
        lines.append('Kanban lane: unavailable in this process')
    features = value.get('features') or {}
    lines.append(f"This Hermes build: steer={features.get('steer', 'no')}, follow_up={features.get('follow_up', 'digest')}, " +
                 ', '.join(f"{key}={'yes' if features.get(key) else 'no'}"
                           for key in ('reasoning_effort', 'route', 'token_receipts')))
    if value.get('followup_mode'):
        lines.append(f"Follow-up mode: {value['followup_mode']}")
    if value.get('config_errors'):
        lines.append('Config problems:')
        lines.extend(f'  {problem}' for problem in value['config_errors'])
    push = features.get('push_completion', {})
    lines.append(f"subagent_wait=yes; push completion={push.get('status')} ({push.get('reason')})")
    lines.append('Results: use subagent_wait for summaries. ' + RELAY_INSTRUCTION +
                 ' If summary_clipped is true, report the text as incomplete.')
    return '\n'.join(lines)
