"""Real loader, lifecycle and resume glue (HermesInternals) on a real SessionDB; only child execution is stubbed."""
import json
from types import SimpleNamespace

from conftest import PLUGIN_ROOT

SYSTEM = 'You are a focused subagent. (stored system prompt)'


class StubChild:
    def __init__(self, index, model):
        self._subagent_id, self.session_id = f'stub-{index}', f'child-session-{index}'
        self.provider, self.model = 'stub', model
        self._cached_system_prompt = None
        self.seen = []

    def run_conversation(self, user_message=None, conversation_history=None, **kwargs):
        self.seen.append(dict(system=self._cached_system_prompt, history=conversation_history, message=user_message))
        reply = f'answer to: {user_message}'
        return {'final_response': reply, 'messages': list(conversation_history or []) +
                [{'role': 'user', 'content': user_message}, {'role': 'assistant', 'content': reply}]}


def test_followup_resumes_from_the_session_db_through_real_hermes_seams(monkeypatch, tmp_path):
    home = tmp_path / 'hermes'
    home.mkdir(exist_ok=True)
    (home / 'plugins').mkdir()
    (home / 'plugins' / 'hermes-subagents').symlink_to(PLUGIN_ROOT, target_is_directory=True)
    (home / 'config.yaml').write_text('plugins:\n  enabled: [hermes-subagents]\n  entries:\n'
                                      '    hermes-subagents:\n      settings:\n'
                                      '        models:\n          worker: {model: stub-model, toolsets: [file]}\n')
    from hermes_cli.plugins import PluginManager
    from hermes_state_registry import acquire, release_or_close
    from agent.subagent_lifecycle import bind_subagent_parent
    from tools import delegate_tool
    from tools.registry import registry
    manager = PluginManager()
    manager.discover_and_load()
    db = acquire()
    try:
        builds, children = [], []

        def build(**kwargs):
            builds.append(kwargs)
            child = StubChild(len(builds), kwargs['model'])
            children.append(child)
            for callback in manager._hooks.get('subagent_start', []):  # what _build_child_agent fires
                callback(child_session_id=child.session_id, child_subagent_id=child._subagent_id)
            return child

        def run(task_index, goal, child=None, parent_agent=None):
            output = child.run_conversation(user_message=goal, task_id='t')
            if child.session_id == 'child-session-1':  # persist the first child like Hermes would
                db.create_session('child-session-1', 'subagent')
                db.update_system_prompt('child-session-1', SYSTEM)
                db.append_message('child-session-1', 'user', goal)
                db.append_message('child-session-1', 'assistant', output['final_response'])
            return {'status': 'completed', 'summary': output['final_response'], 'api_calls': 1}

        monkeypatch.setattr(delegate_tool, '_build_child_preserving_parent_tools', build)
        monkeypatch.setattr(delegate_tool, '_run_child_lifecycle', run)
        parent = SimpleNamespace(session_id='mount-session', _session_db=db)
        entry = lambda name: registry.get_entry(name, scope=manager.scope_key).handler
        with bind_subagent_parent(parent):
            spawned = json.loads(entry('subagent_spawn')({'model': 'worker', 'goal': 'Read a.py.'},
                                                         session_id='mount-session', parent_agent=parent))
            assert spawned['ok'] and spawned['subagent'] == 'worker', spawned
            first = json.loads(entry('subagent_wait')({'run_ids': [spawned['run_id']], 'wait_seconds': 5},
                                                      session_id='mount-session'))['done'][0]
            assert first['status'] == 'succeeded'
            followed = json.loads(entry('subagent_followup')(
                {'run_id': spawned['run_id'], 'message': 'Now fix it.', 'wait_seconds': 5},
                session_id='mount-session', parent_agent=parent))
        assert followed['ok'] and followed['status'] == 'succeeded', followed
        assert followed['followup_mode'] == 'resume' and followed['parent_run_id'] == spawned['run_id']
        assert followed['summary'] == 'answer to: Now fix it.'
        assert len(builds) == 2
        original, rebuilt = builds
        for key in ('goal', 'context', 'toolsets', 'model', 'role'):
            assert rebuilt[key] == original[key], key
        seen, = children[1].seen
        assert seen['system'] == SYSTEM and seen['message'] == 'Now fix it.'
        assert [m['role'] for m in seen['history']] == ['user', 'assistant']
        assert seen['history'][1]['content'] == 'answer to: Read a.py.'
        with bind_subagent_parent(parent):
            listed = json.loads(entry('subagent_wait')({}, session_id='mount-session'))
        modes = {run['run_id']: (run.get('subagent'), run['status']) for run in listed['runs']}
        assert modes[followed['run_id']] == ('worker', 'succeeded')
    finally:
        release_or_close(db)
        manager.unload()
