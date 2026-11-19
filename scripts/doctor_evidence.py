"""Historical evidence only, never promote configured capability to verified behavior."""
import json
from collections import Counter
from datetime import datetime


def read(path):
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError('evidence_too_large')
    return json.loads(path.read_text(encoding='utf-8-sig'))


def batch_evidence(root, name, relative, make):
    base = root / relative
    results, dates, paths = [], [], []
    # Only original records of this exact historical batch; no fallback to documents or mtime.
    try:
        records = sorted((base / 'probe').glob('*/record.json'))
        if not records:
            raise ValueError('no_run_records')
        for path in records:
            record, judge = read(path), read(path.with_name('judge.json'))
            if record.get('task_id') != judge.get('task_id') or judge.get('verdict') not in ('pass', 'fail'):
                raise ValueError('record_judge_mismatch')
            stamp = datetime.fromisoformat(record['end_time'])
            if stamp.tzinfo is None:
                raise ValueError('timestamp_timezone_missing')
            dates.append(stamp)
            results.append(judge['verdict'])
            paths.extend([str(path.relative_to(root)), str(path.with_name('judge.json').relative_to(root))])
        counts = Counter(results)
        return make(name, 'warning', '历史原判样本；不证明当前版本可用', details={
            'verification': 'historical', 'last_verified_at': max(dates).isoformat(),
            'result': dict(counts), 'sample_count': len(results), 'evidence_paths': paths,
            'limitations': '非盲测；批次判据可能已被替代。TASK150历史1/12与TASK151同轨迹重建0/12不同；新版恢复未真跑。'})
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return make(name, 'warning', '未验证：缺少或损坏的逐run原始证据', details={
            'verification': 'unverified', 'last_verified_at': None, 'result': None,
            'evidence_paths': [str(base)], 'error_type': type(exc).__name__})


def n11_evidence(root, make):
    path = root / '.work/task154/analysis.json'
    try:
        analysis = read(path)
        batch = read(path.with_name('batch.json'))
        if not isinstance(batch, list) or not batch:
            raise ValueError('no_trials')
        counts = {arm: Counter() for arm in ('single', 'team')}
        for trial in batch:
            folder = (root / trial['path']).resolve()
            if not folder.is_relative_to((path.parent / 'runs').resolve()):
                raise ValueError('evidence_path_outside_runs')
            record, judge = read(folder / 'record.json'), read(folder / 'judge.json')
            if judge['verdict'] not in ('pass', 'fail') or record['arm'] != trial['arm']:
                raise ValueError('invalid_trial')
            if record['task_id'] != trial['task'] or judge['task_id'] != trial['task']:
                raise ValueError('trial_task_mismatch')
            counts[record['arm']][judge['verdict']] += 1
        for arm, count in counts.items():
            if analysis['arms'][arm]['n'] != sum(count.values()) or analysis['arms'][arm]['success'] != count['pass']:
                raise ValueError('analysis_mismatch')
        return make('N11.single_vs_team', 'warning', '历史headless对照；记录缺结束时间，最后验证时间未知', details={
            'verification': 'historical', 'last_verified_at': None, 'timestamp_status': 'not_recorded',
            'result': {arm: dict(count) for arm, count in counts.items()},
            'evidence_paths': [str(path), str(path.with_name('batch.json')), str(path.parent / 'runs')],
            'limitations': '非产品/team验收；不以文件mtime或报告日期冒充运行时间；不证明当前版本可用。'})
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return make('N11.single_vs_team', 'warning', '未验证：analysis与逐run证据缺失或不一致', details={
            'verification': 'unverified', 'last_verified_at': None, 'result': None,
            'evidence_paths': [str(path)], 'error_type': type(exc).__name__})


def evidence(root, make):
    rows = [batch_evidence(root, 'N10.TASK142', '.work/_km_task142', make),
            batch_evidence(root, 'C5.TASK150', '.work/_km_task150', make), n11_evidence(root, make)]
    for name in ('current.recovery', 'current.mcp_tool_execution', 'current.searxng_search',
                 'current.browser', 'current.report_rendering'):
        rows.append(make(name, 'warning', '未验证：无绑定当前配置与版本的行为验收', details={
            'verification': 'unverified', 'last_verified_at': None, 'result': None, 'evidence_paths': []}))
    return rows
