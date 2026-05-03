"""
Real data fetcher for Chronos.

Replaces mock_data.py for live repos. Fetches:
  - Commit history and diffs (GitPython, local repo)
  - GitHub PR descriptions and review comments (GitHub REST API, no token required for public repos)

Usage:
    from data_fetcher import fetch_scenario_for_lines
    data = fetch_scenario_for_lines(
        repo_path="/path/to/axios",
        file_path="lib/adapters/fetch.js",
        start_line=369,
        end_line=394,
        github_owner="axios",
        github_repo="axios",
        github_token=None,   # optional, avoids rate limits
    )
"""

import json
import os
import re
import subprocess
from pathlib import Path

import httpx


# ── Git helpers (subprocess-based — avoids GitPython install complexity) ──────

def _run_git(repo_path: str, *args) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout.strip()


def get_commits_for_lines(repo_path: str, file_path: str, start_line: int, end_line: int, max_commits: int = 15) -> list[dict]:
    """
    Return commits that touched the given line range, using `git log -L`.
    Each commit includes sha, author, date, message, and the relevant diff hunk.
    """
    raw = _run_git(
        repo_path,
        "log",
        "--format=COMMIT_START|%H|%ae|%as|%s",
        f"-L{start_line},{end_line}:{file_path}",
        "--no-patch",  # we'll fetch diffs separately per commit
    )

    commits = []
    for line in raw.splitlines():
        if not line.startswith("COMMIT_START|"):
            continue
        _, sha, author_email, date, *msg_parts = line.split("|")
        message = "|".join(msg_parts)  # message may contain pipes
        commits.append({
            "sha": sha[:8],
            "full_sha": sha,
            "author": author_email,
            "date": date,
            "message": message.strip(),
        })
        if len(commits) >= max_commits:
            break

    # Enrich each commit with the diff hunk for the target file
    for commit in commits:
        diff = _run_git(
            repo_path,
            "show",
            "--no-color",
            "--unified=3",
            commit["full_sha"],
            "--",
            file_path,
        )
        # Extract only the relevant hunk(s) — keep it concise for LLM context
        commit["diff"] = _extract_relevant_hunks(diff, start_line, end_line)

    return list(reversed(commits))  # chronological order (oldest first)


def get_file_commits(repo_path: str, file_path: str, max_commits: int = 20) -> list[dict]:
    """Return all commits touching a file (full file history, not line-scoped)."""
    raw = _run_git(
        repo_path,
        "log",
        "--follow",
        "--format=COMMIT_START|%H|%ae|%as|%s",
        "--",
        file_path,
    )
    commits = []
    for line in raw.splitlines():
        if not line.startswith("COMMIT_START|"):
            continue
        _, sha, author_email, date, *msg_parts = line.split("|")
        commits.append({
            "sha": sha[:8],
            "full_sha": sha,
            "author": author_email,
            "date": date,
            "message": "|".join(msg_parts).strip(),
        })
        if len(commits) >= max_commits:
            break
    return list(reversed(commits))


def get_current_file_snippet(repo_path: str, file_path: str, start_line: int, end_line: int) -> str:
    """Return the current content of specific lines from a file."""
    full_path = Path(repo_path) / file_path
    try:
        lines = full_path.read_text(encoding="utf-8", errors="replace").splitlines()
        # 1-indexed to 0-indexed
        snippet_lines = lines[start_line - 1 : end_line]
        return "\n".join(f"{start_line + i:4d} | {line}" for i, line in enumerate(snippet_lines))
    except FileNotFoundError:
        return f"[File not found: {file_path}]"


def _extract_relevant_hunks(diff: str, start_line: int, end_line: int) -> str:
    """
    From a full git diff output, extract hunks that overlap with the target line range.
    Falls back to the first 60 lines of the diff if hunk parsing fails.
    """
    lines = diff.splitlines()
    hunks = []
    current_hunk = []
    in_hunk = False

    for line in lines:
        if line.startswith("@@"):
            if current_hunk:
                hunks.append("\n".join(current_hunk))
            current_hunk = [line]
            in_hunk = True
        elif in_hunk:
            current_hunk.append(line)

    if current_hunk:
        hunks.append("\n".join(current_hunk))

    if not hunks:
        # No structured hunks — return truncated raw diff
        return "\n".join(lines[:60])

    # Return all hunks (for focused -L diffs they're already scoped)
    return "\n".join(hunks[:3])  # cap at 3 hunks to stay concise


def _extract_pr_number_from_message(message: str) -> int | None:
    """Parse PR number from commit messages like 'fix: something (#1234)'."""
    match = re.search(r"\(#(\d+)\)", message)
    if match:
        return int(match.group(1))
    match = re.search(r"#(\d+)", message)
    if match:
        return int(match.group(1))
    return None


# ── GitHub API helpers ────────────────────────────────────────────────────────

