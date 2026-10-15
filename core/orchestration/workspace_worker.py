"""Scheduler adapter maps all Worker file authority into a fresh B47 directory."""
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from core.orchestration.validation import terminal
from core.orchestration.worker_runner import ReActWorker


class WorkspaceWorker:
    def __init__(self, envelope, *, manager, **kwargs):
        self.envelope, self.manager, self.kwargs = envelope, manager, kwargs
        self.identity = {key: uuid4().hex for key in ('agent_id', 'session_id', 'run_id')}
        self._used = False
        self.workspace_id = None

    def cancel_before_start(self, envelope):
        if self._used or envelope != self.envelope:
            return None
        self._used = True
        self.kwargs['settle'](None, unstarted=True)
        return terminal(envelope, 'cancelled', 'worker_cancelled_before_start')

    def _map(self, root, paths):
        result = []
        for name in paths:
            path = Path(name).resolve()
            if not path.is_relative_to(self.manager.root):
                raise ValueError('workspace_external_scope_refused')
            result.append(str(root / path.relative_to(self.manager.root)))
        return tuple(result)

    def _build(self):
        allowed = tuple(Path(p).resolve().relative_to(self.manager.root).as_posix()
                        for p in self.envelope.write_scope)
        if '.' in allowed:
            raise ValueError('workspace_explicit_write_paths_required')
        import json
        from dataclasses import asdict
        rows = self.manager.state()['workspaces'].values()
        prior = next((r for r in rows if r['status'] == 'rework'
                      and r['envelope'] == json.loads(json.dumps(asdict(self.envelope)))), None)
        if prior is None:
            key = self.manager.create(self.envelope, worker_id=self.identity['agent_id'], allowed_paths=allowed)
        else:
            key = prior['id']
            self.identity['agent_id'] = prior['worker_id']
        self.workspace_id = key
        root = self.manager.path(key)
        effective = self.kwargs['effective']
        scoped = replace(effective, read_scope=self._map(root, effective.read_scope),
                         write_scope=self._map(root, effective.write_scope))
        task = replace(self.envelope, read_scope=scoped.read_scope, write_scope=scoped.write_scope,
                       constraints=self.envelope.constraints + (f'Isolated workspace: {root}',))
        parent = self.kwargs['parent_tools']
        from core.tools.registry import ToolsContext
        tools = ToolsContext(registry=parent.registry, project_root=root, cwd=root,
                             data_dir=self.manager.directory / key / 'data',
                             protected_paths=(root / '.git', root / '.work', *self._map(
                                 root, tuple(p for p in parent.protected_paths
                                             if Path(p).resolve().is_relative_to(self.manager.root)))),
                             permission_mode=parent.permission_mode,
                             timeout_seconds=parent.timeout_seconds,
                             max_calls_per_turn=parent.max_calls_per_turn,
                             max_tool_rounds=parent.max_tool_rounds,
                             final_answer_policy=parent.final_answer_policy)
        worker = ReActWorker(task, **{**self.kwargs, 'effective': scoped, 'parent_tools': tools})
        worker.identity = self.identity
        return worker, task

    async def run(self, envelope):
        if self._used or envelope != self.envelope:
            raise ValueError('worker_admission_mismatch_or_consumed')
        self._used = True
        try:
            worker, task = self._build()
        except Exception:
            self.kwargs['settle'](None, unstarted=True)
            raise
        started = False
        try:
            with self.manager.running(self.workspace_id):
                started = True
                result = await worker.run(task)
        except BaseException:
            self.manager.retain_failure(self.workspace_id, 'worker_interrupted_or_failed')
            raise
        finally:
            if not started:
                self.kwargs['settle'](None, unstarted=True)
        if result.status != 'completed':
            self.manager.retain_failure(self.workspace_id, 'worker_incomplete')
            return replace(result, artifact_refs=result.artifact_refs + (
                'workspace-partial:' + str(self.manager.path(self.workspace_id)),))
        root = self.manager.path(self.workspace_id)
        changed = tuple(Path(p).resolve().relative_to(root).as_posix() for p in result.changed_paths)
        try:
            artifact = self.manager.capture(self.workspace_id, changed_paths=changed)
        except ValueError as exc:
            self.manager.retain_failure(self.workspace_id, str(exc))
            return replace(result, status='incomplete', blockers=result.blockers + (str(exc),),
                           artifact_refs=result.artifact_refs + ('workspace-partial:' + str(root),))
        return replace(result, artifact_refs=result.artifact_refs + ('workspace:' + artifact,),
                       changed_paths=tuple(str(self.manager.root / name) for name in changed))
