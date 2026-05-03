#!/usr/bin/env bash
# Creates a self-contained demo git repo with scripted commit history.
# Produces: /tmp/demo-auth-service/
#
# Scenario: An auth service token validator built for single-instance deployment.
# When the service was later scaled horizontally, the in-memory token cache created
# silent cache inconsistency bugs — tokens valid on one instance revoked on another.
# The lines of interest are the in-memory cache implementation (added in commit 1,
# never updated through commit 4 when scaling happened).
#
# Usage: bash scripts/create_demo_repo.sh

set -e

REPO_DIR="$HOME/Projects/personal-projects/demo-auth-service"

echo "Creating demo repo at $REPO_DIR ..."
rm -rf "$REPO_DIR"
mkdir -p "$REPO_DIR/src/auth"
cd "$REPO_DIR"

git init -q
git config user.email "pratham1695@gmail.com"
git config user.name "prathameshmahankal"

# ── Commit 1: Initial auth service with in-memory token cache ─────────────────
cat > src/auth/token_validator.py << 'PYEOF'
"""
Token validation for the auth service.

Designed for single-instance deployment behind a load balancer with sticky sessions.
Token cache is intentionally in-memory — Redis overhead not justified at this scale.
"""

import time
import hashlib
from typing import Optional

# In-memory token store. Fast, zero-dependency, sufficient for single-instance.
# Key: token_hash, Value: (user_id, expires_at)
_token_cache: dict[str, tuple[str, float]] = {}

TOKEN_TTL_SECONDS = 3600  # 1 hour — matches session cookie lifetime


def issue_token(user_id: str) -> str:
    """Issue a new auth token for a user and store it in the local cache."""
    raw = f"{user_id}:{time.time()}:secret_key_v1"
    token = hashlib.sha256(raw.encode()).hexdigest()
    _token_cache[token] = (user_id, time.time() + TOKEN_TTL_SECONDS)
    return token


def validate_token(token: str) -> Optional[str]:
    """
    Validate a token and return the associated user_id, or None if invalid/expired.

    Assumes: token was issued by THIS instance (single-instance deployment).
    Cache miss = invalid token — no fallback lookup.
    """
    entry = _token_cache.get(token)
    if entry is None:
        return None  # token not in this instance's cache

    user_id, expires_at = entry
    if time.time() > expires_at:
        del _token_cache[token]
        return None

    return user_id


def revoke_token(token: str) -> bool:
    """Revoke a token. Only affects THIS instance's cache."""
    if token in _token_cache:
        del _token_cache[token]
        return True
    return False


def active_token_count() -> int:
    """Return number of active tokens — used for capacity planning."""
    now = time.time()
    return sum(1 for _, expires_at in _token_cache.values() if expires_at > now)
PYEOF

cat > src/auth/__init__.py << 'PYEOF'
from .token_validator import issue_token, validate_token, revoke_token
PYEOF

cat > README.md << 'MDEOF'
# auth-service

Lightweight authentication service. Single-instance deployment behind sticky-session load balancer.
MDEOF

git add .
git commit -q -m "feat: add JWT-style token validator with in-memory cache (#12)

Implements token issuance, validation, and revocation. In-memory dict chosen
over Redis: single-instance deployment with sticky sessions means all requests
for a user hit the same instance. Redis overhead not justified at ~200 RPS."

COMMIT_1=$(git rev-parse HEAD)
echo "Commit 1 (origin): $COMMIT_1"

# ── Commit 2: Extend TTL for enterprise sessions ──────────────────────────────
cat > src/auth/token_validator.py << 'PYEOF'
"""
Token validation for the auth service.

Designed for single-instance deployment behind a load balancer with sticky sessions.
Token cache is intentionally in-memory — Redis overhead not justified at this scale.
"""

import time
import hashlib
from typing import Optional

# In-memory token store. Fast, zero-dependency, sufficient for single-instance.
# Key: token_hash, Value: (user_id, expires_at, tier)
_token_cache: dict[str, tuple[str, float, str]] = {}

