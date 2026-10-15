"""Additional narrowing guard; existing ToolExecutor remains the permission authority."""
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path

from core.orchestration.contracts import Permissions
from core.tools.registry import ToolParamError, ToolRegistry, ToolsContext, build_tools_context

# B42 host adapter only; service tools and recursive discovery await scoped adapters.
SUPPORTED = frozenset(('read_file', 'stat_path', 'write_file', 'edit_file', 'mkdir'))


def canonical(value):
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('worker_scope_requires_absolute_path')
    return path.resolve()


def within(path, roots):
    target = Path(path).resolve()
    return any(target.is_relative_to(canonical(root)) for root in roots)


def intersect(envelope, profile, parent, sandbox, registry):
    requested = Permissions(envelope.allowed_tools, envelope.read_scope,
                            envelope.write_scope, profile.permissions.memory_scope)
    for ceiling in (parent, profile.permissions, sandbox):
        if not set(requested.tools) <= set(ceiling.tools):
            raise ValueError('worker_tool_escalation')
        for key in ('read_scope', 'write_scope'):
            if any(not within(canonical(root), getattr(ceiling, key))
                   for root in getattr(requested, key)):
                raise ValueError('worker_path_escalation')
    memory = tuple(s for s in requested.memory_scope
                   if s in parent.memory_scope and s in sandbox.memory_scope)
    for name in requested.tools:
        spec = registry.get(name)
        if name not in SUPPORTED or spec is None or not registry.is_enabled(name):
            raise ValueError('worker_tool_unsupported_or_disabled')
        if profile.role == 'reviewer' and spec.effect != 'read':
            raise ValueError('reviewer_must_be_readonly')
    if profile.role == 'reviewer' and requested.write_scope:
        raise ValueError('reviewer_must_be_readonly')
    return replace(requested, memory_scope=memory,
                   read_scope=tuple(str(canonical(p)) for p in requested.read_scope),
                   write_scope=tuple(str(canonical(p)) for p in requested.write_scope))


def build_scoped_tools(parent, effective, revalidate):
    """Fresh operation/auth state; canonical builtin code only, no parent live services."""
    builtins = build_tools_context({'tools': {'enabled': list(effective.tools)}}, parent.project_root)
    registry = ToolRegistry(list(effective.tools))
    for name in effective.tools:
        spec = builtins.registry.get(name)
        registry.register(_guarded(spec, effective, revalidate, parent.protected_paths))
    return ToolsContext(registry=registry, project_root=parent.project_root,
                        cwd=parent.cwd, data_dir=parent.data_dir,
                        protected_paths=parent.protected_paths, permission_mode=parent.permission_mode,
                        timeout_seconds=parent.timeout_seconds,
                        max_calls_per_turn=parent.max_calls_per_turn,
                        max_tool_rounds=parent.max_tool_rounds,
                        final_answer_policy=parent.final_answer_policy)


def _check(paths, effective, effect, revalidate, protected):
    from core.tools.permissions import is_env_path

    try:
        revalidate()
    except ValueError as exc:
        raise ToolParamError('worker_authority_revoked') from exc
    roots = effective.read_scope if effect == 'read' else effective.write_scope
    for path in paths:
        target = Path(path).resolve()
        if (not within(target, roots) or is_env_path(target)
                or any(target.is_relative_to(Path(p).resolve()) for p in protected)):
            raise ToolParamError('worker_target_outside_scope_or_protected')


def _guarded(spec, effective, revalidate, protected):
    def guard(args, **kwargs):
        paths = [kwargs['resolved_path'], *(kwargs.get('resolved_extra') or {}).values()]
        _check(paths, effective, spec.effect, revalidate, protected)
        if spec.plan is not None:
            _check(_targets(spec, args, kwargs), effective, spec.effect, revalidate, protected)
        control = (kwargs.get('op_context') or {}).get('control')
        if control is None and spec.effect != 'read' and hasattr(revalidate, 'commit_fence'):
            from core.tools.operation_control import OperationControl
            control = OperationControl()
            kwargs['op_context'] = {**(kwargs.get('op_context') or {}), 'control': control}
        if control is not None and spec.effect != 'read':
            previous = getattr(control, 'authorization_check', None)
            fence = getattr(revalidate, 'commit_fence', None)
            if fence is not None:
                control.commit_fence = fence

            def check():
                _check(paths, effective, spec.effect, revalidate, protected)
                return previous is None or previous()

            control.authorization_check = check
        fence = getattr(revalidate, 'commit_fence', nullcontext) if spec.effect != 'read' else nullcontext
        quota = getattr(revalidate, 'quota', None)
        # 配额上下文必须在 fence **外层**：RuntimeQuota.tool 以 LeaseService.transaction 开账，
        # 而 commit-fence 标记持有期间任何 LeaseService.transaction 都按重入拒绝
        # （worker_commit_reentrant_mutation）——持久 DAG 租约 worker 的真实写入路径
        # （B51 对照评估首贯）此前从未端到端跑到过这里；同租约 fence 重入本身合法。
        with quota.tool() if quota else nullcontext():
            with fence():   # Includes mkdir/temporary-file preparation, not just final replacement.
                return spec.run(args, **kwargs)

    def plan(args, **kwargs):
        _check([kwargs['resolved_path']], effective, spec.effect, revalidate, protected)
        result = spec.plan(args, **kwargs)
        _check(_targets(spec, args, kwargs), effective, spec.effect, revalidate, protected)
        return result

    return replace(spec, run=guard, plan=plan if spec.plan else None)


def _targets(spec, args, kwargs):
    result = spec.plan(args, resolved_path=kwargs['resolved_path'],
                       resolved_extra=kwargs.get('resolved_extra'))
    targets = list(result.targets)
    # mkdir/write_file can create missing ancestors; include those in the narrowing check.
    for target in result.targets:
        ancestor = Path(target).parent
        while not ancestor.exists() and ancestor != ancestor.parent:
            targets.append(ancestor)
            ancestor = ancestor.parent
    return targets
