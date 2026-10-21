"""Direct routing without roster registration; optional presets and follow-up route stability."""
import json

from dataclasses import replace
from types import SimpleNamespace

import pytest


@pytest.fixture
def direct(rig):
    from hermes_teammates.teammates_resume import ResumeRunner
    from test_resume import FakeInternals
    rig.raw['teammates'] = {}
    rig.host.features = lambda: {'reasoning_effort': False, 'route': 'provider',
                                 'steer': 'lifecycle', 'follow_up': 'resume'}
    original = rig.lifecycle.launch
    def launch(request):
        return replace(original(request), provider=request.provider or 'fake', model=request.model or 'fake-model')
    rig.lifecycle.launch = launch
    rig.internals = FakeInternals()
    rig.host.resume_runner = ResumeRunner(rig.internals)
    rig.host.cache_ttl_seconds = lambda: 300
    rig.host.active_parent = lambda: SimpleNamespace(session_id='owner', _delegate_depth=0)
    rig.service.clock = lambda: 1000.0
    return rig


def finished(direct, model='unregistered-model', provider='anthropic', goal='original task', toolsets=('file',)):
    from test_v020 import result
    first = direct.service.assign('owner', model, goal, provider=provider,
                                  toolsets=list(toolsets) if toolsets is not None else None)
    assert first['ok'], first
    with direct.store() as store:
        store.update(first['run_id'], child_session_id='child-original')
    receipt = result(direct, first['run_id'])
    receipt.completed_at = 1000.0
    direct.service.finalize(first['run_id'], receipt)
    direct.lifecycle.state = 'RUNNING'
    return first['run_id']


def test_direct_run_resumes_without_any_preset(direct):
    run_id = finished(direct)
    reply = direct.service.followup('owner', run_id, 'fix it', wait_seconds=5)
    assert reply['ok'] and reply['followup_mode'] == 'resume', reply
    build, = direct.internals.builds
    assert build['model'] == 'unregistered-model' and build['provider'] == 'anthropic'
    assert build['launch']['allowed_toolsets'] == ['file']


def test_direct_digest_followup_keeps_the_original_model_and_provider(direct):
    run_id = finished(direct)
    direct.raw['followup_mode'] = 'digest'
    # Adding a same-name preset later must not silently replace the actual model/route.
    direct.raw['models'] = {'unregistered-model': {'model': 'different-model', 'route': 'openrouter'}}
    reply = direct.service.followup('owner', run_id, 'continue')
    assert reply['ok'] and reply['followup_mode'] == 'digest', reply
    request = direct.lifecycle.requests[-1]
    assert request.model == 'unregistered-model' and request.provider == 'anthropic'
    assert request.allowed_toolsets == ('file',)


def test_digest_context_is_scoped_to_the_run_chain_not_other_runs_of_the_same_model(direct):
    first = finished(direct, provider='anthropic', goal='FIRST_CHAIN_ONLY')
    finished(direct, provider='openrouter', goal='OTHER_ROUTE_NOT_THIS_CHAIN')
    direct.raw['followup_mode'] = 'digest'
    reply = direct.service.followup('owner', first, 'continue the first chain')
    assert reply['ok'], reply
    context = direct.lifecycle.requests[-1].context
    assert 'FIRST_CHAIN_ONLY' in context and 'OTHER_ROUTE_NOT_THIS_CHAIN' not in context


@pytest.mark.parametrize('field,value,code', [
    ('model', None, 'invalid_model'), ('model', [], 'invalid_model'), ('model', '', 'invalid_model'),
    ('model', ' m', 'invalid_model'), ('model', 'm\n', 'invalid_model'), ('model', 'm' * 513, 'invalid_model'),
    ('provider', [], 'invalid_provider'), ('provider', '', 'invalid_provider'),
    ('provider', 'anthropic\n', 'invalid_provider'), ('provider', 'https://example.com', 'invalid_provider'),
    ('provider', ' custom:local-(gpu)', 'invalid_provider'), ('provider', 'custom:local (gpu)', 'invalid_provider'),
    ('provider', 'custom:bad\tname', 'invalid_provider'), ('provider', 'custom:bad\x00name', 'invalid_provider'),
    ('provider', 'custom:bad\x7fname', 'invalid_provider'), ('provider', 'custom:bad\x80name', 'invalid_provider'),
    ('provider', 'p' * 65, 'invalid_provider'),
])
def test_invalid_route_arguments_fail_before_launch(rig, field, value, code):
    from hermes_teammates.teammates_tools import handler
    args = {'model': 'valid-model', 'provider': 'anthropic', 'goal': 'g', field: value}
    reply = json.loads(handler('subagent_spawn', lambda: rig.service)(args, session_id='owner'))
    assert reply.get('error') == code, reply
    assert not rig.lifecycle.requests


def test_explicit_provider_is_never_silently_ignored_on_an_unsupported_host(rig):
    rig.host.features = lambda: {'route': None}
    reply = rig.service.assign('owner', 'valid-model', 'g', provider='anthropic')
    assert reply.get('error') == 'unsupported_provider', reply
    assert not rig.lifecycle.requests


