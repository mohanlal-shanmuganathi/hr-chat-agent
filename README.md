<img src="web/logo.png" alt="Ideas2IT" width="72" align="right">

# HR Chat Agent

An agentic HR assistant for authenticated Ideas2IT employees. It:

- answers questions from the company's **current** HR and IT policy documents, with citations;
- answers questions about the employee's **own** leave, WFH, loan and certification status;
- decides eligibility with a **deterministic rules engine**, never by model guesswork;
- submits leave requests and opens HR tickets **only after the employee approves**;
- routes sensitive matters and out-of-scope questions to HR (hr@ideas2it.com).

**▶ Demo video:** https://drive.google.com/file/d/1WyVW0T2JHF2QnkJngb6UJ6EXD7ZOnRth/view?usp=drive_link

**Stack:** Python 3.12 · FastAPI · LangGraph · Gemini (provider configurable) · PostgreSQL 16 +
pgvector · Docker Compose · vanilla-JS web UI · Google sign-in (OIDC).

## Architecture

![HR Chat Agent solution architecture](docs/images/architecture.png)

A request moves top to bottom: the employee signs in with Google (only verified `ideas2it.com`
accounts) and chats over HTTPS. FastAPI passes each turn, with the employee's identity from the
session, to a LangGraph state machine: a pattern **guard**, a **scope check**, then the **agent**
(Gemini) plans and calls **tools** for up to six rounds; anything that writes pauses at an
**approval** interrupt. Tools search the current policies (hybrid full-text + vector search),
read the employee's own HR data, and run a deterministic rules engine. PostgreSQL with pgvector
holds the HR data, the policy index, agent checkpoints and the audit log. Only prompts and tool
results leave the boundary, to Gemini. Details: [docs/architecture.md](docs/architecture.md).

## Documentation

| Document | What it covers |
|---|---|
| [Architecture](docs/architecture.md) | Components, request flow, agent graph, RAG pipeline, data model, security, reliability, observability, cost, AWS target |
| [Agent specification](docs/agent-spec.md) | Purpose, inputs and outputs, permissions, memory, decision policy, termination, escalation, and what it must not do |
| [Tools](docs/tools.md) | All 12 tools: schemas, side effects, idempotency, error contract |
| [Decision records](docs/adr/) | LangGraph, single agent, pgvector hybrid search, policy versioning, identity injection, LLM choice, confidential data |
| [Evaluation results](docs/eval-results.md) | Eval-suite pass rate by category |
| [Demo script](docs/demo-script.md) | Walkthrough used for the demo video |

## Highlights

- **One agent, deterministic tools.** The model chooses tools and explains results. Dates, leave
  days, balances, eligibility and permissions are plain, tested Python.
  ([ADR-002](docs/adr/0002-single-agent-with-deterministic-tools.md))
- **Latest policy version wins automatically.** Version tables are read from the PDFs; superseded
  versions (e.g. Leave Policy v1.2) are never searchable.
  ([ADR-004](docs/adr/0004-automatic-policy-versioning.md))
- **Hybrid retrieval in Postgres.** Full-text plus vector search, fused, filtered by version and
  location, with citations down to section and page.
- **Identity comes from the session, never the model.** Tools cannot express "another employee";
  threads are isolated per employee.
- **Human in the loop.** Write actions pause the graph until Approve/Reject. Pending approvals
  survive restarts (Postgres checkpoints). Writes are idempotent and re-validated server-side.
- **Guardrails before the model.** Sensitive personal disclosures and crisis messages get
  reviewed replies routing to people; prompt injection is refused.
- **Location-aware.** Holiday answers use the employee's location on record and mention
  the other locations (Chennai / Karnataka / USA); a named location is used instead.
- **Explainable.** Each turn reports the tools used with their arguments, the verdict findings
  with policy references, citations, tokens and prompt version. An audit log records every tool
  call and approval.
- **Tested and evaluated.** 123 unit and integration tests in CI, plus a 32-case public evaluation
  suite (and private cases for policy facts) with deterministic scoring and an optional LLM judge.

## Confidential data

The repository is public; the policy documents are not.

- Put the original policy PDFs in `data/private_policies/` (git-ignored). They are mounted
  **read-only** into the container and never baked into the image.
- Rule values transcribed from the policies live in `data/private_policies/config/`
  (`leave_types.yaml`, `rules.yaml`). `data/seed/*.example.yaml` hold **fictional** placeholder
  values that are used only when the private files are absent.
- Eval cases that assert policy facts live in `evals/private/` (git-ignored).
- CI fails if anything in those folders, or a `.env`, is ever committed.

Employee data comes from a **mock HR database of fictional people** (`data/seed/employees.yaml`)
behind the `HRDataProvider` interface, ready to be replaced by an employee-portal API client.

## Run it locally (Windows / macOS / Linux)

