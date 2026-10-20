"""Resume a finished child's conversation on the same model, so a follow-up reuses its prompt cache.

The public subagent lifecycle API drops a child when it finishes, so this module rebuilds the child the way
``SubagentLifecycleService.launch`` does, restores the exact system prompt the child ran with, and runs the
follow-up with the earlier transcript as ``conversation_history``. The provider then sees a byte-identical
prefix and serves it from cache.

Every Hermes-internal call lives in ``HermesInternals``. Any missing seam raises ``ResumeUnavailable`` and the
service falls back to a digest follow-up, so a Hermes update degrades the feature instead of breaking the plugin.
Verified against Hermes v0.22.2.
"""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, wait as wait_futures
import contextvars
from dataclasses import asdict, dataclass, field
import hashlib
import json
import sys
import threading
import time
from types import SimpleNamespace
from typing import Optional

TERMINAL_STATES = {'SUCCEEDED', 'FAILED', 'INTERRUPTED', 'CANCELLED'}
MAX_RESULT_CHARS = 32_000
MAX_RETAINED = 64  # finished resume records (and their transcripts) kept per process


class ResumeUnavailable(Exception):
    """This follow-up cannot resume; the caller falls back to a digest follow-up."""


@dataclass(frozen=True)
class ResumeHandle:
    subagent_id: str
    provider: Optional[str]
    model: Optional[str]
    run_id: str
    child_session_id: Optional[str] = None
    backend: str = 'resume'

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        return cls(subagent_id=str(value['subagent_id']), provider=value.get('provider'), model=value.get('model'),
                   run_id=str(value['run_id']), child_session_id=value.get('child_session_id'))


@dataclass
class ResumeResult:
    """Shaped like the lifecycle's SubagentResult, which the service's finalize() reads."""
    handle: ResumeHandle
    terminal_state: str
    ready: bool
    summary: Optional[str] = None
    error_message: Optional[str] = None
    error_classification: Optional[str] = None
    result_hash: Optional[str] = None
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    usage_metadata: dict = field(default_factory=dict)
    tool_execution_summary: dict = field(default_factory=dict)


