"""确定性验收；只使用受管产物/账本事实，模型意见不参与真假裁定。"""
import hashlib
import json
from pathlib import Path

from core.artifacts import ArtifactError
from core.harness.task_completion import commit_validation, completion_results


def check_schema(value, schema):
    validate_schema_contract(schema)
    return _check_value(value, schema)


def validate_schema_contract(schema):
    allowed = {"type", "properties", "required", "additionalProperties", "items", "enum"}
    if not isinstance(schema, dict) or set(schema) - allowed:
        raise ValueError("schema 使用未支持关键字；不能静默忽略")
    if schema.get("type") not in {"object", "array", "string", "integer", "number", "boolean", "null"}:
        raise ValueError("schema.type 不在支持子集内")
    if "additionalProperties" in schema and type(schema["additionalProperties"]) is not bool:
        raise ValueError("additionalProperties 本批仅支持布尔值")
    if not isinstance(schema.get("properties", {}), dict):
        raise ValueError("properties 必须为对象")
    required = schema.get("required", [])
    if not isinstance(required, list) or any(not isinstance(v, str) for v in required):
        raise ValueError("required 必须为字符串列表")
    if "enum" in schema and not isinstance(schema["enum"], list):
        raise ValueError("enum 必须为数组")
    for child in schema.get("properties", {}).values():
        validate_schema_contract(child)
    if "items" in schema:
        validate_schema_contract(schema["items"])


def _json_equal(value, expected):
    """JSON numbers compare numerically, including inside compound enum values."""
    if type(value) in (int, float) and type(expected) in (int, float):
        return value == expected
    if type(value) is not type(expected):
        return False
    if isinstance(value, dict):
        return value.keys() == expected.keys() and all(_json_equal(v, expected[k]) for k, v in value.items())
    if isinstance(value, list):
        return len(value) == len(expected) and all(_json_equal(a, b) for a, b in zip(value, expected, strict=True))
    return value == expected


def _check_value(value, schema):
    types = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool,
             "number": (int, float), "null": type(None)}
    kind = schema.get("type")
    if kind not in types or not isinstance(value, types[kind]):
        return False
    if kind in {"integer", "number"} and isinstance(value, bool):
        return False
    if "enum" in schema and not any(_json_equal(value, member) for member in schema["enum"]):
        return False
    if kind == "object":
        props = schema.get("properties", {})
        if set(schema.get("required", [])) - set(value):
            return False
        if schema.get("additionalProperties") is False and set(value) - set(props):
            return False
        return all(check_schema(value[k], v) for k, v in props.items() if k in value)
    if kind == "array" and "items" in schema:
        return all(check_schema(v, schema["items"]) for v in value)
    return True


