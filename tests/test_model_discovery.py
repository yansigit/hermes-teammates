"""On-demand discovery uses Hermes's inventory, never exposes provider credentials."""
import json
from types import SimpleNamespace

import pytest


@pytest.fixture
def catalog(monkeypatch):
    from hermes_cli import inventory, model_switch
    from agent import models_dev
    rows = [
        {'slug': 'anthropic', 'name': 'Anthropic', 'models': ['claude-haiku-5-5', 'claude-sonnet-5-5'],
         'api_key': 'DO_NOT_RETURN_THIS', 'api_url': 'https://private.endpoint'},
        {'slug': 'custom:local', 'name': 'Local', 'models': ['org/small-v1', 'org/small-v1'],
         'extra_headers': {'Authorization': 'DO_NOT_RETURN_THIS'}},
    ]
    calls = []
    monkeypatch.setattr(inventory, 'load_picker_context', lambda: SimpleNamespace(
        current_provider='anthropic', current_model='claude-haiku-5-5', current_base_url='',
        user_providers={}, custom_providers=[], excluded_providers=[]))
    def listing(**kwargs):
        calls.append(kwargs)
        return rows
    monkeypatch.setattr(model_switch, 'list_authenticated_providers', listing)
    monkeypatch.setattr(models_dev, 'get_model_info', lambda *a, **k: None)
    return rows, calls


def test_discovery_handler_lists_configured_providers_without_secrets(rig, catalog):
    from hermes_teammates.teammates_tools import handler
    reply = json.loads(handler('subagent_models', lambda: rig.service)({}, session_id='owner'))
    assert reply['ok'], reply
    assert [p['provider'] for p in reply['providers']] == ['anthropic', 'custom:local']
    assert [p['model_count'] for p in reply['providers']] == [2, 1]
    assert reply['total'] == 2 and reply['has_more'] is False
    assert 'DO_NOT_RETURN_THIS' not in json.dumps(reply) and 'private.endpoint' not in json.dumps(reply)
    _, calls = catalog
    assert calls[0]['non_blocking_catalogs'] is True and calls[0]['probe_custom_providers'] is False
    assert not rig.lifecycle.requests


@pytest.mark.parametrize('args,expected,total', [
    ({'provider': 'anthropic', 'limit': 1}, ['claude-haiku-5-5'], 2),
    ({'provider': 'anthropic', 'offset': 1, 'limit': 1}, ['claude-sonnet-5-5'], 2),
    ({'query': 'small'}, ['org/small-v1'], 1),
    ({'provider': 'anthropic', 'query': 'haiku'}, ['claude-haiku-5-5'], 1),
    ({'query': 'nothing-matches'}, [], 0),
])
def test_model_search_is_filtered_deduplicated_and_paginated(rig, catalog, args, expected, total):
    from hermes_teammates.teammates_tools import handler
    reply = json.loads(handler('subagent_models', lambda: rig.service)(args, session_id='owner'))
    assert reply['ok'], reply
    assert [m['model'] for m in reply['models']] == expected
    assert reply['total'] == total
    assert reply['has_more'] == (reply.get('next_offset') is not None)
    if reply['has_more']:
        assert reply['next_offset'] == reply['offset'] + len(reply['models'])


@pytest.mark.parametrize('args', [
    {'provider': []}, {'provider': ''}, {'provider': 'https://example.com'},
    {'provider': ' custom:local-(gpu)'}, {'provider': 'custom:local (gpu)'},
    {'provider': 'custom:bad\tname'}, {'provider': 'custom:bad\x00name'},
    {'provider': 'custom:bad\x7fname'}, {'provider': 'custom:bad\x80name'}, {'provider': 'p' * 65},
    {'query': []}, {'query': 'x' * 201}, {'limit': 0}, {'limit': 101}, {'limit': True},
    {'offset': -1}, {'offset': 0.5}, {'offset': False},
])
def test_discovery_validates_before_reading_the_inventory(rig, catalog, args):
    reply = rig.service.models(**args)
    assert reply.get('error') == 'invalid_discovery', reply
    assert catalog[1] == []


def test_native_discovery_failure_never_echoes_credentials(rig, monkeypatch):
    from hermes_cli import inventory
    def broken():
        raise RuntimeError('DO_NOT_RETURN_THIS')
    monkeypatch.setattr(inventory, 'load_picker_context', broken)
    reply = rig.service.models()
    assert reply.get('error') == 'catalog_unavailable'
    assert 'DO_NOT_RETURN_THIS' not in json.dumps(reply)


def test_discovery_output_is_bounded_but_pagination_counts_are_exact(rig, catalog):
    rows, _ = catalog
    rows[0]['models'] = [f'model-{i:04d}-' + 'x' * 450 for i in range(200)]
    reply = rig.service.models(provider='anthropic', limit=100)
    assert reply['ok'], reply
    assert len(json.dumps(reply, ensure_ascii=False)) <= 16000
    assert reply['total'] == 200 and reply['has_more'] is True
    assert reply['next_offset'] == len(reply['models']) > 0


def test_discovery_returns_only_known_brief_metadata_for_the_returned_page(rig, catalog, monkeypatch):
    from agent import models_dev
    calls = []
    def info(provider, model, **kwargs):
        calls.append((provider, model))
        return models_dev.ModelInfo(id=model, name='Compact', family='example', provider_id=provider,
                                    tool_call=True, reasoning=True, context_window=64000)
    monkeypatch.setattr(models_dev, 'get_model_info', info)
    reply = rig.service.models(provider='anthropic', limit=1)
    model, = reply['models']
    assert model['context_window'] == 64000 and model['supports_tools'] is True
    assert model['supports_reasoning'] is True and model['name'] == 'Compact'
    assert calls == [('anthropic', 'claude-haiku-5-5')]


@pytest.mark.parametrize('display_name', ['Local (GPU)', 'GPU+CPU', 'GPU/CPU', 'München', 'x' * 57])
def test_discovery_preserves_native_named_custom_provider_slugs_without_secrets(rig, catalog, display_name):
    from hermes_cli.providers import custom_provider_slug
    provider = custom_provider_slug(display_name)
    rows, _ = catalog
    rows.append({'slug': provider, 'name': display_name, 'models': ['local-model'],
                 'api_key': 'DO_NOT_RETURN_THIS', 'api_url': 'http://private.endpoint/v1',
                 'extra_headers': {'Authorization': 'DO_NOT_RETURN_THIS'}, 'arbitrary_field': 'DO_NOT_RETURN_THIS'})

    providers = rig.service.models()
    assert providers['ok'], providers
    assert {'provider': provider, 'name': display_name, 'model_count': 1} in providers['providers']
    models = rig.service.models(provider=provider)
    assert models['ok'], models
    assert models['models'] == [{'provider': provider, 'model': 'local-model'}]
    assert 'DO_NOT_RETURN_THIS' not in json.dumps([providers, models])
    assert 'private.endpoint' not in json.dumps([providers, models])
