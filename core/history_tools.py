"""B21 装配与 T21（search_history）；身份来自可信本地装配，不由模型授权。"""
import getpass
import json

from core.tools.registry import ToolSpec
from memory.retrieval import HistoryAccess, HistoryService

SEARCH_HISTORY = 'search_history'


def install_history(tools, store, paths, *, user_id=None):
    user = user_id or getpass.getuser()
    store.bind_history_scope(user, paths.project_id)
    service = HistoryService(store, HistoryAccess(user, paths.project_id), tools.artifacts)
    tools.history_service = service
    tools.registry.register(ToolSpec(
        name=SEARCH_HISTORY, description='只检索当前用户、项目的会话历史（T21 首版，不含长期事实）。'
        '分数不是事实概率；空/不可用/被拒分开；next_cursor 非空须分页续查。', level='B',
        parameters={'type': 'object', 'properties': {
            'query': {'type': 'string'}, 'source_ids': {'type': 'array', 'items': {'type': 'string'}},
            'since': {'type': 'string'}, 'until': {'type': 'string'},
            'after': {'type': 'integer', 'minimum': 0}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 200}},
            'required': ['query'], 'additionalProperties': False},
        run=lambda args, **kwargs: json.dumps(search(service, args), ensure_ascii=False),
        resource_scope='history', reconcile='reread'))
    return service


def search(service, args):
    allowed = {'query', 'source_ids', 'since', 'until', 'after', 'limit', 'user_id', 'project_id'}
    if set(args) - allowed or 'query' not in args:
        return {'status': 'unavailable', 'items': [], 'reason': '不支持的检索参数'}
    return service.search(**args)