def _github_headers(token: str | None) -> dict:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _check_rate_limit(response: httpx.Response) -> bool:
    """Return True if response is a rate-limit error; print a helpful message."""
    if response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0":
        reset_ts = int(response.headers.get("x-ratelimit-reset", 0))
        from datetime import datetime
        reset_time = datetime.fromtimestamp(reset_ts).strftime("%H:%M:%S") if reset_ts else "soon"
        print(f"[data_fetcher] GitHub API rate limit hit. Resets at {reset_time}. "
              "Set GITHUB_TOKEN env var to get 5000 req/hr.")
        return True
    return False


def get_pr_for_commit(owner: str, repo: str, sha: str, token: str | None = None) -> dict | None:
    """Find the PR associated with a commit SHA via GitHub API."""
    try:
        resp = httpx.get(
            f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}/pulls",
            headers=_github_headers(token),
            timeout=10.0,
        )
        if _check_rate_limit(resp):
            return None
        if resp.status_code == 200:
            prs = resp.json()
            return prs[0] if prs else None
    except Exception:
        pass
    return None


def get_pr_details(owner: str, repo: str, pr_number: int, token: str | None = None) -> dict:
    """Fetch PR title, body, and review comments."""
    headers = _github_headers(token)
    result = {"pr_number": pr_number, "title": "", "body": "", "review_comments": [], "issue_comments": []}

    try:
        pr_resp = httpx.get(
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
            headers=headers, timeout=10.0,
        )
        if _check_rate_limit(pr_resp):
            return result
        if pr_resp.status_code == 200:
            pr = pr_resp.json()
            result["title"] = pr.get("title", "")
            result["body"] = pr.get("body", "") or ""
            result["url"] = pr.get("html_url", "")
    except Exception:
        pass

    try:
        reviews_resp = httpx.get(
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
            headers=headers, timeout=10.0,
        )
        if reviews_resp.status_code == 200:
            for r in reviews_resp.json():
                body = (r.get("body") or "").strip()
                if body:
                    result["review_comments"].append({
                        "author": r.get("user", {}).get("login", ""),
                        "body": body,
                        "state": r.get("state", ""),
                    })
    except Exception:
        pass

    try:
        comments_resp = httpx.get(
            f"https://api.github.com/repos/{owner}/{repo}/issues/{pr_number}/comments",
            headers=headers, timeout=10.0,
        )
        if comments_resp.status_code == 200:
            for c in comments_resp.json():
                body = (c.get("body") or "").strip()
                if body:
                    result["issue_comments"].append({
                        "author": c.get("user", {}).get("login", ""),
                        "body": body[:500],  # cap length
                    })
    except Exception:
        pass

    return result


def get_issue_details(owner: str, repo: str, issue_number: int, token: str | None = None) -> dict | None:
    """Fetch a GitHub issue (bug reports are often referenced in commit messages)."""
    headers = _github_headers(token)
    try:
        resp = httpx.get(
            f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}",
            headers=headers, timeout=10.0,
        )
        if resp.status_code == 200:
            issue = resp.json()
            comments_resp = httpx.get(
                f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}/comments",
                headers=headers, timeout=10.0,
            )
            comments = []
            if comments_resp.status_code == 200:
                for c in comments_resp.json()[:5]:  # first 5 comments
                    body = (c.get("body") or "").strip()
                    if body:
                        comments.append({"author": c["user"]["login"], "body": body[:400]})
            return {
                "issue_number": issue_number,
                "title": issue.get("title", ""),
                "body": (issue.get("body") or "")[:800],
                "state": issue.get("state", ""),
                "comments": comments,
            }
    except Exception:
        pass
    return None


def _extract_keywords_from_snippet(snippet: str) -> list[str]:
    """
    Extract lowercase keywords from a code snippet to use for augmenting commit searches.
    Strips line numbers, operators, and punctuation; returns unique meaningful tokens.
    """
    # Strip line-number prefixes (e.g. " 369 | ")
    clean = re.sub(r"^\s*\d+\s*\|\s*", "", snippet, flags=re.MULTILINE)
    # Pull out camelCase / snake_case identifiers and plain words
    tokens = re.findall(r"[a-zA-Z][a-zA-Z0-9]{3,}", clean)
    # Lowercase and deduplicate; skip generic JS keywords
    skip = {"function", "return", "const", "typeof", "number", "string", "null", "true", "false", "throw", "else"}
    return list({t.lower() for t in tokens if t.lower() not in skip})[:15]


# ── Main entry point ──────────────────────────────────────────────────────────

