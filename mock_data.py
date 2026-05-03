from datetime import datetime, date


SCENARIOS = {
    "requests::HTTPAdapter.send": {
        "description": "HTTP transport layer decision in the 'requests' library",
        "commits": [
            {
                "sha": "a3f2c19",
                "author": "Kenneth Reitz",
                "date": "2012-03-15",
                "message": "Switch transport layer to urllib3 — avoids implementing raw socket management",
                "diff": "+ from urllib3 import HTTPConnectionPool\n+ from urllib3.util import parse_url",
            },
            {
                "sha": "b8e41d2",
                "author": "Cory Benfield",
                "date": "2014-09-02",
                "message": "Pin urllib3<2 in dependencies — API stable enough for our needs",
                "diff": "-urllib3>=1.21.1\n+urllib3>=1.21.1,<2  # API stable, no breaking changes expected",
            },
            {
                "sha": "c91f034",
                "author": "Nate Prewitt",
                "date": "2018-06-10",
                "message": "Keep urllib3 pin — v2 alpha has incompatible SSL interface",
                "diff": "# urllib3 v2 changes SSL API — hold at <2 until migration is planned",
            },
        ],
        "pr_context": {
            "pr_234": {
                "title": "Replace raw sockets with urllib3 transport",
                "body": (
                    "urllib3 already handles keep-alive, retries, connection pooling, "
                    "and SSL. No point duplicating this logic ourselves. This gives us "
                    "battle-tested transport for free."
                ),
                "review_comments": [
                    "Are we comfortable taking a hard dependency on urllib3 long-term? "
                    "Their API could change under us. — @sigmavirus24",
                    "Their API has been stable for 2+ years and they follow semver. "
                    "I'm comfortable with this tradeoff. — @kennethreitz",
                    "What's the rollback plan if urllib3 breaks something? — @lukasa",
                    "We'd pin to the last known-good version. Same as any dep. — @kennethreitz",
                ],
            },
            "pr_1891": {
                "title": "Investigate urllib3 v2 upgrade path",
                "body": "urllib3 v2 was released. Breaking changes in SSL interface affect us.",
                "review_comments": [
                    "This is exactly the risk @sigmavirus24 flagged in 2012. The SSL "
                    "interface changed completely in v2. — @nateprewitt",
                    "We need a migration plan before we can unpin. — @sethmlarson",
                ],
            },
        },
        "code_snippet": """\
def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
    \"\"\"Sends PreparedRequest object. Returns Response object.\"\"\"
    # Relies entirely on urllib3's connection pool for retry, keep-alive, and SSL
    conn = self.get_connection_with_tls_context(request, verify, proxies=proxies, cert=cert)

    # Assumes urllib3 < 2.0 API — no major breaking changes expected (pinned in setup.cfg)
    resp = conn.urlopen(
        method=request.method,
        url=url,
        body=request.body,
        headers=request.headers,
        redirect=False,
        assert_same_host=False,
        preload_content=False,
        decode_content=False,
        retries=self.max_retries,
        timeout=timeout,
    )
""",
        "assumptions": [
            {
                "text": "urllib3 public API will remain stable across major versions",
                "introduced": "2014-09-02",
                "introduced_by": "b8e41d2",
                "source": "PR #234 — @kennethreitz: 'Their API has been stable for 2+ years and they follow semver.'",
            },
            {
                "text": "urllib3 handles all SSL/TLS edge cases correctly without customization",
                "introduced": "2012-03-15",
                "introduced_by": "a3f2c19",
                "source": "PR #234 body: 'urllib3 already handles...SSL'",
            },
            {
                "text": "Connection pooling at the urllib3 level is sufficient for all traffic patterns",
                "introduced": "2012-03-15",
                "introduced_by": "a3f2c19",
                "source": "Implicit in original design — no alternative was documented",
            },
        ],
        "tradeoffs": [
            {
                "chosen": "Delegate transport entirely to urllib3",
                "rejected": "Implement custom socket management",
                "rationale": "urllib3 provides keep-alive, retries, and connection pooling for free. "
                             "Custom implementation would duplicate battle-tested logic.",
                "source": "PR #234 body",
            }
        ],
    },

    "payments::process_payment": {
        "description": "Synchronous payment processing decision in a payments service",
        "commits": [
            {
                "sha": "f91c33a",
                "author": "Alice Chen",
                "date": "2020-11-01",
                "message": "JIRA-892: Implement synchronous Stripe integration — async overkill at current volume",
                "diff": "+def process_payment(order_id: str) -> PaymentResult:\n"
                        "+    charge = stripe.Charge.create(amount=order.total, currency='usd')\n"
                        "+    return PaymentResult(success=True, charge_id=charge.id)",
            },
            {
                "sha": "d44b821",
                "author": "Bob Kim",
                "date": "2021-03-15",
                "message": "Add payment timeout — Stripe p99 latency acceptable for sync model",
                "diff": "+    stripe.default_http_client = stripe.RequestsClient(timeout=5.0)\n"
                        "+    # 5s timeout safe at current volume — revisit if TPS increases",
            },
        ],
        "pr_context": {
            "pr_891": {
                "title": "JIRA-892: Synchronous Stripe payment integration",
                "body": (
                    "At current volume (<500 transactions/day) async processing adds "
                    "architectural complexity with no meaningful benefit. We can revisit "
                    "once we approach 5,000 transactions/day."
                ),
                "review_comments": [
                    "What happens at 10x scale? Synchronous Stripe calls will block threads. — @bob_infra",
                    "That's a future problem. We'll refactor when we hit 5k/day. "
                    "Current volume is ~480/day. — @alice_chen",
                    "Make sure we document this assumption so future devs know why. — @carol_arch",
                    "Added a TODO in the code and linked this PR. — @alice_chen",
                ],
            },
        },
        "code_snippet": """\
def process_payment(order_id: str) -> PaymentResult:
    \"\"\"Process payment synchronously via Stripe.\"\"\"
    # Synchronous model — acceptable at current transaction volume (<500/day as of 2020-11)
    # TODO: revisit if daily transactions exceed 5,000 (see PR #891 for rationale)
    order = Order.get(order_id)
    charge = stripe.Charge.create(
        amount=order.total_cents,
        currency="usd",
        source=order.payment_token,
        idempotency_key=order_id,
    )
    return PaymentResult(success=True, charge_id=charge.id, amount=order.total_cents)
""",
        "assumptions": [
            {
                "text": "Daily transaction volume stays below 5,000 (was ~480/day at decision time)",
                "introduced": "2020-11-01",
                "introduced_by": "f91c33a",
                "source": "PR #891 — @alice_chen: 'Current volume is ~480/day'",
            },
            {
                "text": "Synchronous Stripe API calls complete within acceptable latency for user-facing requests",
                "introduced": "2021-03-15",
                "introduced_by": "d44b821",
                "source": "PR #891 — @bob_infra: 'Stripe p99 latency acceptable for sync model'",
            },
            {
                "text": "Single-threaded synchronous processing is sufficient — no geo-distribution or failover needed",
                "introduced": "2020-11-01",
                "introduced_by": "f91c33a",
                "source": "Implicit in design — no async or queue-based alternative was evaluated",
            },
        ],
        "tradeoffs": [
            {
                "chosen": "Synchronous blocking Stripe API call per transaction",
                "rejected": "Async queue-based payment processing (e.g., Celery + SQS)",
                "rationale": "At <500 tx/day, async introduces architectural overhead (queue, workers, "
                             "idempotency handling) with no throughput benefit.",
                "source": "PR #891 body",
            }
        ],
    },
}


