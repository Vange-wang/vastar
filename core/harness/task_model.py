"""确定性计划校验；模型提案不能制造完成事实或修改用户验收契约。"""
import copy

STATES = {"READY", "RUNNING", "VERIFYING", "COMPLETED", "WAITING_INPUT", "BLOCKED",
          "FAILED", "CANCEL_REQUESTED", "CANCELLED", "INTERRUPTED", "RECONCILING"}


class TaskConflict(ValueError):
    pass


def require_revision(actual, expected):
    if type(expected) is not int or expected != actual:
        raise TaskConflict(f"expected_revision 缺失或冲突：当前 {actual}，收到 {expected}")


def validate_steps(steps, previous=()):
    if not isinstance(steps, list) or not steps:
        raise ValueError("steps 必须为非空数组")
    old = {s["step_id"]: s for s in previous}
    result, names = [], set()
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError("每个步骤必须为对象")
        name = step.get("step_id")
        if not isinstance(name, str) or not name.startswith("step-") or name in names:
            raise ValueError("步骤身份必须为唯一 step- ID")
        names.add(name)
        if not isinstance(step.get("goal"), str) or not step["goal"].strip():
            raise ValueError("步骤目标不可空")
        before = old.get(name, {})
        if step.get("status", "READY") != before.get("status", "READY"):
            raise ValueError("T25 不得伪造完成证据或直接改变执行状态")
        if step.get("evidence_refs", []) != before.get("evidence_refs", []):
            raise ValueError("T25 不得伪造完成证据")
        if before.get("status") == "COMPLETED" and any(
                step.get(k, []) != before.get(k, []) for k in ("goal", "depends_on")):
            raise ValueError("已完成步骤不得改写；请新增步骤")
        if before.get("status") == "COMPLETED" and any(
                step.get(k, before.get(k)) != before.get(k) for k in ("acceptance_ids", "independent")):
            raise ValueError("已完成步骤验收映射不可改写")
        result.append({"step_id": name, "goal": step["goal"],
                       "depends_on": list(step.get("depends_on", [])),
                       "status": before.get("status", "READY"),
                       "acceptance_ids": list(step.get("acceptance_ids", before.get("acceptance_ids", []))),
                       "independent": bool(step.get("independent", before.get("independent", False))),
                       "evidence_refs": copy.deepcopy(before.get("evidence_refs", [])),
                       "remaining_work": step.get("remaining_work", step["goal"])})
    if any(s["status"] == "COMPLETED" and k not in names for k, s in old.items()):
        raise ValueError("不得删除已完成步骤的证据")
    graph = {s["step_id"]: s["depends_on"] for s in result}
    visiting, visited = set(), set()

    def visit(name):
        if name not in graph:
            raise ValueError("依赖引用不存在")
        if name in visiting:
            raise ValueError("循环依赖")
        if name in visited:
            return
        visiting.add(name)
        for dependency in graph[name]:
            visit(dependency)
        visiting.remove(name)
        visited.add(name)
    for name in graph:
        visit(name)
    return result


def validate_acceptance(previous, proposed, explanations=None):
    removed = set(previous) - set(proposed)
    if removed - set(explanations or {}):
        raise ValueError("删掉未解释的验收项")
    if previous != proposed:
        raise ValueError("模型不得更改 acceptance；解释仅作建议，须用户显式裁决")
