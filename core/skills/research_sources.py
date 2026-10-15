"""S01 evidence comes from scoped B29 receipts, never caller-invented page bodies."""
import json
import re
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit


class EvidenceError(ValueError):
    pass


class Links(HTMLParser):
    def __init__(self, base):
        super().__init__()
        self.base, self.links = base, set()

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.add(urljoin(self.base, href))


def today():
    return datetime.now(UTC).date()


def direct_page(url, kind):
    path = urlsplit(url).path.lower().rstrip("/")
    return kind in {"primary", "commentary"} and path not in {"", "/search", "/results"} and not path.startswith("/search/")


def _date(value, text):
    if value is None:
        return None
    if not isinstance(value, str) or value not in text:
        raise EvidenceError("时间缺少正文/原始响应支持；不得猜日期")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise EvidenceError("日期须为来源可支持的 YYYY-MM-DD；未知填 null") from None


def load_source(context, item, session_id, run_id):
    if not isinstance(item, dict):
        raise EvidenceError("来源必须为对象")
    required = {"operation_id", "topic", "value", "quote", "kind"}
    optional = {"published_at", "event_at", "original_url", "access"}
    if not required <= item.keys() or item.keys() - required - optional:
        raise EvidenceError("来源字段不完整或含未知字段")
    for key, size in (("operation_id", 128), ("topic", 128), ("value", 256), ("quote", 1000)):
        if not isinstance(item[key], str) or not 1 <= len(item[key]) <= size:
            raise EvidenceError("来源标识/论点/引文长度非法")
    operation = context.operation_ledger.store.operation_get(item["operation_id"])
    if not operation or operation["run_id"] != run_id or operation["facts"].get("session_id") != session_id:
        raise EvidenceError("来源回执缺失或不属于当前 Session/Run")
    receipt = operation["receipt"] or {}
    if operation["tool"] != "read_web_page":
        raise EvidenceError("搜索命中不是正文证据；必须读取支撑页")
    if operation["state"] != "committed" or receipt.get("status") != "executed":
        if str(receipt.get("reason", "")).startswith("HTTP 402;"):
            raise EvidenceError("付费阻断 HTTP402，未取得全文；不得伪称通读")
        raise EvidenceError("正文未取得：" + str(receipt.get("origin", "unknown")))
    page = json.loads(receipt["result"])
    if not direct_page(page["url"], item["kind"]):
        raise EvidenceError("引用必须为支撑页，搜索页/聚合页/首页不可作结论引用")
    if item["quote"] not in page["content"] or item["value"] not in item["quote"]:
        raise EvidenceError("正文不支持给定引文/值；不能用搜索摘要代替")
    match = re.fullmatch(r"artifact://tool_output/([^/]+)/v(\d+)#[0-9a-f]+", page["source_artifact"])
    if not match:
        raise EvidenceError("来源 artifact 引用非法")
    meta, raw = context.artifacts.get(match[1], int(match[2]))
    if meta["hash"] != page["source_version"]:
        raise EvidenceError("来源版本已失效")
    text = raw.decode("utf-8-sig")
    links = Links(page["url"])
    links.feed(text)
    original = item.get("original_url")
    if original and original not in links.links:
        raise EvidenceError("追溯地址不在原始页面链接中；不得捏造原始数据链")
    access = item.get("access", "excerpt")
    if access not in {"excerpt", "abstract", "paywall"}:
        raise EvidenceError("只标记已获片段/摘要/付费阻断；不得自称全文通读")
    published, event = _date(item.get("published_at"), text), _date(item.get("event_at"), text)
    stale = bool(published and (today() - date.fromisoformat(published)).days > 365)
    return {**item, "url": page["url"], "title": page["title"], "published_at": published,
            "event_at": event, "retrieved_at": page["retrieved_at"], "source_artifact": page["source_artifact"],
            "source_version": page["source_version"], "access": access, "original_url": original,
            "stale": stale, "read_scope": "仅核验已取得片段，不声明通读", "classification": "model_proposed",
            "cache_hit": page["cache_hit"], "cached_at": page["cached_at"]}
