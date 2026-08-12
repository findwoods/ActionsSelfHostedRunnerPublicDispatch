#!/usr/bin/env python3
"""Minimal public repository-local iPad dispatcher.

This file deliberately has no control-repository access. It binds every write to
GITHUB_REPOSITORY and uses only that workflow's github.token.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

API = "https://api.github.com"
SHA_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
CAPABILITY_BY_KIND = {
    "write-file": "files",
    "append-file": "files",
    "read-file": "files",
    "remove": "files",
    "copy": "files",
    "move": "files",
    "list": "files",
    "sha256": "sha256",
    "http": "http",
    "mini-shell": "mini-shell",
    "javascript": "javascript-core",
    "python": "python-3.13-ios",
    "wasm": "wasm-wasi",
}
FORBIDDEN_HEADERS = {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key", "api-key", "x-auth-token"}


class APIError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"GitHub API HTTP {status}")
        self.status = status
        self.body = body


def api_request(token: str, method: str, path: str, payload: dict | None = None) -> dict:
    body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    request = urllib.request.Request(
        API + path,
        data=body,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "findwoods-public-ipad-dispatch",
            **({"Content-Type": "application/json"} if body is not None else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise APIError(error.code, error.read().decode("utf-8", "replace")) from error


def safe_relative(path: str) -> None:
    candidate = Path(path)
    if not path or candidate.is_absolute() or ".." in candidate.parts:
        raise SystemExit(f"unsafe relative path: {path!r}")


def validate_branch(branch: str) -> None:
    if (
        not BRANCH_RE.fullmatch(branch)
        or ".." in branch
        or "//" in branch
        or branch.endswith(".")
        or branch.endswith("/")
        or any(not part or part.startswith(".") or part.lower().endswith(".lock") for part in branch.split("/"))
    ):
        raise SystemExit(f"invalid queue branch: {branch!r}")


def validate_task(task: dict, repository: str, sha: str) -> dict:
    if not isinstance(task, dict):
        raise SystemExit("task must be a JSON object")
    if not SHA_RE.fullmatch(sha):
        raise SystemExit("task SHA must be a full 40- or 64-character commit SHA")
    task = dict(task)
    task["version"] = 1
    task["id"] = task.get("id") or str(uuid.uuid4())
    if not ID_RE.fullmatch(task["id"]):
        raise SystemExit("invalid task id")
    task["repository"] = {"fullName": repository, "sha": sha}
    task.setdefault("requiredLabels", ["ipad", "ipad-light", "arm64"])
    task.setdefault("requiredCapabilities", [])
    task.setdefault("statusContext", "ipad-runner/light")
    task.setdefault("artifactPaths", [])
    task.setdefault("completionEvent", "ipad-runner-complete")
    if not isinstance(task["steps"], list) or not 1 <= len(task["steps"]) <= 100:
        raise SystemExit("task steps must contain 1..100 entries")
    capabilities = set(task["requiredCapabilities"])
    for step in task["steps"]:
        if not isinstance(step, dict):
            raise SystemExit("each task step must be an object")
        kind = step.get("kind")
        required = CAPABILITY_BY_KIND.get(kind)
        if required is None or required not in capabilities:
            raise SystemExit(f"step capability missing or unsupported: {kind!r}")
        if not ID_RE.fullmatch(str(step.get("id", ""))):
            raise SystemExit("invalid step id")
        for header in step.get("headers", {}):
            if header.lower() in FORBIDDEN_HEADERS:
                raise SystemExit("secret-bearing HTTP headers are not allowed")
        for field in ("path", "destination"):
            if field in step and step[field] is not None:
                safe_relative(str(step[field]))
    for path in task["artifactPaths"]:
        safe_relative(path)
    return task


def contents_path(repository: str, branch: str, path: str) -> str:
    owner, name = repository.split("/", 1)
    return f"/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(name)}/contents/{urllib.parse.quote(path, safe='/')}?ref={urllib.parse.quote(branch, safe='')}"


def ensure_branch(token: str, repository: str, branch: str, sha: str) -> None:
    owner, name = repository.split("/", 1)
    ref_path = f"/repos/{owner}/{name}/git/ref/heads/{urllib.parse.quote(branch, safe='')}"
    try:
        api_request(token, "GET", ref_path)
        return
    except APIError as error:
        if error.status != 404:
            raise
    try:
        api_request(token, "POST", f"/repos/{owner}/{name}/git/refs", {"ref": f"refs/heads/{branch}", "sha": sha})
    except APIError as error:
        if error.status != 422:
            raise
        api_request(token, "GET", ref_path)


def ensure_fresh_id(token: str, repository: str, branch: str, task_id: str) -> None:
    for state in ("pending", "claims", "results", "rejected"):
        try:
            api_request(token, "GET", contents_path(repository, branch, f"queue/{state}/{task_id}.json"))
        except APIError as error:
            if error.status == 404:
                continue
            raise
        raise SystemExit(f"task id already exists in queue state: {state}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("task", type=Path)
    parser.add_argument("--queue-branch", default="ipad-queue")
    parser.add_argument("--sha", required=True)
    parser.add_argument("--token-env", default="IPAD_PUBLIC_DISPATCH_TOKEN")
    args = parser.parse_args()
    if os.environ.get("GITHUB_EVENT_NAME") in {"pull_request", "pull_request_target"}:
        raise SystemExit("public iPad dispatch rejects pull_request events")
    repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not REPO_RE.fullmatch(repository):
        raise SystemExit("GITHUB_REPOSITORY is required")
    validate_branch(args.queue_branch)
    workspace = Path(os.environ.get("GITHUB_WORKSPACE", ".")).resolve()
    task_path = args.task.resolve()
    try:
        task_path.relative_to(workspace)
    except ValueError as error:
        raise SystemExit("task-file must remain inside GITHUB_WORKSPACE") from error
    if not task_path.is_file() or task_path.stat().st_size > 128 * 1024:
        raise SystemExit("task-file is missing or oversized")
    task = validate_task(json.loads(task_path.read_text(encoding="utf-8")), repository, args.sha)
    token = os.environ.get(args.token_env)
    if not token:
        raise SystemExit(f"{args.token_env} is required")
    ensure_branch(token, repository, args.queue_branch, args.sha)
    ensure_fresh_id(token, repository, args.queue_branch, task["id"])
    data = base64.b64encode((json.dumps(task, indent=2, sort_keys=True) + "\n").encode()).decode()
    owner, name = repository.split("/", 1)
    path = f"/repos/{owner}/{name}/contents/queue/pending/{task['id']}.json"
    api_request(token, "PUT", path, {
        "message": f"Queue public iPad task {task['id']}",
        "content": data,
        "branch": args.queue_branch,
    })
    print(task["id"])


if __name__ == "__main__":
    main()