TOKEN_TTL_SECONDS = 3600          # 1 hour — standard users
TOKEN_TTL_ENTERPRISE = 86400      # 24 hours — enterprise accounts (PROD-441)


def issue_token(user_id: str, tier: str = "standard") -> str:
    """Issue a new auth token. Enterprise accounts get 24-hour TTL."""
    ttl = TOKEN_TTL_ENTERPRISE if tier == "enterprise" else TOKEN_TTL_SECONDS
    raw = f"{user_id}:{time.time()}:secret_key_v1"
    token = hashlib.sha256(raw.encode()).hexdigest()
    _token_cache[token] = (user_id, time.time() + ttl, tier)
    return token


def validate_token(token: str) -> Optional[str]:
    """
    Validate a token and return the associated user_id, or None if invalid/expired.

    Assumes: token was issued by THIS instance (single-instance deployment).
    Cache miss = invalid token — no fallback lookup.
    """
    entry = _token_cache.get(token)
    if entry is None:
        return None  # token not in this instance's cache

    user_id, expires_at, _tier = entry
    if time.time() > expires_at:
        del _token_cache[token]
        return None

    return user_id


def revoke_token(token: str) -> bool:
    """Revoke a token. Only affects THIS instance's cache."""
    if token in _token_cache:
        del _token_cache[token]
        return True
    return False


def active_token_count() -> int:
    """Return number of active tokens — used for capacity planning."""
    now = time.time()
    return sum(1 for _, expires_at, _ in _token_cache.values() if expires_at > now)
PYEOF

git add .
git commit -q -m "feat: extend token TTL to 24h for enterprise tier (#31)

PROD-441: Enterprise customers complained sessions expire mid-workday.
Extended TTL for enterprise tier only. Standard accounts unchanged.
Single-instance assumption still holds — sticky sessions route all
enterprise user traffic to the same pod."

COMMIT_2=$(git rev-parse HEAD)
echo "Commit 2 (enterprise TTL): $COMMIT_2"

# ── Commit 3: Add token introspection endpoint ────────────────────────────────
cat >> src/auth/token_validator.py << 'PYEOF'


def introspect_token(token: str) -> dict:
    """
    Return metadata about a token without consuming it (OAuth 2.0 RFC 7662 style).
    Used by downstream services to check token validity without re-authenticating.
    """
    entry = _token_cache.get(token)
    if entry is None:
        return {"active": False}
    user_id, expires_at, tier = entry
    if time.time() > expires_at:
        del _token_cache[token]
        return {"active": False}
    return {
        "active": True,
        "user_id": user_id,
        "tier": tier,
        "expires_in": int(expires_at - time.time()),
    }
PYEOF

git add .
git commit -q -m "feat: add token introspection endpoint for service-to-service auth (#47)

Downstream microservices (billing, reporting) need to validate tokens
without re-authenticating. Added introspect_token() following RFC 7662.
Reads from same in-memory cache — no added latency."

COMMIT_3=$(git rev-parse HEAD)
echo "Commit 3 (introspection): $COMMIT_3"

# ── Commit 4: Scale auth service to 3 replicas (infra change) ────────────────
# This commit doesn't touch token_validator.py at all — that's the whole point.
# The service was scaled but nobody updated the token cache to be shared.

mkdir -p deployment
cat > deployment/auth-service.yaml << 'YAMLEOF'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: auth-service
spec:
  replicas: 3          # scaled from 1 → 3 to handle Black Friday traffic
  selector:
    matchLabels:
      app: auth-service
  template:
    spec:
      containers:
      - name: auth-service
        image: auth-service:2.1.0
        resources:
          requests:
            memory: "128Mi"
            cpu: "100m"
---
apiVersion: v1
kind: Service
metadata:
  name: auth-service
spec:
  # NOTE: sessionAffinity removed — ops team needs round-robin for even load spread
  sessionAffinity: None
  ports:
  - port: 8080
YAMLEOF

