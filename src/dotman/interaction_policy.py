from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Iterator

_no_confirm = ContextVar("no_confirm", default=False)


class InteractionRequiredError(ValueError):
    """Execution requires a decision unavailable in this invocation."""


def no_confirm_enabled() -> bool:
    return _no_confirm.get()


@contextmanager
def interaction_scope(*, no_confirm: bool) -> Iterator[None]:
    token = _no_confirm.set(no_confirm)
    try:
        yield
    finally:
        _no_confirm.reset(token)
