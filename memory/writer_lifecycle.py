"""受管理的短期异步落盘窗口；close 必须先于 Store.close。"""
import asyncio


class WriterLifecycle:
    """只负责定时器、显式提交和关闭；FIFO 写入在 StoreWriter。"""

    def _cancel_timer(self):
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _arm_timer(self):
        if self._closing or self._timer is not None or not self._buffer:
            return
        if self._timer_task is not None and not self._timer_task.done():
            return
        self._timer = asyncio.get_running_loop().call_later(
            self._flush_interval, self._start_timer_flush)

    def _start_timer_flush(self):
        self._timer = None
        self._timer_task = asyncio.create_task(self._timed_flush())

    async def _timed_flush(self):
        try:
            await self.flush_batch()
        finally:
            self._timer_task = None
            # 失败不无限重试；等新输入、显式 flush/commit 或 close。
            if self.last_error is None:
                self._arm_timer()

    async def commit(self, event=None):
        """关键操作屏障：成功才可确认，失败显式抛存储错误。"""
        if event is not None:
            await self.append(event)
        result = await self.flush()
        if result != "confirmed":
            raise self.storage_error()
        return result

    async def close(self):
        """停止接收，等在途任务完成；调用者取消也不能遗留写线程。"""
        if self._close_task is None:
            self._closing = True
            self._cancel_timer()
            self._close_task = asyncio.create_task(self._finish_close())
        cancelled = False
        while not self._close_task.done():
            try:
                await asyncio.shield(self._close_task)
            except asyncio.CancelledError:
                cancelled = True
        result = self._close_task.result()
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def _finish_close(self):
        if self._timer_task is not None:
            await asyncio.shield(self._timer_task)
        return await self.flush()
