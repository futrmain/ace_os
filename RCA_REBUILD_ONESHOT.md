# One-Shot Build Prompt — Multi-Agent RCA Operator Console (meerkat / meerkat-mobkit)

> **Read this first.** This is a self-brief to rebuild the *final* product in a single pass.
> It assumes the meerkat/mobkit docs at **https://docs.rkat.ai** are available and that you
> will read them for anything *standard* (how to write an axum server, ordinary JSON-RPC,
> a polling web UI, normal Docker). It deliberately does **not** re-derive what the docs
> cover. It **does** spell out — marked **⚠ DOC-GAP** — every place the docs are *missing*,
> *vague*, or *actively misleading*, because each of those cost real debugging last time.
> When a ⚠ item conflicts with what a doc seems to imply, **trust the ⚠ item.**
>
> Scope: build **only the final product**. Do **not** rebuild the dead ends we went through
> (local llama.cpp, an `llmshim` boolean-schema proxy, letting agents write the WorkGraph,
> gpt-4o-mini, `autonomous_host` members). They are gone on purpose.

---

## 1. What you are building

A **multi-agent SRE / Root-Cause-Analysis operator console.** A human operator describes an
incident (free text or a structured JSON payload); a small mob of LLM agents reasons about it;
a deterministic controller turns that reasoning into a **WorkGraph** (incident → facts →
hypotheses → diagnostic tests) with correct edges and honest semantics; a single vanilla web
page renders the live graph and lets the operator answer diagnostic tests and steer the case.

The defining property is **epistemic honesty**: the system *falsifies* hypotheses, it never
"confirms" one. That invariant is enforced in *code*, not in prompts (see §3).

---

## 2. Starting point (given — do not rebuild)

- A **working nginx container** already serving on `:8010` with a base `nginx.conf`
  (`server_name futrmain.com`, includes for `CIS_guidelines.conf` + `acme_errors.conf`).
  You may **add `location` blocks and volume mounts** to it. Reachable at
  **http://futrmain.com:8010/**, plain HTTP, no TLS.
- **`ace/.env`** exists on the host containing `OPENAI_API_KEY=sk-…` (compose auto-loads it;
  it is git-ignored; never commit or echo it).
- **`ace/UI_v1.png`** — the UI wireframe (Hypotheses panel, Tests panel, YES/NO ovals,
  Operator-chat panel + composer + SEND). Build the UI to match it, then add the extras in §4.7.
- **`ace/incident.json`** and **`ace/late-evidence.json`** — the demo payloads (schema in §4.4).
- Host = `futrmain`, repo copy lives at `/home/ace-user/ace`. Deploy is `bash deploy`
  (rsync `ace/ → rom@futrmain:/home/ace-user/ace`).
  **You never SSH to the host and never run deploy or `docker compose` yourself** — you edit
  files locally and hand the user a redeploy command. Config/scripts are bind-mounted, so most
  changes need only a container restart, **no rebuild**; only Rust source changes need a rebuild.

---

## 3. Architecture — the design, stated as final

**Controller-driven. A deterministic Python `controller` is the SOLE WorkGraph writer.**
The three LLM agents are **pure, comms-only reasoning functions with NO graph tools**; the
controller prompts each over the console, reads its *text* reply, and makes *every* graph
mutation itself.

Why this shape (do **not** revert to letting agents call `workgraph_*` tools): moving all
mutations + role enforcement into deterministic code turns two things into **hard invariants**
instead of prompt hopes —

1. every hypothesis is `parent → Hypotheses → incident` and every test is `parent → hypothesis`
   (correct edges, always);
2. `test_runner` can **never** validate a hypothesis — it has no tools, and the controller only
   ever sets a hypothesis to `failed` or leaves it `open` (there is no "completed" path for a
   hypothesis).

**Services (final stack):**

| service      | image / build                     | role |
|--------------|-----------------------------------|------|
| `nginx`      | given                             | serves the static UI + reverse-proxies the console |
| `mobkit`     | **built** from `ace/mobkit-host`  | Rust *library host* embedding the meerkat mob runtime; serves the mobkit console app on `0.0.0.0:8090` |
| `controller` | stock `python:3.12` + bind-mount  | deterministic RCA protocol; sole WorkGraph writer |
| `seed`       | stock `python:3.12-slim` + bind-mount | one-shot idempotent roster seeder |

**LLM backend = OpenAI `gpt-4.1`, talked to directly** (`https://api.openai.com/v1`). No local
model, no sidecar proxy — see **⚠ W2** for exactly why the final design can go direct (and the
one condition that would force a proxy back in).

**Flow:** operator → UI → `nginx /console/rpc` → `mobkit` timeline → `controller` watches the
coordinator timeline → prompts agents via `console/send` → writes the WorkGraph → UI polls the
WorkGraph snapshot + timeline and renders.

