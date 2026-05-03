---
name: chronos
description: Reconstruct decision history and surface stale assumptions for selected lines of code. Use when asked what happened here, why was this built this way, build a timeline, or what assumptions are at risk.
---

## When the user selects lines and asks what happened / why / build a timeline:

This is the primary flow. The user has highlighted code in the editor.

**Step 1** — Call `get_repo_info` with the absolute path of the currently open file.
This returns the repo root, relative file path, and GitHub owner/repo automatically.

```
get_repo_info("/absolute/path/to/the/open/file.py")
```

**Step 2** — Call `analyze_real_file` with the values from step 1 plus the selected
line range. Use the line numbers the user selected (or mentioned), or the line range
of the function/block they're asking about.

```
analyze_real_file(
  repo_path=   <repo_path from step 1>,
  file_path=   <file_path from step 1>,
  start_line=  <first selected line>,
  end_line=    <last selected line>,
  github_owner=<github_owner from step 1>,
  github_repo= <github_repo from step 1>
)
```

**Step 3** — Present the result as-is. It is already structured markdown.
Do NOT paraphrase, summarize, or cut the evidence trail — the commit SHAs and
PR quotes are the whole point.

After presenting, ask: "Want me to validate which assumptions are still valid today?"

---

## When the user asks to refactor, simplify, remove stale logic, or generate a PR:

This flow follows a decision analysis. The user has already seen what's stale and is now asking to act on it.

Call `generate_refactor` with values from the prior `analyze_real_file` call (check conversation context):

```
generate_refactor(
  repo_path=              <repo_path from get_repo_info>,
  file_path=              <file_path from get_repo_info>,
  start_line=             <start line from last analyze_real_file call>,
  end_line=               <end line from last analyze_real_file call>,
  stale_assumption=       <the stale decision or assumption — summarize from the analysis or the user's message>,
  desired_simplification= <what the user wants to achieve — from their message>,
  github_owner=           <github_owner from get_repo_info>,
  github_repo=            <github_repo from get_repo_info>
)
```

Present the result as-is — it already contains the refactored code, git diff, and PR description.
Do NOT summarize or shorten the diff or PR description.

---

## When the user asks about a mock/demo scenario by name:

Call `analyze_decisions` with the scenario key:
- `"requests::HTTPAdapter.send"` — urllib3 transport decision
- `"payments::process_payment"` — synchronous payment processing

---

## When the user asks to scan a whole repo for decision debt:

Call `scan_for_decision_debt` with the repo name (e.g. `"requests"`, `"payments"`).

---

## Rules — never break these:

- Always call `get_repo_info` first to auto-detect the repo. Never ask the user for a repo path.
- Never fabricate commits, PR quotes, or assumptions. Only present what the tools return.
- If `analyze_real_file` returns fewer than 2 commits, say: "Only N commit(s) touched
  these exact lines. The decision may have been made in a parent scope — try selecting
  a wider range or the enclosing function."
- If the MCP server is not reachable, tell the user:
  `cd /Users/p.mahankal/Projects/personal-projects/codebase-time-machine && .venv/bin/python mcp_server.py`