class HermesInternals:
    """The private Hermes seams resume needs, mirroring SubagentLifecycleService.launch/_run/cancel (v0.22.2)."""

    # What SubagentLifecycleService.launch honors; anything else could make the rebuilt child differ.
    LAUNCH_KEYS = frozenset({'goal', 'context', 'role', 'allowed_toolsets', 'model', 'provider'})

    def build_child(self, parent, launch, model, provider):
        unknown = sorted(set(launch) - self.LAUNCH_KEYS)
        if unknown:
            raise ResumeUnavailable(f"the run was launched with {', '.join(unknown)}, which resume cannot reproduce")
        try:
            from tools.delegate_tool import (_build_child_preserving_parent_tools, _credential_overrides,
                                             _load_config, _resolve_delegation_credentials, DEFAULT_MAX_ITERATIONS)
        except ImportError as exc:
            raise ResumeUnavailable(f'this Hermes build lacks the child builder ({exc})') from exc
        # Same precedence as launch(): an explicit provider (route) wins, else the delegation config.
        routing_cfg = {'provider': provider, 'model': model or ''} if provider is not None else _load_config()
        try:
            creds = _resolve_delegation_credentials(routing_cfg, parent)
        except ValueError as exc:
            raise ResumeUnavailable(f'cannot resolve the route: {exc}') from exc
        toolsets = launch.get('allowed_toolsets')
        return _build_child_preserving_parent_tools(
            task_index=0, goal=launch['goal'], context=launch.get('context'),
            toolsets=list(toolsets) if toolsets else None, model=model or creds['model'],
            max_iterations=DEFAULT_MAX_ITERATIONS, task_count=1, parent_agent=parent,
            role=launch.get('role') or 'leaf', **_credential_overrides(creds, routing_cfg))

    def run_child(self, message, child, parent):
        from tools.delegate_tool import _run_child_lifecycle
        return _run_child_lifecycle(0, message, child, parent)

    def steer(self, subagent_id, text):
        from tools.delegate_tool_registry import steer_subagent
        return bool(steer_subagent(subagent_id, text))

    def interrupt(self, child, reason=''):
        """Interrupt the child object itself, as the lifecycle's cancel() does; works before it is registered."""
        try:
            from agent.interrupt_compat import request_hard_interrupt
        except ImportError:
            from tools.delegate_tool_registry import interrupt_subagent
            return bool(interrupt_subagent(str(getattr(child, '_subagent_id', ''))))
        return bool(request_hard_interrupt(child, f'Subagent cancellation requested: {str(reason)[:500]}',
                                           tool_reason='subagent cancellation requested'))

    def discard(self, child):
        """Release a rebuilt child that will never run (its session DB handle, client and parent registration)."""
        try:
            child.close()
        except Exception:
            pass

    def load_transcript(self, parent, child_session_id):
        """(messages, system_prompt) a finished child persisted, read the way `hermes chat --resume` reads them,
        including the hop to the live tip when context compression moved the child to a continuation session."""
        parent_db = getattr(parent, '_session_db', None)
        if parent_db is None or not child_session_id:
            raise ResumeUnavailable('the parent session has no session database')
        try:
            from hermes_state_registry import acquire, release_or_close
        except ImportError as exc:
            raise ResumeUnavailable(f'this Hermes build lacks the session registry ({exc})') from exc
        path = getattr(parent_db, 'db_path', None)
        db = acquire(path) if path is not None else acquire()
        try:
            resolve = getattr(db, 'resolve_resume_session_id', None)
            if not callable(resolve):
                raise ResumeUnavailable('this Hermes build cannot follow compressed sessions')
            session_id = resolve(child_session_id) or child_session_id
            messages = db.get_messages_as_conversation(session_id, repair_alternation=True)
            row = db.get_session(session_id) or {}
        finally:
            release_or_close(db)
        return messages, row.get('system_prompt')

    @staticmethod
    def available():
        try:
            from tools.delegate_tool import _build_child_preserving_parent_tools, _run_child_lifecycle  # noqa: F401
            from tools.delegate_tool_registry import steer_subagent  # noqa: F401
            from hermes_state_registry import acquire  # noqa: F401
        except ImportError:
            return False
        return True


class _Record:
    def __init__(self, handle, child, parent):
        self.handle, self.child, self.parent = handle, child, parent
        self.state = 'PENDING'
        self.future = None
        self.result = None
        self.transcript = None
        self.system_prompt = None
        self.started_at = None


def _conversation(messages):
    """The replayable transcript: no system message (the system prompt is restored separately)."""
    return [dict(message) for message in messages or () if isinstance(message, dict) and message.get('role') != 'system']


def _executor(max_workers):
    """Hermes's DaemonThreadPoolExecutor when present: daemon workers (a wedged child never blocks exit) that carry
    the caller's contextvars, which hold the per-profile Hermes home and session scope in multi-profile processes."""
    try:
        from tools.daemon_pool import DaemonThreadPoolExecutor as executor_class
    except ImportError:
        executor_class = ThreadPoolExecutor
    return executor_class(max_workers=max_workers, thread_name_prefix='hermes-subagents-resume')