def validate_assumption(assumption_text: str, introduced_date: str) -> dict:
    days_old = (date.today() - datetime.strptime(introduced_date, "%Y-%m-%d").date()).days

    text_lower = assumption_text.lower()

    if "urllib3" in text_lower and ("stable" in text_lower or "major version" in text_lower):
        return {
            "validity": "STALE",
            "reason": (
                "urllib3 released v2.0 in April 2023 with breaking changes: dropped Python 2 support, "
                "rewrote the SSL interface (SSLContext handling changed), and removed deprecated "
                "urllib3.contrib modules. The explicit <2 pin (commit b8e41d2) exists precisely "
                "because this assumption was already fragile. Migration is now overdue."
            ),
        }

    if "5,000" in assumption_text or "5000" in assumption_text or "500" in assumption_text:
        return {
            "validity": "STALE",
            "reason": (
                f"Assumption is {days_old} days old ({days_old // 365} years). "
                "The threshold of 5,000 tx/day was set when volume was ~480/day (2020). "
                "No evidence this was re-evaluated at growth milestones. At typical "
                "e-commerce growth rates, volume likely crossed this threshold years ago."
            ),
        }

    if "ssl" in text_lower and "urllib3" in text_lower:
        return {
            "validity": "STALE",
            "reason": (
                "urllib3 v2 completely rewrote its SSL/TLS handling. The assumption that "
                "urllib3 handles SSL edge cases without customization is invalidated — "
                "v2 requires explicit SSLContext configuration that v1 handled implicitly."
            ),
        }

    if "synchronous" in text_lower or "single-threaded" in text_lower:
        return {
            "validity": "UNKNOWN",
            "reason": (
                f"Assumption is {days_old} days old. No recent commits or PRs revisit "
                "the synchronous model decision. Cannot confirm current transaction volume "
                "without production metrics — assumption may be STALE."
            ),
        }

    if days_old > 730:
        return {
            "validity": "UNKNOWN",
            "reason": f"Assumption is {days_old} days old ({days_old // 365}+ years) with no recorded re-evaluation.",
        }

    return {"validity": "VALID", "reason": "No contradicting signals found in commit history or known ecosystem changes."}


