"""标准库 HTTP 流式 POST（urllib，零第三方依赖）。

设计约束（基线 §3/§4）：
- 事件管道有界缓冲 + 显式背压，**不丢任何增量**（含思考 token）：
  队列满时阻塞读取线程，消费端自然减速；
- 取消可打断：close() 从事件循环线程关闭底层响应，解除阻塞读取；
- 墙钟不参与延迟判定（超时用 socket 超时 + 消费端计时器）；
- 错误统一转 VastarError（带分类），错误信息不得包含请求头（API key 所在）。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import socket
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from concurrent.futures import TimeoutError as FuturesTimeoutError

from core.errors import ErrorCategory, ErrorCode, VastarError

_CHUNK_SIZE = 1024


def _classify_http_error(status: int, body_text: str) -> VastarError:
    if status in (401, 403):
        return VastarError(
            code=ErrorCode.AUTH_ERROR, category=ErrorCategory.AUTH,
            message=f"鉴权失败（HTTP {status}），不重试相同凭据",
            details={"status": status, "provider_message": body_text[:400]},
        )
    if status == 429:
        return VastarError(
            code=ErrorCode.RATE_LIMITED, category=ErrorCategory.RATE_LIMITED,
            message="触发限流（HTTP 429）",
            retryable=True,
            details={"status": status, "provider_message": body_text[:400]},
        )
    if status == 400:
        return VastarError(
            code=ErrorCode.PARAM_ERROR, category=ErrorCategory.PARAM,
            message=f"请求参数被拒绝（HTTP 400）：{body_text[:200]}",
            details={"status": status},
        )
    if status >= 500:
        return VastarError(
            code=ErrorCode.SERVER_ERROR, category=ErrorCategory.TRANSIENT,
            message=f"服务端错误（HTTP {status}）",
            retryable=True,
            details={"status": status, "provider_message": body_text[:200]},
        )
    return VastarError(
        code=ErrorCode.NETWORK_ERROR, category=ErrorCategory.TRANSIENT,
        message=f"HTTP 错误（{status}）", retryable=True, details={"status": status},
    )


def _classify_io_error(exc: Exception, *, opening=True) -> VastarError:
    if isinstance(exc, socket.timeout):
        return VastarError(
            code=ErrorCode.STREAM_IDLE_TIMEOUT, category=ErrorCategory.TIMEOUT,
            message="流式读取超时（socket 级空闲超时）", retryable=True,
        )
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    code = getattr(reason, "winerror", None) or getattr(reason, "errno", None)
    not_connected = opening and (isinstance(reason, (socket.gaierror, ConnectionRefusedError))
                                 or code in (51, 61, 101, 111, 113, 10051, 10061, 10065, 11001))
    return VastarError(
        details={"connection_established": False} if not_connected else {},
        code=ErrorCode.NETWORK_ERROR, category=ErrorCategory.TRANSIENT,
        message=f"网络错误：{type(exc).__name__}: {exc}", retryable=True,
    )


QUEUE_PUT_TIMEOUT_S = 120.0   


class BlockingSSESession:
    """一次流式 POST 会话：线程读 socket → 有界队列 → 事件循环消费。

    事件元组：(kind, payload)，kind ∈ {"data", "heartbeat", "done", "error"}。
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, *, queue_maxsize: int = 256) -> None:
        self._loop = loop
        self._queue: asyncio.Queue[tuple[str, object]] = asyncio.Queue(maxsize=queue_maxsize)
        self._response = None
        self._thread: threading.Thread | None = None
        self._closed = threading.Event()

    @property
    def queue(self) -> asyncio.Queue:
        return self._queue

    def start(
        self, *, url: str, headers: dict[str, str], body: dict,
        connect_timeout_s: float, read_timeout_s: float,
    ) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload, headers=headers, method="POST",
        )
        self._thread = threading.Thread(
            target=self._read_loop,
            args=(req, float(connect_timeout_s), float(read_timeout_s)),
            name="vastar-sse-reader",
            daemon=True,
        )
        self._thread.start()

    
    # 不依赖 no_proxy 环境变量是否配置正确。
    _OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _push(self, item: tuple[str, object]) -> None:
        if self._closed.is_set():
            return
        kind = item[0] if isinstance(item, tuple) else type(item).__name__
        log = logging.getLogger("vastar.http_stream")
        coro = self._queue.put(item)
        fut = None
        try:
            fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
            fut.result(timeout=QUEUE_PUT_TIMEOUT_S)
        except FuturesTimeoutError:
            
            # 并投递显式失败信号（消费端立即收到错误，而非等 idle 超时兜底）。
            log.error("队列投递超时（>=%ss）：丢弃 %s 项（KM-17 显式化，禁静默）",
                      QUEUE_PUT_TIMEOUT_S, kind)
            drop = VastarError(
                code=ErrorCode.STREAM_IDLE_TIMEOUT, category=ErrorCategory.TIMEOUT,
                message=f"流式队列投递超时，{kind} 项被丢弃（背压未解除，已显式计数）",
                retryable=True)
            try:
                self._queue.put_nowait(("error", drop))
            except asyncio.QueueFull:
                log.error("失败信号投递亦满——置 closed 收尾（丢弃事实已留日志）")
                self._closed.set()
        except Exception as exc:  # noqa: BLE001
            
            # 多为 RuntimeError）——**投递不可行**，至少留日志并收尾（禁只 set 了事）。
            log.error("事件循环已退出：%s 项无法投递（%s: %s）——"
                      "本分支无法投递失败信号，留此日志为唯一审计（KM-17）",
                      kind, type(exc).__name__, exc)
            self._closed.set()

        finally:
            # Submission failure leaves ownership here. A stopped loop may also
            # leave a never-started put behind; never close a live loop's task.
            if fut is None or (not self._loop.is_running()
                               and inspect.getcoroutinestate(coro) == inspect.CORO_CREATED):
                coro.close()

    def _read_loop(self, req: urllib.request.Request, connect_timeout: float, read_timeout: float) -> None:
        try:
            resp = BlockingSSESession._OPENER.open(req, timeout=connect_timeout)
        except urllib.error.HTTPError as exc:
            body = b""
            try:
                body = exc.read(2048)
            except Exception:  # noqa: BLE001 - 错误体读不到不影响分类
                pass
            self._push(("error", _classify_http_error(exc.code, body.decode("utf-8", "replace"))))
            self._push(("done", None))
            return
        except Exception as exc:  # noqa: BLE001
            self._push(("error", _classify_io_error(exc)))
            self._push(("done", None))
            return

        self._response = resp
        try:
            for chunk in self._iter_chunks(resp, read_timeout):
                self._push(("data", chunk))
            self._push(("done", None))
        except Exception as exc:  # noqa: BLE001
            if not self._closed.is_set():
                self._push(("error", _classify_io_error(exc, opening=False)))
                self._push(("done", None))
        finally:
            self._close_response()

    @staticmethod
    def _iter_chunks(resp, read_timeout: float) -> Iterator[bytes]:
        # socket 级空闲超时兜底（粗粒度）；精细的 first_delta/idle 超时在运行时层计时
        resp.fp.raw._sock.settimeout(read_timeout)  # type: ignore[union-attr]
        while True:
            chunk = resp.read(_CHUNK_SIZE)
            if not chunk:
                return
            yield chunk

    def close(self) -> None:
        self._closed.set()
        self._close_response()

    def _close_response(self) -> None:
        resp, self._response = self._response, None
        if resp is not None:
            try:
                resp.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响主流程
                pass
