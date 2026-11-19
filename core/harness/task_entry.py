"""Conservative natural-language entry into the existing draft/accept workflow."""
import re

from core.harness.task_draft import extract_draft, find_draft_answer, task_new_command

_GOAL = re.compile(
    r'^(?:(?:请|麻烦|帮我|请帮我)\s*)?(?:把|将|整理|生成|创建|修改|修复|转换|复制|汇总|导出)'
    r'|^(?:please\s+)?(?:create|write|convert|copy|fix|generate|export)\s', re.I)
_QUESTION = re.compile(r'^(?:请|帮我)?(?:解释|介绍|说说|告诉我|讲讲)|[?？]|(?:是什么|怎么|如何|为什么)')


# Inspect the instruction before inline source material (CSV, prose, examples).
_INLINE_DATA = re.compile(r'[：:\n](?![\\/])')
_TASK_ACTION = re.compile(
    r'(?:保存|写入|写到|存到|存为|另存为|导出到|导出为|落盘|存盘|写进)'
    r'|(?:创建|生成|修改|修复|读取|复制|删除|新建|导出)\s*[^，。；]{0,32}(?:文件|目录|脚本文件|[\w-]+\.\w+\b)'
    r'|^(?:(?:请|麻烦|帮我)\s*)*(?:检索|搜索|联网查|运行|执行|调用工具|打开文件|读取文件)'
    r'|(?:先.+(?:然后|接着|再)|然后|接着|做成任务|建成任务|创建任务|验收条件|验收标准)'
    r'|(?:把|将)(?:那个|那份)(?:表|文件|目录)(?:格)?(?:整理|修改|复制)'
    r'|\b(?:save (?:as|to)|write to|export to|search for|run the|execute the|acceptance criteria)\b'
    r'|\b(?:create|write|copy|fix|export)\s+[^,;]{0,32}(?:\bfile\b|\bdirectory\b|[\w-]+\.\w+\b)'
    r'|\bfirst\b.+\bthen\b', re.I)


def _requires_task(text):
    instruction = _INLINE_DATA.split(text.strip(), maxsplit=1)[0]
    # A prohibition such as “不要写入文件” is not an instruction to use tools.
    instruction = re.sub(r'(?:不要|无需|不用|禁止|不必)[^，,。；;]*', '', instruction)
    return not _QUESTION.search(instruction) and bool(_TASK_ACTION.search(instruction))


async def natural_task_entry(coord, text):
    """Return a note when routed; None preserves ordinary chat and active Tasks."""
    service = getattr(coord, 'task_service', None)
    if service is None:
        return None
    if service.store.task_for_session(coord.session_id):
        return None
    if coord.has_active_or_pending():
        return '当前回答尚未结束；目标起草请等结束后再提交。' if _GOAL.search(text) and not _QUESTION.search(text) else None
    draft = getattr(coord, '_task_draft', None)
    if draft and draft['session_id'] == coord.session_id:
        answer = find_draft_answer(coord._store, coord.session_id, draft['request_message_id'])
        try:
            clarification = extract_draft(answer or '').get('clarification')
        except ValueError:
            clarification = None
        if clarification and not _QUESTION.search(text):
            return await task_new_command(coord, draft['goal'] + '\n用户补充：' + text)
        return None
    if _requires_task(text):
        return await task_new_command(coord, text)
    return None