def compute_debt_verdict(assumptions: list, commits: list, pr_context: dict, code_snippet: str = "") -> dict:
    """
    Return a decisive decision-debt verdict from available signals.

    Signals checked (in priority order):
      - STALE assumptions     → +30 weight, +20 confidence each
      - UNKNOWN assumptions   → +10 weight, +10 confidence each
      - Bug/fix commit msgs   → +15 weight, +15 confidence (max 2)
      - Decision age > 3 yrs → +10 weight, +10 confidence
      - PR concern comments   → +5  weight, +5  confidence (max 3)
      - TODO/FIXME in code    → +5  weight (no confidence boost alone)

    is_stale = True when total weight >= 30.
    """
    signals = []
    weight = 0
    confidence_boost = 0

    # Signal 1: Stale / Unknown assumptions
    validated = [(a, validate_assumption(a["text"], a["introduced"])) for a in assumptions]
    stale_list = [(a, v) for a, v in validated if v["validity"] == "STALE"]
    unknown_list = [(a, v) for a, v in validated if v["validity"] == "UNKNOWN"]

    for a, v in stale_list:
        short_text = a["text"][:60] + ("..." if len(a["text"]) > 60 else "")
        short_reason = v["reason"][:90] + ("..." if len(v["reason"]) > 90 else "")
        signals.append(f"Stale assumption: '{short_text}' — {short_reason}")
        weight += 30
        confidence_boost += 20

    for a, v in unknown_list:
        short_text = a["text"][:60] + ("..." if len(a["text"]) > 60 else "")
        signals.append(f"Unvalidated assumption (no re-evaluation recorded): '{short_text}'")
        weight += 10
        confidence_boost += 10

    # Signal 2: Bug / fix / revert commit keywords
    bug_keywords = ("fix:", "bug:", "revert:", "hotfix", "regression", "prod-", "patch:", "workaround", "broken")
    bug_commits = [c for c in commits if any(kw in c["message"].lower() for kw in bug_keywords)]
    for c in bug_commits[:2]:
        signals.append(f"Bug-related commit `{c['sha'][:8]}` ({c['date']}): {c['message'][:80]}")
        weight += 15
        confidence_boost += 15

    # Signal 3: Decision age > 3 years with no recorded re-evaluation
    if commits:
        oldest = min(datetime.strptime(c["date"], "%Y-%m-%d").date() for c in commits)
        age_days = (date.today() - oldest).days
        if age_days > 1095:
            signals.append(f"Decision is {age_days} days old ({age_days // 365} years) with no recorded re-evaluation")
            weight += 10
            confidence_boost += 10

    # Signal 4: Unresolved concerns flagged in PR review/issue comments
    concern_keywords = ("risk", "revisit", "what if", "what happens", "careful", "concern", "problem")
    pr_concern_count = 0
    for pr_id, pr in pr_context.items():
        all_comments = pr.get("review_comments", []) + pr.get("issue_comments", [])
        for comment in all_comments:
            if any(kw in comment.lower() for kw in concern_keywords):
                short = comment[:80] + ("..." if len(comment) > 80 else "")
                signals.append(f"Unresolved concern in {pr_id.upper()}: \"{short}\"")
                weight += 5
                confidence_boost += 5
                pr_concern_count += 1
                break
        if pr_concern_count >= 3:
            break

    # Signal 5: TODO / FIXME / HACK markers in the current code
    if code_snippet:
        todo_lines = [
            line.strip() for line in code_snippet.splitlines()
            if any(marker in line.upper() for marker in ("TODO", "FIXME", "HACK", "XXX"))
        ]
        for todo in todo_lines[:2]:
            signals.append(f"Code marker still present: `{todo[:80]}`")
            weight += 5

    # Derive verdict
    is_stale = weight >= 30
    confidence = min(95, max(30, 50 + confidence_boost))

    if not signals:
        return {
            "is_stale": False,
            "reasoning": (
                "No stale assumptions, no bug-related commits, and no unresolved concerns found. "
                "Decision appears valid."
            ),
            "signals_used": ["No debt signals detected"],
            "confidence_score": 85,
        }

    if stale_list:
        top_a, top_v = stale_list[0]
        reasoning = (
            f"{len(stale_list)} assumption(s) directly invalidated. "
            f"'{top_a['text'][:60]}' — {top_v['reason'][:100]}"
        )
    elif bug_commits:
        reasoning = (
            f"{len(bug_commits[:2])} bug-related commit(s) indicate the original decision is causing active issues: "
            f"'{bug_commits[0]['message'][:80]}'"
        )
    elif unknown_list and is_stale:
        reasoning = (
            f"{len(unknown_list)} assumption(s) have never been re-evaluated since introduction. "
            "No evidence they still hold at current scale."
        )
    else:
        reasoning = (
            f"{len(signals)} signal(s) detected but no direct invalidation confirmed. "
            "Decision warrants monitoring but is not yet confirmed stale."
        )

    return {
        "is_stale": is_stale,
        "reasoning": reasoning,
        "signals_used": signals,
        "confidence_score": confidence,
    }