class ResumeRunner:
    """Runs resumed children behind the same interface as the lifecycle facade (status/wait/result/cancel/steer)."""

    def __init__(self, internals=None, max_workers=8):
        self.internals = internals or HermesInternals()
        self._executor = _executor(max_workers)
        self._records = OrderedDict()  # subagent_id -> _Record
        self._by_run = {}  # run_id -> subagent_id
        self._lock = threading.RLock()

    def start(self, *, parent, launch, model, provider, transcript, system_prompt, message, run_id,
              expected_provider=None):
        if parent is None:
            raise ResumeUnavailable('no active Hermes parent session')
        history = _conversation(transcript)
        if not history:
            raise ResumeUnavailable('the earlier transcript is empty or unavailable')
        if not isinstance(system_prompt, str) or not system_prompt:
            raise ResumeUnavailable("the earlier child's system prompt is unavailable")
        child = self.internals.build_child(parent, launch, model, provider)
        subagent_id = str(getattr(child, '_subagent_id', '') or '')
        problem = None
        if not subagent_id:
            problem = 'Hermes did not assign the rebuilt child an identity'
        elif model and getattr(child, 'model', model) != model:
            problem = f"the route now resolves to model {getattr(child, 'model', None)!r}, not {model!r}"
        elif expected_provider and getattr(child, 'provider', None) not in (None, expected_provider):
            problem = (f"the route now resolves to provider {getattr(child, 'provider', None)!r}, not "
                       f'{expected_provider!r} (the provider changed since the run)')
        if problem:
            self.internals.discard(child)
            raise ResumeUnavailable(problem)
        # Restoring the exact bytes skips the rebuild (turn_context: only a None prompt is rebuilt).
        child._cached_system_prompt = system_prompt
        handle = ResumeHandle(subagent_id=subagent_id, provider=getattr(child, 'provider', provider),
                              model=getattr(child, 'model', model), run_id=run_id,
                              child_session_id=getattr(child, 'session_id', None) or None)
        record = _Record(handle, child, parent)
        record.system_prompt = system_prompt
        original = child.run_conversation
        replay = [True]

        def run_conversation(*args, **kwargs):
            # Only the first turn replays the earlier conversation; a later turn (Hermes's output-schema retry)
            # continues the child's own state like any other child's.
            if replay[0]:
                replay[0] = False
                kwargs.setdefault('conversation_history', [dict(message) for message in history])
            output = original(*args, **kwargs)
            if isinstance(output, dict) and isinstance(output.get('messages'), list):
                record.transcript = _conversation(output['messages'])
            return output

        child.run_conversation = run_conversation
        with self._lock:
            self._records[subagent_id] = record
            self._by_run[run_id] = subagent_id
            self._trim()
        record.future = self._executor.submit(contextvars.copy_context().run, self._run, record, message)
        return handle

    def _run(self, record, message):
        state, fields = 'FAILED', {}
        try:
            with self._lock:
                cancelled_before_start = record.state == 'CANCEL_REQUESTED'
                if not cancelled_before_start:
                    record.state = 'RUNNING'
                record.started_at = time.time()
            if cancelled_before_start:
                self.internals.discard(record.child)
                state, fields = 'CANCELLED', dict(error_message='cancelled before it started')
            else:
                raw = self.internals.run_child(message, record.child, record.parent)
                raw = raw if isinstance(raw, dict) else {}
                status = str(raw.get('status', 'error'))
                if status == 'interrupted':
                    state = 'CANCELLED' if record.state == 'CANCEL_REQUESTED' else 'INTERRUPTED'
                else:
                    state = 'SUCCEEDED' if status == 'completed' else 'FAILED'
                fields = dict(summary=_clip(raw.get('summary')), error_message=_clip(raw.get('error') or None),
                              error_classification=None if state == 'SUCCEEDED' else status.upper(),
                              usage_metadata={'api_calls': raw.get('api_calls', 0)},
                              tool_execution_summary={'duration_seconds': raw.get('duration_seconds', 0)})
        except BaseException as exc:
            state, fields = 'FAILED', dict(error_classification=type(exc).__name__, error_message=_clip(exc))
        finally:
            # Always reach a terminal state, so waiters and the monitor never spin on a finished future.
            system_prompt = getattr(record.child, '_cached_system_prompt', None)
            result = ResumeResult(record.handle, state, True, started_at=record.started_at,
                                  completed_at=time.time(), **fields)
            try:
                payload = asdict(result)
                payload.pop('result_hash', None)
                result.result_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
            except Exception:
                pass
            with self._lock:
                if isinstance(system_prompt, str) and system_prompt:
                    record.system_prompt = system_prompt
                record.child, record.parent = None, None  # the transcript is all a later follow-up needs
                record.result, record.state = result, state

    def _record(self, handle):
        subagent_id = getattr(handle, 'subagent_id', None)
        if subagent_id is None and isinstance(handle, dict):
            subagent_id = handle.get('subagent_id')
        with self._lock:
            return self._records.get(subagent_id)

    def _trim(self):
        finished = [key for key, record in self._records.items() if record.state in TERMINAL_STATES]
        for key in finished[:max(0, len(self._records) - MAX_RETAINED)]:
            record = self._records.pop(key)
            self._by_run.pop(record.handle.run_id, None)

    def status(self, handle):
        record = self._record(handle)
        return SimpleNamespace(state=record.state if record else 'UNKNOWN')

    def wait(self, handle, *, timeout_seconds=None):
        record = self._record(handle)
        if record is None:
            return SimpleNamespace(state='UNKNOWN', timed_out=False)
        if record.future is not None:
            wait_futures([record.future], timeout=timeout_seconds)
            # _run always ends terminal, so a done future is never reported as still running.
            return SimpleNamespace(state=record.state, timed_out=not record.future.done())
        return SimpleNamespace(state=record.state, timed_out=record.state not in TERMINAL_STATES)

    def result(self, handle):
        record = self._record(handle)
        if record is None:
            handle = handle if isinstance(handle, ResumeHandle) else ResumeHandle.from_dict(dict(handle))
            return ResumeResult(handle, 'UNKNOWN', False)
        if record.result is not None:
            return record.result
        return ResumeResult(record.handle, record.state, False)

    def cancel(self, handle, *, reason=''):
        """Cooperative, like the lifecycle's: a queued run is skipped; a running child is interrupted directly."""
        record = self._record(handle)
        if record is None:
            return SimpleNamespace(accepted=False, state='UNKNOWN')
        with self._lock:
            if record.result is not None or record.state in TERMINAL_STATES:
                return SimpleNamespace(accepted=False, state=record.state)
            running, child = record.state == 'RUNNING', record.child
            record.state = 'CANCEL_REQUESTED'
        if running and child is not None:
            try:
                self.internals.interrupt(child, reason)
            except Exception:
                pass
        return SimpleNamespace(accepted=True, state='CANCEL_REQUESTED')

    def steer(self, handle, text):
        record = self._record(handle)
        if record is None or record.state not in ('PENDING', 'RUNNING') or not str(text or '').strip():
            return False
        try:
            return self.internals.steer(record.handle.subagent_id, text)
        except Exception:
            return False

    def transcript(self, run_id):
        """(messages, system_prompt) a finished resume run left, or None once it is forgotten."""
        with self._lock:
            record = self._records.get(self._by_run.get(run_id))
            if record is None or record.state not in TERMINAL_STATES or not record.transcript:
                return None
            return [dict(message) for message in record.transcript], record.system_prompt


