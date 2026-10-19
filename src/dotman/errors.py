from __future__ import annotations


class UsageError(ValueError):
    """The input is wrong and no prompt would fix it: bad selector, scope, manifest, or config.

    The CLI exits 2 for these. Every other ValueError is a runtime failure (exit 1).
    """
