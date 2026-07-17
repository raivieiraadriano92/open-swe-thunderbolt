# THU-696 PoC — Todo

Companion checklist to [`THUNDERBOLT.md`](./THUNDERBOLT.md). Items are grouped by whether we can act now or are waiting on something. Check off as PRs / config changes land.

## Immediate (can start now)

- [ ] **Attach Render Persistent Disk to `/app/.langgraph_api` (persistence approach chosen)**
  - No code change — pure Render config. Dashboard → `open-swe-poc` → **Disks** → **Add Disk**: name `langgraph-state`, mount path `/app/.langgraph_api`, size `1 GB`.
  - Cost: $0.25/mo.
  - Verify: trigger a small run → restart service via Render UI → confirm thread still visible.
  - Persists: in-flight thread state, LangGraph Store (OAuth tokens, feedback, review-style profiles, thread message queues), ops queue, retry counter.
  - Trade-off: pins us to single-instance scale. Acceptable — Thunderbolt's usage pattern (one team, one repo, sub-10 concurrent runs) is nowhere near that limit.

- [ ] **Document the LangGraph Platform licensing finding in the report**
  - `langgraph-runtime-postgres` / `langgraph-runtime-community` are **not on public PyPI** — only `-inmem` is.
  - `langgraph up` / `langgraph build` produce images that require `LANGGRAPH_CLOUD_LICENSE_KEY` for production (see `langgraph_cli/cli.py:298` + `langchain/langgraph-orchestrator-licensed` image reference).
  - Real production options: (1) buy LangGraph Platform Self-Hosted license, (2) build a custom FastAPI wrapper using OSS `langgraph-checkpoint-postgres`, (3) use `langchain/langgraph-trial` free image, (4) stay on `langgraph dev` + persistent disk (what we chose).
  - Material for stakeholders — adoption decision depends on this.

- [ ] **Run 1–2 varied tickets beyond README edits**
  - Current cost/latency data is entirely from trivial README-touching tasks.
  - Need: (a) a small bug fix requiring multi-file reads + a repro test; (b) a small typed refactor that touches 3–5 files with cross-file type consistency.
  - Both against `thunderbird/thunderbolt-sandbox`.
  - Track per run: cost, wall-time, patches needed, PR quality (matches our review bar?).

## Waiting on org owner

- [ ] **Set `GITHUB_APP_CLIENT_SECRET` + verify dashboard login**
  - Blocked on:
    - Adding callback URL `https://open-swe-poc.onrender.com/dashboard/api/auth/callback` in the GitHub App settings.
    - Generating a Client Secret in the App settings and handing it over.
  - Once received: paste into Render env → save → verify OAuth roundtrip → screenshot logged-in dashboard.

- [ ] **Activate GitHub App webhook for `thunderbolt-sandbox`**
  - Blocked on org owner enabling the webhook on the Thunderbolt Automation Agent App.
  - URL: `https://open-swe-poc.onrender.com/webhooks/github` — secret matches `GITHUB_WEBHOOK_SECRET` on Render.
  - Unlocks: triggering runs from GitHub issue comments (not just Linear).

## After login is live (unlocked by client secret)

- [ ] **Try plan-approval flow and capture cost-control screenshot**
  - The #1 differentiator over LangGraph Studio — would have caught our Round-1 $20 incident.
  - Trigger a Linear run, wait for plan, visit `/agents/$threadId/plan`, approve/reject.
  - Screenshot for the final report as "why adopt" evidence.

- [ ] **Trigger review-style analyzer on `thunderbolt-sandbox`**
  - Depends on Render Persistent Disk being attached (Store persistence needed to see the synthesized prompt survive restart).
  - Dashboard → click "Analyze style" for the repo → wait for the analyzer graph to finish.
  - Compare the synthesized prompt to our real `CLAUDE.md`. How close does it come?
  - Novel capability worth calling out in the report as "consider adapting."

## Nice-to-have (probably skip for PoC)

- [ ] **Switch OpenRouter → direct Anthropic** — removes patch #2 (cache_control injection hack). Cleaner code, better cost numbers, but OpenRouter already proved the pluggable-providers story. Do this only if it makes the report cleaner.

- [ ] **AWS ECS / Fargate sandbox** — Open SWE has no built-in AWS provider; would need a ~200 LOC custom factory (`agent/integrations/aws_ecs.py`). Only pursue if there's a business reason (data residency, Daytona pricing at scale). Otherwise Daytona is fine.

- [ ] **Datadog MCP** — only if Thunderbolt sends logs there. Would let the agent read prod telemetry when debugging: "what errors did we see in prod for this endpoint?"

## Final deliverable

- [ ] **Write final THU-696 adopt/adapt/discard report**
  - Depends on all prior tasks.
  - Post as a Linear comment on THU-696.
  - Structure:
    1. TL;DR recommendation.
    2. What worked / what didn't.
    3. 8-patch summary + maintenance burden going forward.
    4. Cost data across ticket variety.
    5. Production-readiness matrix (persistence via disk ✓, sandbox ✓, teardown ✓, dashboard ✓, plan-approval ✓, review-style ✓; Postgres/multi-instance needs custom wrapper or paid license).
    6. Risks / caveats.
    7. Next steps if adopted.
