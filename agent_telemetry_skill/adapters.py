from __future__ import annotations

import functools
import inspect
from typing import Any, Callable, TypeVar, cast

from .client import TelemetryClient


F = TypeVar("F", bound=Callable[..., Any])


def trace_tool(client: TelemetryClient, name: str | None = None) -> Callable[[F], F]:
    def decorator(func: F) -> F:
        tool_name = name or func.__name__

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                with client.tool_call(tool_name, {"args": args, "kwargs": kwargs}) as span:
                    result = await func(*args, **kwargs)
                    span.add_event("tool.result", {"result": result})
                    return result

            return cast(F, async_wrapper)

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with client.tool_call(tool_name, {"args": args, "kwargs": kwargs}) as span:
                result = func(*args, **kwargs)
                span.add_event("tool.result", {"result": result})
                return result

        return cast(F, wrapper)

    return decorator


def trace_agent_run(client: TelemetryClient, run_name: str | None = None, agent_name: str | None = None) -> Callable[[F], F]:
    def decorator(func: F) -> F:
        name = run_name or func.__name__

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                with client.run(name, agent_name=agent_name):
                    return await func(*args, **kwargs)

            return cast(F, async_wrapper)

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with client.run(name, agent_name=agent_name):
                return func(*args, **kwargs)

        return cast(F, wrapper)

    return decorator
