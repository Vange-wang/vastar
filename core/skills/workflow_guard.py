"""首批文件工作流的确定性边界；只收紧已有工具权限，不绕过工具底座。

S05 拒绝写；S08 每项由真实 ASK 展示精确预览并批准，auto/旧授权也不能跳过。
文件事实/硬拒/备份/TOCTOU/提交由既有执行链负责。这里不新增持久任务引擎。
"""
import json

from core.tools.ask_gate import AskQuestion
from core.tools.operation_receipt import ToolOutcome
from core.tools.operation_resources import file_facts
from core.tools.operation_runner import thread_call


def is_organization(skills) -> bool:
    return skills is not None and "file-organization" in skills.active_names()


def reject_write(skills, spec, args_sum):
    if skills is None or spec.effect == "read":
        return None
    active = skills.active_names()
    reason = ""
    if "codebase-understanding" in active:
        reason = "代码库理解技能仅允许只读操作；本次写操作未执行"
    elif "project-knowledge" in active:
        reason = "项目知识技能仅整理候选和只读检索；归档须用户显式确认，不允许模型自授权写入"
    elif "file-organization" in active and spec.effect not in ("move", "delete"):
        reason = "文件整理技能只支持经预览批准的移动/删除；本次写操作未执行"
    if reason:
        return ToolOutcome(spec.name, "rejected", reason, args_summary=args_sum,
                           origin="skill_policy")
    return None


def _snapshot(path):
    facts = file_facts([path])
    try:
        stat = path.stat()
        facts[str(path)].update(size=stat.st_size, mtime_ns=stat.st_mtime_ns)
    except OSError as exc:
        facts[str(path)]["verification_error"] = type(exc).__name__
    return facts


async def authorize_preview(executor, spec, plan, decision, src, extra, backup,
                            args_sum, started, emit, cancel, session_id, request_id):
    from core.tools.executor import _ask_user, _denied_outcome
    gate = executor._ctx.ask_gate
    if gate is None or emit is None:
        return _denied_outcome(spec.name, "文件整理需要可见影响预览与明确批准，通道未装配",
                               "ask_unavailable", args_sum, plan, started, origin="skill_policy")
    facts = await thread_call(_snapshot, src)
    if "verification_error" in facts[str(src)]:
        return _denied_outcome(spec.name, "无法核实整理预览的文件事实，未执行",
                               "preview_unverified", args_sum, plan, started, origin="skill_policy")
    dest = extra.get("dest")
    lines = ["文件整理影响预览（尚未执行）：",
             f"旧路径：{src}", f"新路径：{dest if dest else '删除（可逆）'}",
             f"影响：{plan.detail}", f"当前事实：{json.dumps(facts, ensure_ascii=False)}"]
    if plan.overwrite:
        lines.append(f"名称冲突：批准即明确同意覆盖；旧目标备份：{backup}")
    lines.append("1 允许本项   2 拒绝本项（本预览不接受同类动作批量授权）")
    question = AskQuestion(spec.name, decision.action_category, decision.location_category,
                           lines, allow_session_grant=False)
    await emit({"tool": spec.name, "lines": lines, "source": "model",
                "action_category": decision.action_category,
                "location_category": decision.location_category,
                "workflow": "file-organization", "preview": facts})
    answer = await _ask_user(gate, question, session_id=session_id, request_id=request_id,
                             project_root=executor._ctx.project_root, cancel_event=cancel)
    if not answer.approved:
        return _denied_outcome(spec.name, f"整理预览未获批准：{gate.deny_label(answer.approval_source)}",
                               answer.approval_source, args_sum, plan, started, origin="ask_gate")
    if await thread_call(_snapshot, src) != facts:
        return _denied_outcome(spec.name, "文件已变化，旧预览批准已失效，请重新预览",
                               "toctou_recheck", args_sum, plan, started, origin="toctou_recheck")
    return "ALLOW", answer.approval_source


def attach_receipt(outcome, src, dest):
    """只根据机械回执填字段；无事实用 null，不能捏造 hash 或恢复位置。"""
    before = outcome.audit.get("before", {}).get(str(src), {})
    after = outcome.audit.get("after", {}).get(str(dest), {}) if dest else {}
    recovery = outcome.contract.get("recovery")
    if dest and outcome.status == "executed":
        recovery = {"from": str(dest), "to": str(src),
                    "overwritten_target_backup": outcome.audit.get("backup_path"),
                    "note": "先核对目标未变化，再逆向移动；覆盖前旧目标须另从备份恢复"}
    receipt = {"old_path": str(src), "new_path": str(dest) if dest else None,
               "hash": after.get("sha256") or before.get("sha256"),
               "status": outcome.status, "recovery": recovery,
               "reason": outcome.reason}
    outcome.contract["organization"] = receipt
    text = "\n整理逐项回执：" + json.dumps(receipt, ensure_ascii=False)
    if outcome.status == "executed":
        outcome.result += text
    else:
        outcome.reason += text
