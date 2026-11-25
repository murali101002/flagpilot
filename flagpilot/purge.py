"""The purge flow: preview → snapshot → branch → removals → PR → pending.

Locked order (Phase 9):
1. Preview: files, removed lines, environments, last evaluation, and the
   diff to be made. Shown to the user; nothing is written.
2. Snapshot the flag's full Flagsmith config into Postgres, for restore.
3. Create a branch (flagpilot/remove-<flag>), apply the removal — for
   practice, a controlled removal of the flag check that keeps the
   enabled branch's code.
4. Create the PR through the GitHub API and return its URL.
5. Mark the flag pending removal. Archiving in Flagsmith happens only
   after the PR merges (a later phase).

Safety:
- A branch or PR that already exists is returned as-is — never duplicated.
- If a file changed since the preview (content hash mismatch), the purge
  stops and reports the conflict instead of overwriting.
"""

from __future__ import annotations

import hashlib
import json

from flagpilot import github_client as github
from flagpilot import store
from flagpilot.evidence import code_references
from flagpilot.flagsmith_client import find_feature
from flagpilot.usage import usage_for_feature

BRANCH_PREFIX = "flagpilot/remove-"


def _purge_allowed() -> bool:
    """Dogfood gate: purging is off unless flagpilot_purge_pr is enabled."""
    feature = find_feature("flagpilot_purge_pr")
    if feature is None:
        raise RuntimeError("flagpilot_purge_pr does not exist in Flagsmith — create it first")
    return bool((feature.get("environment_feature_state") or {}).get("enabled"))


class PurgeConflict(RuntimeError):
    """The file changed between preview and execution."""


def _remove_flag_check(content: str, flag_name: str) -> tuple[str, list[str]]:
    """Controlled removal of a shipped flag check.

    Finds the `if … <flag_name> …:` check, keeps its enabled branch, and
    drops the check line and everything after it that belongs to the
    disabled branch (dead weight). Returns (new content, removed lines).
    Raises when the expected shape is not there.
    """
    lines = content.splitlines()
    index = next(
        (i for i, line in enumerate(lines) if flag_name in line and line.strip().startswith("if ")),
        None,
    )
    if index is None:
        raise RuntimeError(
            f"no `if` flag check for {flag_name!r} found — the purge only handles the "
            "controlled demo pattern (check line, enabled branch, disabled branch)"
        )

    check_indent = len(lines[index]) - len(lines[index].lstrip())

    # The enabled branch is everything indented deeper than the check.
    enabled_branch: list[str] = []
    cursor = index + 1
    while cursor < len(lines):
        line = lines[cursor]
        if line.strip() == "":
            enabled_branch.append(line)
            cursor += 1
            continue
        if len(line) - len(line.lstrip()) <= check_indent:
            break
        enabled_branch.append(line)
        cursor += 1

    # The disabled branch is what follows at the check's own indent — that is
    # the dead weight the purge deletes.
    disabled: list[str] = []
    while cursor < len(lines):
        line = lines[cursor]
        if line.strip() == "":
            disabled.append(line)
            cursor += 1
            continue
        if len(line) - len(line.lstrip()) < check_indent:
            break
        disabled.append(line)
        cursor += 1

    removed = [lines[index]] + disabled
    new_lines = lines[:index] + enabled_branch + lines[cursor:]
    return "\n".join(new_lines) + ("\n" if content.endswith("\n") else ""), removed


def preview(flag_name: str) -> dict:
    """Everything the user needs to decide: files, removed lines, the
    proposed diff, usage evidence — nothing written."""
    if not _purge_allowed():
        raise RuntimeError("purge is disabled: flagpilot_purge_pr is off in Flagsmith")
    refs = code_references(flag_name)
    base_branch = github.default_branch()
    files: list[dict] = []
    for path in refs:
        original = github.file_text(path, ref=base_branch)
        if original is None:
            continue
        new_content, removed_lines = _remove_flag_check(original, flag_name)
        files.append(
            {
                "path": path,
                "removed_lines": removed_lines,
                "new_content": new_content,
                "content_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
            }
        )
    usage = usage_for_feature(flag_name)
    return {
        "flag": flag_name,
        "base_branch": base_branch,
        "branch": f"{BRANCH_PREFIX}{flag_name}",
        "files": files,
        "environments": ["Development"],
        "last_evaluated": usage["last_evaluated"],
        "total_evaluations": usage["total_evaluations"],
        "note": (
            "no code references: the PR documents the archive only"
            if not files
            else "the enabled branch's code is kept; only the flag check is removed"
        ),
    }


def execute(flag_name: str) -> dict:
    """The full purge — and convergent on retry: whatever the previous
    attempt already did (branch created, files committed) is skipped, the
    rest is finished, and an open PR is returned instead of duplicated."""
    if not _purge_allowed():
        raise RuntimeError("purge is disabled: flagpilot_purge_pr is off in Flagsmith")
    branch = f"{BRANCH_PREFIX}{flag_name}"

    # Duplicate safety: an open PR is returned as-is.
    existing_branch = github.branch_exists(branch)
    open_pr = (
        github.open_pr_for_head(f"{github._owner()}:{branch}") if existing_branch else None
    )
    if open_pr:
        return {"pr_url": open_pr["html_url"], "existing": True, "branch": branch}

    preview_data = preview(flag_name)
    base_branch = preview_data["base_branch"]

    # 2. Snapshot the flag's full config for restore.
    feature = find_feature(flag_name)
    if feature is None:
        raise RuntimeError(f"flag {flag_name!r} not found in Flagsmith")
    store.snapshot_flag(flag_name, json.dumps(feature, default=str))

    # 3. Branch (if missing) + conflict-checked commits. A file whose branch
    #    copy already has the removal applied is skipped, not duplicated.
    if not existing_branch:
        github.create_branch(branch, from_branch=base_branch)
    for file_change in preview_data["files"]:
        path = file_change["path"]
        on_branch = github.file_text(path, ref=branch)
        if on_branch is None:
            raise PurgeConflict(f"{path} is missing on the purge branch")
        current_hash = hashlib.sha256(on_branch.encode("utf-8")).hexdigest()
        if current_hash == hashlib.sha256(file_change["new_content"].encode("utf-8")).hexdigest():
            continue  # this file is already purged on the branch
        if current_hash != file_change["content_sha256"]:
            raise PurgeConflict(
                f"{path} changed since the preview (expected sha {file_change['content_sha256'][:8]}, "
                f"found {current_hash[:8]}). Re-run the preview; nothing was overwritten."
            )
        record = github.get_file(path, ref=branch)
        github.put_file(
            path,
            file_change["new_content"],
            message=f"Remove {flag_name} flag check (keep enabled code)",
            branch=branch,
            sha=record["sha"],
        )

    # 4. The PR.
    pr = github.create_pull(
        title=f"Remove dead flag {flag_name}",
        head=branch,
        base=base_branch,
        body=(
            f"FlagPilot purge of `{flag_name}`.\n\n"
            f"- zero evaluations in 30 days (last evaluated: {preview_data['last_evaluated'] or 'never'})\n"
            f"- removes the flag check, keeps the enabled code\n"
            f"- flag config snapshot stored for restore\n"
            f"- the flag stays in Flagsmith (pending removal) until this PR merges"
        ),
    )

    # 5. Pending removal (archiving happens after the merge).
    store.mark_pending_removal(flag_name, pr["html_url"])
    return {
        "pr_url": pr["html_url"],
        "existing": existing_branch,
        "branch": branch,
        "flag": flag_name,
    }