def test_spawn_accepts_an_exact_model_and_provider_with_an_empty_roster(rig):
    from hermes_teammates.teammates_tools import handler
    rig.raw['teammates'] = {}
    rig.host.features = lambda: {'reasoning_effort': False, 'route': 'provider',
                                 'steer': 'lifecycle', 'follow_up': 'digest'}
    spawn = handler('subagent_spawn', lambda: rig.service)
    reply = json.loads(spawn({'model': 'vendor/model-v1:latest', 'provider': 'openrouter', 'goal': 'summarize',
                              'toolsets': ['file']}, session_id='owner'))
    assert reply['ok'], reply
    request, = rig.lifecycle.requests
    assert request.model == 'vendor/model-v1:latest' and request.provider == 'openrouter'
    assert request.allowed_toolsets == ('file',)
    with rig.store() as store:
        row = store.get(reply['run_id'], 'owner')
    assert json.loads(row['launch_json'])['provider'] == 'openrouter'


def test_unregistered_model_without_provider_uses_hermes_default_routing(direct):
    reply = direct.service.assign('owner', 'org/model:latest', 'g')
    assert reply['ok'], reply
    request = direct.lifecycle.requests[-1]
    assert request.model == 'org/model:latest' and request.provider is None


def test_optional_preset_still_supplies_instructions_and_toolset_narrowing(direct):
    direct.raw['models'] = {'quick': {'model': 'chosen-model', 'route': 'anthropic',
                                    'instructions': 'Keep the answer short.', 'toolsets': ['file']}}
    reply = direct.service.assign('owner', 'quick', 'g')
    assert reply['ok'], reply
    request = direct.lifecycle.requests[-1]
    assert request.model == 'chosen-model' and request.provider == 'anthropic'
    assert request.context == 'Keep the answer short.' and request.allowed_toolsets == ('file',)
    refused = direct.service.assign('owner', 'quick', 'g', toolsets=['terminal'])
    assert refused['error'] == 'toolsets_not_allowed'


def test_explicit_provider_makes_model_an_exact_id_even_when_a_preset_has_that_name(direct):
    direct.raw['models'] = {'chosen-model': {'model': 'preset-target', 'route': 'openrouter'}}
    reply = direct.service.assign('owner', 'chosen-model', 'g', provider='anthropic')
    assert reply['ok'], reply
    request = direct.lifecycle.requests[-1]
    assert request.model == 'chosen-model' and request.provider == 'anthropic'


def test_removed_preset_does_not_lose_followup_route_or_original_instructions(direct):
    direct.raw['models'] = {'quick': {'model': 'chosen-model', 'route': 'anthropic',
                                    'instructions': 'ORIGINAL_BRIEF', 'toolsets': ['file']}}
    run_id = finished(direct, model='quick', provider=None)
    direct.raw['models'] = {}
    direct.raw['followup_mode'] = 'digest'
    reply = direct.service.followup('owner', run_id, 'continue')
    assert reply['ok'], reply
    request = direct.lifecycle.requests[-1]
    assert request.model == 'chosen-model' and request.provider == 'anthropic'
    assert 'ORIGINAL_BRIEF' in request.context and request.allowed_toolsets == ('file',)


def test_followup_ownership_is_still_enforced_without_a_roster(direct):
    run_id = finished(direct)
    assert direct.service.followup('other-owner', run_id, 'continue')['error'] == 'unknown_run'
    assert len(direct.lifecycle.requests) == 1 and direct.internals.builds == []


def test_catalog_failure_does_not_block_an_explicit_model_spawn(direct, monkeypatch):
    def broken(**kwargs):
        raise RuntimeError('catalog offline')
    monkeypatch.setattr(direct.host, 'discover_models', broken)
    reply = direct.service.assign('owner', 'not-in-any-catalog', 'g', provider='anthropic')
    assert reply['ok'], reply
    assert direct.lifecycle.requests[-1].model == 'not-in-any-catalog'


@pytest.mark.parametrize('original', [None, ['file', 'web']])
def test_followup_respects_a_later_toolset_narrowing_of_its_preset(direct, original):
    direct.raw['models'] = {'quick': {'model': 'chosen-model', 'route': 'anthropic', 'toolsets': original}}
    run_id = finished(direct, model='quick', provider=None, toolsets=None)
    direct.raw['models']['quick']['toolsets'] = ['file']
    reply = direct.service.followup('owner', run_id, 'continue')
    assert reply['ok'] and reply['followup_mode'] == 'digest', reply
    assert 'toolset' in reply['resume_skipped']
    assert direct.lifecycle.requests[-1].allowed_toolsets == ('file',)
    assert direct.internals.builds == []