---

## 4. Component specs

### 4.1 `ace/mobkit-host` — the Rust library host

**Why a custom host at all (⚠ H1 — DOC-GAP: the *why*, not the *how*).** The bundled console
binaries `mobkit_gateway` / `rpc_gateway` bind an ephemeral **loopback** port via
`GatewayHttpBinding::bind_loopback()` (`127.0.0.1:0`), and — verified in 0.8.28 source — that is the
**only public binding constructor**: no CLI flag, env var, or config field re-binds it (the
`MOBKIT_FLOW_EDITOR_LISTEN_ADDR` env var is the *flow-editor* binary only, not the console). So a
prebuilt binary can't be reached from nginx in another container, and there is **no config-only
escape** — you are forced to compile a small library host. (The Python/TS SDKs are **not** an
escape hatch: they spawn this same binary over stdio JSON-RPC and inherit its
`127.0.0.1:<ephemeral>` console — it *does* serve the full browser UI/rpc/SSE/blobs, just on
loopback with a random port and no bind override — verified in 0.8.28.) That *forcing* is the under-documented
part. The *how* is actually exemplified: `guides/unified-runtime` plus the `library_mode_reference.rs`
example show serving the router on your own listener — the one-call form is
`runtime.serve(listener, decisions).await?`. This host uses the equivalent explicit two-step
(`build_reference_app_router(decisions)` → `axum::serve`), which is handy when you want the `Router`
in hand; either is fine:

```rust
let runtime = UnifiedRuntime::builder()
    .definition_path("/app/mob.toml")
    .persistent_state("/srv/mobkit/state")   // also AUTO-WIRES the WorkGraph — see ⚠ W1
    .meerkat_config(meerkat_config)          // = Config::default().merge_toml_str(config.toml)
    .timeout(Duration::from_secs(60))
    .build().await?;
let app = runtime.build_reference_app_router(decisions);   // decisions from build_runtime_decision_state
let listener = tokio::net::TcpListener::bind("0.0.0.0:8090").await?;
axum::serve(listener, app).await?;
```
This gives routes `GET /console` (+ `/console/assets/*`), `POST /console/rpc`, SSE
`GET /console/timeline/stream`, `GET /blobs/{id}`, `GET /healthz`.

Imports: `use meerkat::Config;` and
`use meerkat_mobkit::{UnifiedRuntime, build_runtime_decision_state, RuntimeDecisionInputs, BigQueryNaming, AuthPolicy, TrustedOidcRuntimeConfig, ConsolePolicy, RuntimeOpsPolicy};`.
(Build the meerkat config as `let mut c = Config::default(); c.merge_toml_str(&fs::read_to_string("/app/config.toml")?)?;` — `merge_toml_str` takes `&mut self`, it does not chain.) Rustdoc for 0.8.28 may fail to build, so if any import path is uncertain, grep the crate source rather than trusting rustdoc.

