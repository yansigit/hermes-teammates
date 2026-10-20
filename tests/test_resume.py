"""Resume follow-ups: the same child conversation, system prompt and model, so the prompt cache is reused."""
import json
import threading
from types import SimpleNamespace

import pytest

SYSTEM = 'SYSTEM PROMPT OF THE ORIGINAL CHILD'
TRANSCRIPT = [{'role': 'user', 'content': 'Read a.py and summarize it.'},
              {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'c1', 'type': 'function',
               'function': {'name': 'read_file', 'arguments': '{"path": "a.py"}'}}]},
              {'role': 'tool', 'tool_call_id': 'c1', 'content': 'def a(): return 1'},
              {'role': 'assistant', 'content': 'a.py defines a() returning 1.'}]


class FakeChild:
    def __init__(self, subagent_id, model, provider, session_id):
        self._subagent_id, self.model, self.provider, self.session_id = subagent_id, model, provider, session_id
        self._cached_system_prompt = None
        self.calls = []
        self.gate = None

    def run_conversation(self, user_message=None, conversation_history=None, **kwargs):
        self.calls.append(dict(user_message=user_message, conversation_history=conversation_history,
                               system_prompt=self._cached_system_prompt))
        if self.gate is not None:
            self.gate.wait(5)
        history = list(conversation_history or [])
        reply = f'done: {user_message}'
        return {'final_response': reply, 'completed': True, 'api_calls': 1,
                'messages': [{'role': 'system', 'content': self._cached_system_prompt}] + history +
                            [{'role': 'user', 'content': user_message}, {'role': 'assistant', 'content': reply}]}


class FakeInternals:
    def __init__(self):
        self.builds, self.children, self.steers, self.interrupts, self.loads = [], [], [], [], []
        self.resolved_model = None
        self.transcript = (TRANSCRIPT, SYSTEM)
        self.gate = None

    def build_child(self, parent, launch, model, provider):
        self.builds.append(dict(launch=launch, model=model, provider=provider))
        child = FakeChild(f'sa-resume-{len(self.builds)}', self.resolved_model or model, provider,
                          f'child-session-{len(self.builds)}')
        child.gate = self.gate
        self.children.append(child)
        return child

    def run_child(self, message, child, parent):
        output = child.run_conversation(user_message=message, task_id='t')
        return {'status': 'completed', 'summary': output['final_response'], 'api_calls': 1, 'duration_seconds': 0.1}

    def steer(self, subagent_id, text):
        self.steers.append((subagent_id, text))
        return True

    def interrupt(self, subagent_id):
        self.interrupts.append(subagent_id)
        if self.gate is not None:
            self.gate.set()
        return True

    def load_transcript(self, parent, child_session_id):
        self.loads.append(child_session_id)
        if self.transcript is None:
            from hermes_teammates.teammates_resume import ResumeUnavailable
            raise ResumeUnavailable('no transcript stored')
        return self.transcript


@pytest.fixture
def resume_rig(rig):
    from hermes_teammates.teammates_resume import ResumeRunner
    internals = FakeInternals()
    rig.host.resume_runner = ResumeRunner(internals)
    rig.host.cache_ttl_seconds = lambda: 300
    rig.host.active_parent = lambda: SimpleNamespace(session_id='owner', _delegate_depth=0)
    rig.raw['teammates']['worker'].update(model='cheap-model', toolsets=['file'])
    clock = SimpleNamespace(now=1000.0)
    rig.service.clock = lambda: clock.now
    rig.internals, rig.time = internals, clock
    return rig


def finished_run(rig, *, child_session='orig-child-session'):
    """A lifecycle run that finished, with its child session recorded by the subagent_start hook."""
    from test_v020 import result
    run_id = rig.service.assign('owner', 'worker', 'Read a.py and summarize it.')['run_id']
    with rig.store() as store:
        row = store.get(run_id, 'owner')
    if child_session:
        with rig.store() as store:
            store.update(run_id, child_session_id=child_session)
    receipt = result(rig, run_id)
    receipt.completed_at = rig.time.now  # the fake lifecycle reports t=3.0; use the test clock
    rig.service.finalize(run_id, receipt)
    rig.lifecycle.state = 'RUNNING'  # result() leaves the fake lifecycle SUCCEEDED for every handle
    return run_id, row


def wait_done(rig, run_id):
    reply = rig.service.wait('owner', [run_id], mode='all', wait_seconds=5)
    assert reply['ok'] and reply['done'], reply
    return reply['done'][0]


# --- ResumeRunner ---------------------------------------------------------------------------------------------