git add .
git commit -q -m "ops: scale auth-service to 3 replicas for Black Friday (#89)

PROD-892: Auth service hitting CPU limits under load. Scaled to 3 replicas.
NOTE: removed sessionAffinity (sticky sessions) to get even load distribution.
Token validation should still work — tokens are stored server-side."

COMMIT_4=$(git rev-parse HEAD)
echo "Commit 4 (scale — BREAKS assumption): $COMMIT_4"

# ── Commit 5: Bug reports start — intermittent 401s ──────────────────────────
cat >> src/auth/token_validator.py << 'PYEOF'


# TODO PROD-901: Intermittent 401s reported after scale-out. Investigating.
# Hypothesis: load balancer routing requests to instance that didn't issue the token.
# Workaround: restart pods to clear caches and let users re-authenticate.
# Long-term fix: migrate to shared cache (Redis). Tracked in PROD-901.
PYEOF

git add .
git commit -q -m "fix: add TODO for PROD-901 intermittent 401s after scale-out (#94)

Users reporting random 401s after Black Friday scale-out. Root cause:
in-memory cache is per-instance; token issued on pod-A is unknown to pod-B.
Workaround: force re-login. Proper fix: Redis shared cache (PROD-901).
This is the assumption that was made in PR #12 that is now invalidated."

COMMIT_5=$(git rev-parse HEAD)
echo "Commit 5 (bug acknowledgement): $COMMIT_5"

# ── Write pr_fixtures.json ────────────────────────────────────────────────────
cat > pr_fixtures.json << JSONEOF
{
  "$COMMIT_1": {
    "key": "pr_12",
    "title": "feat: add JWT-style token validator with in-memory cache",
    "body": "Implements token issuance, validation, and revocation for the auth service.\n\nIn-memory dict chosen over Redis for simplicity: this service runs as a **single instance** behind a sticky-session load balancer. All requests for a given user always hit the same pod, so local cache is equivalent to a shared cache for our deployment model.\n\nRedis adds operational complexity (another infra dependency, cache invalidation, serialization) with no benefit in a single-instance setup. We can revisit if we ever need to scale horizontally, but that's not on the roadmap.",
    "url": "https://github.com/example/auth-service/pull/12",
    "review_comments": [
      "What happens if we ever need to scale this service? The in-memory cache won't work across instances. — @ops_lead",
      "That's a valid concern for future-us. Right now we have sticky sessions and no horizontal scaling requirement. I'd rather ship something simple and refactor when needed. — @alice_dev",
      "LGTM. Flag this assumption explicitly in a comment so future devs know. — @arch_review",
      "Added a comment in the docstring. — @alice_dev"
    ],
    "issue_comments": []
  },
  "$COMMIT_2": {
    "key": "pr_31",
    "title": "feat: extend token TTL to 24h for enterprise tier",
    "body": "PROD-441: Enterprise customers reported sessions expiring during the workday. Extended TTL to 24 hours for enterprise accounts.\n\nNo architecture changes — still single-instance with sticky sessions. TTL extension doesn't affect the caching model.",
    "url": "https://github.com/example/auth-service/pull/31",
    "review_comments": [
      "Does the longer TTL create any security concerns? — @security_team",
      "Enterprise tier has MFA enabled so the risk is acceptable per security policy. — @alice_dev"
    ],
    "issue_comments": []
  },
  "$COMMIT_4": {
    "key": "pr_89",
    "title": "ops: scale auth-service to 3 replicas for Black Friday",
    "body": "PROD-892: Auth service hitting CPU limits under pre-Black Friday load testing. Scaling to 3 replicas.\n\nRemoving sessionAffinity (sticky sessions) to get even load distribution across pods — ops team confirmed this is safe.\n\n**Note**: Token validation stores tokens server-side so routing doesn't matter for correctness.",
    "url": "https://github.com/example/auth-service/pull/89",
    "review_comments": [
      "Are we sure removing sticky sessions is safe? The token cache is in-memory per pod. — @alice_dev",
      "Token data is server-side, routing is transparent to the client. Should be fine. — @ops_lead",
      "Merged under time pressure — revisit after Black Friday if issues arise. — @ops_lead"
    ],
    "issue_comments": [
      "PROD-901: Users reporting 401s after this change. Looks like the load balancer is routing to a pod that didn't issue the token. — @alice_dev",
      "This is exactly what I flagged in PR #12 and PR #89. The in-memory cache doesn't work across instances. We need Redis. — @ops_lead"
    ]
  },
  "$COMMIT_5": {
    "key": "pr_94",
    "title": "fix: document PROD-901 intermittent 401s after scale-out",
    "body": "Adding a TODO to track the root cause of PROD-901.\n\nRoot cause confirmed: in-memory token cache is per-instance. When the load balancer routes a validation request to a pod that didn't issue the token, the cache miss returns 401.\n\nThe original assumption in PR #12 — 'single-instance with sticky sessions' — is now invalid. Fix: migrate to Redis shared cache (tracked in PROD-901).",
    "url": "https://github.com/example/auth-service/pull/94",
    "review_comments": [
      "This was warned about in PR #12 review. The assumption of single-instance was invalidated when we removed sticky sessions in PR #89. — @alice_dev",
      "Adding to the post-mortem. We need to migrate to Redis before the next scaling event. — @arch_review"
    ],
    "issue_comments": []
  }
}
JSONEOF

