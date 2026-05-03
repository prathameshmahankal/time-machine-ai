"""
Chronos MCP Server

Serves two clients:
  1. IBM Bob (VS Code extension, MCP client) — developer-facing interface
  2. watsonx Orchestrate agents — called by Decision Analyst agent tools

Run: python mcp_server.py
Listens at: http://localhost:8001/mcp (Streamable HTTP transport)
"""

import json
import os
import re
import sys
import threading
import time
from datetime import datetime, date

import httpx
from dotenv import load_dotenv
from fastmcp import FastMCP

from mock_data import SCENARIOS, validate_assumption, compute_risk_score, compute_debt_verdict
from data_fetcher import fetch_scenario_for_lines

load_dotenv()

def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    sys.stderr.write(f"[Chronos {ts}] {msg}\n")
    sys.stderr.flush()

ORCHESTRATE_URL = os.environ.get("WATSONX_ORCHESTRATE_URL", "")
# WATSONX_API_KEY: IBM Cloud API key — exchanged for short-lived IAM bearer tokens automatically.
# WATSONX_ORCHESTRATE_TOKEN: used as-is for local Developer Edition (no IAM exchange needed).
WATSONX_API_KEY = os.environ.get("WATSONX_API_KEY", "")
ORCHESTRATE_TOKEN = os.environ.get("WATSONX_ORCHESTRATE_TOKEN", "")
USE_ORCHESTRATE = bool(ORCHESTRATE_URL and (WATSONX_API_KEY or ORCHESTRATE_TOKEN))

# IAM token cache — auto-refreshed when within 60s of expiry
_iam_cache: dict = {"token": None, "expires_at": 0.0}
_iam_lock = threading.Lock()


def _get_bearer_token() -> str | None:
    """
    Return a valid IBM Cloud IAM bearer token, refreshing automatically when needed.
    Falls back to WATSONX_ORCHESTRATE_TOKEN for local Developer Edition (no IAM exchange).
    """
    if not WATSONX_API_KEY:
        return ORCHESTRATE_TOKEN or None

    with _iam_lock:
        now = time.time()
        if _iam_cache["token"] and now < _iam_cache["expires_at"] - 60:
            return _iam_cache["token"]

        try:
            resp = httpx.post(
                "https://iam.cloud.ibm.com/identity/token",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data=f"grant_type=urn:ibm:params:oauth:grant-type:apikey&apikey={WATSONX_API_KEY}",
                timeout=15.0,
            )
            resp.raise_for_status()
            payload = resp.json()
            _iam_cache["token"] = payload["access_token"]
            _iam_cache["expires_at"] = now + payload.get("expires_in", 3600)
            _log(f"  IAM token refreshed — valid for {payload.get('expires_in', 3600)}s")
            return _iam_cache["token"]
        except Exception as e:
            _log(f"  IAM token refresh FAILED: {e}")
            return None

mcp = FastMCP(
    "chronos",
    instructions=(
        "Chronos is a developer intelligence platform that reconstructs historical "
        "engineering decisions and identifies stale assumptions that create risk. "
        "Use analyze_decisions to understand why specific code was built a certain way. "
        "Use scan_for_decision_debt to find the highest-risk stale decisions across a repo."
    ),
)


def _build_analysis_message(scenario_key: str, scenario_data: dict) -> str:
    """Bundle all scenario data into a natural language prompt for the Orchestrate agent."""
    commits = scenario_data.get("commits", [])
    pr_context = scenario_data.get("pr_context", {})
    assumptions = scenario_data.get("assumptions", [])
    tradeoffs = scenario_data.get("tradeoffs", [])

    lines = [
        f"Analyze the engineering decisions for: **{scenario_key}**",
        "",
        "## Commit History",
    ]
    for c in commits:
        lines += [f"- `{c['sha']}` ({c['date']}) **{c['author']}**: {c['message']}"]

    if pr_context:
        lines += ["", "## PR Context"]
        for pr_id, pr in pr_context.items():
            lines += [f"**{pr_id.upper()}**: {pr.get('title', '')}", pr.get("body", "")[:500]]
            for comment in pr.get("review_comments", []):
                lines.append(f"  - Review: {comment}")
            for comment in pr.get("issue_comments", []):
                lines.append(f"  - Issue: {comment}")

    if assumptions:
        lines += ["", "## Detected Assumptions"]
        for a in assumptions:
            lines.append(f"- '{a['text']}' (introduced {a['introduced']}, source: {a.get('source','')})")

    if tradeoffs:
        lines += ["", "## Tradeoffs"]
        for t in tradeoffs:
            lines.append(f"- Chosen: {t['chosen']} | Rejected: {t['rejected']} | Rationale: {t['rationale']}")

    lines += [
        "",
        "---",
        "",
        "## Output Instructions",
        "",
        "Produce a compact key-insights report. Follow this template exactly — no extra sections, no padding:",
        "",
        "```",
        "# 🔴 Decision Debt Detected  OR  # 🟢 No Decision Debt",
        "`<function>` in `<repo>` · Risk <X>/10 · Confidence <N>/100",
        "",
        "<one sentence: why is_stale is true or false — be decisive, no hedging>",
        "",
        "---",
        "",
        "## What Was Decided",
        "<one sentence describing the core engineering choice> — `<sha>` (<date>)",
        "",
        "---",
        "",
        "## Key Signals",
        "- <signal 1 — most important, e.g. stale assumption with specific reason>",
        "- <signal 2>  ← max 3 bullets total; omit this section if no signals",
        "- <signal 3>",
        "",
        "---",
        "",
        "## Evidence",
        "- `<sha>` (<date>) **<author>** — <commit message>  ← max 3 commits",
        "",
        "> \"<most relevant PR quote, max 120 chars>\"",
        "> *— <PR title>*",
        "",
        "---",
        "",
        "## Recommendation",
        "<one sentence action — specific, not generic>",
        "```",
        "",
        "Rules:",
        "- is_stale = true if: original assumptions no longer hold, unnecessary complexity added, or new issues caused.",
        "- If no signals exist, write '🟢 No Decision Debt' and say so directly — do not hedge.",
        "- Every claim must trace to a specific commit SHA or PR quote from the data above.",
        "- Never invent commits, quotes, or assumptions not present in the data.",
        "- Max 3 commits in Evidence. Max 1 PR quote. Max 3 signals. One sentence per section.",
    ]
    return "\n".join(lines)


