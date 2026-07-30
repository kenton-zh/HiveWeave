"""GitWorktreeService — isolated worktrees per agent, managed by coordinators."""
from __future__ import annotations

from .service_create import CreateMixin
from .service_lifecycle import LifecycleMixin
from .service_merge import MergeMixin


class GitWorktreeService(CreateMixin, MergeMixin, LifecycleMixin):
    """GitWorktreeService — isolated worktrees per agent, managed by coordinators.

    契约 09: coordinator-only. 7 operations: create / list / checkpoint /
    merge / rollback / delete / info. Each returns a dict with ``success``
    plus operation-specific fields (and ``message`` on error).
    """

    pass
