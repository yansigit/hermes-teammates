"""Actual pinned loader and lifecycle launch, with only child execution stubbed."""
import json
from types import SimpleNamespace

from conftest import PLUGIN_ROOT


def test_pinned_loader_registers_and_real_lifecycle_launches(monkeypatch, tmp_path):
    home = tmp_path / 'hermes'
    home.mkdir(exist_ok=True)
    (home / 'plugins').mkdir()
    (home / 'plugins' / 'hermes-subagents').symlink_to(PLUGIN_ROOT, target_is_directory=True)
    (home / 'config.yaml').write_text('plugins:\n  enabled: [hermes-subagents]\n  entries:\n'
                                     '    hermes-subagents:\n      settings:\n'
                                     '        models:\n          worker: {}\n')
    from hermes_cli.plugins import PluginManager
    from agent.subagent_lifecycle import bind_subagent_parent
    from tools import delegate_tool
    from tools.registry import registry
    manager = PluginManager()
    manager.discover_and_load()
    try:
        assert 'subagents' in manager._plugin_commands
        names = {'subagent_spawn','subagent_followup','subagent_wait','subagent_steer','subagent_stop'}
        assert names <= manager._plugin_tool_names
        loaded = manager._plugins['hermes-subagents']
        assert not loaded.error
        assert loaded.module.__package__.startswith('hermes_plugins.hermes_subagents')
        command = manager._plugin_commands['subagents']['handler']
        text = command('')
        assert text.startswith('Models (1):') and '  worker - ' in text and 'This Hermes build: steer=' in text
        constructions = []
        def build(**kwargs):
            constructions.append(kwargs)
            return SimpleNamespace(_subagent_id='stub-mount', provider='stub', model='stub-model')
        monkeypatch.setattr(delegate_tool, '_build_child_preserving_parent_tools', build)
        monkeypatch.setattr(delegate_tool, '_run_child_lifecycle',
                            lambda *args: {'status':'completed','summary':'done'})
        parent = SimpleNamespace(session_id='mount-session')
        with bind_subagent_parent(parent):
            assign = registry.get_entry('subagent_spawn', scope=manager.scope_key).handler
            wait = registry.get_entry('subagent_wait', scope=manager.scope_key).handler
            result = json.loads(assign({'model':'worker','goal':'goal'}, session_id='mount-session'))
            assert result['ok'], result
            final = json.loads(wait({'run_ids':[result['run_id']],'wait_seconds':5}, session_id='mount-session'))['done'][0]
        assert final['status'] == 'succeeded' and final['summary'] == 'done'
        assert len(constructions) == 1 and constructions[0]['role'] == 'leaf'
    finally:
        manager.unload()