def _call_orchestrate(scenario_key: str, scenario_data: dict, message: str = "", agent_id: str = "") -> str | None:
    """
    Forward a request to watsonx Orchestrate (cloud async API).
    POST /v1/orchestrate/runs → poll → fetch /v1/orchestrate/threads/{id}/messages.
    Returns synthesized markdown string or None on failure.
    If message is provided it is used directly; otherwise _build_analysis_message is called.
    If agent_id is provided it overrides the WATSONX_AGENT_ID env var.
    """
    if not USE_ORCHESTRATE:
        return None

    _log(f"→ watsonx Orchestrate — dispatching analysis for: {scenario_key}")

    token = _get_bearer_token()
    if not token:
        _log("  FAILED: could not obtain IAM bearer token")
        return None

    agent_id = agent_id or os.environ.get("WATSONX_AGENT_ID", "")
    _log(f"  agent: {agent_id or '(default)'} · model: Granite 3 8B Instruct")
    _log(f"  endpoint: {ORCHESTRATE_URL}/v1/orchestrate/runs")

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    base = ORCHESTRATE_URL.rstrip("/")

    try:
        # Step 1: Create a run
        content = message if message else _build_analysis_message(scenario_key, scenario_data)
        payload: dict = {
            "message": {"role": "user", "content": content},
            "capture_logs": False,
        }
        if agent_id:
            payload["agent_id"] = agent_id

        create_resp = httpx.post(f"{base}/v1/orchestrate/runs", headers=headers, json=payload, timeout=30.0)
        create_resp.raise_for_status()
        run = create_resp.json()

        run_id = run.get("run_id")
        thread_id = run.get("thread_id")
        if not run_id:
            _log(f"  FAILED: no run_id in response: {run}")
            return None

        _log(f"  run created — run_id: {run_id}")
        _log(f"  thread_id: {thread_id}")

        # Step 2: Poll until completed (up to 90s)
        elapsed = 0
        last_status = ""
        for _ in range(45):
            time.sleep(2)
            elapsed += 2
            status_resp = httpx.get(f"{base}/v1/orchestrate/runs/{run_id}", headers=headers, timeout=15.0)
            if status_resp.status_code == 200:
                current = status_resp.json().get("status", "unknown")
                if current != last_status:
                    _log(f"  polling... status={current} ({elapsed}s elapsed)")
                    last_status = current
                if current in ("completed", "failed", "cancelled"):
                    thread_id = thread_id or status_resp.json().get("thread_id")
                    break

        # Step 3: Fetch assistant message from thread
        if thread_id:
            msgs_resp = httpx.get(f"{base}/v1/orchestrate/threads/{thread_id}/messages", headers=headers, timeout=15.0)
            if msgs_resp.status_code == 200:
                msgs_data = msgs_resp.json()
                messages = msgs_data if isinstance(msgs_data, list) else msgs_data.get("data", [])
                for msg in reversed(messages):
                    if msg.get("role") == "assistant":
                        content = msg.get("content", "")
                        if isinstance(content, str) and content.strip():
                            _log(f"← Granite response received ({len(content):,} chars) — returning to IBM Bob")
                            
                            # Validate response quality
                            if len(content) < 200:
                                _log(f"  WARNING: Response unusually short ({len(content)} chars)")
                                _log(f"  This may indicate an error or incomplete generation")
                            
                            return content
                        if isinstance(content, list):
                            parts = []
                            for item in content:
                                if isinstance(item, dict):
                                    if item.get("response_type") == "text":
                                        parts.append(item.get("text", ""))
                                    elif isinstance(item.get("text"), str):
                                        parts.append(item["text"])
                            result = "\n".join(p for p in parts if p)
                            if result.strip():
                                _log(f"← Granite response received ({len(result):,} chars) — returning to IBM Bob")
                                return result

        _log(f"  WARNING: run completed but no assistant message found (thread={thread_id})")
        return None

    except Exception as e:
        _log(f"  FAILED: {type(e).__name__}: {e}")
        return None


