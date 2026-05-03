"""
watsonx Orchestrate ADK tool definitions for the Chronos Decision Analyst agent.

These @tool-decorated functions are imported by the ADK and made available
to the chronos_decision_analyst agent. They call into mock_data.py — the same
data layer used by the MCP server — ensuring consistency between both integration paths.

To register with the ADK:
    orchestrate tools import agents/tools/decision_tools.py
"""

import json
import sys
from pathlib import Path

from ibm_watsonx_orchestrate.agent_builder.tools import tool

# Allow imports from the project root when running via ADK
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from mock_data import SCENARIOS, validate_assumption, compute_risk_score


@tool
def get_commit_history(scenario_key: str) -> str:
    """
    Retrieve the git commit timeline for a function, ordered oldest to newest.
    Each commit includes the SHA, author, date, commit message, and diff summary.

    Args:
        scenario_key: Identifies the function. Format: "repo::FunctionName".
                      Example: "requests::HTTPAdapter.send"

    Returns:
        JSON-formatted list of commits with sha, author, date, message, and diff fields.
    """
    data = SCENARIOS.get(scenario_key, {})
    commits = data.get("commits", [])
    if not commits:
        return json.dumps({"error": f"No commit data found for '{scenario_key}'"})
    return json.dumps(commits, indent=2)


@tool
def get_pr_context(scenario_key: str) -> str:
    """
    Retrieve PR discussions, review comments, and decision context for a function.
    This is where the 'why' behind decisions is documented by the engineering team.

    Args:
        scenario_key: Identifies the function. Format: "repo::FunctionName".
                      Example: "requests::HTTPAdapter.send"

    Returns:
        JSON-formatted dict of PRs, each with title, body, and review_comments list.
    """
    data = SCENARIOS.get(scenario_key, {})
    pr_context = data.get("pr_context", {})
    if not pr_context:
        return json.dumps({"error": f"No PR context found for '{scenario_key}'"})
    return json.dumps(pr_context, indent=2)


@tool
def detect_code_assumptions(scenario_key: str) -> str:
    """
    Detect assumptions embedded in the code: hardcoded thresholds, dependency version pins,
    scale-dependent logic, temporal conditions, and TODO markers flagging future re-evaluation.

    Args:
        scenario_key: Identifies the function. Format: "repo::FunctionName".

    Returns:
        JSON list of assumption objects, each with: text, introduced date, introduced_by commit SHA,
        and source (the PR comment or commit message where the assumption was documented).
    """
    data = SCENARIOS.get(scenario_key, {})
    assumptions = data.get("assumptions", [])
    if not assumptions:
        return json.dumps({"error": f"No assumptions found for '{scenario_key}'"})
    return json.dumps(assumptions, indent=2)


@tool
def validate_assumptions(scenario_key: str) -> str:
    """
    Classify each assumption as VALID, STALE, or UNKNOWN based on current ecosystem state,
    known breaking changes, and how long ago the assumption was introduced.

    Args:
        scenario_key: Identifies the function. Format: "repo::FunctionName".

    Returns:
        JSON list with validity field (VALID/STALE/UNKNOWN) and reason for each assumption.
    """
    data = SCENARIOS.get(scenario_key, {})
    assumptions = data.get("assumptions", [])
    results = []
    for a in assumptions:
        validation = validate_assumption(a["text"], a["introduced"])
        results.append({
            "assumption": a["text"],
            "introduced": a["introduced"],
            "source": a.get("source", ""),
            "validity": validation["validity"],
            "reason": validation["reason"],
        })
    return json.dumps(results, indent=2)


@tool
def compute_risk_score_for_scenario(scenario_key: str) -> str:
    """
    Compute a 0–10 risk score for the decisions in a function, based on:
    - Number of stale/unknown assumptions
    - How long ago the original decision was made
    - Blast radius (how many other files depend on this one)

    Args:
        scenario_key: Identifies the function. Format: "repo::FunctionName".

    Returns:
        JSON with score (float), tier (CRITICAL/HIGH/MEDIUM/LOW), and a detailed breakdown.
    """
    data = SCENARIOS.get(scenario_key, {})
    assumptions = data.get("assumptions", [])
    commits = data.get("commits", [])
    if not assumptions:
        return json.dumps({"error": f"No data found for '{scenario_key}'"})

    oldest_date = min(c["date"] for c in commits) if commits else "2020-01-01"
    dependent_count = 47 if "requests" in scenario_key else 12
    result = compute_risk_score(assumptions, oldest_date, dependent_count)
    result["scenario"] = scenario_key
    return json.dumps(result, indent=2)


@tool
def scan_repo_for_debt(repo_name: str) -> str:
    """
    Scan all known components in a repository and return a ranked list of decision debt
    hotspots, ordered by risk score. Use this for the Future Prediction mode demo.

    Args:
        repo_name: Repository name prefix to filter scenarios. E.g. "requests", "payments".

    Returns:
        JSON list of risk items sorted by score descending, each with function name,
        risk score, tier, stale assumption count, and decision age in days.
    """
    matching = {k: v for k, v in SCENARIOS.items() if repo_name.lower() in k.lower()}
    if not matching:
        available = sorted({k.split("::")[0] for k in SCENARIOS})
        return json.dumps({"error": f"No scenarios for '{repo_name}'", "available": available})

    items = []
    for key, data in matching.items():
        _, func = key.split("::", 1) if "::" in key else (key, key)
        assumptions = data.get("assumptions", [])
        commits = data.get("commits", [])
        oldest_date = min(c["date"] for c in commits) if commits else "2020-01-01"
        dependent_count = 47 if "requests" in key else 12
        risk = compute_risk_score(assumptions, oldest_date, dependent_count)
        items.append({
            "function": func,
            "scenario_key": key,
            "risk_score": risk["score"],
            "tier": risk["tier"],
            "breakdown": risk["breakdown"],
        })

    items.sort(key=lambda x: x["risk_score"], reverse=True)
    return json.dumps(items, indent=2)