def test_runner_restores_system_prompt_and_replays_the_transcript():
    from hermes_teammates.teammates_resume import ResumeRunner
    internals = FakeInternals()
    runner = ResumeRunner(internals)
    handle = runner.start(parent=object(), launch={'goal': 'g'}, model='m', provider='p', transcript=TRANSCRIPT,
                          system_prompt=SYSTEM, message='now fix it', run_id='r2')
    assert runner.wait(handle, timeout_seconds=5).state == 'SUCCEEDED'
    call, = internals.children[0].calls
    assert call['system_prompt'] == SYSTEM
    assert call['conversation_history'] == TRANSCRIPT and call['user_message'] == 'now fix it'
    result = runner.result(handle)
    assert result.ready and result.summary == 'done: now fix it' and result.result_hash
    messages, system = runner.transcript('r2')
    assert system == SYSTEM and messages[:4] == TRANSCRIPT and messages[-1]['role'] == 'assistant'
    assert all(message['role'] != 'system' for message in messages)


@pytest.mark.parametrize('transcript,system', [([], SYSTEM), (TRANSCRIPT, None), (TRANSCRIPT, '')])
def test_runner_refuses_without_a_transcript_or_system_prompt(transcript, system):
    from hermes_teammates.teammates_resume import ResumeRunner, ResumeUnavailable
    internals = FakeInternals()
    with pytest.raises(ResumeUnavailable):
        ResumeRunner(internals).start(parent=object(), launch={'goal': 'g'}, model='m', provider=None,
                                      transcript=transcript, system_prompt=system, message='x', run_id='r')
    assert internals.builds == []


def test_runner_refuses_when_the_route_now_resolves_another_model():
    from hermes_teammates.teammates_resume import ResumeRunner, ResumeUnavailable
    internals = FakeInternals()
    internals.resolved_model = 'other-model'
    with pytest.raises(ResumeUnavailable, match='other-model'):
        ResumeRunner(internals).start(parent=object(), launch={'goal': 'g'}, model='m', provider=None,
                                      transcript=TRANSCRIPT, system_prompt=SYSTEM, message='x', run_id='r')


def test_runner_steer_and_cancel_reach_the_running_child():
    from hermes_teammates.teammates_resume import ResumeRunner
    internals = FakeInternals()
    internals.gate = threading.Event()
    runner = ResumeRunner(internals)
    handle = runner.start(parent=object(), launch={'goal': 'g'}, model='m', provider=None, transcript=TRANSCRIPT,
                          system_prompt=SYSTEM, message='x', run_id='r')
    assert runner.steer(handle, 'focus on tests') is True
    assert internals.steers == [(handle.subagent_id, 'focus on tests')]
    assert runner.cancel(handle, reason='enough').accepted is True
    assert runner.wait(handle, timeout_seconds=5).state in ('CANCELLED', 'SUCCEEDED')
    assert internals.interrupts == [handle.subagent_id]
    assert runner.steer(handle, 'too late') is False


def test_runner_unknown_handle_reads_unknown():
    from hermes_teammates.teammates_resume import ResumeHandle, ResumeRunner
    runner = ResumeRunner(FakeInternals())
    handle = ResumeHandle('sa-gone', 'p', 'm', 'r-gone')
    assert runner.status(handle).state == 'UNKNOWN'
    assert runner.wait(handle, timeout_seconds=0).state == 'UNKNOWN'
    assert runner.transcript('r-gone') is None


def test_handle_round_trips_through_the_ledger(rig):
    from hermes_teammates.teammates_resume import ResumeHandle
    handle = ResumeHandle('sa-1', 'anthropic', 'claude-haiku', 'tm_1', 'cs-1')
    assert rig.host.handle_from_dict(json.loads(json.dumps(handle.to_dict()))) == handle


# --- TeammatesService.followup --------------------------------------------------------------------------------

def test_followup_resumes_the_child_on_the_same_model(resume_rig):
    rig = resume_rig
    run_id, original = finished_run(rig)
    launched = rig.lifecycle.requests[-1]
    rig.time.now += 60
    reply = rig.service.followup('owner', run_id, 'Now add a docstring to a().')
    assert reply['ok'], reply
    assert reply['followup_mode'] == 'resume' and reply['parent_run_id'] == run_id
    assert rig.internals.loads == ['orig-child-session']
    build, = rig.internals.builds
    assert build['model'] == original['model']
    assert build['launch']['goal'] == launched.goal and build['launch']['context'] == launched.context
    assert build['launch']['allowed_toolsets'] == ['file']
    done = wait_done(rig, reply['run_id'])
    assert done['status'] == 'succeeded' and done['summary'] == 'done: Now add a docstring to a().'
    call, = rig.internals.children[0].calls
    assert call['system_prompt'] == SYSTEM and call['conversation_history'] == TRANSCRIPT
    with rig.store() as store:
        row = store.get(reply['run_id'], 'owner')
    assert row['followup_mode'] == 'resume' and row['parent_run_id'] == run_id
    assert row['teammate'] == 'worker' and row['child_session_id'] == 'child-session-1'