def _format_decision_markdown(scenario_key: str, data: dict) -> str:
    """
    Compact, verdict-first key-insights report.

    Structure:
      [verdict header — 3 lines]
      ## What Was Decided  (1-2 sentences + origin commit)
      ## Key Signals       (top 3 signals that fired)
      ## Evidence          (max 3 commits + 1 PR quote)
      ## Recommendation    (1 sentence)
    """
    assumptions = data.get("assumptions", [])
    commits = data.get("commits", [])
    tradeoffs = data.get("tradeoffs", [])
    pr_context = data.get("pr_context", {})
    code_snippet = data.get("code_snippet", "")

    repo, func = scenario_key.split("::", 1) if "::" in scenario_key else (scenario_key, "")

    # ── Sparse-content guard ───────────────────────────────────────────────────
    if len(commits) < 2 and not pr_context:
        header = f"⚠️ Limited History — `{func}` in `{repo}`"
        msg = (
            f"Only {len(commits)} commit(s) found touching these lines. "
            "The decision may have been made in a parent scope — "
            "try selecting the enclosing function for richer context."
        )
        return "\n".join([header, "", msg])

    # ── Core computations ──────────────────────────────────────────────────────
    oldest_date = min(c["date"] for c in commits) if commits else "unknown"
    risk = compute_risk_score(assumptions, oldest_date, dependent_count=47)
    verdict = compute_debt_verdict(assumptions, commits, pr_context, code_snippet)

    tier_emoji = {"CRITICAL": "🔴", "HIGH": "🟡", "MEDIUM": "🟠", "LOW": "🟢"}[risk["tier"]]
    verdict_icon = "🔴 Decision Debt Detected" if verdict["is_stale"] else "🟢 No Decision Debt"

    lines = [
        f"# {verdict_icon}",
        f"`{func}` in `{repo}` · Risk {risk['score']}/10 {tier_emoji} · Confidence {verdict['confidence_score']}/100",
        "",
        verdict["reasoning"],
        "",
        "---",
    ]

    # ── What Was Decided ───────────────────────────────────────────────────────
    lines += ["", "## What Was Decided", ""]
    if tradeoffs:
        t = tradeoffs[0]
        lines.append(f"{t['chosen']} *(rejected: {t['rejected']})*")
    if commits:
        origin = commits[0]
        lines += ["", f"> `{origin['sha']}` {origin['author']} ({origin['date']}) — {origin['message']}"]

    # ── Key Signals (top 3) ────────────────────────────────────────────────────
    lines += ["", "---", "", "## Key Signals", ""]
    top_signals = verdict["signals_used"][:3]
    if top_signals == ["No debt signals detected"]:
        lines.append("No debt signals detected for this decision.")
    else:
        for s in top_signals:
            lines.append(f"- {s}")

    # ── Evidence (max 3 commits + 1 PR quote) ─────────────────────────────────
    lines += ["", "---", "", "## Evidence", ""]

    # Pick: oldest, the commit that introduced a stale assumption (if distinct), newest
    stale_shas = {
        a.get("introduced_by", "")
        for a in assumptions
        if validate_assumption(a["text"], a["introduced"])["validity"] == "STALE"
    }
    selected: list[dict] = []
    seen: set[str] = set()
    for c in ([commits[0]] + [c for c in commits if c["sha"] in stale_shas] + [commits[-1]]):
        if c["sha"] not in seen:
            selected.append(c)
            seen.add(c["sha"])
    for c in selected[:3]:
        lines.append(f"- `{c['sha']}` ({c['date']}) **{c['author']}** — {c['message']}")

    # Most signal-relevant PR quote (first concern-flagging comment found)
    concern_kw = ("risk", "revisit", "what if", "what happens", "careful", "concern", "problem")
    best_quote: str = ""
    best_pr: str = ""
    for pr_id, pr in pr_context.items():
        for comment in pr.get("review_comments", []) + pr.get("issue_comments", []):
            if any(kw in comment.lower() for kw in concern_kw):
                best_quote = comment
                best_pr = pr.get("title", pr_id)
                break
        if best_quote:
            break
    if not best_quote and pr_context:
        # Fall back to the first review comment in any PR
        for pr_id, pr in pr_context.items():
            comments = pr.get("review_comments", [])
            if comments:
                best_quote = comments[0]
                best_pr = pr.get("title", pr_id)
                break
    if best_quote:
        lines += ["", f"> \"{best_quote[:120]}{'...' if len(best_quote) > 120 else ''}\"", f"> *— {best_pr}*"]

    # ── Recommendation (1 sentence) ────────────────────────────────────────────
    lines += ["", "---", "", "## Recommendation", ""]
    if verdict["is_stale"]:
        if risk["tier"] == "CRITICAL":
            lines.append("⚠️ **Immediate review required** — schedule an architecture review before the next release touching this code.")
        else:
            lines.append("**Plan a re-evaluation** — add to the next tech debt sprint and revisit the stale assumptions.")
    else:
        lines.append("No action required — monitor during the next architecture review cycle.")

    return "\n".join(lines)


@mcp.tool()
def analyze_decisions(scenario_key: str) -> str:
    """
    Reconstruct the engineering decisions, assumptions, and risks behind a specific function or module.

    Routes through watsonx Orchestrate for LLM-powered synthesis when available,
    falls back to direct analysis using mock data.

    Args:
        scenario_key: Identifies the function to analyze. Format: "repo::FunctionName".
                      Available: "requests::HTTPAdapter.send", "payments::process_payment"

    Returns:
        Structured markdown analysis: Decision | Assumptions (VALID/STALE) | Tradeoffs | Evidence | Risk Score
    """
    data = SCENARIOS.get(scenario_key)
    if not data:
        available = ", ".join(f'"{k}"' for k in SCENARIOS)
        return f"Unknown scenario '{scenario_key}'. Available: {available}"

    orchestrate_result = _call_orchestrate(scenario_key, data)
    if orchestrate_result:
        return orchestrate_result

    return _format_decision_markdown(scenario_key, data)


