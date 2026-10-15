"""Immediate intervention reuses coordinator cancellation and durable input handling."""


async def handle_steer(ui, arg):
    coord = ui._coordinator
    if not arg:
        ui._renderer.render_note('用法：/steer <新要求>，立即介入当前任务；/status 查看进度')
        return
    active = coord._active
    text = arg
    if active is not None:
        text = ('用户即时介入。优先处理下面的新要求，并在不冲突时继续原任务；'
                '如果新要求明确停止或替换任务，以新要求为准。\n'
                f'原任务：\n{active.input_text}\n\n最新要求：\n{arg}')
    # Cancelling an ASK never grants permission to the interrupted action.
    receipt = await coord.submit(text, interrupt=True)
    if ui._ask_gate is not None:
        ui._ask_gate.cancel()
    ui._model_mode = False
    ui._renderer.render_note('已介入：正在取消当前步骤，随后优先处理新要求'
                             if active is not None else '已接受新要求')
    if not receipt.persisted:
        ui._renderer.render_note(receipt.note)
    ui._announce_generation()
