"""All GitHub API calls FlagPilot makes live in this module.

Uses a fine-grained token scoped to the demo repo (GITHUB_TOKEN) and the
repo slug (GITHUB_REPO, e.g. "murali101002/flagsmith-practice"). Purge
flows branch off the default branch, commit file removals through the
Contents API, and open PRs — never force-pushes, never direct pushes to
the default branch.
"""

from __future__ import annotations

import base64
import os

import httpx


def _repo() -> str:
    """The owner/name slug. Tolerates a full repo URL pasted into .env."""
    repo = os.environ.get("GITHUB_REPO", "").strip()
    if not repo:
        raise RuntimeError("GITHUB_REPO is not set — e.g. murali101002/flagsmith-practice")
    if repo.startswith("http"):
        cleaned = repo.rstrip("/").removeprefix("https://").removeprefix("http://").removeprefix("www.")
        parts = [part for part in cleaned.split("/") if part]
        repo = "/".join(parts[-2:])
    return repo


def _owner() -> str:
    return _repo().split("/")[0]


def _headers() -> dict[str, str]:
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise RuntimeError(
            "GITHUB_TOKEN is not set — create a fine-grained token scoped to the demo repo "
            "with Contents and Pull requests write access"
        )
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _base() -> str:
    return f"https://api.github.com/repos/{_repo()}"


def _request(method: str, path: str, *, json: dict | None = None, params: dict | None = None) -> httpx.Response:
    response = httpx.request(
        method, f"{_base()}{path}", headers=_headers(), json=json, params=params, timeout=15.0
    )
    if response.status_code >= 400:
        hint = ""
        if response.status_code == 401:
            hint = " (token missing, expired, or lacking the needed permissions)"
        raise RuntimeError(f"GitHub {method} {path} → {response.status_code}: {response.text[:200]}{hint}")
    return response


def default_branch() -> str:
    response = _request("GET", "")
    return response.json().get("default_branch", "main")


def branch_exists(branch: str) -> bool:
    response = httpx.get(
        f"{_base()}/git/ref/heads/{branch}", headers=_headers(), timeout=15.0
    )
    return response.status_code == 200


def create_branch(branch: str, from_branch: str | None = None) -> str:
    """Create branch from from_branch (default: the repo's default branch).
    Returns the base commit sha."""
    source = from_branch or default_branch()
    base_ref = _request("GET", f"/git/ref/heads/{source}").json()
    sha = base_ref["object"]["sha"]
    _request(
        "POST",
        "/git/refs",
        json={"ref": f"refs/heads/{branch}", "sha": sha},
    )
    return sha


def get_file(path: str, ref: str) -> dict | None:
    """The file's GitHub contents record on ref (base64 content + sha), or
    None when it does not exist there."""
    response = httpx.get(
        f"{_base()}/contents/{path}",
        headers=_headers(),
        params={"ref": ref},
        timeout=15.0,
    )
    if response.status_code == 404:
        return None
    if response.status_code >= 400:
        raise RuntimeError(f"GitHub GET contents/{path} → {response.status_code}: {response.text[:200]}")
    return response.json()


def file_text(path: str, ref: str) -> str | None:
    """The decoded text of a file on ref, or None when missing."""
    record = get_file(path, ref)
    if record is None:
        return None
    return base64.b64decode(record["content"]).decode("utf-8")


def put_file(path: str, content: str, message: str, branch: str, sha: str) -> dict:
    """Commit one file to a branch (Contents API; sha makes it an update)."""
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    response = _request(
        "PUT",
        f"/contents/{path}",
        json={
            "message": message,
            "content": encoded,
            "branch": branch,
            "sha": sha,
        },
    )
    return response.json()


def open_pr_for_head(head_branch: str) -> dict | None:
    """The open PR whose head is this branch, or None (duplicate safety)."""
    response = _request(
        "GET",
        "/pulls",
        params={"head": f"{_owner()}:{head_branch}", "state": "open"},
    )
    pulls = response.json()
    return pulls[0] if pulls else None


def create_pull(title: str, head: str, base: str, body: str) -> dict:
    response = _request(
        "POST",
        "/pulls",
        json={"title": title, "head": head, "base": base, "body": body},
    )
    return response.json()
