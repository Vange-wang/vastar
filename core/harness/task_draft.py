"""/task new 起草与人话摘要（HM-144/HM-145/HM-146，TASK-2026-10-05-78）。

口径：
- 起草走**正常 Run**（模型调用不绕开 model_gateway 唯一供应商边界）：/task new 只是
  提交一条带起草指令的用户输入；模型回答里的 ```json 计划草案即「展示给用户确认」。
- 用户显式确认不可省：/task ok 才落库；落库仍走既有 accept（全部硬校验原样生效，
  本模块只做「草案预检」，不替代也不弱化 accept）。
- 旧会话不自动变任务：/task ok 只消费本会话 /task new 登记过的起草请求；普通聊天
  不产生草稿，ok 显式拒绝（反例语义保持）。
- HM-146：命令层 accept 与草案预检都要求 budget 含正整数 max_calls（缺则显式报错
  并给示例），杜绝「建得出、永远派发不了」的任务；服务层契约与派发期兜底不变。
"""
import json
import re

from core.harness.task_model import validate_steps
from core.harness.task_service import TaskService

DRAFT_INSTRUCTION = (
    "[结构化任务起草请求] 请为下面的目标起草一份任务计划，回答中只输出一个 ```json 代码块"
    "（不要输出其他内容）。JSON 字段：\n"
    '"goal"：字符串，任务目标；\n'
    '"acceptance"：非空对象，键 a1/a2…，值为确定性验收对象，不能是描述字符串；'
    '文件成果使用 {"kind":"file_hash","path":"绝对输出路径","sha256":"预期内容SHA256"}；'
    '报告或表格使用 {"kind":"deliverable","tool":"generate_report","source_path":"原始输入绝对路径",'
    '"files":["pdf","docx"]}。表格工具为 analyze_spreadsheet，files 可含 analysis.json、grouped-1.json。'
    '报告选列可用 fields 按顺序列出字段。表格可验收 cleaned.xlsx，group_by 可约定 key_column、value_column（从1开始）。'
    '已知输入 SHA256 时可用 source_sha256 代替 source_path，无须预测生成文件的路径和哈希。'
    '编码任务可用 {"kind":"code_check","workspace":"Git 项目绝对路径","scope":["源文件相对路径"],'
    '"command":"批准的离线 pwsh 验证命令"}；'
    '命令使用 {"kind":"command","argv":["程序","参数"],"exit_code":0,"stdout_sha256":"预期输出SHA256"}。'
    '不能猜造路径、内容或哈希；缺关键参数时只输出 {"clarification":"请补充哪些信息"}，不生成计划。\n'
    '"constraints"：数组（可为空）；\n'
    '"budget"：对象，必须含 "max_calls"（正整数，如 {"max_calls": 10}）；\n'
    '"steps"：非空数组，每项 {"step_id": "step-1"（step- 前缀且唯一）, "goal": "步骤目标", '
    '"depends_on": [前置 step_id], "acceptance_ids": [acceptance 的键], "independent": true/false}。\n'
    "这里只起草，不执行工具；用户 /task ok 接受后才可选步骤执行，最后 /task deliver 验收交付。\n目标：")


def is_draft_request(request):
    """The existing draft instruction is a restrictive, never privilege-granting marker."""
    return request.input_text.startswith(DRAFT_INSTRUCTION)

_FENCE = re.compile(r"```(?:json)?[ \t]*\n(.*?)```", re.S)


def build_draft_text(goal: str, budget=None) -> str:
    proposal = ('\n预算草案初值（四维账户，用户可改；须 /task ok 确认，不改变已有任务）：'
                + json.dumps(budget, ensure_ascii=False) + '\n目标：') if budget else ''
    return DRAFT_INSTRUCTION + proposal + goal


def _decode_object(text: str):
    """从文本中找第一个可完整解析的 JSON 对象（raw_decode 逐 '{' 尝试，容忍嵌套）。"""
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            data, _ = decoder.raw_decode(text, match.start())
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def extract_draft(text: str) -> dict:
    """从起草回答提取 JSON 计划（优先 ```json 围栏块；找不到即显式报错，不猜造）。"""
    blocks = [m.group(1) for m in _FENCE.finditer(text)] or [text]
    for block in blocks:
        data = _decode_object(block)
        if data is not None:
            return data
    raise ValueError("起草回答中未找到可解析的 JSON 计划")


def validate_draft(data: dict) -> dict:
    """草案预检：与 accept 同口径（accept 落库时还会再查一遍，硬校验不省）。"""
    goal, acceptance = data.get("goal"), data.get("acceptance")
    constraints, budget = data.get("constraints", []), data.get("budget", {})
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError("草案缺 goal（非空字符串）")
    if not isinstance(acceptance, dict) or not acceptance:
        raise ValueError("草案缺 acceptance（非空对象）")
    if not isinstance(constraints, list) or not isinstance(budget, dict):
        raise ValueError("草案 constraints 须为数组、budget 须为对象")
    require_max_calls(budget)
    steps = validate_steps(data.get("steps"))
    TaskService._check_mapping(steps, acceptance)
    return {"goal": goal.strip(), "acceptance": acceptance,
            "constraints": constraints, "budget": budget, "steps": steps}


def require_max_calls(budget) -> None:
    """HM-146：budget 须含正整数 max_calls（否则任务建得出、永远派发不了）。"""
    if isinstance(budget, dict) and (type(budget.get("max_calls")) is not int
                                     or budget["max_calls"] <= 0):
        raise ValueError('budget 须含 "max_calls": 正整数（如 {"max_calls": 10}）；'
                         "缺了它任务永远无法派发")