@mcp.tool()
def scan_for_decision_debt(repo_name: str) -> str:
    """
    Scan a repository for stale engineering decisions across all major components.
    Returns a risk heatmap ranked by staleness score — the highest-risk decisions first.

    Args:
        repo_name: Repository name to scan. E.g. "requests", "payments"

    Returns:
        Markdown risk table sorted by risk score descending.
    """
    matching = {k: v for k, v in SCENARIOS.items() if repo_name.lower() in k.lower()}

    if not matching:
        available_repos = sorted({k.split("::")[0] for k in SCENARIOS})
        return f"No scenarios found for '{repo_name}'. Available repos: {', '.join(available_repos)}"

    items = []
    for key, data in matching.items():
        _, func = key.split("::", 1) if "::" in key else (key, key)
        assumptions = data.get("assumptions", [])
        commits = data.get("commits", [])
        oldest_date = min(c["date"] for c in commits) if commits else "2020-01-01"

        risk = compute_risk_score(assumptions, oldest_date, dependent_count=47 if "requests" in key else 12)
        items.append(
            {
                "key": key,
                "func": func,
                "risk": risk,
                "oldest_date": oldest_date,
                "assumption_count": len(assumptions),
            }
        )

    items.sort(key=lambda x: x["risk"]["score"], reverse=True)

    lines = [
        f"# Decision Debt Scan: `{repo_name}`",
        "",
        f"Scanned {len(items)} component(s). Ranked by risk score.",
        "",
        "| Component | Risk Score | Tier | Stale | Age (days) |",
        "|-----------|-----------|------|-------|------------|",
    ]

    tier_emoji = {"CRITICAL": "🔴", "HIGH": "🟡", "MEDIUM": "🟠", "LOW": "🟢"}
    for item in items:
        r = item["risk"]
        b = r["breakdown"]
        emoji = tier_emoji[r["tier"]]
        lines.append(
            f"| `{item['func']}` | **{r['score']}/10** | {emoji} {r['tier']} "
            f"| {b['stale_assumptions']}/{item['assumption_count']} | {b['decision_age_days']} |"
        )

    lines += [""]

    critical = [i for i in items if i["risk"]["tier"] == "CRITICAL"]
    high = [i for i in items if i["risk"]["tier"] == "HIGH"]

    if critical:
        lines += [
            "## ⚠️ Critical Risk Items",
            "",
            "These require **immediate architecture review** before the next release:",
            "",
        ]
        for item in critical:
            lines.append(f"- `{item['func']}` — {item['risk']['score']}/10 ({item['risk']['breakdown']['stale_assumptions']} stale assumptions, decision is {item['risk']['breakdown']['decision_age_days']} days old)")

    if high:
        lines += [
            "",
            "## 🟡 High Risk Items",
            "",
            "Prioritize for the next tech debt sprint:",
            "",
        ]
        for item in high:
            lines.append(f"- `{item['func']}` — {item['risk']['score']}/10")

    lines += [
        "",
        "---",
        f"*Run `analyze_decisions(\"<repo>::<function>\")` on any component for a full decision breakdown.*",
    ]

    return "\n".join(lines)


@mcp.tool()
def get_repo_info(absolute_file_path: str) -> str:
    """
    Given an absolute path to any file inside a git repository, return the
    repo root path and the GitHub owner/repo derived from the git remote URL.
    Use this before calling analyze_real_file to auto-detect the parameters.

    Args:
        absolute_file_path: Absolute path to any file inside the repo.
                            Example: "/Users/you/projects/axios/lib/adapters/fetch.js"

    Returns:
        JSON with repo_path, file_path (relative), github_owner, github_repo.
    """
    import subprocess
    from pathlib import Path

    _log(f"← IBM Bob connected — get_repo_info({absolute_file_path})")

    path = Path(absolute_file_path)
    search = path if path.is_dir() else path.parent
    repo_path = None
    for parent in [search, *search.parents]:
        if (parent / ".git").exists():
            repo_path = str(parent)
            break

    if not repo_path:
        _log(f"  ERROR: no git repo found at {absolute_file_path}")
        return json.dumps({"error": f"No git repo found containing {absolute_file_path}"})

    file_path = str(path.relative_to(repo_path)) if not path.is_dir() else ""

    # Derive GitHub owner/repo from remote URL
    github_owner, github_repo = "", ""
    try:
        remote = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=repo_path, capture_output=True, text=True
        ).stdout.strip()
        # Handles https://github.com/owner/repo.git and git@github.com:owner/repo.git
        match = re.search(r"github\.com[:/]([^/]+)/([^/\s]+?)(?:\.git)?$", remote)
        if match:
            github_owner, github_repo = match.group(1), match.group(2)
    except Exception:
        pass

    # If no remote (e.g. local demo repo with pr_fixtures.json), leave owner/repo blank
    pr_fixtures = (Path(repo_path) / "pr_fixtures.json").exists()

    _log(f"  repo: {repo_path}  file: {file_path}")
    _log(f"  remote: {github_owner}/{github_repo}  {'[offline — pr_fixtures.json]' if pr_fixtures else '[GitHub API]'}")

    return json.dumps({
        "repo_path": repo_path,
        "file_path": file_path,
        "github_owner": github_owner,
        "github_repo": github_repo,
        "has_pr_fixtures": pr_fixtures,
        "note": "Uses pr_fixtures.json (offline)" if pr_fixtures else "Uses GitHub API",
    }, indent=2)


