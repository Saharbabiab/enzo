"""
agent/events.py - a tiny in-process event bus.

Parts of Enzo announce what happened ("turn_finished", "tool_called", ...); other parts (traces, memory, learning)
listen. A listener can never break or slow the thing that announced the event: errors are logged and swallowed,
and async listeners run as background tasks.

    from agent import events
    events.on("turn_finished", my_handler)        # handler(**data), sync or async
    events.emit("turn_finished", trace=trace)
"""

from __future__ import annotations

import asyncio
import inspect
from collections import defaultdict
from typing import Callable

from loguru import logger

_handlers: dict[str, list[Callable]] = defaultdict(list)


def on(event: str, handler: Callable) -> Callable:
    if handler not in _handlers[event]:
        _handlers[event].append(handler)
    return handler


def off(event: str, handler: Callable) -> None:
    if handler in _handlers[event]:
        _handlers[event].remove(handler)


def emit(event: str, **data) -> None:
    for handler in list(_handlers.get(event, ())):
        try:
            if inspect.iscoroutinefunction(handler):
                try:
                    asyncio.get_running_loop().create_task(_guarded(event, handler, data))
                except RuntimeError:                      # no loop (a worker thread, a script): skip async listeners
                    logger.debug(f"events: no loop for async listener of {event}")
            else:
                handler(**data)
        except Exception as e:
            logger.warning(f"events: listener for {event} failed: {e.__class__.__name__}: {e}")


async def _guarded(event: str, handler: Callable, data: dict) -> None:
    try:
        await handler(**data)
    except Exception as e:
        logger.warning(f"events: listener for {event} failed: {e.__class__.__name__}: {e}")