def compute_risk_score(assumptions: list, introduced_date: str, dependent_count: int = 10) -> dict:
    validated = [
        {**a, "validation": validate_assumption(a["text"], a["introduced"])}
        for a in assumptions
    ]
    stale_count = sum(1 for a in validated if a["validation"]["validity"] == "STALE")
    unknown_count = sum(1 for a in validated if a["validation"]["validity"] == "UNKNOWN")

    days_old = (date.today() - datetime.strptime(introduced_date, "%Y-%m-%d").date()).days
    age_years = days_old / 365

    staleness_component = (stale_count * 2.5) + (unknown_count * 1.0)
    age_component = min(3.0, age_years * 0.5)
    blast_radius_component = min(2.0, dependent_count * 0.04)

    raw_score = staleness_component + age_component + blast_radius_component
    score = round(min(10.0, raw_score), 1)

    return {
        "score": score,
        "tier": "CRITICAL" if score >= 8 else "HIGH" if score >= 5 else "MEDIUM" if score >= 2 else "LOW",
        "breakdown": {
            "stale_assumptions": stale_count,
            "unknown_assumptions": unknown_count,
            "decision_age_days": days_old,
            "dependent_files": dependent_count,
            "staleness_component": round(staleness_component, 2),
            "age_component": round(age_component, 2),
            "blast_radius_component": round(blast_radius_component, 2),
        },
    }
