"""Git worktree service package — public API + patch-compatible re-exports.

Behavior-preserving mechanical split of the former monolithic
``git_worktree.py``. External callers keep::

    from hiveweave.services.git_worktree import X
    patch("hiveweave.services.git_worktree.X", ...)

Patch propagation: ``unittest.mock.patch`` / ``monkeypatch.setattr`` on this
package module also updates consumer submodule globals for symbols that tests
and call sites patch (so ``LOAD_GLOBAL`` inside mixins still sees the mock).
"""
from __future__ import annotations

import sys
import types

from .constants import (
    CHECKPOINT_PREFIX,
    GENERATED_FILES,
    GIT_TIMEOUT,
    QUARANTINE_DIR,
    SLUG_MAX_LEN,
    WORKTREE_DIR,
    _CONFLICT_MARKER_RE,
    _IN_FLIGHT_AFTER_MERGE_STATUSES,
    _MARKER_SCAN_MAX_BYTES,
    _MARKER_SCAN_MAX_HITS,
    _MARKER_SCAN_SKIP_DIRS,
    _PROTECT_TASK_STATUSES,
    _RELOCATION_SUFFIXES,
    _SLUG_INVALID,
    _SLUG_SPACE,
    _SLUG_TRIM,
    _TASK_BRANCH_RE,
    _UNTRACKED_FILE_LINE_RE,
    _UNTRACKED_OVERWRITE_RE,
    _WT_LIST_RE,
    _create_locks,
    _create_locks_guard,
)
from .naming import _branch_name, _slugify, compute_branch_name
from .paths import (
    _force_clear_path,
    _has_git,
    _is_bound_worktree_basename,
    _worktree_binding_under_project,
    _worktree_path,
)
from .git_cmd import _current_branch, _git, _resolve_base_branch, _target_tip_short
from .porcelain import (
    _porcelain_non_hiveweave_dirty,
    _porcelain_tracked_dirty,
    _target_worktree_is_dirty,
)
from .merge_support import (
    _abort_landed_merge,
    _auto_checkpoint_dirty_target,
    _merge_failure_result,
    parse_untracked_overwrite,
    quarantine_untracked_on_target,
)
from .conflict_markers import _reject_if_markers_landed, scan_conflict_markers
from .service import GitWorktreeService
from .reconcile import (
    _agent_id_for_short_id,
    _assignee_has_open_tasks,
    _assignee_is_verify_only,
    _assignee_needs_write_worktree,
    _log_worktree_rebuild_event,
    _open_project_db_raw,
    _parse_worktree_porcelain,
    _project_db_if_exists,
    _protected_worktree_short_ids,
    _task_branch_candidate,
    _try_reattach_worktree,
    quarantine_orphan_branch,
    reconcile_worktrees,
)
from .ensure import (
    agent_gets_write_worktree,
    ensure_executor_worktree,
    heal_project_executor_worktrees,
    heal_workspace_binding_from_disk,
    worktree_commits_behind_main,
)
from .dispatch_pin import pin_dispatch_message_to_worktree

# Submodules that bind patched names into their globals (for LOAD_GLOBAL).
_PATCH_CONSUMERS = (
    "hiveweave.services.git_worktree.git_cmd",
    "hiveweave.services.git_worktree.porcelain",
    "hiveweave.services.git_worktree.merge_support",
    "hiveweave.services.git_worktree.conflict_markers",
    "hiveweave.services.git_worktree.paths",
    "hiveweave.services.git_worktree.naming",
    "hiveweave.services.git_worktree.service",
    "hiveweave.services.git_worktree.service_create",
    "hiveweave.services.git_worktree.service_merge",
    "hiveweave.services.git_worktree.service_lifecycle",
    "hiveweave.services.git_worktree.reconcile",
    "hiveweave.services.git_worktree.ensure",
    "hiveweave.services.git_worktree.dispatch_pin",
)

_PATCH_NAMES = frozenset({
    "_git",
    "_current_branch",
    "_try_reattach_worktree",
    "_project_db_if_exists",
    "_resolve_base_branch",
    "_target_tip_short",
    "_has_git",
    "_worktree_path",
    "_slugify",
    "_branch_name",
    "compute_branch_name",
    "ensure_executor_worktree",
    "agent_gets_write_worktree",
    "GitWorktreeService",
    "reconcile_worktrees",
    "quarantine_orphan_branch",
    "scan_conflict_markers",
    "parse_untracked_overwrite",
    "quarantine_untracked_on_target",
    "_log_worktree_rebuild_event",
    "_force_clear_path",
    "_is_bound_worktree_basename",
    "_worktree_binding_under_project",
    "_merge_failure_result",
    "_reject_if_markers_landed",
    "_target_worktree_is_dirty",
    "_porcelain_tracked_dirty",
    "_abort_landed_merge",
    "_assignee_has_open_tasks",
    "_assignee_is_verify_only",
    "_assignee_needs_write_worktree",
    "heal_workspace_binding_from_disk",
})


class _GitWorktreePackage(types.ModuleType):
    """Propagate setattr on patched symbols into consumer submodule globals."""

    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        if name not in _PATCH_NAMES:
            return
        for modname in _PATCH_CONSUMERS:
            mod = sys.modules.get(modname)
            if mod is not None and name in mod.__dict__:
                object.__setattr__(mod, name, value)


# Install patch-propagating module class (must keep identity in sys.modules).
_mod = sys.modules[__name__]
_mod.__class__ = _GitWorktreePackage

__all__ = [
    "GitWorktreeService",
    "WORKTREE_DIR",
    "QUARANTINE_DIR",
    "GENERATED_FILES",
    "CHECKPOINT_PREFIX",
    "GIT_TIMEOUT",
    "SLUG_MAX_LEN",
    "compute_branch_name",
    "reconcile_worktrees",
    "quarantine_orphan_branch",
    "ensure_executor_worktree",
    "heal_project_executor_worktrees",
    "heal_workspace_binding_from_disk",
    "agent_gets_write_worktree",
    "pin_dispatch_message_to_worktree",
    "worktree_commits_behind_main",
    "scan_conflict_markers",
    "parse_untracked_overwrite",
    "quarantine_untracked_on_target",
    "_git",
    "_has_git",
    "_worktree_path",
    "_current_branch",
    "_slugify",
    "_branch_name",
    "_resolve_base_branch",
    "_is_bound_worktree_basename",
    "_worktree_binding_under_project",
    "_force_clear_path",
    "_try_reattach_worktree",
    "_project_db_if_exists",
    "_parse_worktree_porcelain",
    "_log_worktree_rebuild_event",
    "_agent_id_for_short_id",
    "_open_project_db_raw",
    "_task_branch_candidate",
    "_protected_worktree_short_ids",
    "_assignee_has_open_tasks",
    "_assignee_is_verify_only",
    "_assignee_needs_write_worktree",
    "_reject_if_markers_landed",
    "_merge_failure_result",
    "_abort_landed_merge",
    "_auto_checkpoint_dirty_target",
    "_target_worktree_is_dirty",
    "_porcelain_tracked_dirty",
    "_porcelain_non_hiveweave_dirty",
    "_create_locks",
    "_create_locks_guard",
]
