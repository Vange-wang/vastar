"""外部副作用核实不等于 exactly-once；证据不足必须 unknown。"""
from pathlib import Path

from core.tools.operation_resources import file_facts, proves_unchanged


def reconcile_facts(operation):
    facts = operation["facts"]
    if operation['tool'] in {'browser_click', 'browser_fill'}:
        return {'status': 'committed' if operation['state'] == 'committed' and operation['receipt'] else 'unknown',
                'evidence': {'reason': 'use browser_reconcile; DOM node identity/file facts cannot prove a web effect',
                             'proof': facts.get('business_proof')}}
    if operation["tool"] in {"web_search", "read_web_page"}:
        confirmed = operation["state"] == "committed" and operation["receipt"]
        return {"status": "committed" if confirmed else "unknown", "evidence": {
            "receipt": operation["receipt_ref"], "network": facts.get("network"),
            "reason": "received-content receipt only; issued requests cannot be undone or inferred absent"}}
    if operation["tool"] == "start_process":
        return {"status": "unknown", "evidence": {
            "process": facts.get("process"),
            "reason": "process exit/PID absence cannot prove arbitrary command effects; never auto-retry"}}
    before = operation["resource_version"]
    after = file_facts([Path(p) for p in before])
    evidence = {"before": before, "observed": after, "expected": facts.get("expected"),
                "backup": None, "receipt_ref": operation["receipt_ref"]}
    if operation["state"] == "prepared":
        return {"status": "not_committed" if proves_unchanged(before, after) else "unknown", "evidence": evidence}
    expected = facts.get("expected") or {}
    backup = facts.get("backup_path")
    target = facts.get("target")
    backup_ok = not (before.get(target, {}).get("exists") and operation["classification"] == "idempotent_write")
    if backup:
        evidence["backup"] = file_facts([Path(backup)])[str(Path(backup))]
        backup_ok = (evidence["backup"].get("sha256") is not None
                     and evidence["backup"].get("sha256") == before.get(target, {}).get("sha256"))
    matches = expected and all(after.get(path) == value for path, value in expected.items())
    if matches and backup_ok:
        return {"status": "committed", "evidence": evidence}
    if operation["state"] == "committed" and operation["receipt"]:
        # 正常完成回执仍须资源与回执的提交后快照一致，不能凭返回文本。
        recorded_after = facts.get("after")
        if proves_unchanged(recorded_after, after):
            return {"status": "committed", "evidence": evidence}
    if proves_unchanged(before, after) and not expected:
        return {"status": "not_committed", "evidence": evidence}
    # 即使已开始提交且目标回到旧值，也可能写后被外部覆盖；不猜未提交。
    return {"status": "unknown", "evidence": evidence}
