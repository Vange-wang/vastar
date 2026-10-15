"""显式 compact UI 门面；C07 验证/版本链在 memory.compaction。"""
from core.compact_runtime import compaction_service, preview_record


class CompactSessionMixin:
    def compact_preview(self):
        if self.is_busy():
            return {'ok': False, 'note': '正在回答中，/compact 稍后再试'}
        try:
            service = compaction_service(self)
            record = preview_record(self, service)
            row = service.publish(record)
            return {'ok': True, **row, 'actual_ratio': row['tokens_after'] / max(1, row['tokens_before'])}
        except (ValueError, RuntimeError, KeyError, TypeError, OSError) as exc:
            return {'ok': False, 'note': f'压缩未生效，保留上版；请缩小任务或调整预算：{exc}'}

    def compact_decide(self, action):
        if self.is_busy():
            return False, '正在回答中，/compact 稍后再试'
        try:
            row = compaction_service(self).decide(action)
            word = {'approved': '已批准', 'dropped': '已丢弃', 'rolled_back': '已回滚'}[action]
            return True, (f"快照 {row['snapshot_id']} {word}；估算历史 token "
                          f"{row['tokens_before']}→{row['tokens_after']}，非硬上界。"
                          '回滚恢复上一批准版本（首版回滚恢复原文）；删除/失效来源不能复活。')
        except (ValueError, KeyError) as exc:
            return False, str(exc)
