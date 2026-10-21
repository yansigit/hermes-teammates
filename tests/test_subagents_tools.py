"""hermes-subagents tool surface: model-named subagents, follow-ups, Kanban off by default."""
import json
from types import SimpleNamespace

import pytest

TOOLS = {'subagent_models', 'subagent_spawn', 'subagent_followup', 'subagent_wait', 'subagent_steer', 'subagent_stop'}


def _register(plugin, raw):
    tools, commands = {}, {}

    class Context:
        def get_config(self, key, default=None):
            return raw.get(key, default)

        def register_tool(self, **kwargs):
            tools[kwargs['name']] = kwargs

        def register_command(self, name, **kwargs):
            commands[name] = kwargs

    plugin.register(Context())
    return tools, commands


def test_empty_roster_is_not_described_as_a_missing_requirement(plugin):
    tools, commands = _register(plugin, {})
    description = tools['subagent_spawn']['schema']['description']
    assert 'No roster required' in description and 'subagent_models' in description
    assert 'No models are configured yet' not in description
    assert 'provider' in tools['subagent_spawn']['schema']['parameters']['properties']
    text = commands['subagents']['handler']('')
    assert 'No preset required' in text and 'Add them under' not in text


def test_registers_six_subagent_tools_in_subagents_toolset(plugin):
    tools, commands = _register(plugin, {'models': {'flash': {'description': 'cheap and fast'}}})
    assert set(tools) == TOOLS
    assert {spec['toolset'] for spec in tools.values()} == {'subagents'}
    assert set(commands) == {'subagents'}


def test_spawn_schema_takes_a_model_name_and_lists_the_roster(plugin):
    tools, _ = _register(plugin, {'models': {'flash': {'description': 'cheap and fast'}, 'opus': {}}})
    schema = tools['subagent_spawn']['schema']
    properties = schema['parameters']['properties']
    assert schema['parameters']['required'] == ['model', 'goal']
    assert properties['model']['type'] == 'string' and 'enum' not in properties['model']
    assert 'flash (cheap and fast)' in schema['description'] and 'opus' in schema['description']
    assert properties['toolsets']['type'] == 'array'


def test_kanban_parameter_only_when_kanban_enabled(plugin):
    tools, _ = _register(plugin, {'models': {'flash': {}}})
    assert 'kanban_task' not in tools['subagent_spawn']['schema']['parameters']['properties']
    tools, _ = _register(plugin, {'models': {'flash': {}}, 'kanban': True})
    assert 'kanban_task' in tools['subagent_spawn']['schema']['parameters']['properties']


def test_spawn_handler_launches_the_named_model_and_reports_subagent(rig):
    from hermes_teammates.teammates_tools import handler
    reply = json.loads(handler('subagent_spawn', lambda: rig.service)(
        {'model': 'worker', 'goal': 'summarize'}, session_id='owner'))
    assert reply['ok'] and reply['subagent'] == 'worker' and 'teammate' not in reply
    assert rig.lifecycle.requests[-1].goal == 'summarize'


def test_spawn_toolsets_narrow_within_the_roster(rig):
    from hermes_teammates.teammates_tools import handler
    rig.raw['teammates']['worker']['toolsets'] = ['file', 'web']
    spawn = handler('subagent_spawn', lambda: rig.service)
    reply = json.loads(spawn({'model': 'worker', 'goal': 'g', 'toolsets': ['file']}, session_id='owner'))
    assert reply['ok'], reply
    assert rig.lifecycle.requests[-1].allowed_toolsets == ('file',)
    refused = json.loads(spawn({'model': 'worker', 'goal': 'g', 'toolsets': ['terminal']}, session_id='owner'))
    assert refused['error'] == 'toolsets_not_allowed'


def test_spawn_refuses_kanban_task_while_kanban_is_off(rig):
    from hermes_teammates.teammates_tools import handler
    rig.raw['kanban'] = False
    reply = json.loads(handler('subagent_spawn', lambda: rig.service)(
        {'model': 'worker', 'goal': 'g', 'kanban_task': 't_1'}, session_id='owner'))
    assert reply['error'] == 'kanban_disabled'


def test_wait_without_run_ids_lists_recent_runs(rig):
    from hermes_teammates.teammates_tools import handler
    spawned = json.loads(handler('subagent_spawn', lambda: rig.service)(
        {'model': 'worker', 'goal': 'g'}, session_id='owner'))
    listed = json.loads(handler('subagent_wait', lambda: rig.service)({}, session_id='owner'))
    assert listed['ok'] and [run['run_id'] for run in listed['runs']] == [spawned['run_id']]
    assert listed['runs'][0]['subagent'] == 'worker'


def test_followup_handler_forwards_to_service():
    from hermes_teammates.teammates_tools import handler
    calls = []
    service = SimpleNamespace(followup=lambda *args, **kwargs: calls.append((args, kwargs)) or {'ok': True})
    parent = SimpleNamespace(session_id='owner')
    reply = json.loads(handler('subagent_followup', lambda: service)(
        {'run_id': 'tm_1', 'message': 'now fix it', 'wait_seconds': 5}, parent_agent=parent))
    assert reply == {'ok': True}
    (args, kwargs), = calls
    assert args == ('owner', 'tm_1', 'now fix it') and kwargs['wait_seconds'] == 5
    assert kwargs['parent_agent'] is parent


@pytest.mark.parametrize('name', sorted(TOOLS))
def test_every_handler_returns_json_on_internal_error(name):
    from hermes_teammates.teammates_tools import handler
    def broken():
        raise RuntimeError('oops')
    assert json.loads(handler(name, broken)({'run_id': 'x', 'model': 'm', 'goal': 'g'}))['error'] == 'internal_error'