def _clip(value):
    return str(value)[:MAX_RESULT_CHARS] if value is not None else None


def shared_runner():
    """One runner per process, so a plugin reload keeps reaching resumed children that are still running."""
    runner = sys.__dict__.get('_hermes_subagents_resume_runner')
    if runner is None:
        runner = sys.__dict__.setdefault('_hermes_subagents_resume_runner', ResumeRunner())
    return runner


class RoutingLifecycle:
    """The lifecycle facade, plus resume handles routed to the ResumeRunner."""

    def __init__(self, lifecycle, runner):
        self.lifecycle, self.runner = lifecycle, runner

    def _target(self, handle):
        backend = handle.get('backend') if isinstance(handle, dict) else getattr(handle, 'backend', None)
        return self.runner if backend == 'resume' else self.lifecycle

    def launch(self, request):
        return self.lifecycle.launch(request)

    def status(self, handle):
        return self._target(handle).status(handle)

    def wait(self, handle, *, timeout_seconds=None):
        return self._target(handle).wait(handle, timeout_seconds=timeout_seconds)

    def result(self, handle):
        return self._target(handle).result(handle)

    def cancel(self, handle, *, reason=''):
        return self._target(handle).cancel(handle, reason=reason)

    def __getattr__(self, name):
        # steer (and any optional facade method) is probed with getattr; keep the lifecycle's answer.
        attribute = getattr(self.lifecycle, name)
        if name == 'steer' and callable(attribute):
            return lambda handle, text: self._target(handle).steer(handle, text)
        return attribute
