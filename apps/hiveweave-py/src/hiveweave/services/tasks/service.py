"""TaskService composed from domain mixins (one class)."""
from __future__ import annotations

from .claim import ClaimMixin
from .close import CloseMixin
from .crud import CrudMixin
from .lifecycle import LifecycleMixin
from .obligations import ObligationsMixin
from .progress import ProgressMixin
from .review import ReviewMixin
from .submit import SubmitMixin
from .transitions import TransitionsMixin
from .verify import VerifyMixin


class TaskService(
    ProgressMixin,
    TransitionsMixin,
    CrudMixin,
    ClaimMixin,
    LifecycleMixin,
    SubmitMixin,
    ReviewMixin,
    CloseMixin,
    VerifyMixin,
    ObligationsMixin,
):
    """Task Ledger — task lifecycle from creation to closure with rework support."""

    pass
