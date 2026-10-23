"""REPL 命令分派；呈现/权限语义保持不变。"""

from ui.status_view import _status_lines


class ReplCommands:
    async def _handle_command(self, text: str) -> None:
        from ui import repl as commands

        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        coord = self._coordinator
        if await _handle_extension(self, cmd, arg):
            return
        if cmd == "/help":
            self._renderer.render_note(commands._HELP_TEXT + "\n/task help：结构化任务\n/history help：历史检索\n/memory help：长期记忆")
        elif cmd == "/exit":
            if coord.is_busy():
                self._renderer.render_note("正在回答中；先 Ctrl+C 取消，或等回答完成再 /exit")
                return
            self._exit_requested = True
            coord.stop()
        elif cmd == "/new":
            if coord.is_busy():
                self._renderer.render_note("正在回答中，/new 稍后再试")
                return
            await coord.new_session()
            self._renderer.render_note("已新建会话（旧会话已落库，可用 --continue 续聊）")
        elif cmd == "/model":

            # 无参按固定顺序列出 "N) provider/model" 并标注当前；带参支持序号 /
            # 裸 model-id / provider/model 三种写法（序号制为主，名字向后兼容）。
            if not arg:
                for index, provider, model, is_current in coord.model_catalog():
                    mark = " ← 当前" if is_current else ""
                    self._renderer.render_note(f"  {index}) {provider}/{model}{mark}")
                self._renderer.render_note("请输入序号选择模型；Ctrl+C 回退")
                self._model_mode = True   # R3：进入选择模式（模式吃输入，Ctrl+C 唯一回退）
                return
            _ok, message = coord.switch_model(arg)
            self._renderer.render_note(message)
        elif cmd == "/mode":
            if arg in ("queue", "interrupt"):
                coord.set_mode(arg)
                self._renderer.render_note(f"插话模式 = {arg}"
                                           + ("（流式期间新输入将打断当前回答）" if arg == "interrupt" else ""))
            else:
                self._renderer.render_note(f"当前插话模式 = {coord.mode}；用法 /mode [queue|interrupt]")
        elif cmd == "/status":
            self._renderer.render_note("\n".join(_status_lines(
                coord, self._tool_registry, self._permission_mode,
                startup_facts=getattr(self, "_startup_facts", ""))))
        elif cmd == "/retry":
            if coord.is_busy():
                self._renderer.render_note("正在回答中；先 Ctrl+C 取消再 /retry")
                return
            last_input = coord.last_user_input()
            if not last_input:
                self._renderer.render_note("没有可重问的输入")
                return
            coord.retry_last()
        elif cmd == "/think":
            # R2 v1.1：开关——首次展开全文，再次收起（诚实边界由 toggle_think 判定）
            message = self._renderer.toggle_think()
            if message:
                self._renderer.render_note(message)
        elif cmd == "/continue":

            _handle_continue(self._renderer, coord)
        elif cmd == "/compact":

            # 忙时拒绝（/new 同口径）；摘要为本地确定性拼装（零模型请求）。
            _handle_compact(self._renderer, coord, arg)
        elif cmd == "/skill":
            # B11：显式触发通道（V3②；模块级分派守 V6 类红线）
            _handle_skill(self._renderer, coord, arg)
        else:
            self._renderer.render_note(f"未知命令 {cmd}；可用：{' '.join(commands._COMMANDS)}")


async def _handle_extension(ui, cmd, arg):
    if cmd in ('/sessions', '/resume'):
        from ui.session_commands import handle_sessions
        await handle_sessions(ui, cmd, arg)
    elif cmd == '/steer':
        from ui.steering_commands import handle_steer
        await handle_steer(ui, arg)
    elif cmd == '/history':
        from ui.history_commands import handle_history
        handle_history(ui, arg)
    elif cmd in ('/remember', '/memory'):
        from ui.memory_commands import handle_memory
        handle_memory(ui, cmd, arg)
    elif cmd == '/task':
        await _handle_task(ui, arg)
    elif cmd == '/team':
        from ui.team_commands import handle_team
        handle_team(ui, arg)
    elif cmd == '/trace':
        from ui.trace_commands import handle_trace
        handle_trace(ui, arg)
    else:
        return False
    return True


async def _handle_task(ui, arg):
    import json

    from core.harness.task_commands import task_command
    try:
        result = await task_command(ui._coordinator, arg)
        ui._renderer.render_note(result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, KeyError, TypeError) as exc:
        ui._renderer.render_note(f"Task：{exc}")
def _handle_compact(renderer, coord, arg: str) -> None:
    """Vastar runtime component."""
    if coord.is_busy():
        renderer.render_note("正在回答中，/compact 稍后再试")
        return
    if not arg:
        result = coord.compact_preview()
        if not result["ok"]:
            renderer.render_note(result["note"])
            return
        lines = [
            f"已生成压缩快照 {result['snapshot_id']}（待审批，未生效）：",
            f"覆盖历史 {result['covered_count']} 条消息；原始消息完整保留，未删除任何内容。",
            f"估算 token 由 {result['tokens_before']} → 约 {result['tokens_after']}"
            f"（目标占比 {result['target_ratio']:.0%}，实际估算 {result['actual_ratio']:.0%}；"
            "启发式估算口径，非硬上界）。",
        ]
        if result.get("superseded"):
            lines.append(f"此前待审批快照 {result['superseded']} 已自动作废。")
        lines.append("批准：/compact ok（下一轮请求生效） · 丢弃：/compact drop · 回滚：/compact rollback")
        renderer.render_note("\n".join(lines))
        return
    action = {"ok": "approved", "drop": "dropped", "rollback": "rolled_back"}.get(arg)
    if action is None:
        renderer.render_note(
            "用法：/compact 生成快照 · /compact ok 批准 · /compact drop 丢弃 · /compact rollback 回滚")
        return
    _, message = coord.compact_decide(action)
    renderer.render_note(message)


def _handle_continue(renderer, coord) -> None:
    """Vastar runtime component."""
    if coord.is_busy():
        renderer.render_note("正在回答中，/continue 稍后再试")
        return
    _ok, message = coord.continue_last()
    renderer.render_note(message)


def _handle_skill(renderer, coord, arg: str) -> None:
    """/skill 分派（B11）：无参 = 清单；off = 取消安排；<名> = 下一条输入显式加载。"""
    if arg == 'release' or arg.startswith('release '):
        from core.skills.maintenance_commands import handle
        handle(renderer, coord, arg.partition(' ')[2])
        return
    if not arg:
        renderer.render_note("\n".join(coord.skill_status_lines()))
        return
    if arg.lower() == "off":
        renderer.render_note(coord.disarm_skill())
        return
    renderer.render_note(coord.arm_skill(arg))