class Validator:
    def __init__(self, store, artifacts):
        self.store, self.artifacts = store, artifacts

    def _artifact(self, ref):
        if not isinstance(ref, str) or not ref.startswith("artifact://"):
            raise ValueError("必须引用受管证据，不接受模型自报结果")
        aid, version, scope = self.artifacts._parse_ref(ref)
        meta, payload = self.artifacts.get(aid, version)
        if scope != meta["scope"] or ref.rsplit("#", 1)[-1] != meta["hash"][:12]:
            raise ValueError("产物作用域不符")
        return payload

    def check_step_result(self, task, step, criterion, evidence):
        if not isinstance(criterion, dict) or criterion.get('kind') not in {'deliverable', 'code_check'}:
            return self.check(criterion, evidence)
        try:
            if not isinstance(evidence, dict) or not isinstance(evidence.get('operation_id'), str):
                raise ValueError('验收须引用任务操作编号')
            op = self.store.operation_get(evidence['operation_id'])
            latest = self.store._conn.execute(
                'SELECT run_id,terminal_state FROM runs WHERE task_id=? AND step_id=? ORDER BY rowid DESC LIMIT 1',
                (task['task_id'], step['step_id'])).fetchone()
            if not op or not latest or latest['terminal_state'] not in {'COMPLETED', 'INTERRUPTED'}:
                raise ValueError('缺少本步骤已结束的运行')
            if criterion['kind'] == 'code_check':
                from core.skills.code_change_delivery import verify_code_check
                proof = verify_code_check(self.store, self.artifacts, criterion, evidence,
                                          task_id=task['task_id'], step_id=step['step_id'], run_id=latest['run_id'])
                return {'passed': True, 'kind': criterion['kind'], 'outputs': proof['outputs'],
                        'evidence': proof['evidence'], 'accounted_operation_ids': proof['accounted_operation_ids'],
                        'run_id': latest['run_id']}
            else:
                from core.harness.office_delivery import office_outputs
                if (op['task_id'], op['step_id'], op['run_id']) != (task['task_id'], step['step_id'], latest['run_id']):
                    raise ValueError('办公成果不属于本任务步骤的当前运行')
                outputs = office_outputs(self.artifacts, criterion, op)
            return {'passed': True, 'kind': criterion['kind'], 'outputs': outputs, 'evidence': evidence}
        except (ValueError, KeyError, TypeError, OSError, ArtifactError) as exc:
            return {'passed': False, 'reason': str(exc), 'evidence': evidence}

    def check(self, criterion, evidence):
        try:
            kind = criterion["kind"]
            observed = {}
            if kind == "schema":
                payload = self._artifact(evidence["artifact_ref"])
                ok = check_schema(json.loads(payload), criterion["schema"])
            elif kind == "file_hash":
                path = Path(criterion["path"])
                if not path.is_absolute():
                    raise ValueError("验收路径必须明确为绝对路径")
                payload = path.read_bytes()
                actual_hash = hashlib.sha256(payload).hexdigest()
                observed = {str(path): {"exists": True, "sha256": actual_hash}}
                ok = actual_hash == criterion["sha256"]
                if "contains" in criterion:
                    ok = ok and criterion["contains"] in payload.decode("utf-8")
            elif kind == "command":
                op = self.store.operation_get(evidence["operation_id"])
                receipt = op["receipt"] if op and op["state"] == "committed" else {}
                facts = receipt.get("audit", {}).get("command_result", {})
                ok = (bool(facts) and facts.get("argv") == criterion["argv"]
                      and facts.get("exit_code") == criterion.get("exit_code", 0)
                      and facts.get("stdout_sha256") == criterion["stdout_sha256"])
            elif kind == "source_coverage":
                required = criterion["sources"]
                refs = evidence["sources"]
                # 来源必须是受管证据；每项内容与用户冻结 hash 一致，不能只报 ID。
                ok = bool(required) and set(required) <= set(refs) and all(
                    hashlib.sha256(self._artifact(refs[k])).hexdigest() == h for k, h in required.items())
            else:
                raise ValueError("未知验收器")
            return {"passed": bool(ok), "kind": kind, "evidence": evidence, "observed": observed}
        except (ValueError, KeyError, TypeError, OSError, ArtifactError) as exc:
            return {"passed": False, "reason": str(exc), "evidence": evidence}

    def validate_step(self, tasks, session_id, step_id, evidence, reviewer=None):
        if not isinstance(evidence, dict):
            raise ValueError("evidence 必须为对象")
        task = tasks.current(session_id)
        row = self.store._conn.execute("SELECT recovery_error FROM task_runtime WHERE task_id=?", (task["task_id"],)).fetchone()
        if row and row[0]:
            raise ValueError(f"恢复需核实：{row[0]}")
        if task["status"] in {"BLOCKED", "CANCELLED", "COMPLETED"}:
            raise ValueError("任务终态或返工超限，需要用户处理，禁止无限重规划")
        step = next((s for s in task["steps"] if s["step_id"] == step_id), None)
        if not step or step["status"] not in {"RUNNING", "VERIFYING", "BLOCKED"}:
            raise ValueError("必须验收已明确执行的步骤")
        from core.harness.questions import QuestionService
        waiting = any(q["required"] for q in QuestionService(tasks).pending(task["task_id"]))
        if waiting and not step["independent"]:
            raise ValueError("等待必要信息；当前步骤未获计划允许独立继续")
        keys = step["acceptance_ids"]
        if not keys:
            raise ValueError("步骤没有用户明确关联的验收项；needs_input")
        results = {k: self.check_step_result(task, step, task["acceptance"][k], evidence.get(k, {})) for k in keys}
        checked = completion_results(self, task, step, results)
        return commit_validation(self.store, task, step, checked, reviewer, waiting)