@mcp.tool()
def get_commit_history(scenario_key: str) -> str:
    """
    Return the git commit timeline for a function (used by watsonx Orchestrate agents).

    Args:
        scenario_key: Format "repo::FunctionName"

    Returns:
        JSON string of commits ordered oldest to newest.
    """
    data = SCENARIOS.get(scenario_key, {})
    return json.dumps(data.get("commits", []), indent=2)


@mcp.tool()
def get_pr_context(scenario_key: str) -> str:
    """
    Return PR discussions and review comments for a function (used by watsonx Orchestrate agents).

    Args:
        scenario_key: Format "repo::FunctionName"

    Returns:
        JSON string of PR context including review comments.
    """
    data = SCENARIOS.get(scenario_key, {})
    return json.dumps(data.get("pr_context", {}), indent=2)


@mcp.tool()
def detect_code_assumptions(scenario_key: str) -> str:
    """
    Detect assumptions embedded in code — hardcoded thresholds, dependency pins,
    scale-dependent logic, TODO comments, and temporal assumptions.

    Args:
        scenario_key: Format "repo::FunctionName"

    Returns:
        JSON string of detected assumptions with their source locations.
    """
    data = SCENARIOS.get(scenario_key, {})
    assumptions = data.get("assumptions", [])
    validated = [
        {**a, "validation": validate_assumption(a["text"], a["introduced"])}
        for a in assumptions
    ]
    return json.dumps(validated, indent=2)


@mcp.tool()
def validate_assumptions(scenario_key: str) -> str:
    """
    Classify each assumption as VALID, STALE, or UNKNOWN based on current ecosystem state.

    Args:
        scenario_key: Format "repo::FunctionName"

    Returns:
        JSON string with validity classification and reasoning for each assumption.
    """
    data = SCENARIOS.get(scenario_key, {})
    results = [
        {
            "assumption": a["text"],
            "introduced": a["introduced"],
            **validate_assumption(a["text"], a["introduced"]),
        }
        for a in data.get("assumptions", [])
    ]
    return json.dumps(results, indent=2)


@mcp.tool()
def compute_risk_score_tool(scenario_key: str) -> str:
    """
    Compute a 0-10 risk score for a decision based on stale assumptions, age, and blast radius.

    Args:
        scenario_key: Format "repo::FunctionName"

    Returns:
        JSON string with risk score, tier (CRITICAL/HIGH/MEDIUM/LOW), and breakdown.
    """
    data = SCENARIOS.get(scenario_key, {})
    assumptions = data.get("assumptions", [])
    commits = data.get("commits", [])
    oldest_date = min(c["date"] for c in commits) if commits else "2020-01-01"
    dependent_count = 47 if "requests" in scenario_key else 12
    result = compute_risk_score(assumptions, oldest_date, dependent_count)
    return json.dumps(result, indent=2)


def _build_refactor_message(
    file_path: str,
    start_line: int,
    end_line: int,
    current_code: str,
    stale_assumption: str,
    desired_simplification: str,
    github_owner: str,
    github_repo: str,
) -> str:
    return "\n".join([
        "You are a staff engineer performing a minimal, targeted code refactor.",
        "",
        f"## Context",
        f"Repo: {github_owner}/{github_repo}",
        f"File: {file_path}, lines {start_line}–{end_line}",
        "",
        "## Stale Decision",
        stale_assumption,
        "",
        "## Current Code",
        "```",
        current_code,
        "```",
        "",
        "## Desired Simplification",
        desired_simplification,
        "",
        "## Task",
        "Generate a minimal refactor that:",
        "1. Removes or updates only the logic tied to the stale decision",
        "2. Preserves all other functionality exactly",
        "3. Makes no unrelated changes",
        "",
        "Output EXACTLY this structure using the markers literally:",
        "",
        "REFACTORED_CODE_START",
        "[complete refactored code for the selected lines, ready to paste]",
        "REFACTORED_CODE_END",
        "",
        "PR_TITLE_START",
        "[imperative mood, max 60 chars]",
        "PR_TITLE_END",
        "",
        "WHAT_REMOVED_START",
        "[2-3 bullet points: exactly what was changed or removed]",
        "WHAT_REMOVED_END",
        "",
        "WHY_SAFE_START",
        "[2-3 bullet points: why this is safe — what guarantees correctness]",
        "WHY_SAFE_END",
        "",
        "EXPECTED_IMPACT_START",
        "[2-3 bullet points: performance, maintainability, or correctness improvements]",
        "EXPECTED_IMPACT_END",
        "",
        "Rules:",
        "- REFACTORED_CODE must be complete and syntactically valid",
        "- Do not add explanatory comments unless replacing an outdated one",
        "- Do not refactor beyond the stale decision scope",
        "- Preserve indentation and style of the original",
    ])