# ── Create .bob configuration for Chronos skill ──────────────────────────────
mkdir -p .bob/skills/chronos

cat > .bob/mcp.json << 'BOBEOF'
{
  "mcpServers": {
    "chronos": {
      "url": "http://localhost:8001/sse",
      "alwaysAllow": [
        "get_repo_info",
        "analyze_real_file",
        "generate_refactor",
        "analyze_decisions",
        "scan_for_decision_debt",
        "get_commit_history",
        "get_pr_context",
        "detect_code_assumptions",
        "validate_assumptions",
        "compute_risk_score_tool"
      ],
      "disabled": false
    }
  }
}
BOBEOF

cat > .bob/skills/chronos/SKILL.md << 'SKILLEOF'
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
- NEVER read `pr_fixtures.json`, `.git/` internals, or any local files directly. All data must come through the MCP tools above — that is the entire point of this integration.
- Never fabricate commits, PR quotes, or assumptions. Only present what the tools return.
- If `analyze_real_file` returns fewer than 2 commits, say: "Only N commit(s) touched
  these exact lines. The decision may have been made in a parent scope — try selecting
  a wider range or the enclosing function."
- If the MCP server is not reachable, tell the user:
  `cd /Users/p.mahankal/Projects/personal-projects/codebase-time-machine && .venv/bin/python mcp_server.py`
SKILLEOF

echo ""
echo "✅ Demo repo created at $REPO_DIR"
echo ""
echo "Commit SHAs (for reference):"
echo "  Origin (token cache design): $COMMIT_1"
echo "  Enterprise TTL:              $COMMIT_2"
echo "  Introspection endpoint:      $COMMIT_3"
echo "  Scale-out (breaks assumption):$COMMIT_4"
echo "  Bug acknowledgement:         $COMMIT_5"
echo ""
echo "Interesting line range for Chronos analysis:"
echo "  File: src/auth/token_validator.py"
echo "  Lines: the _token_cache dict and validate_token function"
grep -n "_token_cache\|def validate_token\|def issue_token" "$REPO_DIR/src/auth/token_validator.py" | head -8
echo ""
echo "To analyze with Chronos (IBM Bob or MCP tool):"
echo "  analyze_real_file("
echo "    repo_path=\"$REPO_DIR\","
echo "    file_path=\"src/auth/token_validator.py\","
echo "    start_line=<see above>,"
echo "    end_line=<see above>,"
echo "    github_owner=\"\",  # ignored — uses pr_fixtures.json"
echo "    github_repo=\"\""
echo "  )"