def task_summary(task: dict) -> str:
    """HM-145：accept/ok 成功的人话摘要；内部字段细节留给 /task status。"""
    lines = [f"任务已接受：{task['task_id']} · 状态 {task['status']}",
             f"目标：{task['goal']}", "步骤："]
    for s in task["steps"]:
        lines.append(f"  {s['step_id']} [{s['status']}] {s['goal']}")
    remaining = ", ".join(task["remaining_work"]) or "无"
    lines.append(f"剩余工作：{remaining} · 细节 /task status")
    lines.append("用 /task run 执行已确认的下一步，或 /task step <Step ID> 选步骤并输入要求。结束后 /task deliver 验收交付。")
    return "\n".join(lines)


def present_result(result, *, as_json: bool):
    """命令层统一呈现：默认人话摘要；--json 原样返回 dict（机器可读）。"""
    if as_json:
        return result
    if isinstance(result, dict) and "checkpoint_status" in result:
        return task_summary(result["result"]) + f"\n注意：检查点未保存（{result['reason']}）"
    return task_summary(result)


def find_draft_answer(store, session_id: str, request_message_id: str) -> str | None:
    """起草请求之后的第一条完整助手回答（只读直读；无 = 尚未起草成功）。"""
    with store._lock:
        row = store._conn.execute(
            "SELECT content FROM messages WHERE session_id=? AND role='assistant'"
            " AND status='complete'"
            " AND rowid > (SELECT rowid FROM messages WHERE message_id=?)"
            " ORDER BY rowid LIMIT 1", (session_id, request_message_id)).fetchone()
    return row["content"] if row else None


def artifacts_blocker(coord) -> str | None:
    """HM-149：产物库不可用的前置拦截文案（B13 口径：如实报错 + 可复制的初始化命令）。

    不静默退化、不自动初始化——只把「不可用」的提示提前到花模型调用/落库之前；
    产物库可用性判据本身不变（put 时 _require_dir 的显式报错仍在）。
    """
    ctx = getattr(coord, "task_tools_context", None)
    artifacts = getattr(ctx, "artifacts", None)
    if artifacts is not None and artifacts.available():
        return None
    root = getattr(ctx, "project_root", None) or "<项目路径>"
    return ("产物库不可用：data_dir 未初始化——建任务前请先初始化项目数据目录："
            f"vastar --project \"{root}\" --data-dir \"<数据目录>\" --init-project"
            "（B13 口径：显式初始化，不静默退化；初始化后再试）")


async def task_new_command(coord, goal: str) -> str:
    """/task new <自然语言目标>：登记起草请求并提交正常 Run（模型起草在回答里展示）。"""
    from core.llm.pricing import require_task_model
    require_task_model(coord)
    goal = goal.strip()
    if not goal:
        raise ValueError("用法：/task new <自然语言目标>")
    if coord.task_service.store.task_for_session(coord.session_id):
        raise ValueError("当前会话已有 Task；不自动另建目标（/task status 查看，/new 开新会话）")
    blocker = artifacts_blocker(coord)   # HM-149：花模型调用起草之前先拦
    if blocker:
        raise ValueError(blocker)
    budget = getattr(getattr(coord, 'task_tools_context', None), 'task_budget_defaults', {})
    receipt = await coord.submit(build_draft_text(goal, budget))
    coord._task_draft = {"session_id": coord.session_id,
                         "request_message_id": receipt.input_id, "goal": goal}
    return ("已提交起草请求：模型将在下一条回答中给出 ```json 计划草案；"
            "确认落库 /task ok（--json 看明细）· 放弃 /task drop")


def task_ok_command(coord, tail: str = ""):
    """/task ok：用户显式确认——提取本会话草案、预检后复用 accept 落库。"""
    from core.harness.task_commands import _source, checkpoint_command  # 延迟导入防循环
    draft = getattr(coord, "_task_draft", None)
    if not draft or draft["session_id"] != coord.session_id:
        raise ValueError("无待确认的起草草案；先 /task new <自然语言目标>（普通聊天不会产生草案）")
    answer = find_draft_answer(coord._store, coord.session_id, draft["request_message_id"])
    if answer is None:
        raise ValueError("起草回答尚未落库（模型仍在起草或起草未成功）；稍后再 /task ok")
    blocker = artifacts_blocker(coord)   # HM-149：落库前最后拦截（new 与 ok 之间环境可能变化）
    if blocker:
        raise ValueError(blocker)
    try:
        data = extract_draft(answer)
        if data.get('clarification'):
            return f"仍缺信息：{data['clarification']}；请补充后继续，尚未建立 Task。"
        plan = validate_draft(data)
    except ValueError as exc:
        raise ValueError(f"草案不可用：{exc}；/task drop 放弃后重新 /task new") from exc
    task = coord.task_service.accept(coord.session_id, _source(coord, draft["goal"]), **plan)
    coord._task_draft = None
    return present_result(checkpoint_command(coord, task), as_json=tail.strip() == "--json")


def task_drop_command(coord) -> str:
    """/task drop：放弃待确认草案（不落库、不留任务事实）。"""
    had = getattr(coord, "_task_draft", None)
    coord._task_draft = None
    return "已放弃起草草案" if had else "无待确认的起草草案"