@pytest.fixture
def native_custom_routes(direct, monkeypatch):
    """Resolve real native routes offline, but never construct or execute a model client."""
    import socket
    from agent.subagent_lifecycle import SubagentLaunchRequest
    from hermes_cli import config, runtime_provider, runtime_provider_backends
    from tools.delegate_tool_config import _resolve_delegation_credentials

    settings = {
        'model': {'provider': 'custom', 'default': 'local-model', 'base_url': 'http://127.0.0.1:18001/v1'},
        'delegation': {'base_url': 'http://127.0.0.1:18002/v1'},
    }
    monkeypatch.setattr(config, 'load_config', lambda: settings)
    monkeypatch.setattr(runtime_provider, 'load_pool', lambda *a, **k: None)
    monkeypatch.setattr(runtime_provider, 'get_secret_str', lambda name, default='': default)
    monkeypatch.setattr(runtime_provider_backends, 'get_secret_str', lambda name, default='': default)
    monkeypatch.setattr(runtime_provider, '_host_gated_env_key_candidates', lambda *a, **k: [])

    def no_network(*args, **kwargs):
        raise AssertionError('routing regression tests must not use the network')
    monkeypatch.setattr(socket.socket, 'connect', no_network)
    monkeypatch.setattr(socket.socket, 'connect_ex', no_network)

    routes = []
    original = direct.lifecycle.launch
    def launch(request):
        assert isinstance(request, SubagentLaunchRequest)
        routing = ({'provider': request.provider, 'model': request.model or ''}
                   if request.provider is not None else settings['delegation'])
        resolved = _resolve_delegation_credentials(routing, direct.host.active_parent())
        routes.append({'provider': resolved['provider'], 'base_url': resolved['base_url']})
        # Native custom child runtimes expose this generic label even for a named launch route.
        return replace(original(request), provider='custom', model=request.model)
    direct.lifecycle.launch = launch
    return SimpleNamespace(settings=settings, routes=routes)


@pytest.mark.parametrize('record', ['current', 'legacy_selection', 'legacy_launch'])
@pytest.mark.parametrize('mode', ['digest', 'auto', 'resume'])
def test_digest_refuses_an_unpinnable_implicit_custom_endpoint(direct, native_custom_routes, record, mode):
    run_id = finished(direct, model='local-model', provider=None)
    assert native_custom_routes.routes == [
        {'provider': 'custom', 'base_url': 'http://127.0.0.1:18002/v1'}]
    with direct.store() as store:
        row = store.get(run_id, 'owner')
        assert row['provider'] == 'custom'
        assert json.loads(row['launch_json']).get('provider') is None
        if record != 'current':
            store.update(run_id, selection_json=None,
                         **({'launch_json': None} if record == 'legacy_launch' else {}))
    direct.raw['followup_mode'] = mode
    direct.internals.transcript = None  # auto/resume must also refuse when their digest fallback is unpinnable

    reply = direct.service.followup('owner', run_id, 'continue')

    assert reply.get('error') == 'followup_route_unavailable', reply
    assert 'custom' in reply['message'] and 'explicit provider' in reply['message']
    assert len(direct.lifecycle.requests) == 1
    assert len(native_custom_routes.routes) == 1


@pytest.mark.parametrize('preset', [False, True])
def test_digest_keeps_an_explicit_generic_custom_launch_route(direct, native_custom_routes, preset):
    if preset:
        direct.raw['models'] = {'quick': {'model': 'local-model', 'route': 'custom'}}
    run_id = finished(direct, model='quick' if preset else 'local-model', provider=None if preset else 'custom')
    direct.raw['followup_mode'] = 'digest'

    reply = direct.service.followup('owner', run_id, 'continue')

    assert reply['ok'] and reply['followup_mode'] == 'digest', reply
    assert direct.lifecycle.requests[-1].provider == 'custom'
    assert native_custom_routes.routes == [
        {'provider': 'custom', 'base_url': 'http://127.0.0.1:18001/v1'},
        {'provider': 'custom', 'base_url': 'http://127.0.0.1:18001/v1'}]


@pytest.mark.parametrize('preset', [False, True])
@pytest.mark.parametrize('display_name', ['Local (GPU)', 'GPU+CPU', 'GPU/CPU', 'München', 'x' * 57])
def test_native_named_custom_provider_slug_can_spawn_and_followup(direct, native_custom_routes, preset, display_name):
    from hermes_cli.providers import custom_provider_slug
    provider = custom_provider_slug(display_name)
    assert len(provider) <= 64
    native_custom_routes.settings['custom_providers'] = [
        {'name': display_name, 'base_url': 'http://127.0.0.1:18003/v1'}]
    if preset:
        direct.raw['models'] = {'quick': {'model': 'local-model', 'route': provider}}
    run_id = finished(direct, model='quick' if preset else 'local-model', provider=None if preset else provider)
    with direct.store() as store:
        row = store.get(run_id, 'owner')
    assert row['provider'] == 'custom' and json.loads(row['launch_json'])['provider'] == provider
    direct.raw['models'] = {}
    direct.raw['followup_mode'] = 'digest'

    reply = direct.service.followup('owner', run_id, 'continue')

    assert reply['ok'] and reply['followup_mode'] == 'digest', reply
    assert direct.lifecycle.requests[-1].provider == provider
    assert native_custom_routes.routes == [
        {'provider': provider, 'base_url': 'http://127.0.0.1:18003/v1'},
        {'provider': provider, 'base_url': 'http://127.0.0.1:18003/v1'}]