def test_chained_followup_continues_the_resumed_conversation_from_memory(resume_rig):
    rig = resume_rig
    run_id, _ = finished_run(rig)
    first = rig.service.followup('owner', run_id, 'step two')
    wait_done(rig, first['run_id'])
    second = rig.service.followup('owner', first['run_id'], 'step three')
    assert second['ok'] and second['followup_mode'] == 'resume', second
    wait_done(rig, second['run_id'])
    assert rig.internals.loads == ['orig-child-session']  # the chain is read from memory, not the DB
    history = rig.internals.children[1].calls[0]['conversation_history']
    assert history[:4] == TRANSCRIPT
    assert [m['content'] for m in history[4:]] == ['step two', 'done: step two']


def test_auto_mode_sends_a_digest_once_the_cache_has_expired(resume_rig):
    rig = resume_rig
    run_id, _ = finished_run(rig)
    rig.time.now += 301
    before = len(rig.lifecycle.requests)
    reply = rig.service.followup('owner', run_id, 'late follow-up')
    assert reply['ok'] and reply['followup_mode'] == 'digest', reply
    assert 'expired' in reply['resume_skipped']
    assert rig.internals.builds == [] and len(rig.lifecycle.requests) == before + 1
    assert 'Previous assignments' in rig.lifecycle.requests[-1].context


def test_resume_mode_resumes_even_after_the_cache_expired(resume_rig):
    rig = resume_rig
    rig.raw['followup_mode'] = 'resume'
    run_id, _ = finished_run(rig)
    rig.time.now += 3600
    assert rig.service.followup('owner', run_id, 'late but resume')['followup_mode'] == 'resume'


def test_digest_mode_never_resumes(resume_rig):
    rig = resume_rig
    rig.raw['followup_mode'] = 'digest'
    run_id, _ = finished_run(rig)
    reply = rig.service.followup('owner', run_id, 'x')
    assert reply['followup_mode'] == 'digest' and rig.internals.builds == []


def test_caching_off_falls_back_to_digest_in_auto_mode(resume_rig):
    rig = resume_rig
    rig.host.cache_ttl_seconds = lambda: None
    run_id, _ = finished_run(rig)
    reply = rig.service.followup('owner', run_id, 'x')
    assert reply['followup_mode'] == 'digest' and 'caching is off' in reply['resume_skipped']


@pytest.mark.parametrize('cause', ['no_child_session', 'no_transcript', 'model_changed'])
def test_unresumable_runs_fall_back_to_a_digest_with_the_reason(resume_rig, cause):
    rig = resume_rig
    run_id, _ = finished_run(rig, child_session=None if cause == 'no_child_session' else 'orig-child-session')
    if cause == 'no_transcript':
        rig.internals.transcript = None
    if cause == 'model_changed':
        rig.internals.resolved_model = 'other-model'
    reply = rig.service.followup('owner', run_id, 'x')
    assert reply['ok'] and reply['followup_mode'] == 'digest', reply
    assert reply['resume_skipped']


def test_followup_of_a_running_run_is_refused(resume_rig):
    rig = resume_rig
    run_id = rig.service.assign('owner', 'worker', 'g')['run_id']
    reply = rig.service.followup('owner', run_id, 'x')
    assert reply['error'] == 'still_running'


def test_followup_of_another_conversations_run_is_unknown(resume_rig):
    rig = resume_rig
    run_id, _ = finished_run(rig)
    assert rig.service.followup('someone-else', run_id, 'x')['error'] == 'unknown_run'


def test_followups_count_against_live_capacity(resume_rig):
    rig = resume_rig
    rig.raw['max_live_runs'] = 1
    run_id, _ = finished_run(rig)
    rig.service.assign('owner', 'worker', 'still running')
    assert rig.service.followup('owner', run_id, 'x')['error'] == 'too_many_live_runs'
    assert rig.internals.builds == []


def test_steer_and_stop_route_to_a_resumed_run(resume_rig):
    rig = resume_rig
    rig.host.features = lambda: {'reasoning_effort': False, 'route': None, 'steer': 'lifecycle', 'follow_up': 'resume'}
    rig.internals.gate = threading.Event()
    run_id, _ = finished_run(rig)
    reply = rig.service.followup('owner', run_id, 'long task')
    assert rig.service.message('owner', reply['run_id'], 'hurry')['ok'] is True
    assert rig.internals.steers and rig.internals.steers[0][1] == 'hurry'
    assert rig.service.stop('owner', reply['run_id'], 'enough')['ok'] is True
    assert rig.internals.interrupts
    assert wait_done(rig, reply['run_id'])['status'] == 'cancelled'
