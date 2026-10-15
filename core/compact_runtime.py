"""安全请求边界上的可选自动压缩；完整预算门通过后才原子切换批准版本。"""
from core.context import CompactSnapshot, prepare_context
from core.errors import ErrorCategory, ErrorCode, VastarError
from core.run_state import RunRequest
from memory.compaction import CompactionService, seal


def compaction_service(coord):
    tools = getattr(coord, 'task_tools_context', None) or getattr(coord._runtime, '_tools', None)
    return CompactionService(coord._store, coord.session_id, artifacts=getattr(tools, 'artifacts', None))


def options(config):
    opts = config.get('compact_v2', {})
    if not isinstance(opts, dict) or type(opts.get('auto_enabled', False)) is not bool:
        raise ValueError('compact_v2.auto_enabled 必须为布尔值')
    high, low = opts.get('high_watermark'), opts.get('low_watermark')
    if opts.get('auto_enabled') and (not isinstance(high, (int, float)) or not isinstance(low, (int, float))
                                     or not 0 < low < high <= 1):
        raise ValueError('自动 compact 须显式配置 0 < low_watermark < high_watermark <= 1')
    return opts


def build_options(config):
    opts = options(config)
    return {'target_ratio': float(config.get('compact_target_ratio', 0.55)),
            'recent_groups': opts.get('recent_tool_groups', 2), 'rebuild_every': opts.get('rebuild_every', 3)}


def metrics(record, result, profile):
    record['metrics']['full_request_estimate'] = {
        'input_tokens': result.measurement.estimated_tokens, 'gate_tokens': result.gate.gate_tokens,
        'input_budget': profile.input_budget, 'output_reserve': profile.reserve_output_tokens,
        'guarantee': 'estimated', 'scope': 'prompt + tools + skills + memory + history + input'}
    record['hash'] = seal(record)
    return record


def preview_record(coord, service):
    record = service.build(**build_options(coord._context_cfg))
    from core.memory_session import memory_context
    request = RunRequest('compact-preview', coord.session_id, '', '',
                         memory_context(coord) + coord._store.history_messages(coord.session_id),
                         prompt_bundle=coord._prompt_bundle, token_cfg=coord._token_cfg,
                         context_cfg=coord._context_cfg,
                         approved_compact=CompactSnapshot.from_record({**record, 'snapshot_id': 'preview'}))
    try:
        result = coord._runtime._prepare_context(request, coord._primary)
    except VastarError as exc:
        raise ValueError(str(exc)) from exc
    return metrics(record, result, coord._primary.profile)


class CompactBoundary:
    def __init__(self, coordinator):
        self.coord = coordinator
        self.attempted = False

    def __call__(self, request, kwargs):
        try:
            return self._prepare(request, kwargs)
        except (ValueError, RuntimeError, KeyError, TypeError, OSError) as exc:
            raise VastarError(ErrorCode.CONTEXT_BUDGET_EXCEEDED, ErrorCategory.PARAM,
                              f'compact 无法安全完成；保留上版，请求用户缩小范围或调整预算：{exc}') from exc

    def _prepare(self, request, kwargs):
        service = compaction_service(self.coord)
        request.approved_compact = service.load()
        kwargs['approved_compact'] = request.approved_compact
        opts = options(request.context_cfg)
        result, failure = None, None
        try:
            result = prepare_context(**kwargs)
        except VastarError as exc:
            failure = exc
        if not opts.get('auto_enabled') or self.attempted or request.cancel_event.is_set():
            if failure:
                raise failure
            return result
        self.attempted = True
        if result and result.gate.gate_tokens < kwargs['profile'].input_budget * opts['high_watermark']:
            return result
        store = self.coord._store
        if store._conn.in_transaction:
            raise ValueError('自动 compact 必须在事务外安全边界')
        record = service.build(**build_options(request.context_cfg))
        if any(op['state'] == 'executing' for op in record['handoff']['operations_to_reconcile']):
            raise ValueError('存在执行中的操作；尚非安全边界')
        candidate = CompactSnapshot.from_record({**record, 'snapshot_id': 'candidate'})
        trial = prepare_context(**{**kwargs, 'approved_compact': candidate})
        if trial.gate.gate_tokens > kwargs['profile'].input_budget * opts['low_watermark']:
            raise ValueError('交接事实/近期配对无法压到低水位；需要用户缩小范围，未切换快照')
        metrics(record, trial, kwargs['profile'])
        record['safe_boundary'] = {'run_id': request.run_id, 'phase': 'before_model_request',
                                   'owner_id': store._owner.owner_id, 'generation': store.fencing_generation}
        record['hash'] = seal(record)
        service.publish(record, approve=True)
        request.approved_compact = service.load()
        self.coord._renderer.render_note('自动 compact 已在安全请求边界提交；完整请求估算已降至低水位。')
        return prepare_context(**{**kwargs, 'approved_compact': request.approved_compact})