def _build_simple_refactor_message(
    current_code: str,
    stale_assumption: str,
    desired_simplification: str,
) -> str:
    """Simplified refactor prompt for fallback retry - focuses only on code generation."""
    return "\n".join([
        "Refactor this code to address the stale assumption.",
        "",
        "## Current Code",
        "```",
        current_code,
        "```",
        "",
        "## Problem",
        stale_assumption,
        "",
        "## Goal",
        desired_simplification,
        "",
        "## Instructions",
        "Output ONLY the refactored code between these exact markers:",
        "",
        "REFACTORED_CODE_START",
        "[your refactored code here - complete and syntactically valid]",
        "REFACTORED_CODE_END",
        "",
        "Do not include explanations, just the markers and code.",
    ])


def _parse_refactor_response(response: str) -> dict:
    """
    Extract the five sections from Granite's structured refactor output.
    Tries three strategies in order, logging which one succeeded.
    """
    # Debug logging: capture response details for troubleshooting
    _log(f"  Parsing response: {len(response)} chars")
    
    # Validate response quality before parsing
    if len(response) < 200:
        _log(f"  WARNING: Response suspiciously short ({len(response)} chars)")
        _log(f"  Full response: {response}")
        
        # Check for common error patterns
        error_patterns = ["I cannot", "I'm unable", "Error:", "Failed:",
                         "Sorry", "I don't have", "not available", "I apologize"]
        response_lower = response.lower()
        for pattern in error_patterns:
            if pattern.lower() in response_lower:
                _log(f"  ERROR: Response appears to be an error message (contains '{pattern}')")
                break
    else:
        # Log preview for longer responses
        preview = response[:300] + "..." if len(response) > 300 else response
        _log(f"  Response preview: {preview}")
    
    def extract(tag: str) -> str:
        # Strategy 1: flexible whitespace around the markers (Granite adds blank lines)
        m = re.search(rf"{tag}_START\s*(.*?)\s*{tag}_END", response, re.DOTALL | re.IGNORECASE)
        return m.group(1).strip() if m else ""

    refactored_code = extract("REFACTORED_CODE")
    used_fallback = False

    # Strategy 2 (code only): first triple-backtick block in the response
    if not refactored_code:
        m = re.search(r"```(?:\w*)\n(.*?)```", response, re.DOTALL)
        if m:
            refactored_code = m.group(1).strip()
            used_fallback = True

    pr_title = extract("PR_TITLE")
    what_removed = extract("WHAT_REMOVED")
    why_safe = extract("WHY_SAFE")
    expected_impact = extract("EXPECTED_IMPACT")

    # Strategy 3 (PR sections only): match Granite's natural markdown headings
    if not what_removed:
        m = re.search(r"(?:\*\*What was removed\*\*|##\s*What Was Removed)[:\s]*(.*?)(?=\n\n|\Z)", response, re.DOTALL | re.IGNORECASE)
        if m:
            what_removed = m.group(1).strip()
    if not why_safe:
        m = re.search(r"(?:\*\*Why (?:it was )?safe\*\*|##\s*Why (?:It Was )?Safe)[:\s]*(.*?)(?=\n\n|\Z)", response, re.DOTALL | re.IGNORECASE)
        if m:
            why_safe = m.group(1).strip()
    if not expected_impact:
        m = re.search(r"(?:\*\*Expected impact\*\*|##\s*Expected Impact)[:\s]*(.*?)(?=\n\n|\Z)", response, re.DOTALL | re.IGNORECASE)
        if m:
            expected_impact = m.group(1).strip()

    if used_fallback and refactored_code:
        _log("  parsed refactored_code via fallback (code-fence extraction)")

    return {
        "refactored_code": refactored_code,
        "pr_title": pr_title,
        "what_removed": what_removed,
        "why_safe": why_safe,
        "expected_impact": expected_impact,
    }


def _compute_unified_diff(original_content: str, refactored_code: str, file_path: str, start_line: int, end_line: int) -> str:
    import difflib
    orig_lines = original_content.splitlines(keepends=True)
    new_section = refactored_code.rstrip("\n").splitlines(keepends=True)
    new_section = [l if l.endswith("\n") else l + "\n" for l in new_section]
    new_lines = orig_lines[: start_line - 1] + new_section + orig_lines[end_line:]
    diff = list(difflib.unified_diff(
        orig_lines, new_lines,
        fromfile=f"a/{file_path}",
        tofile=f"b/{file_path}",
        lineterm="\n",
    ))
    return "".join(diff) if diff else "(no changes detected)"


