"""asyncio helpers."""

from __future__ import annotations

import asyncio
from typing import Any


async def cancel_and_wait(task: asyncio.Task[Any]) -> None:
    """Cancel ``task`` and wait for it to finish.

    Only the task's own cancellation is absorbed. If the caller is cancelled
    while it waits, that cancellation propagates instead of being swallowed
    (which would leave the caller running after someone asked it to stop).
    """
    current = asyncio.current_task()
    cancels_before = current.cancelling() if current is not None else 0
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        if current is not None and current.cancelling() > cancels_before:
            raise
