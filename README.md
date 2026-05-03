# Chronos (Time-Machine-AI) — Developer Intelligence Platform

Demo Link: https://youtu.be/ppD84UtPnzE

Reconstructs historical engineering decisions from codebases and predicts future failures caused by outdated assumptions.

> *Like a staff engineer who remembers every decision ever made — and knows which ones are now ticking time bombs.*

---

## How It Works

```
IBM Bob (VS Code) ──MCP──▶ mcp_server.py ──▶ watsonx Orchestrate ──▶ Decision Analysis
```

1. **IBM Bob** is the developer interface — ask it about any function in natural language
2. **MCP server** (`mcp_server.py`) routes the request to watsonx Orchestrate and formats results
3. **watsonx Orchestrate** runs the Decision Analyst agent, which calls tools to gather commit history, PR context, and assumption data, then synthesizes a structured analysis

---

## Quick Start

### 1. Install dependencies

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your watsonx Orchestrate URL and token
```

### 3. Start the MCP server

```bash
python mcp_server.py
# Listening at http://localhost:8001/mcp
```

### 4. Connect IBM Bob

The `.bob/mcp.json` file in this repo automatically connects IBM Bob to the MCP server when you open this directory in VS Code with the Bob extension installed.

Open any file and ask Bob:

> *"Why was HTTPAdapter built this way? What assumptions are at risk?"*

Bob will call `analyze_decisions("requests::HTTPAdapter.send")` and return a full Decision analysis.

---

## Register with watsonx Orchestrate (Optional)

If you have watsonx Orchestrate running locally or on IBM Cloud:

```bash
cd agents
pip install ibm-watsonx-orchestrate-adk

# Import the tools
orchestrate tools import tools/decision_tools.py

# Deploy the agent
orchestrate agents deploy agent_definitions/decision_analyst_agent.yaml

# Register the MCP server as a toolkit (so agents can also call MCP tools)
orchestrate toolkits add \
  --kind mcp \
  --name chronos-mcp \
  --url http://localhost:8001/mcp \
  --transport streamable_http
```

Set `WATSONX_ORCHESTRATE_URL` and `WATSONX_ORCHESTRATE_TOKEN` in `.env` and the MCP server will automatically route through Orchestrate.

---

## Demo Scenarios

| Scenario Key | Description |
|---|---|
| `requests::HTTPAdapter.send` | urllib3 dependency decision — API stability assumption invalidated by v2 |
| `payments::process_payment` | Synchronous payment processing — scale assumption never re-evaluated |

---

## Two Modes

### Time Machine Mode
Ask Bob: *"Why was [function] built this way? What assumptions are at risk?"*

Returns: Decision → Assumptions (VALID/STALE/UNKNOWN) → Tradeoffs → Evidence → Risk Score

### Future Prediction Mode
Ask Bob: *"Scan the requests library for decision debt"*

Returns: Risk heatmap of all components ranked by staleness score

---

## Post-Hackathon TODOs

- [ ] Replace `mock_data.py` with real GitPython + GitHub API integration
- [ ] Web frontend (Next.js + DecisionTimeline UI)
- [ ] All 7 agents pipeline (Git Historian, Context Gatherer, etc.)
- [ ] Real Jira + Slack integrations
- [ ] VS Code extension with inline gutter risk scores
- [ ] Team dashboard + decision debt trend over time
- [ ] "Watch mode" — alert when a stale decision is touched in a new PR