**⚠ H3 — DOC-GAP (NOT documented): install a tracing subscriber or fly blind.** meerkat logs
through the `tracing` crate. With **no global subscriber installed, every meerkat warn/error
(including LLM-call failures) is silently dropped** — the container shows only your `println!`s.
This is what once hid a fatal error for hours. **First thing in `main`:**
```rust
tracing_subscriber::fmt()
    .with_env_filter(tracing_subscriber::EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")))
    .init();
```
Set `RUST_LOG` in compose (e.g. `info,meerkat=debug,meerkat_runtime=debug,meerkat_workgraph=debug`).
(There is also an undocumented `UnifiedRuntime::set_error_hook()` to route runtime error events
to a callback — mobkit even logs *"no error hook is registered, so runtime error events reach
logs only."* The subscriber is enough; know the hook exists.)

**⚠ H2 — DOC-GAP (partial): `build_runtime_decision_state(RuntimeDecisionInputs{…})` VALIDATES
its inputs and each failure crashes startup.** Only the first two below are documented
(`reference/decisions`); the JWKS and audience rules are **undocumented** (validator source only).
Use exactly this (auth is off, but the validators still run):

```rust
let decisions = build_runtime_decision_state(RuntimeDecisionInputs {
    bigquery: BigQueryNaming { dataset: "tux_local".into(), table: "runtime_events".into() },
    // (i) documented: must be valid TOML; `modules` is NOT serde(default), so "" fails to parse
    trusted_mobkit_toml: "modules = []".into(),
    auth: AuthPolicy::default(),
    trusted_oidc: TrustedOidcRuntimeConfig {
        discovery_json: r#"{"issuer":"https://noop.example.com","jwks_uri":"https://noop.example.com/.well-known/jwks.json"}"#.into(),
        // (iii) UNDOCUMENTED: JWKS must hold >=1 key even with auth OFF, else MissingKeys.
        //       Use the crate's own HS256 test fixture:
        jwks_json: r#"{"keys":[{"kid":"kid-current","kty":"oct","alg":"HS256","k":"cGhhc2U3LXRydXN0ZWQtY3VycmVudC1zZWNyZXQ"}]}"#.into(),
        // (iv) UNDOCUMENTED: audience must be non-empty.
        audience: "ace-mobkit".into(),
    },
    console: ConsolePolicy { require_app_auth: false, ..ConsolePolicy::default() }, // unauth console; plain HTTP — do NOT expose publicly for real
    ops: RuntimeOpsPolicy::default(),
    // (ii) documented: targets MUST be exactly these four, else MissingReleaseTarget
    release_metadata_json: r#"{"targets":["crates.io","npm","pypi","github-releases"],"support_matrix":"same-as-meerkat"}"#.into(),
})?;
```

**`Cargo.toml`** (version pins matter — cargo will error if wrong, but save the round-trip):
```toml
meerkat        = { version = "=0.8.31", features = ["comms"] }  # "comms" feature IS required for the comms tool group
meerkat-mobkit = "=0.8.28"                                       # newest publish; it in turn pins meerkat =0.8.31
axum   = "0.8"                                                   # must match meerkat-mobkit's axum
tokio  = { version = "1", features = ["full"] }
tracing-subscriber = { version = "0.3", features = ["env-filter"] }

[profile.release]
opt-level = 1   # ⚠ opt-level=3 OOM-kills (SIGKILL) the big meerkat crates on a small host;
                # this binary is I/O-bound so opt-level=1 costs nothing at runtime.
```

**`Dockerfile`** (multi-stage). Non-obvious bits worth stating:
- **glibc must match**: pin **both** stages to trixie — `FROM rust:1-slim-trixie AS builder` and
  `FROM debian:trixie-slim` runtime. A floating `rust:1-slim` drifts and you get
  `GLIBC_2.39 not found` at runtime.
- Builder apt: `pkg-config libssl-dev cmake protobuf-compiler build-essential`.
- **BuildKit cache mounts** on `/usr/local/cargo/registry` and `/build/target` make rebuilds
  incremental (the first compile is huge — ~591 crates). Because `target/` is a *cache mount*
  (not in the image), you must **`cp` the binary to a normal path inside the same `RUN`**:
  `cargo build --release && cp target/release/mobkit-host /usr/local/bin/`, then
  `COPY --from=builder /usr/local/bin/mobkit-host …`.
  - *Stale-binary trap*: the target cache can hand back an old binary when only the base image
    changed (cargo's fingerprint ignores glibc). If you ever get a mystery glibc mismatch after a
    green build, bump the cache-mount `id=` or `docker builder prune --filter type=exec.cachemount`.
- Runtime stage: `apt-get install -y ca-certificates` — **required** for the outbound TLS to
  `api.openai.com` (this is why we can drop the old CA-bearing proxy sidecar).
- `RUN mkdir -p /srv/mobkit/state`; `config.toml`/`mob.toml` are bind-mounted at `/app`.

### 4.2 `ace/mobkit-host/config.toml` — meerkat runtime config (merged onto `Config::default()`)

The realm-binding shape is fiddly and not obvious from the docs; use exactly:

```toml
[self_hosted.servers.openai]
transport = "openai_compatible"      # OpenAI is OpenAI-compatible; same transport path
base_url  = "https://api.openai.com/v1"
api_style = "chat_completions"

[self_hosted.models.dev]             # mob.toml refers to model alias "dev"
server = "openai"
remote_model = "gpt-4.1"             # sent verbatim to OpenAI; no catalog entry needed
display_name = "GPT-4.1 (OpenAI)"
context_window = 128000
max_output_tokens = 8192

[realm.global]
default_binding = "openai"

[realm.global.backend.openai]
provider = "self_hosted"
backend_kind = "self_hosted"
server = "openai"

[realm.global.auth.openai_auth]
provider = "self_hosted"
auth_method = "static_bearer"
source = { kind = "env", env = "OPENAI_API_KEY" }   # key read from env INSIDE the container; never in this file

[realm.global.binding.openai]
backend_profile = "openai"
auth_profile = "openai_auth"
```
**⚠ OpenAI-key gotcha:** a missing/empty/quota-exhausted key fails **silently** — turns stick at
`delivered` with no timeline or log error. If turns never complete, check OpenAI credit *first*.

### 4.3 `ace/mobkit-host/mob.toml` — the mob definition

Bind-mounted → changes take effect on container **restart, no rebuild**.

```toml
[mob]
id = "ace-mob"
orchestrator = "coordinator"
```
Three profiles: `coordinator`, `investigator`, `test_runner`. Each:
`model = "dev"`, `provider = "self_hosted"`, `self_hosted_server_id = "openai"`,
`runtime_mode = "turn_driven"`, `external_addressable = true`, `skills = ["<name>"]`, and a
tools table:
```toml
[profiles.<name>.tools]
comms     = true    # ⚠ M2 — mandatory floor, see below
workgraph = false   # agents NEVER touch the graph — the controller does
builtins  = false
mob       = false
```
Wiring: `auto_wire_orchestrator = false`, `role_wiring = []` (the controller mediates all
coordination via console sends, so no peer wiring is needed — and this sidesteps the whole
peer-messaging / `peer_id`-is-a-UUID minefield entirely).

**⚠ M2 — DOC-GAP (NOT documented): `comms` is a mandatory floor.** A mob member with
`tools.comms = false` is hard-rejected at `ensure_member` time:
`wiring error: profile 'X' has tools.comms=false; mob meerkats require comms=true`. There is no
tool-free member. Nothing in the docs says this.

**Skills = the agent prompts** (`[skills.<name>]`, `source = "inline"`, `content = "…"`). Each
agent is text-in / text-out; the controller parses the text. Required behavior:

- **coordinator** — two cases. (1) An **incident report** → reply with ONLY a JSON object
  `{"title": "<=8-word title", "facts": [{"title": "...", "detail": "..."}, …]}`, one entry per
  distinct stated fact, no invented facts, no prose, no code fence. (2) A message starting
  `SUMMARY:` → reply verbatim with the text after `SUMMARY:` (status relay).
- **investigator** — two cases decided by the message. (A) *hypotheses*: 2–5 **distinct causal
  claims** that a fact could contradict (never tasks/questions like "check…"/"verify…"), grounded
  **strictly** in the given facts (never invent versions/components/timestamps), one per line, no
  bullets. (B) *diagnostic questions* for one hypothesis: up to 2 of the most discriminating **new**
  yes/no questions, each judged against current facts; reply in blocks of exactly two lines:
  `QUESTION: <yes/no question ending '?', or NONE>` / `ANSWER: YES|NO|UNKNOWN` (ANSWER = what the
  current facts imply; UNKNOWN unless a fact settles it).
- **test_runner** — the falsification checker. Given ONE hypothesis + a NUMBERED fact list, do two
  things: (A) classify **each** fact as `SUPPORTING` / `CONFLICTING` (a **present** fact contradicts
  the mechanism — *absent* data is never conflicting) / `NEUTRAL`; (B) `VERDICT: FALSIFIED` if any
  fact conflicts the mechanism, else `CONSISTENT`. **It never confirms/validates/"proves" — there is
  no "confirmed" outcome.** Reply exactly:
  `SUPPORTING: <nums|none>` / `CONFLICTING: <nums|none>` / `VERDICT: FALSIFIED|CONSISTENT`.

### 4.4 `ace/controller/controller.py` — deterministic RCA protocol (the heart)

Stdlib only, stock `python:3.12`, bind-mounted, no build. Env: `RPC`
(`http://mobkit:8090/console/rpc`), `HEALTH` (`…/healthz`), `POLL_SECONDS`, `ASK_TIMEOUT`,
`ASK_ATTEMPTS`.

**Graph shape (the controller writes all of it):**
```
incident
 ├─ "Facts"       (group, parent->incident)
 │    └─ fact_i             (parent->Facts)
 └─ "Hypotheses"  (group, parent->incident)
      └─ hypothesis_j       (parent->Hypotheses)
           │  hyp --derived_from--> fact   = a SUPPORTING fact   (edge kind carries polarity)
           │  hyp --related-->      fact   = a CONFLICTING fact
           │  hyp --related-->      test   = the test whose answer FALSIFIED this hyp
           └─ test           (parent->hypothesis; open yes/no question)
```
Edges carry no metadata, so **fact polarity is encoded in the edge kind**: `derived_from`
= supporting, `related` = conflicting (both cosmetic; only `parent` drives machine reachability).
There is no unlink RPC, so classify each `(hypothesis, fact)` pair **exactly once** (track it in a
set). Item labels used: `incident`, `fact`, `hypothesis`, `test`, `group`, `reset`, `control`
(each item carries **exactly one** label — see ⚠ W3).

**WorkGraph RPC surface** (all reachable on `/console/rpc`, auto-wired — see ⚠ W1). Method names +
the params that matter (params are **⚠ undocumented** in the RPC reference — take these as truth):
- `mobkit/workgraph/create` `{title (<=120), description, labels:[…], status?}` → `{item:{id,revision,…}}`
- `mobkit/workgraph/link` `{kind, from_id, to_id}` — `kind ∈ {parent, derived_from, related, …}`;
  edge direction is `from → to` (e.g. `parent` link from child to parent).
- `mobkit/workgraph/get` `{id}` → `{item}` (read `revision` before any mutation)
- `mobkit/workgraph/update` `{id, expected_revision, …fields}` (optimistic concurrency — fetch, then send its `revision`)
- `mobkit/workgraph/close` `{id, status, expected_revision}` — `status ∈ {completed, failed, cancelled}`
- `mobkit/workgraph/evidence/add` `{id, expected_revision, evidence:{kind:"summary", id:"<own id>", summary}}`
  — **⚠ W5 (NOT documented): the evidence OBJECT needs its OWN `id`** (an evidence-ref id), separate
  from the item `id`. Treat evidence as best-effort; never let it block a close.
- `mobkit/workgraph/snapshot` `{include_terminal:true}` → `{items:[…], edges:[…]}`
  - **⚠ W4 (vague): terminal items (completed/failed/cancelled) are hidden by default** —
    always pass `include_terminal:true` or you'll think "nothing happened."
  - **⚠ W3 (NOT documented): the `labels` filter is match-ALL (AND), not OR.** `labels:["a","b"]`
    returns only items carrying **both**. Since each item has one label, a multi-label filter returns
    `[]`. Query with **no** label filter (or exactly one) and bucket client-side.

**⚠ M3 — DOC-GAP (NOT documented, MAJOR): a member answers only its FIRST console turn per
session.** A `turn_driven` member runs a real LLM turn only for the **first** `console/send` in its
session. The 2nd/3rd/… each complete **instantly (~1 ms) with an EMPTY `interaction_complete`** (no
text, no LLM call). Also **every** turn emits an immediate empty `interaction_complete` frame first,
then (only on the 1st message) the real text ~1 s later. Nothing in the docs mentions any of this.

The **only reliable fix** is to give the member a fresh session before each query. So the
controller's `ask(identity, content)` is:
```
for attempt in 1..=ASK_ATTEMPTS:
    rpc("mobkit/respawn_member", {member_id: identity})     # fresh session; returns {"accepted":true}
    wait until list_members[identity].status == "active"    # mobkit/list_members
    exclude = { ids of current timeline frames }
    rpc("mobkit/console/send", {identity, origin:"controller",
         idempotency_key, handling_mode:"queue", content})
    poll query_timeline(identity) until a NEW frame with kind=="interaction_complete",
         status=="completed", AND non-empty text  →  return that text   # SKIP the empty-first frame
    (on timeout: respawn + retry)
```
Extract reply text from `frame.payload.message.blocks[]` where `block_type=="text"` → `data.text`
(the reply is in nested blocks, **not** a top-level body).

**Operator messages** all arrive on the **coordinator** timeline as `kind=="user_input"`. Ignore
frames whose `payload.origin` starts with `"controller"` (those are the controller's own sends).
Dispatch:
- `TEST <work_id> YES|NO` → `answer_test`: record `OPERATOR_ANSWER: yes|no` on the test, close it
  `completed`, turn the answer into a new fact, re-assess every open hypothesis; a hypothesis that
  flips `open→failed` in that pass gets `hyp --related--> <this test>` as its named falsifier.
- `NEW INCIDENT …` → force a fresh incident.
- `RESET` (exact word) → **soft reset** (see below).
- **anything else** → first message starts the incident; later free-text **defaults to amending**
  the current incident (add facts + re-assess open hypotheses).

**Protocol per new incident:** parse coordinator JSON → create `incident` + `Facts`/`Hypotheses`
group nodes → create each fact (`parent→Facts`) → ask investigator for hypotheses → create each
(`parent→Hypotheses`) → for each hypothesis `assess_hypothesis` (ask test_runner; create
supporting/conflicting edges; close `failed` if falsified, else leave `open`; **never**
`completed`) → for each `propose_tests` (ask investigator; a question the facts already settle
becomes an answered test closed `completed` with `FACT_ANSWER: yes|no`; an unsettled one stays
`open` with `OPERATOR_ANSWER:` for the operator) → `generate_more_hypotheses` **once** if every
hypothesis is already falsified (ask for a fresh batch grounded in all facts, excluding the ruled-out
titles; assess + test them). *One batch per operator message — never loops.*

**Structured input (skip the LLM).** If an operator message parses as our schema — a JSON object
with a `sources` array whose entries carry a `fact` — build `{title, facts}` deterministically:
`summary` → title (absent in late-evidence → title falls back, amend ignores it anyway); each
`fact` → a fact (`detail` = fact + `(observed <observed_at>)` when present). Ignore extra keys
(`requested_handoff`, `caution`, `kind`, `source_id`, …). This is exactly the shape of
`incident.json` / `late-evidence.json`. Non-JSON free text still goes through the coordinator LLM.

**Busy lock.** Keep a singleton `control`-labelled item whose `description` flips `busy`/`idle`
around every heavy handler (`try/finally`). It's a plain graph write (no LLM turn); the UI reads it
to lock its composer so the operator can't fire a second message mid-generation. The UI must read it
from the **raw, un-epoch-filtered** items so a reset can't hide it.

**Soft reset (⚠ never `mobkit/reset_all`).** There is no delete/unlink RPC, and `reset_all` is
off-limits — last time it took the **whole stack unreachable** (all endpoints returned HTTP 000 and
the foreground process came down); it may not even exist in current source. So RESET is an **epoch
cut**: create a `reset`-labelled marker item (its `created_at` = graph epoch) **and** send a chat
frame with `origin:"controller:reset"` (chat epoch), then drop the in-memory case. The UI hides
every graph item created ≤ the latest reset marker and every chat frame ≤ the last
`controller:reset`. To clear one member instead, use `mobkit/retire_member` /
`mobkit/force_cancel_member`.

**Status voice.** Every controller status line is posted to chat via
`send("coordinator", text, origin="controller:status")` (the UI shows these as the "Operator"
lane; the `controller` origin prefix keeps them from being re-ingested as incidents). Write them
as an informal old-school bearded dev who interjects in French (*bon / voilà / allez / hop /
nickel / attends / dis donc*). Deterministic strings — no LLM.

**Startup order.** The controller must (1) wait for `/healthz` 2xx, then (2) wait until
`mobkit/list_members` shows **all three** members present — only then start watching/prompting.
Starting before `seed` finishes means the first `respawn_member` targets a member that doesn't exist
yet. Create the `control` item (idle) at startup too.

**`post_status` is fire-and-forget.** It sends to `coordinator` purely to inject a chat frame the UI
reads (origin `controller:status`); it does **not** wait for or need a reply, and the coordinator
turn it triggers is a harmless empty frame (2nd+ turn — see ⚠ M3). Only `ask()` needs a real reply,
and `ask()` **always respawns first**, so any number of status posts never disturb the next parse.
Wrap `post_status` in try/except — a status failure must never break the protocol.

**Coordinator-parse fallback.** If the coordinator reply isn't valid JSON (or `ask` times out),
fall back to a regex heuristic that splits an `INCIDENT: … FACTS: (1)… (2)…`-style report into a
title + facts, so an incident is never lost to a bad parse.

**Restart resilience.** On startup, rebuild the in-memory `Case` from the graph snapshot (respecting
the latest reset epoch) so a controller-only restart keeps `TEST` answers mapping to the right items.

### 4.5 `ace/seed/seed.py` — roster seeder

**⚠ M1 — DOC-GAP (vague): non-orchestrator profiles are NOT auto-spawned.** On boot the library
host reconciles **only** the orchestrator (`coordinator`) + `[wiring]`. The docs discuss
`ensure_member` (`concepts/roster`) but never say *when/why* you need it. So this one-shot service,
once the host is healthy, calls for **each** of `coordinator`, `investigator`, `test_runner`:
```
mobkit/ensure_member {member_id:<n>, role:<n>, agent_identity:<n>, profile:<n>, runtime_mode:"turn_driven"}
```
(`role` == the mob.toml profile name). `ensure_member` is **idempotent** (upsert/resume of a durable
identity) → safe on every `up`. Then call `mobkit/reconcile_edges {}` and log `mobkit/list_members {}`
for confirmation. Always `exit 0` so a partial failure never wedges `compose up`. Wait for `/healthz`
2xx before starting.

### 4.6 nginx — add these `location`s + mounts to the given server block

Volume mounts (compose): `webui/ → /usr/share/nginx/html/` (read-only); and — **⚠ gotcha** — the
demo payloads must mount into a **separate** dir, **not** nested under the read-only html mount
(runc errors `read-only file system` creating a nested mountpoint): `incident.json →
/payloads/incident.json`, `late-evidence.json → /payloads/late-evidence.json` (read-only).

Locations:
```nginx
location / { limit_except GET { deny all; } root /usr/share/nginx/html; index index.html; }

location = /incident.json      { root /payloads; }
location = /late-evidence.json { root /payloads; }

location /console {                       # SPA + JSON-RPC + SSE; forward path unchanged (no URI on proxy_pass)
    proxy_pass http://mobkit:8090;
    proxy_set_header Host $host;
    proxy_http_version 1.1;
    proxy_set_header Connection "";       # SSE at /console/timeline/stream: keep open + unbuffered
    proxy_buffering off;
    proxy_read_timeout 3600s; proxy_send_timeout 3600s;
}
location /blobs/ { proxy_pass http://mobkit:8090; proxy_set_header Host $host; }
location = /healthz { proxy_pass http://mobkit:8090; }
```
(POST is needed on `/console/rpc`, so no `limit_except GET` there.) **Do not** add the old `/llm/`
location — that was for the parked local model, which the final product does not run.

### 4.7 `ace/webui/index.html` — single vanilla page (no build, no framework)

Plain HTML/CSS/JS, same-origin (no CORS). Polls `POST /console/rpc` every ~1.5 s with
`mobkit/workgraph/snapshot {include_terminal:true}` **and**
`mobkit/console/query_timeline {identity:"coordinator"}`, renders entirely client-side. Live on
browser refresh (bind-mounted).

Match **`UI_v1.png`** for the core — **Hypotheses** panel (`(status) Hnn (support | conflict)
title`, colored open=blue / failed=red; a failed row shows a "falsified by: Tnn (Yes/No)" or
"falsified by: stated facts" sub-line), **Tests** panel (`Tnn (Hnn) question (Pending|Yes|No)`,
newest first, only open tests selectable), the **YES / NO** ovals, and the **Operator chat** (User
lane + Operator lane) + composer + SEND. Then **add** what the wireframe predates:
- a leftmost **Facts** column (~10% width, read-only, numbered `F001…`);
- a **RESET** button in the top bar (sends the bare word `RESET`; confirm first);
- a **SUB AGENT** button next to YES/NO (enabled only with an open test selected) that plays a
  full-screen rickroll gif splash — an intentional gag; keep it;
- the **busy lock** (disable composer + YES/NO/SUB/RESET while the `control` item reads `busy`,
  plus a short optimistic client lock covering the gap before `busy` lands);
- the **guided preload**: `fetch('/incident.json')` and `/late-evidence.json`; on a fresh/empty
  board (first load or after RESET) preload the composer with the incident text; sending it
  preloads the late-evidence text; sending that leaves it blank;
- a small live status line (connection dot + `N facts, N hyp, N tests` / `working…`).

Client-side model rules the UI depends on: synthesize friendly ids (`F/H/T` + zero-padded) by
`created_at` order; read `hyp→fact` edge **kind** for supporting (`derived_from`) vs conflicting
(`related`) counts and `hyp→test related` for falsifiers (tell fact-vs-test targets apart by the
target item's label); chat lanes are `kind=="user_input"` frames split by `payload.origin`
(`console*` → User, `controller:status` → Operator — both are injected user_input frames, *not*
assistant replies; ignore `interaction_complete` frames in the chat); apply the **reset epoch** filter (hide graph items ≤ latest `reset` marker, chat ≤ last
`controller:reset`); read `busy` from the raw items. Test answer is parsed from the test
description: `(OPERATOR|FACT)_ANSWER: (yes|no)`.

### 4.8 `ace/compose.yaml`

- **`nginx`**: given; add the volume mounts from §4.6; `depends_on: [mobkit]`.
- **`mobkit`**: `build: ./mobkit-host`, `pull_policy: missing` (reuse the heavy Rust image; rebuild
  explicitly with `docker compose build mobkit`), `expose: ["8090"]` (not published),
  env `OPENAI_API_KEY=${OPENAI_API_KEY:?…}` + `RUST_LOG`, bind-mount `config.toml` + `mob.toml` at
  `/app`, and **`/srv/mobkit/state` as a `tmpfs`** so every `--force-recreate` is a clean slate
  (swap to a named volume if you ever want durable incidents).
- **`controller`**: `image: python:3.12`, bind-mount `controller.py`, env `RPC`/`HEALTH`,
  `depends_on: [mobkit]`, `restart: unless-stopped`.
- **`seed`**: `image: python:3.12-slim`, bind-mount `seed.py`, env `RPC`/`HEALTH`,
  `depends_on: [mobkit]`, `restart: "no"` (one-shot).
- `cap_drop: [ALL]` everywhere; `logging: journald` to taste.

---

## 5. ⚠ Blockers & doc-gaps — quick reference

Each cost real time; the doc verdict says whether you'll find it in docs.rkat.ai.

| # | Blocker | Correct solution | Doc verdict |
|---|---------|------------------|-------------|
| **H1** | Bundled console binaries bind loopback only (`bind_loopback()` = 127.0.0.1:0, the only constructor; no flag/env/config override — verified 0.8.28) → unreachable from nginx, no config-only fix | Compile a small library host: `builder…build()` → `runtime.serve(listener, decisions)` (or `build_reference_app_router` + `axum::serve`) on `0.0.0.0:8090` | **Partial** — the *how* is exemplified (`library_mode_reference.rs`); the *why you're forced* (bundled binaries can't re-bind) is undocumented |
| **H2** | `build_runtime_decision_state` validators crash startup | `trusted_mobkit_toml="modules = []"`; release targets = the 4 canonical; JWKS ≥1 key; audience non-empty | **Partial** — TOML+targets documented; **JWKS + audience undocumented** |
| **H3** | No tracing subscriber ⇒ all meerkat errors silently dropped | Install `tracing_subscriber::fmt().with_env_filter(…)` in `main`; set `RUST_LOG`; know `set_error_hook` exists | **Not documented** |
| **M1** | Worker profiles not auto-spawned on boot | `mobkit/ensure_member` each (idempotent) + `reconcile_edges` | **Vague** — `ensure_member` shown, the *why/when* isn't |
| **M2** | `tools.comms=false` hard-rejected | Every member profile `comms=true` (the floor) | **Not documented** |
| **M3** | **Member answers only its FIRST console turn/session; 2nd+ = empty instant frame; each turn emits an empty frame first** | `respawn_member` → wait `active` → `console/send` → read the first **non-empty** `interaction_complete`. Retry. | **Not documented (major)** |
| **M4** | `mobkit/reset_all` takes the whole stack down (may not even exist) | Never call it. Do a **soft-reset epoch**; use `retire_member`/`force_cancel_member` to clear one | **Not documented** |
| **W1** | WorkGraph namespace grant | **Auto-issued** by `builder().persistent_state(dir)`; just enable it — no manual grant call | **Misleading** — docs imply *you* must issue a grant |
| **W2** | OpenAI 400s tools with a **top-level** `oneOf/anyOf/allOf/not/enum/const`; meerkat's `workgraph_claim` schema has a top-level `not`; **meerkat's `openai_compatible` adapter does NOT lower schemas** (only anthropic/gemini do) | Final design's agents are **comms-only**, so no agent is ever offered `workgraph_claim` → **you can point straight at `api.openai.com`.** *Only* if you ever give an agent the `workgraph`/tool groups on OpenAI must you reintroduce a proxy that strips forbidden top-level keys from `tools[].function.parameters`. | **Not documented — undocumented defect** (confirmed in source) |
| **W3** | `labels` filter is match-ALL (AND) | Query with no label filter (or one) and bucket client-side | **Not documented** |
| **W4** | Terminal items hidden by default | Pass `include_terminal:true` on list/snapshot | **Vague** |
| **W5** | `evidence/add` needs an inner `id` on the evidence object | `evidence:{kind, id:"<own>", summary}` + item `id` + `expected_revision`; treat as best-effort | **Not documented** |
| build | glibc mismatch | Pin both Docker stages to **trixie**; runtime needs `ca-certificates` | general (failure mode `GLIBC_2.39 not found` is non-obvious) |
| build | OOM at `opt-level=3` | `[profile.release] opt-level = 1`; bounded `CARGO_BUILD_JOBS` | general |
| build | stale cached binary after base-image change | bump cache-mount `id=` / prune exec.cachemount | general |
| OpenAI | exhausted/empty key fails **silently** (turns stick at `delivered`) | check OpenAI credit first when turns never complete | provider quirk |

**Docs worth reading (they exist and are correct for the standard parts):**
`guides/unified-runtime`, `reference/decisions`, `concepts/roster`, `concepts/workgraph`,
`api/rpc`, `sdks/rust`, and the `llms.txt` index. Source of truth for the ⚠ items:
`lukacf/meerkat` (`meerkat-workgraph/src/tools.rs` `claim_schema`, `meerkat-openai/src/client_compatible.rs`,
`service_factory.rs`) and `lukacf/meerkat-mobkit` (`unified_runtime/*`, `mob_methods.rs`).

---

## 6. Deploy & verify (hand the user the commands — you do not run them)

Build once, then bring up the final set (no `-d`, no `--no-deps`; compose resolves deps):
```
docker compose build mobkit
docker compose up --force-recreate --no-build mobkit nginx controller seed
```
Because state is a tmpfs, this is a clean slate; `seed` re-materializes the roster and exits.

**Ready-to-paste smoke-test incident** (paste into the UI composer, or `console/send` to
`coordinator`) — several competing facts so the investigator generates rival hypotheses and
`test_runner` has something to falsify:
```
INCIDENT: Public checkout API returning 503s and p99 latency up 6x since 15:20 UTC. FACTS: (1) release v2.7.0 deployed at 15:18; (2) DB connection pool exhausted (100/100) from 15:22; (3) app CPU/memory flat, no host alarms; (4) logs show "timeout acquiring connection" from the checkout service only.
```
(Or just open the UI on an empty board and use the **guided preload** — it pre-fills `incident.json`,
then `late-evidence.json`.)

**Verify:**
1. `GET /healthz` → 2xx.
2. `mobkit/list_members` shows all three (`coordinator`, `investigator`, `test_runner`) `active`.
3. After the incident: `mobkit/workgraph/snapshot {include_terminal:true}` shows `incident` +
   `Facts`/`Hypotheses` groups + facts + hypotheses (each `parent`-linked correctly) + tests, with
   **no hypothesis in a `completed` state**, and at least one hypothesis `failed` iff a stated fact
   contradicts it.
4. The UI renders Facts / Hypotheses (with support|conflict counts) / Tests, chat shows the
   French-voiced Operator status lines, answering a test via YES/NO adds a fact and re-evaluates,
   and RESET clears the board to a fresh epoch.
```