def fetch_scenario_for_lines(
    repo_path: str,
    file_path: str,
    start_line: int,
    end_line: int,
    github_owner: str,
    github_repo: str,
    github_token: str | None = None,
    max_commits: int = 10,
) -> dict:
    """
    Fetch everything Chronos needs to analyze a specific line range in a real repo.

    Returns a dict in the same shape as SCENARIOS entries in mock_data.py, so
    the rest of the pipeline (mcp_server.py, agents) works without modification.
    """
    print(f"[data_fetcher] Fetching git history for {file_path} L{start_line}-{end_line}...")
    commits = get_commits_for_lines(repo_path, file_path, start_line, end_line, max_commits)

    print(f"[data_fetcher] Found {len(commits)} commits touching those lines.")

    # When the line-scoped commits are sparse (e.g. the lines were *added* by a fix
    # commit, so git log -L only finds that one commit), also include the file's very
    # first commit — the origin where the original assumption was baked in.
    # We do NOT do a broad keyword sweep across file history; that would pull in
    # unrelated commits from large repos. One origin commit is enough to anchor the
    # "what did the original author assume?" question.
    if len(commits) < 3:
        all_file_commits = get_file_commits(repo_path, file_path, max_commits=50)
        seen_shas = {c["full_sha"] for c in commits}
        origin = all_file_commits[0] if all_file_commits else None
        if origin and origin["full_sha"] not in seen_shas:
            print(f"[data_fetcher] Augmenting with file origin commit {origin['sha']} ({origin['date']})...")
            diff = _run_git(repo_path, "show", "--no-color", "--unified=3",
                            origin["full_sha"], "--", file_path)
            origin = dict(origin)
            origin["diff"] = _extract_relevant_hunks(diff, start_line, end_line)
            all_commits = sorted(commits + [origin], key=lambda c: c["date"])
            commits = all_commits[:max_commits]
    # ── PR context: fixtures file takes priority over GitHub API ─────────────────
    # If the repo contains a pr_fixtures.json, use it. This enables fully offline
    # demos with scripted PR narratives and zero GitHub API dependency.
    fixtures_path = Path(repo_path) / "pr_fixtures.json"
    pr_context = {}

    if fixtures_path.exists():
        print(f"[data_fetcher] Using pr_fixtures.json (offline mode)...")
        all_fixtures: dict = json.loads(fixtures_path.read_text(encoding="utf-8"))
        # Match fixtures to the commits we found, by full SHA or short SHA
        for commit in commits:
            for sha_key in (commit["full_sha"], commit["sha"]):
                if sha_key in all_fixtures:
                    pr_data = all_fixtures[sha_key]
                    pr_key = pr_data.get("key", f"pr_{sha_key[:7]}")
                    pr_context[pr_key] = pr_data
                    break
    else:
        print(f"[data_fetcher] Fetching PR context from GitHub ({github_owner}/{github_repo})...")
        seen_prs: set = set()

        # Prioritise oldest commit (origin assumption) and newest (fix) to minimise
        # API calls. Without a token the rate limit is 60 req/hr — cap at 5 PRs.
        MAX_PR_FETCHES = 5
        if commits:
            priority_shas = {commits[0]["full_sha"], commits[-1]["full_sha"]}
            ordered_commits = (
                [c for c in commits if c["full_sha"] in priority_shas]
                + [c for c in commits if c["full_sha"] not in priority_shas]
            )
        else:
            ordered_commits = commits

        for commit in ordered_commits:
            if len(seen_prs) >= MAX_PR_FETCHES:
                break
            pr_number = _extract_pr_number_from_message(commit["message"])

            if not pr_number:
                pr = get_pr_for_commit(github_owner, github_repo, commit["full_sha"], github_token)
                if pr:
                    pr_number = pr.get("number")

            if pr_number and pr_number not in seen_prs:
                seen_prs.add(pr_number)
                details = get_pr_details(github_owner, github_repo, pr_number, github_token)
                pr_key = f"pr_{pr_number}"
                pr_context[pr_key] = {
                    "title": details["title"],
                    "body": details["body"][:600],
                    "url": details.get("url", f"https://github.com/{github_owner}/{github_repo}/pull/{pr_number}"),
                    "review_comments": [
                        f"{c['body'][:300]} — @{c['author']}" for c in details["review_comments"][:4]
                    ],
                    "issue_comments": [
                        f"{c['body'][:300]} — @{c['author']}" for c in details["issue_comments"][:3]
                    ],
                }
                # Linked issue fetching deferred — costs extra API calls.
                # Enable by passing github_token to raise rate limit to 5000/hr.

    print(f"[data_fetcher] Fetched context for PRs/issues: {list(pr_context.keys())}")

    current_snippet = get_current_file_snippet(repo_path, file_path, start_line, end_line)

    # Derive the GitHub remote URL for display
    remote_url = f"https://github.com/{github_owner}/{github_repo}"

    return {
        "description": f"Real analysis of {file_path} L{start_line}-{end_line} in {github_owner}/{github_repo}",
        "repo_path": repo_path,
        "file_path": file_path,
        "line_range": [start_line, end_line],
        "github_url": f"{remote_url}/blob/HEAD/{file_path}#L{start_line}-L{end_line}",
        "commits": [
            {
                "sha": c["sha"],
                "full_sha": c["full_sha"],
                "author": c["author"],
                "date": c["date"],
                "message": c["message"],
                "diff": c["diff"],
            }
            for c in commits
        ],
        "pr_context": pr_context,
        "code_snippet": current_snippet,
        # assumptions will be extracted by the LLM — not pre-defined for real repos
        "assumptions": [],
        "tradeoffs": [],
    }