@mcp.tool()
def generate_refactor(
    repo_path: str,
    file_path: str,
    start_line: int,
    end_line: int,
    stale_assumption: str,
    desired_simplification: str,
    github_owner: str = "",
    github_repo: str = "",
) -> str:
    """
    Generate a minimal, safe refactored version of selected code that addresses
    identified decision debt. Returns the refactored code, a unified git diff,
    and a PR description explaining what was removed and why it was safe.

    Args:
        repo_path:              Absolute path to the git repository.
        file_path:              Path to the file relative to repo root.
        start_line:             First line of the region to refactor (1-indexed).
        end_line:               Last line of the region to refactor (1-indexed).
        stale_assumption:       The stale decision being addressed.
                                Example: "Synchronous processing assumed <500 tx/day; volume is now 12k/day"
        desired_simplification: What the refactor should achieve.
                                Example: "Replace synchronous Stripe call with async queue dispatch"
        github_owner:           GitHub owner (for PR description).
        github_repo:            GitHub repository name (for PR description).

    Returns:
        Markdown with: refactored code, unified git diff, and PR description.
    """
    _log(f"← IBM Bob → generate_refactor")
    _log(f"  file:  {file_path}  L{start_line}–{end_line}")
    _log(f"  stale: {stale_assumption[:80]}...")

    from pathlib import Path as _Path

    abs_path = _Path(repo_path) / file_path
    try:
        original_content = abs_path.read_text()
    except Exception as e:
        return f"[Chronos] Could not read {abs_path}: {e}"

    all_lines = original_content.splitlines()
    current_code = "\n".join(all_lines[start_line - 1 : end_line])

    msg = _build_refactor_message(
        file_path, start_line, end_line,
        current_code, stale_assumption, desired_simplification,
        github_owner, github_repo,
    )

    refactor_agent_id = os.environ.get("WATSONX_REFACTOR_AGENT_ID", "")
    if not refactor_agent_id:
        _log("  ERROR: WATSONX_REFACTOR_AGENT_ID not set — refactor engineer agent not deployed")
        return (
            "[Chronos] The refactor engineer agent is not deployed yet.\n\n"
            "Run:\n"
            "  cd agents\n"
            "  .venv/bin/orchestrate agents import agent_definitions/refactor_engineer_agent.yaml\n"
            "  .venv/bin/orchestrate agents list   # copy the ID for chronos_refactor_engineer\n\n"
            "Then add to .env:\n"
            "  WATSONX_REFACTOR_AGENT_ID=<id>"
        )

    _log(f"→ routing to watsonx Orchestrate — agent: chronos_refactor_engineer ({refactor_agent_id})")
    
    # Attempt 1: Full structured output with all sections
    raw = _call_orchestrate(
        scenario_key=f"{github_repo}::{file_path}:L{start_line}-{end_line}",
        scenario_data={},
        message=msg,
        agent_id=refactor_agent_id,
    )

    if not raw:
        _log(f"  Orchestrate unavailable — cannot generate refactor without LLM")
        return (
            "[Chronos] Refactor generation requires watsonx Orchestrate to be configured.\n"
            "Start the MCP server with `WATSONX_ORCHESTRATE_URL` and `WATSONX_API_KEY` set in `.env`."
        )

    parsed = _parse_refactor_response(raw)
    refactored_code = parsed["refactored_code"]

    # Attempt 2: Retry with simplified prompt if parsing failed
    if not refactored_code and len(raw) < 500:
        _log(f"  Retry 1: First attempt failed or returned short response, trying simplified prompt")
        simple_msg = _build_simple_refactor_message(current_code, stale_assumption, desired_simplification)
        raw = _call_orchestrate(
            scenario_key=f"{github_repo}::{file_path}:L{start_line}-{end_line}",
            scenario_data={},
            message=simple_msg,
            agent_id=refactor_agent_id,
        )
        if raw:
            parsed = _parse_refactor_response(raw)
            refactored_code = parsed["refactored_code"]
            if refactored_code:
                _log(f"  Retry 1: SUCCESS - got refactored code with simplified prompt")

    if not refactored_code:
        _log(f"  WARNING: could not parse REFACTORED_CODE after retries — returning raw output")
        _log(f"  Consider: 1) Using a larger model, 2) Simplifying the refactor scope, 3) Breaking into smaller changes")
        return raw or "[Chronos] Failed to generate refactored code after multiple attempts."

    diff_str = _compute_unified_diff(original_content, refactored_code, file_path, start_line, end_line)
    _log(f"← refactor generated — diff is {len(diff_str)} chars")

    pr_title = parsed["pr_title"] or f"Remove stale logic: {stale_assumption[:50]}"
    what_removed = parsed["what_removed"] or "_Not provided_"
    why_safe = parsed["why_safe"] or "_Not provided_"
    expected_impact = parsed["expected_impact"] or "_Not provided_"

    return "\n".join([
        f"## Refactored Code",
        "",
        f"```",
        refactored_code,
        "```",
        "",
        "---",
        "",
        "## Git Diff",
        "",
        "```diff",
        diff_str,
        "```",
        "",
        "---",
        "",
        f"## Pull Request: {pr_title}",
        "",
        "**What was removed**",
        what_removed,
        "",
        "**Why it was safe**",
        why_safe,
        "",
        "**Expected impact**",
        expected_impact,
    ])