Follow the steps in order; each command is run from the project folder. Commands are shown for
PowerShell and work the same in a macOS/Linux terminal (use `cp` instead of `copy`).

### Step 0: What you need

| Requirement | Where to get it |
|---|---|
| Docker Desktop (Windows: WSL 2 backend), running | https://www.docker.com/products/docker-desktop/ |
| Git | https://git-scm.com/downloads |
| A Gemini API key (free, no card) | https://aistudio.google.com → *Get API key* |
| The Ideas2IT policy documents (PDFs) | Internal — they are confidential and **not** in this repository |

### Step 1: Get the code

```powershell
git clone https://github.com/mohanlal-shanmuganathi/hr-chat-agent.git
cd hr-chat-agent
```

### Step 2: Add the policy documents (on your machine only)

Copy the policy PDFs into `data/private_policies/`, for example
`data/private_policies/Revised Leave Policy - I2I.pdf`, `.../Holiday List - 2026.pdf`,
`.../Karnataka Holiday List - 2026.pdf`, `.../USA Holiday List - 2026.pdf`.

- The folder is git-ignored and mounted read-only into the container; nothing in it can be
  committed (CI also checks).
- If you were given the private rule files (`leave_types.yaml`, `rules.yaml`), put them in
  `data/private_policies/config/`. Without them the eligibility rules use the **fictional**
  values in `data/seed/` (answers quoted from the policies still come from the real PDFs).

### Step 3: Create your `.env`

```powershell
copy .env.example .env
```

Open `.env` and set two values:

1. `GOOGLE_API_KEY=` your Gemini key.
2. `SESSION_SECRET=` a long random string. To generate one:
   ```powershell
   docker run --rm python:3.12-slim python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

Leave the rest as is: `AUTH_DEV_LOGIN_ENABLED=true` turns on the demo employees (Step 6).

### Step 4: Start everything

```powershell
docker compose up -d --build
```

This starts PostgreSQL (with pgvector), applies the database migrations and starts the app. The
first build takes a few minutes. Check it is up (both should say `ok`):

```powershell
curl http://localhost:8000/ready
```

### Step 5: Load the demo employees and index the policies

```powershell
docker compose run --rm api python -m app.hr.seed
docker compose run --rm api python -m app.rag.ingest
```

- `seed` creates the **fictional** employees and their leave history.
- `ingest` reads the PDFs, detects each policy's version (older versions are kept out of
  search), and builds the search index. The first run downloads the local embedding model
  (~130 MB, once). It prints a report of every document and its status.
- Re-run `ingest` whenever you add or change a policy file.

### Step 6: Use it

Open **http://localhost:8000** and pick a demo employee (no password; local demo only):

| Demo employee | Location | Good for showing |
|---|---|---|
| **Priya R** · Senior Software Engineer | Chennai | Everyday questions: balances, leave eligibility, applying leave, staff loan |
| **Rohan K** · Engineering Manager | Karnataka | Location-aware holidays |
| **Kavya S** · Associate Software Engineer | Chennai | On probation: eligibility refusals with reasons |
| **Rahul P** · QA Lead | Chennai | Notice period: leave and WFH blocked |
| **Vikram N** · Software Engineer | Chennai | Legacy privilege-leave balance |
| **Meena J** · HR Business Partner | Chennai | General policy questions |
| **Emily Carter** · Delivery Lead | USA | US holiday list and general policy questions |

Try: *"How many sick leaves do I get in a year?"*, *"What is my leave balance?"*,
*"Can I take casual leave from 16 to 20 Oct?"*, *"When is Diwali this year?"* then
*"What about Bengaluru?"*, *"Apply earned leave for 9 and 10 November"* (approve the card).
Click **"Used N tools"** above an answer to see the steps, tool arguments and sources.

### Step 7 (optional): Sign in with your Ideas2IT Google account

1. In [Google Cloud Console](https://console.cloud.google.com), create or pick a project, then
   **APIs & Services → OAuth consent screen**: app name `HR Assistant`; user type **Internal** if
   offered (else **External** in *Testing*, and add your email as a test user).
2. **APIs & Services → Credentials → Create credentials → OAuth client ID** → type **Web
   application** → authorised redirect URI exactly `http://localhost:8000/auth/google/callback`.
3. Put the values in `.env`:
   ```
   GOOGLE_CLIENT_ID=<client id>.apps.googleusercontent.com
   GOOGLE_CLIENT_SECRET=<client secret>
   GOOGLE_ALLOWED_DOMAIN=ideas2it.com
   MOCK_HR_AUTOPROVISION=true
   ```
