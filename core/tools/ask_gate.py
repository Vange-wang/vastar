"""Vastar runtime component."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

ACTOR_SINGLE_AGENT = "agent:main"     # B08：单 Agent 固定身份（B42/B46 前唯一 actor）
POLICY_REVISION = "perm-r25-v1"       # 权限策略修订号（R25 判定表；升版即失效旧授权）


@dataclass
class AskQuestion:
    """一次待确认的写操作（执行层构造；lines 走 tool.ask 事件落屏）。"""

    tool: str
    action_category: str       # 授权键·动作半：create/edit/move/delete/batch/read
    location_category: str     # 授权键·资源范围半：in_root/out_root
    lines: list[str] = field(default_factory=list)
    allow_session_grant: bool = True  # 精确预览只能逐项确认，不能继承/产生同类授权


@dataclass(frozen=True)
class AuthContext:
    """一次询问的显式授权上下文（B08.01）——授权键与并发绑定的唯一来源。

    scope 判定与授权存储都在 gate（单一判定点）；session 侧不复制权限表。
    """

    session_id: str
    project_scope: str
    actor_id: str
    action: str
    resource_scope: str
    policy_revision: str = POLICY_REVISION
    request_id: str = ""       # 发起该询问的 Run（并发绑定：请求 ID）

    @property
    def grant_key(self) -> tuple[str, ...]:
        """临时授权键（B08.02）：六元组，替代原「动作 × in_root」进程级全局键。"""
        return (self.session_id, self.project_scope, self.actor_id,
                self.action, self.resource_scope, self.policy_revision)


def build_auth(question: AskQuestion, *, session_id: str, request_id: str,
               project_scope: str,
               actor_id: str = ACTOR_SINGLE_AGENT) -> AuthContext:
    """执行层组装 AuthContext（动作/资源半取自判定结果；三值判定仍归 permissions）。"""
    return AuthContext(
        session_id=session_id, project_scope=project_scope, actor_id=actor_id,
        action=question.action_category, resource_scope=question.location_category,
        policy_revision=POLICY_REVISION, request_id=request_id,
    )


@dataclass
class AskAnswer:
    approved: bool
    approval_source: str       # user_allow / user_deny / session_allow / user_cancel /
                               # ask_timeout / session_changed / superseded_by_new_ask


_DENY_LABELS = {
    "user_deny": "用户拒绝",
    "user_cancel": "用户取消（Ctrl+C）",
    "ask_timeout": "询问超时未应答",
    "session_changed": "会话已切换（迟到授权不生效）",
    "superseded_by_new_ask": "被新的询问取代",
}


class AskGate:
    """进程内询问闸门：一次只挂起一个问题；答案经 REPL 输入路由或取消路径写入。

    B08：临时授权按 AuthContext 六元组存储；会话切换经 rotate_session（/new 既有
    生命周期点接线）通知——切换后迟到的批准按 session_changed 拒绝、不产生授权。
    """

    def __init__(self, timeout_s: float = 60.0,
                 policy_revision: str = POLICY_REVISION) -> None:
        self.timeout_s = timeout_s
        self.policy_revision = policy_revision
        self._grants: set[tuple[str, ...]] = set()
        self._current_session_id: str | None = None
        self._session_bound = False
        self._pending: AskQuestion | None = None
        self._pending_auth: AuthContext | None = None
        self._future: asyncio.Future | None = None
        self.authorization_epoch = 0

    def revoke(self):
        """撤回尚未提交的授权；提交后不能声称副作用已撤销。"""
        self.authorization_epoch += 1
        self._grants.clear()
        self.cancel()

    def has_pending(self) -> bool:
        return self._future is not None and not self._future.done()

    def rotate_session(self, session_id: str) -> None:
        """会话切换通知（B08 最小接线：/new 的既有生命周期点调用，不新造钩子）。

        仅更新"当前会话"；已挂起的旧会话询问保留原状——其后续批准在 resolve 时
        因会话不匹配被显式拒绝（迟到授权不生效），不产生任何授权记录。
        """
        if self._current_session_id != session_id:
            self.authorization_epoch += 1
        self._current_session_id = session_id
        self._session_bound = True

    async def ask(self, auth: AuthContext, question: AskQuestion) -> AskAnswer:
        from core.budget_topup.tool_runtime import user_wait
        with user_wait():
            return await self._ask(auth, question)

    async def _ask(self, auth: AuthContext, question: AskQuestion) -> AskAnswer:
        """发起询问并等待答案；授权键已在本会话六元组内 → 直接放行不再问。"""
        if self._session_bound and auth.session_id != self._current_session_id:
            return AskAnswer(False, "session_changed")
        self._current_session_id = auth.session_id
        if question.allow_session_grant and auth.grant_key in self._grants:
            return AskAnswer(True, "session_allow")
        if self.has_pending():
            # B08.02：并发绑定——旧询问被新询问取代时显式拒绝（不静默吞）
            self._resolve(AskAnswer(False, "superseded_by_new_ask"))
        self._pending = question
        self._pending_auth = auth
        future = asyncio.get_running_loop().create_future()
        self._future = future
        try:
            return await asyncio.wait_for(future, timeout=self.timeout_s)
        except TimeoutError:
            return AskAnswer(False, "ask_timeout")
        finally:
            # B08.02：仅当仍属本次询问才清状态——被 superseded 后不得清掉继任者的挂起
            if self._future is future:
                self._pending = None
                self._pending_auth = None
                self._future = None

    def resolve_input(self, text: str) -> str:
        """REPL 输入路由：pending 时消费 1/2/3。返回 no_pending / answered / invalid。

        非法输入不消费问题（保持 pending，超时仍生效）；REPL 负责提示重答。
        B08：会话已切换（pending 会话 ≠ 当前会话）→ 任何选项都按 session_changed
        拒绝，且**不产生授权记录**（迟到批准不授权新操作）。
        """
        if not self.has_pending():
            return "no_pending"
        choice = text.strip()
        future = self._future
        pending = self._pending
        auth = self._pending_auth
        if future is None or pending is None or auth is None:
            return "no_pending"
        if choice not in ("1", "2", "3"):
            return "invalid"
        if choice == "3" and not pending.allow_session_grant:
            return "invalid"
        if auth.session_id != self._current_session_id:
            self._resolve(AskAnswer(False, "session_changed"))
            return "answered"
        if choice == "1":
            self._resolve(AskAnswer(True, "user_allow"))
            return "answered"
        if choice == "2":
            self._resolve(AskAnswer(False, "user_deny"))
            return "answered"
        self._grants.add(auth.grant_key)     # "3"：本会话内这类动作都允许（六元组键）
        self._resolve(AskAnswer(True, "session_allow"))
        return "answered"

    def cancel(self) -> bool:
        """Vastar runtime component."""
        if not self.has_pending() or self._future is None:
            return False
        self._resolve(AskAnswer(False, "user_cancel"))
        return True

    def _resolve(self, answer: AskAnswer) -> None:
        future = self._future
        if future is not None and not future.done():
            future.set_result(answer)

    def deny_label(self, source: str) -> str:
        return _DENY_LABELS.get(source, source)
