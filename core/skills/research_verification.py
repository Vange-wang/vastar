"""Deterministic provenance checks; source truth and semantic reasoning are not certified."""
import json
from collections import defaultdict

from core.skills.research_sources import EvidenceError, load_source


def resolve_conflict(rows):
    primary = [r for r in rows if r["kind"] == "primary"]
    if not primary:
        return None, "缺一手来源；转载数量不能决定正确性"
    values = {r["value"] for r in primary}
    if len(values) != 1:
        return None, "原始来源仍冲突，需进一步核对原始数据/适用条件"
    urls = {r["url"] for r in primary}
    if len({r["value"] for r in rows}) > 1:
        untraced = [r for r in rows if r["kind"] != "primary" and r["original_url"] not in urls]
        if untraced:
            return None, "冲突尚未追溯到已读取的原始数据，不能按网页数量投票"
    return primary[0], None


def verify(context, args, session_id, run_id):
    if set(args) - {"sources", "inferences"} or len(json.dumps(args).encode()) > 64000:
        raise EvidenceError("核验请求超过边界或含未知字段")
    sources, inferences = args.get("sources", []), args.get("inferences", [])
    if not isinstance(sources, list) or len(sources) > 12 or not isinstance(inferences, list) or len(inferences) > 8:
        raise EvidenceError("最多 12 条来源、8 条推断")
    rows, gaps, seen = [], [], set()
    for item in sources:
        try:
            row = load_source(context, item, session_id, run_id)
            key = (row["url"], row["source_version"], row["topic"], row["value"])
            if key not in seen:
                rows.append(row)
                seen.add(key)
        except EvidenceError as exc:
            gaps.append(str(exc))
        except Exception:
            gaps.append("来源回执/artifact 无法核实，未形成结论")
    facts, citations, groups = [], [], defaultdict(list)
    for row in rows:
        groups[row["topic"]].append(row)
        if not row["published_at"] or not row["event_at"]:
            gaps.append(row["topic"] + "：发布时间/事件时间未知，时效尚未核实")
    for topic, candidates in groups.items():
        chosen, issue = resolve_conflict(candidates)
        if issue:
            gaps.append(topic + "：" + issue)
        elif chosen["stale"] or chosen["access"] == "paywall":
            gaps.append(topic + "：过时或付费阻断，不能形成当前事实结论")
        else:
            fact = {"topic": topic, "kind": "source_statement", "quote": chosen["quote"],
                    "citation": chosen["url"], "operation_id": chosen["operation_id"]}
            facts.append(fact)
            citations.append({"statement": chosen["quote"], "url": chosen["url"],
                              "source_artifact": chosen["source_artifact"]})
    assumptions = _inferences(inferences, facts, gaps)
    if not rows:
        gaps.append("缺口：没有可核验正文；无结果/搜索命中/抓取失败均不等于正文支持")
    report = {"conclusion": ["来源记载：" + f["quote"] for f in facts] or ["证据不足，暂不下事实结论"],
              "facts": facts, "inferences": assumptions, "evidence_table": rows,
              "unresolved_questions": list(dict.fromkeys(gaps)), "citations": citations,
              "verification": "引用/来源链机械核验；来源分类为提议，语义及原始数据真实性仍需审阅",
              "trust": "external_evidence", "automatic_memory_write": False}
    if len(json.dumps(report, ensure_ascii=False).encode()) > 48000:
        raise EvidenceError("核验输出过大；请缩小来源批次，不静默截断")
    return report


def _inferences(items, facts, gaps):
    ids = {f["operation_id"] for f in facts}
    result = []
    for item in items:
        if (not isinstance(item, dict) or set(item) != {"text", "premises"}
                or not isinstance(item["text"], str) or not 1 <= len(item["text"]) <= 1000
                or not isinstance(item["premises"], list) or not item["premises"]
                or any(not isinstance(p, str) or p not in ids for p in item["premises"])):
            gaps.append("推断缺少已核验引文前提，不升级为事实")
        else:
            result.append({**item, "kind": "inference", "status": "reasoning_not_verified"})
    return result
