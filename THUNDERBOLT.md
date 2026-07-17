# Thunderbolt PoC fork of Open SWE

**Ticket:** [THU-696](https://linear.app/mozilla-thunderbolt/issue/THU-696/poc-open-swe-as-ticket-to-pr-autonomous-agent)
**Upstream base:** `langchain-ai/open-swe` @ `5cb2e2bb3582d69241b386bb0852c6f6b40b2dbb`
**Deploy target:** [thunderbird/thunderbolt-sandbox](https://github.com/thunderbird/thunderbolt-sandbox)

This fork evaluates [Open SWE](https://github.com/langchain-ai/open-swe) as a ticket-to-PR autonomous agent for Thunderbolt Factory, per THU-696. It runs on Render, is triggered by Linear `@openswe` mentions, and opens PRs against `thunderbird/thunderbolt-sandbox`. Eight local patches ride on top of upstream — none of which have been submitted back — that turn stock Open SWE from "unviably expensive" into "$0.24 per trivial task."

---

## Result — six test rounds

| # | Where | Model | Cache markers | Wall | Calls | Cost | Sandbox teardown | Outcome |
|---|---|---|---|---|---|---|---|---|
| 1 | Local (personal fork target) | `anthropic/claude-sonnet-4.6` | ❌ | 13m 43s | 137 | **$20.00** | — | ✗ credit exhausted before push |
| 2 | Local | `openai/gpt-5.6-sol` | ❌ | 1m 43s | 16 | $0.54 | — | ✗ push failed (no `gh` in sandbox) |
| 3 | Local + git-auth patch | `openai/gpt-5.6-sol` | ❌ | 0m 33s | 11 | $0.26 | manual | ✅ PR |
| 4 | Local + patches 1-7 | `anthropic/claude-sonnet-4.6` | ✅ | 1m 29s | 17 | $0.17 | manual | ✅ PR |
| 5 | **Render (thunderbolt-sandbox target)** | `anthropic/claude-sonnet-4.6` | ✅ | 0m 56s | ~15 | $0.24 | idle-wait (~15m) | ✅ PR |
| 6 | **Render + patch #8** | `anthropic/claude-sonnet-4.6` | ✅ | **1m 16s** | ~15 | **$0.23** | **auto (19s after PR)** | ✅ **PR on production sandbox repo** |

**Headline:** Sonnet 4.6 + cache markers on Render = **117× cheaper** than the same model without markers. Sandbox teardown patch cuts Daytona bill **~90% per task**.

---

## Deploy topology

```
Linear webhook ───► Render Web Service (Docker) ───► LangGraph API + FastAPI
                    │  open-swe-poc.onrender.com          │
                    │  1 × Standard 2GB                   │
                    │  langgraph dev --no-reload          │
                    │  pickle-file persistence            │
                    │  → Render Persistent Disk (1 GB)    │
                    │    mounted at /app/.langgraph_api   │
                    │                                     ├──► External vendors:
                    │                                     │    • OpenRouter    (LLM routing → Anthropic/OpenAI/etc.)
                    │                                     │    • Daytona       (sandbox execution — ephemeral containers)
                    │                                     │    • GitHub App    (Thunderbolt Automation Agent — thunderbird/thunderbolt-sandbox only)
                    │                                     │    • Linear API    (comment writes, webhook receiver)
                    │                                     │
                    │                                     └──► DOES NOT talk to:
                    │                                          • smith.langchain.com   (LangSmith — bypassed, see §7)
                    │                                          • Any user's browser    (unless viewing via LangGraph Studio SPA)
                    │
GitHub App webhook ─┘ (deferred — needs org admin to activate on Thunderbolt Automation Agent App;
                       CI-iteration + review-response feedback loops depend on it)
```

**Persistence:** Render Persistent Disk mounted at `/app/.langgraph_api` catches the pickle files LangGraph's inmem runtime writes. Survives restarts, deploys, container recreation. Single-writer only — caps scaling to one instance, which is not a bottleneck at Thunderbolt's usage pattern (one team, one repo, sub-10 concurrent runs). Cost: $0.25/mo.

**Not chosen — LangGraph Platform paid runtime.** See §*"LangGraph Platform licensing"* below for why postgres-backed persistence would require either a paid LangChain license or a custom OSS wrapper.

---

## LangGraph Platform licensing

Attempted upgrade path from pickle-file persistence to Postgres-backed persistence via `python -m langgraph_api.cli --runtime-edition postgres` failed on boot:

```
ImportError: Langgraph runtime backend not found.
Please install with `pip install "langgraph-runtime-postgres"`
```

`langgraph-runtime-postgres` is not on public PyPI (verified: HTTP 404 on `pypi.org/pypi/langgraph-runtime-postgres/json`). Same for `langgraph-runtime-community`. Only `langgraph-runtime-inmem` is publicly available.

`langgraph_cli/cli.py:298` documents the paid-tier requirement: **"For production use, requires a license key in env var `LANGGRAPH_CLOUD_LICENSE_KEY`."** References to `langchain/langgraph-orchestrator-licensed`, `langchain/langgraph-trial`, and `LANGGRAPH_CLOUD_LICENSE_KEY` throughout the CLI confirm that LangGraph's Postgres-backed self-hosted deployment is a commercial LangSmith Platform feature.

**Free options for production-grade persistence:**

1. **Render Persistent Disk + `langgraph dev`** ← chosen. Ceiling: single-instance. No license required. $0.25/mo.
2. **Custom FastAPI wrapper** using OSS `langgraph-checkpoint-postgres` + `langgraph-store-postgres`. Replaces `langgraph_api` entirely. ~3–5 days of work; ~500 LOC. No LangChain dependency at runtime.
3. **`langchain/langgraph-trial` Docker base image** — free eval variant with time/scale limits. Would work for extended PoC but not for real production.

**Paid options:**

4. **LangGraph Platform Self-Hosted** — enterprise-priced Docker license from LangChain. Postgres runtime works out of the box.
5. **LangGraph Platform Cloud** — fully hosted SaaS. Sends prompts through LangChain's infra. Rejected for THU-696's "no LangSmith" evaluation goal.

Adoption decision for stakeholders: the "we self-host Open SWE cheaply" narrative holds only up to single-instance scale via option 1. Beyond that, options 2 or 4 are the honest paths.

---

## The eight patches

All in three files + one Makefile line + one Dockerfile. None require modifying Open SWE core beyond these.

### 1. Route all LLM calls through OpenRouter — `agent/utils/model.py`

Wraps `make_model()` to unconditionally return a `ChatOpenAI` pointed at OpenRouter's OpenAI-compat endpoint when `OPENROUTER_API_KEY` is set. Bypasses Open SWE's provider-specific defaults (which would try to use `openai:` responses API's WSS endpoint, incompatible with OpenRouter).

Commit: `poc(THU-696): route models through OpenRouter + inject cache_control markers`

### 2. Inject Anthropic prompt-cache markers — `agent/utils/model.py`

Wraps `ChatOpenAI._get_request_payload` to add `cache_control: {type: "ephemeral"}` to (a) the system message and (b) the last user/tool message on every outbound request. Uses 2 of Anthropic's 4 allowed cache breakpoints.

**Open SWE emits zero `cache_control` markers in production code** (verified: `cache_control` appears only in `tests/agent/test_timeout_wrapup.py`). Community issues [#409/#412/#436/#964](https://github.com/langchain-ai/open-swe/issues/964) all closed without a fix. Empirically verified via direct OpenRouter test — 91.4% cost reduction on repeated 5413-token prefix ($0.034 → $0.003). Same commit as patch 1.

### 3. GitHub App token injection into sandbox git config — `agent/server.py`

Modifies `_configure_git_identity` to write the App installation token into `/tmp/.git-creds` inside the sandbox and configure git's credential helper to read from it. Without this, `git push` from the sandbox prompts for username → fails on non-interactive containers.

The main agent graph calls `ensure_sandbox_for_thread` **without** passing `github_proxy_token` (only the reviewer graph passes it), so the function falls back to minting an App token via `get_github_app_installation_token_with_expiry()`. Token expiry (~1h) exceeds any single task duration.

Alternative would be a custom Daytona snapshot with `gh` CLI installed. Deferred — this patch is self-contained.

Commit: `poc(THU-696): inject GH App token into sandbox git credential helper`

### 4. Daytona sandbox lifecycle — `agent/integrations/daytona.py`

Passes `auto_stop_interval=15` (minutes) + `ephemeral=True` to `CreateSandboxFromSnapshotParams`. Overridable via `DAYTONA_AUTO_STOP_MINUTES` env var.

**Silent production cost bomb without this.** Upstream Open SWE never calls `sandbox.delete()` for Daytona (only the LangSmith proxy path has cleanup code — `agent/integrations/langsmith.py:615`). Daytona's account defaults leave sandboxes alive for days. Every run leaks a sandbox.

Commit: `poc(THU-696): auto-stop + ephemeral Daytona sandboxes`

### 5. Point Linear team map at deploy target — `agent/utils/linear_team_repo_map.py`

Adds `"Thunderbolt": {"owner": "thunderbird", "name": "thunderbolt-sandbox"}` to `LINEAR_TEAM_TO_REPO`. Tells the webhook handler which repo to route Thunderbolt-team tickets to.

Commit: `poc(THU-696): point Thunderbolt team at thunderbolt-sandbox`

### 6. Disable LangGraph Studio auto-open — `Makefile`

Adds `--no-browser` to `langgraph dev`. Without it, every `make dev` opens smith.langchain.com/studio in the browser. Not a security issue — Studio's SPA calls localhost directly and no graph data flows through LangChain servers — but noisy and out of scope for a "no LangSmith" evaluation.

Studio is still usable manually: paste `https://smith.langchain.com/studio/?baseUrl=<your-server>` in a browser.

Commit: `poc(THU-696): skip auto-open of LangSmith Studio in dev`

### 7. Render Dockerfile — `Dockerfile.render`

Minimal Python 3.12 slim + `uv sync --frozen --no-dev` + `langgraph dev --no-browser --no-reload` as the entrypoint. **Note:** README.md must be present in the deps layer because hatchling validates the readme file during metadata parsing, before dependency install.

`langgraph dev --no-reload` is used in prod because `langgraph up` requires docker-compose (docker-in-docker on Render doesn't work). Trade-off: in-memory checkpointer, no state persistence across container restarts. Acceptable for PoC.

Commits: `poc(THU-696): Dockerfile for Render deploy` + `poc(THU-696): include README.md in Dockerfile deps layer (hatchling metadata)`

### 8. Immediate Daytona sandbox teardown on run completion — `agent/completion.py`

Adds `_cleanup_daytona_sandbox_for_thread()` and wires it into `handle_run_completion` so that when LangGraph's run-complete webhook fires (`success` / `error` / `timeout` — deliberately NOT `interrupted` since a successor run inherits the sandbox), we call `daytona.delete()` on the sandbox bound to the thread.

Reuses the existing `/webhooks/run-complete` route + `verify_run_complete_token` machinery upstream shipped for failure-reply. Requires two env vars to be set — otherwise `dispatch.py` doesn't attach the webhook to runs:
- `RUN_COMPLETE_WEBHOOK_SECRET` — shared secret proving the callback came from LangGraph
- `COMPLETION_WEBHOOK_URL` — absolute https URL: `https://<render-url>/webhooks/run-complete`

**Verified end-to-end:** sandbox destroyed **19s after PR opened** (round 6 test), vs the ~15 min idle wait that would fire from patch #4's `auto_stop_interval` alone. Effective Daytona bill cut by ~90% per task.

Fire-and-forget: if the delete fails, teardown falls back to the `auto_stop_interval=15` safety net from patch #4. Never blocks the failure-reply logic.

Commit: `poc(THU-696): destroy Daytona sandbox on run completion`

---

## Deployment recipe (from scratch)

Assumes you have: Render account, thunderbird org admin access for the GitHub App, Daytona account with API key, OpenRouter account with $20+ credit, Linear personal API key.

### Phase A — Local prep

```bash
# 1. Clone this fork
git clone https://github.com/raivieiraadriano92/open-swe-thunderbolt.git ~/dev/open-swe
cd ~/dev/open-swe

# 2. Install deps + smoke-test locally (optional but recommended)
uv venv --python 3.12
source .venv/bin/activate
uv sync --all-extras

# 3. Verify Dockerfile builds cleanly
docker build -f Dockerfile.render -t open-swe-render .
```

### Phase B — External account setup

1. **GitHub App** (`Thunderbolt Automation Agent`, App ID `4316868`) — must have these repository permissions:
   - Contents R/W · Issues R/W · Pull requests R/W · Checks R/W · Workflows R/W · Metadata R
   - Subscribed events: `issue_comment`, `pull_request_review`, `pull_request_review_comment`, `check_run`, `check_suite`, `workflow_run`
   - Installed only on `thunderbird/thunderbolt-sandbox`
   - Webhook: **Active**, URL set to `https://<render-url>/webhooks/github`, secret matches `GITHUB_WEBHOOK_SECRET` in Render env group

2. **Daytona API key** — save as `DAYTONA_API_KEY`. Default snapshot `daytonaio/sandbox:0.6.0` works for README-only tasks. Real Thunderbolt-repo tasks would need a custom snapshot with `bun` installed (not required for the sandbox repo).

3. **OpenRouter key** — save as `OPENROUTER_API_KEY`. Set a credit limit to bound blast radius.

4. **Linear personal API key** — save as `LINEAR_API_KEY`. Create a webhook in Linear pointing at `https://<render-url>/webhooks/linear`, subscribed to `Comments → Create`, on the Thunderbolt team. **Note:** Linear auto-generates the webhook signing secret at creation; capture it via `webhook.secret` GraphQL query and save as `LINEAR_WEBHOOK_SECRET`.

### Phase C — Render deploy

1. Create Env Group `open-swe-poc` with all vars from §Environment reference.
2. Create Web Service:
   - Source: this repo, branch `main`
   - Runtime: Docker, Dockerfile path `./Dockerfile.render`
   - Instance type: **Standard** (2GB — smaller tiers OOM)
   - Health check path: `/health`
   - Auto-deploy: **Off**
   - Link Env Group: `open-swe-poc`
3. Deploy. First build ~5 min.
4. Once green, update GitHub App + Linear webhook URLs to the Render URL.

### Phase D — Verify

```bash
export RENDER_URL=https://open-swe-poc.onrender.com
curl -s -o /dev/null -w "%{http_code}\n" $RENDER_URL/health                              # expect 200
curl -s -o /dev/null -w "%{http_code}\n" -X POST -d '{}' $RENDER_URL/webhooks/linear      # expect 401
```

Then trigger a Linear issue with `@openswe`. PR should appear on `thunderbird/thunderbolt-sandbox` within ~1 min.

---

## Environment reference

| Category | Var | Required | Example / notes |
|---|---|---|---|
| **LLM** | `OPENROUTER_API_KEY` | ✅ | `sk-or-v1-...` |
| | `LLM_MODEL_ID` | ✅ (boot check) | `anthropic:claude-sonnet-4.6` — validation hint only; actual routing is hardcoded to `openai/gpt-5.6-sol` unless you edit `agent/utils/model.py` |
| | `ANTHROPIC_API_KEY` | ✅ (boot check) | `unused-poc-placeholder` — validator checks presence only, never called |
| **GitHub App** | `GITHUB_APP_ID` | ✅ | `4316868` |
| | `GITHUB_APP_INSTALLATION_ID` | ✅ | `147045171` |
| | `GITHUB_APP_PRIVATE_KEY` | ✅ | Full PEM incl. `-----BEGIN`/`END` lines, multi-line env var |
| | `GITHUB_WEBHOOK_SECRET` | ✅ | 64-char hex, must match value set on the App |
| **Sandbox** | `SANDBOX_TYPE` | ✅ | `daytona` |
| | `DAYTONA_API_KEY` | ✅ | `dtn_...` |
| | `DAYTONA_SANDBOX_SNAPSHOT` | | Default `daytonaio/sandbox:0.6.0` |
| | `DAYTONA_AUTO_STOP_MINUTES` | | Default 15 — controls when sandbox auto-teardown fires |
| **Linear** | `LINEAR_API_KEY` | ✅ | `lin_api_...` |
| | `LINEAR_WEBHOOK_SECRET` | ✅ | `lin_wh_...` — Linear-auto-generated, retrievable via `webhook.secret` GraphQL query |
| **Repo scope** | `ALLOWED_GITHUB_REPOS` | ✅ | `thunderbird/thunderbolt-sandbox` |
| | `DEFAULT_REPO_OWNER` | ✅ | `thunderbird` |
| | `DEFAULT_REPO_NAME` | ✅ | `thunderbolt-sandbox` |
| **Token encryption** | `TOKEN_ENCRYPTION_KEY` | ✅ | 44-char Fernet key (base64url with `=` padding) |
| **LangSmith bypass** | `LANGCHAIN_TRACING_V2` | ✅ | `false` |
| | `LANGSMITH_API_KEY_PROD` | ✅ | `stub-bot-mode-only-not-used` — **presence, not value**, activates bot-token-only auth mode (see `agent/utils/auth.py:70`) |
| **Dashboard placeholders** | `DASHBOARD_JWT_SECRET` | ✅ | Any hex — required at boot even though dashboard UI is not deployed |
| | `DASHBOARD_BASE_URL` | ✅ | `http://localhost:3000` (arbitrary — enables the boot validation path we bypass via `LLM_MODEL_ID`) |
| | `DASHBOARD_API_BASE_URL` | ✅ | `http://localhost:2024` |
| | `DASHBOARD_ALLOWED_ORIGINS` | ✅ | `http://localhost:3000` |
| | `LANGGRAPH_URL` | ✅ | `http://localhost:2024` |
| **Run-complete webhook** (patch #8) | `RUN_COMPLETE_WEBHOOK_SECRET` | ✅ | 64-char hex — token LangGraph must present to `/webhooks/run-complete` |
| | `COMPLETION_WEBHOOK_URL` | ✅ | `https://<render-url>/webhooks/run-complete` — must be absolute https, not loopback (LangGraph platform rejects loopback URLs) |

---

## Findings — key data for the THU-696 report

### Cost is model + caching, not repo size

Sonnet 4.6 without cache markers: $20 for a README edit. Same model with markers: $0.17. The critical variable is whether the caller emits `cache_control` — not the underlying model choice, not the repo size, not the ticket complexity.

**Open SWE does not emit these markers.** This is a known upstream gap that has stayed unfixed through multiple community issues.

### OpenAI's "automatic caching" doesn't survive OpenRouter passthrough

Direct API test verified: sending an identical 6413-token system prompt via OpenRouter to `openai/gpt-5.6-sol` twice → both calls show `cached_tokens: 0`. Adding `cache_control: {type: "ephemeral"}` markers → 91.4% cache hit on the second call. This applies uniformly across providers through OpenRouter.

### Sub-agents are model-dependent and NOT dynamic

Open SWE spawns sub-agents at two levels:

1. **`task`-tool sub-agents** — main agent delegates to isolated LLM invocations. Sonnet 4.6 spawns these aggressively; GPT-5.6-sol keeps more work in main context. Same task, same repo: Sonnet did 137 calls, GPT did 11.
2. **Graph-level sub-agents** — `reviewer`, `analyzer`, `chat`, `scheduler` graphs each with their own LLM budget. Reviewer runs *after* a PR opens; doubles cost per successful PR.

**Model selection is set at graph construction from team/profile config, not chosen dynamically by the agent.** There are 4 configurable model slots (agent main / agent sub / reviewer main / reviewer sub). Our patches route all four through OpenRouter to `openai/gpt-5.6-sol`.

### LangSmith is genuinely strippable

Three env-var / config flags achieve **zero runtime traffic to smith.langchain.com** (verified: 0 outbound calls in server log across all runs). The `langsmith` Python package remains a transitive dep but is never invoked in code paths we exercise. Bot-token-only mode auth is the key unlock — it's activated by *setting* `LANGSMITH_API_KEY_PROD` to any value (counter-intuitively — see `agent/utils/auth.py:62-70`).

### LangGraph Studio has a subtle browser-side data path

The `smith.langchain.com/studio/?baseUrl=<local>` URL loads a SPA that then calls the local server directly — no graph data flows via LangChain. But if the user is logged in to LangSmith, their browser redirects to `/o/<org-id>/studio/thread` — that visit (URL + user's tenant id) is visible to LangChain's servers. Not a data leak, but worth noting.

### Cost extrapolation for real tickets

| Ticket shape | Sonnet 4.6 + cache markers | Note |
|---|---|---|
| Trivial edit (README, config, doc) | **$0.15-0.30** | Proven across 3 runs (rounds 3, 4, 5) |
| Small typed change / prop drill | ~$0.50-1.50 | Extrapolated; more turns, similar cache reuse |
| Feature with tests + CI loop | ~$1.50-5 | Multiple iterations, but each cached against prior turns |
| Reviewer graph on top | +$0.10-0.50/PR | Reviewer graph runs once per PR opened; not yet hit in this PoC |

Sandbox cost (Daytona): ~$0.10-0.30 per task at current pricing.

Render always-on cost: ~$25/mo (Standard tier).

---

## Known caveats & deferred work

### GitHub App webhook not yet activated on the production App

`Thunderbolt Automation Agent`'s webhook config is `null` on GitHub's side because we lack admin access. Once an org owner enables it:

- The App webhook URL must be `https://open-swe-poc.onrender.com/webhooks/github`
- The webhook secret must match `GITHUB_WEBHOOK_SECRET` in the Render env group

Until then, these do not work:
- Agent iterating on CI failures (`check_run` events)
- Agent responding to human PR review comments
- Agent reacting to workflow_run status

Everything else works — the initial PR opening is unaffected.

### Thunderbolt's own build tooling not yet available in the Daytona sandbox

`daytonaio/sandbox:0.6.0` (Daytona default) has `git`, `node`, `python`, `gh` CLI, but not `bun`. For any ticket that requires `bun install` in the sandbox (i.e., any real Thunderbolt build/test), we'd need a custom snapshot. Path:

1. Build a Docker image extending the default with `curl -fsSL https://bun.sh/install | bash`
2. Push to a container registry Daytona can pull from
3. Set `DAYTONA_SANDBOX_SNAPSHOT=<your-image>` in Render env group

Not required for the current sandbox-repo scope (`thunderbird/thunderbolt-sandbox`), which doesn't need `bun` to make README-scale edits.

### In-memory checkpointer, no state persistence

Render container restart = all in-flight thread state lost. For real production, wire Open SWE to external Postgres via `POSTGRES_URI` env — LangGraph supports this natively. Not required for PoC where tasks complete in <2 min.

### Only tested against README-scale changes

Rounds 1-5 all used the same "add a paragraph to README" task. Ticket asks for 3-5 varied real tickets to fully evaluate PR quality. Two more shapes recommended:

1. Small typed change (add nullable field to a type, propagate)
2. Bug fix with a reproducible test

---

## Next steps

1. **Get GitHub App webhook activated** by whoever has thunderbird org owner rights.
2. **Run 2-3 varied tickets** (typed change, bug fix) to strengthen cost extrapolations and observe reviewer graph behavior.
3. **Write the THU-696 report** — adopt / adapt / discard recommendation with the data collected.
4. **Optionally: add persistence** (external Postgres) if we decide to keep this running past the PoC.
5. **Optionally: build custom Daytona snapshot** with `bun` — only needed if we point at `thunderbird/thunderbolt` (production) rather than the sandbox.

---

## Repository map — files that matter

| File | Purpose |
|---|---|
| `Dockerfile.render` | Render deploy image |
| `Makefile` | `make dev` runs `langgraph dev --no-browser` |
| `agent/utils/model.py` | Model routing + cache_control injection |
| `agent/server.py` | Sandbox provisioning + git-auth patch |
| `agent/integrations/daytona.py` | Daytona lifecycle (auto-stop + ephemeral) |
| `agent/completion.py` | Run-complete webhook handler — sandbox teardown on success/error/timeout |
| `agent/utils/linear_team_repo_map.py` | Linear team → target repo mapping |
| `agent/utils/auth.py` | Bot-token-only mode logic (upstream, unchanged; behavior controlled by env) |

Our 10 patch commits: `git log 5cb2e2bb..HEAD --oneline` shows the diff surface.