@mcp.tool()
def analyze_real_file(
    repo_path: str,
    file_path: str,
    start_line: int,
    end_line: int,
    github_owner: str,
    github_repo: str,
    github_token: str = "",
) -> str:
    """
    Analyze engineering decisions, assumptions, and risks for a specific line range
    in a real (non-mocked) git repository. Fetches actual commit history and GitHub
    PR discussions, then synthesizes a Decision analysis.

    Args:
        repo_path:     Absolute path to a locally cloned git repository.
                       Example: "/Users/you/projects/axios"
        file_path:     Path to the file relative to the repo root.
                       Example: "lib/adapters/fetch.js"
        start_line:    First line of the region to analyze (1-indexed).
        end_line:      Last line of the region to analyze (1-indexed).
        github_owner:  GitHub repository owner. Example: "axios"
        github_repo:   GitHub repository name. Example: "axios"
        github_token:  Optional GitHub personal access token (avoids rate limits for
                       public repos; required for private repos).

    Returns:
        Structured markdown analysis: Commits | PR Context | Current Code |
        Decision synthesis prompt ready for watsonx Orchestrate.
    """
    _log(f"← IBM Bob → analyze_real_file")
    _log(f"  repo:  {repo_path}")
    _log(f"  file:  {file_path}  L{start_line}–{end_line}")
    _log(f"  remote: {github_owner}/{github_repo}  token={'set' if (github_token or os.environ.get('GITHUB_TOKEN')) else 'none'}")

    token = github_token or os.environ.get("GITHUB_TOKEN") or None

    try:
        data = fetch_scenario_for_lines(
            repo_path=repo_path,
            file_path=file_path,
            start_line=start_line,
            end_line=end_line,
            github_owner=github_owner,
            github_repo=github_repo,
            github_token=token,
        )
    except Exception as e:
        _log(f"  ERROR fetching data: {e}")
        return f"[Chronos] Failed to fetch data: {e}"

    commits = data.get("commits", [])
    pr_context = data.get("pr_context", {})
    code_snippet = data.get("code_snippet", "")

    _log(f"  fetched {len(commits)} commit(s), {len(pr_context)} PR(s), {len(code_snippet)} chars of code")

    if not commits:
        _log(f"  no commits found — returning early")
        return (
            f"[Chronos] No commits found touching {file_path} L{start_line}-{end_line}.\n"
            "The line range may be recently added or never modified after initial commit."
        )

    # Try routing through watsonx Orchestrate first
    _log(f"→ routing to watsonx Orchestrate for AI synthesis")
    orchestrate_result = _call_orchestrate(
        scenario_key=f"{github_repo}::{file_path}:L{start_line}-{end_line}",
        scenario_data=data,
    )
    if orchestrate_result:
        return orchestrate_result

    _log(f"  Orchestrate unavailable — falling back to direct markdown synthesis")
    # Direct synthesis: format everything as structured markdown for Bob to reason over
    lines = [
        f"# Chronos Analysis: `{file_path}` L{start_line}–{end_line}",
        f"**Repo:** [{github_owner}/{github_repo}]({data.get('github_url', '')})",
        "",
        "---",
        "",
        "## Current Code (Lines Under Analysis)",
        "",
        "```javascript",
        code_snippet,
        "```",
        "",
        "---",
        "",
        f"## Commit History ({len(commits)} commits touching these lines)",
        "",
    ]

    for c in commits:
        lines += [
            f"### `{c['sha']}` — {c['date']} — {c['author']}",
            f"> {c['message']}",
            "",
        ]
        if c.get("diff"):
            lines += ["```diff", c["diff"][:800], "```", ""]

    lines += ["---", "", f"## PR & Issue Context ({len(pr_context)} linked)"]

    for pr_key, pr in pr_context.items():
        lines += [
            "",
            f"### {pr_key.upper().replace('_', ' ')}: {pr.get('title', '')}",
            f"[View on GitHub]({pr.get('url', '')})" if pr.get("url") else "",
            "",
            pr.get("body", "")[:500],
            "",
        ]
        for comment in pr.get("review_comments", [])[:3]:
            lines.append(f"> {comment}")
        for comment in pr.get("issue_comments", [])[:3]:
            lines.append(f"> 💬 {comment}")
        if pr.get("comments"):  # issue format
            for comment in pr["comments"][:3]:
                lines.append(f"> 💬 {comment}")

    lines += [
        "",
        "---",
        "",
        "## Analysis Instructions for IBM Bob / watsonx Orchestrate",
        "",
        "Based on the commit history and PR discussions above, please synthesize:",
        "",
        "1. **Decision** — What engineering decision was made that led to this code?",
        "   What problem was being solved? Cite the commit SHA or PR that shows this.",
        "",
        "2. **Assumptions** — What assumptions had to be true for this decision to be correct?",
        "   Look for: implicit trusts in external APIs, scale thresholds, environment assumptions,",
        "   feature parity assumptions with sibling modules (e.g. HTTP adapter).",
        "   For each: was it documented? Was it explicitly validated? Is it still valid today?",
        "",
        "3. **Tradeoffs** — What was chosen vs. rejected? What does the PR discussion reveal",
        "   about alternatives that were considered?",
        "",
        "4. **Risk Score (0–10)** — How dangerous are the stale assumptions?",
        "   Consider: how long did the bug go undetected, how many users were affected,",
        "   what is the blast radius of this code path.",
        "",
        "5. **Recommendation** — What should engineers do next to prevent similar assumption failures?",
        "",
        "**Evidence rule**: Every assumption and tradeoff must cite a specific commit SHA or PR/issue reference.",
    ]

    return "\n".join(l for l in lines if l is not None)


if __name__ == "__main__":
    host = os.environ.get("MCP_HOST", "localhost")
    port = int(os.environ.get("MCP_PORT", "8001"))

    agent_id = os.environ.get("WATSONX_AGENT_ID", "")
    _log(f"Starting Chronos MCP Server at http://{host}:{port}/sse")
    _log(f"watsonx Orchestrate: {'connected — agent=' + (agent_id or 'default') if USE_ORCHESTRATE else 'not configured (direct analysis fallback)'}")
    _log(f"Available scenarios: {', '.join(SCENARIOS.keys())}")

    mcp.run(transport="sse", host=host, port=port)