4. Restart the app: `docker compose up -d --force-recreate api`
5. Open http://localhost:8000 → **Continue with Google**. Only verified `@ideas2it.com` accounts
   are accepted (checked on the token's signed `hd` claim). Because the HR data is a mock,
   `MOCK_HR_AUTOPROVISION=true` creates a demo HR profile for your account on first sign-in.
   Re-running `seed` removes it; just sign in again.

To allow Google sign-in only, set `AUTH_DEV_LOGIN_ENABLED=false` and restart.

### Everyday commands and troubleshooting

| Task | Command |
|---|---|
| Stop / start again | `docker compose stop` · `docker compose up -d` |
| Update to the latest code | `git pull` then `docker compose up -d --build` |
| App logs | `docker compose logs api --tail 100` |
| Check the AI key and model | `docker compose run --rm api python -m app.agent.llm_check` |
| Terminal chat | `docker compose run --rm -it api python -m app.agent.cli --email priya.r@example.com` |
| Evaluation suite | `docker compose --profile evals run --rm evals` (add `python -m evals.run_evals --judge` for the LLM judge) |

| Problem | Fix |
|---|---|
| `/ready` says `database: unavailable` | Wait 30 s after the first start and retry; then check `docker compose logs api` |
| "The AI service rate limit was reached" or "daily free quota is used up" | Free-tier limit: wait, or set another model in `LLM_MODEL` (e.g. `gemini-3.1-flash-lite`) and run `docker compose up -d --force-recreate api` |
| Google sign-in shows `redirect_uri_mismatch` | The redirect URI in Google Cloud must be exactly `http://localhost:8000/auth/google/callback` |
| Port 8000 already in use | Stop the other program, or change the port mapping in `docker-compose.yml` |
| Browser shows an old page after an update | Press Ctrl+F5 |

### Configuration (`.env`)

| Variable | Purpose |
|---|---|
| `GOOGLE_API_KEY` | Gemini API key (free at aistudio.google.com) |
| `LLM_PROVIDER`, `LLM_MODEL` | Model selection (default `google_genai` / `gemini-3.5-flash-lite`) |
| `GOOGLE_GENAI_USE_VERTEXAI` | `true` when `GOOGLE_API_KEY` is a Google Cloud (Vertex AI express mode) key rather than an AI Studio key |
| `LLM_BASE_URL`, `LLM_API_KEY` | With `LLM_PROVIDER=openai_compatible`: any OpenAI-style API with tool calling (Groq, OpenRouter, local Ollama) |
| `SESSION_SECRET` | Signs session cookies (required outside local development) |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_ALLOWED_DOMAIN` | Google sign-in (see below) |
| `AUTH_DEV_LOGIN_ENABLED`, `MOCK_HR_AUTOPROVISION` | Demo conveniences; keep off in shared deployments |
| `EMBEDDING_PROVIDER` | `fastembed` (semantic, downloads ~130 MB once) or `hashing` (offline) |
| `LANGSMITH_TRACING`, `LANGSMITH_API_KEY` | Optional tracing. Sends prompts and excerpts to LangSmith; off by default |
| `AGENT_MAX_TOOL_ROUNDS`, `AGENT_TURN_TIMEOUT_S`, `TOOL_TIMEOUT_S`, `RATE_LIMIT_PER_MINUTE` | Limits |
| `SCOPE_CHECK_ENABLED` | Off-topic questions get a fixed reply (default `true`; one extra model call per message) |

**Free-tier quotas:** each question uses 2–3 model calls. Some free tiers allow only a few
requests per minute and a few dozen per day (check yours at https://ai.dev/rate-limit for
Gemini). When the quota runs out the assistant says so; switch `LLM_MODEL` or `LLM_PROVIDER`.

**Privacy note:** on the Gemini free tier, Google may use prompts (questions and policy excerpts)
to improve its products. Use a paid tier or another provider if that is not acceptable.

### Google sign-in

1. In Google Cloud Console, create an OAuth client of type **Web application**.
2. Add the redirect URI `http://localhost:8000/auth/google/callback`.
3. Use consent screen *Internal* if your Workspace allows it; otherwise *External* in testing mode,
   with yourself as a test user.
4. Put the client ID and secret in `.env`. Only verified `@ideas2it.com` accounts are accepted,
   checked on the token's signed `hd` claim.

### Behind a TLS-inspecting corporate proxy (e.g. Zscaler)

If the image build fails with `CERTIFICATE_VERIFY_FAILED`, pass your proxy's root CA as a build
secret. It is used only during the build and never stored in the image.

```powershell
$env:EXTRA_CA_FILE = "C:\path\to\corp-root-ca.pem"
docker compose up -d --build
```

## Development

```bash
uv sync
docker compose up -d db
uv run alembic upgrade head && uv run python -m app.hr.seed
uv run uvicorn app.main:create_app --factory --reload

# checks (same as CI)
docker compose exec db createdb -U hr_agent hr_agent_test
export TEST_DATABASE_URL=postgresql+asyncpg://hr_agent:hr_agent@localhost:5432/hr_agent_test
uv run ruff check . && uv run ruff format --check . && uv run mypy app evals && uv run pytest -q
```
