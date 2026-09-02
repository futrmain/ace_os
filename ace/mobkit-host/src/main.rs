//! Rust *library host* for the MobKit console gateway, driving meerkat agents on
//! a self-hosted llama.cpp LLM.
//!
//! Why this shape (see https://docs.rkat.ai/mobkit/quickstart): the bundled
//! `mobkit_gateway`/`rpc_gateway` binaries bind an available **loopback** port and
//! advertise the URL through a discovery registry -- perfect for a local SDK, but
//! unreachable from nginx running in a *separate* compose container. The docs note
//! "a library host may choose its own address", so this host builds the very same
//! reference console app (`UnifiedRuntime::build_reference_app_router`) and serves
//! it on 0.0.0.0:8090 with a plain listener. nginx proxies it at `/console/`.
//!
//! Config comes from two mounted files:
//!   /app/config.toml  -- meerkat runtime config ([self_hosted.*] + [realm.*])
//!   /app/mob.toml     -- the mob definition (profiles reference model "dev")

use std::time::Duration;

use meerkat::Config;
use meerkat_mobkit::{
    AuthPolicy, BigQueryNaming, ConsolePolicy, RuntimeDecisionInputs, RuntimeOpsPolicy,
    TrustedOidcRuntimeConfig, UnifiedRuntime, build_runtime_decision_state,
};

/// Bind all interfaces (not loopback) so nginx in another container can reach us.
const BIND_ADDR: &str = "0.0.0.0:8090";
const RUNTIME_ID: &str = "ace-mobkit";

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    // --- tracing subscriber ---
    // meerkat logs through `tracing`; with no global subscriber installed those
    // events are dropped, so the container shows only our println! lines. Install
    // an fmt subscriber honoring RUST_LOG (set in compose), defaulting to info.
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")),
        )
        .init();

    // --- meerkat runtime config: self-hosted llama server + model + binding ---
    // Start from defaults, then layer our [self_hosted.*] + [realm.*] on top.
    let cfg_toml = std::fs::read_to_string("/app/config.toml")?;
    let mut meerkat_config = Config::default();
    meerkat_config.merge_toml_str(&cfg_toml)?;

    // --- build the unified runtime straight from the mob definition ---
    // The convenience builder wires the session service and mob storage; with
    // persistent_state it also opens the SQLite/runtime/blob stores under the
    // state dir. The mob.toml orchestrator is reconciled via [wiring].
    let runtime = UnifiedRuntime::builder()
        .definition_path("/app/mob.toml")
        .persistent_state("/srv/mobkit/state")
        .meerkat_config(meerkat_config)
        .timeout(Duration::from_secs(60))
        .build()
        .await?;
    println!("mobkit runtime built from /app/mob.toml");

    // --- console/admin policy ---
    // require_app_auth = false: the console is served over PLAIN HTTP via nginx.
    // Do NOT expose this to the open internet without turning require_app_auth on
    // and wiring a real AuthPolicy / OIDC. read_only = false (ConsolePolicy
    // default) lets the console actually drive the mob.
    let decisions = build_runtime_decision_state(RuntimeDecisionInputs {
        bigquery: BigQueryNaming {
            dataset: "tux_local".to_string(),
            table: "runtime_events".to_string(),
        },
        // No trusted subprocess modules. The field is not serde(default), so an
        // empty string would fail to parse -- an explicit empty array is required.
        trusted_mobkit_toml: "modules = []".to_string(),
        auth: AuthPolicy::default(),
        trusted_oidc: TrustedOidcRuntimeConfig {
            discovery_json:
                r#"{"issuer":"https://noop.example.com","jwks_uri":"https://noop.example.com/.well-known/jwks.json"}"#
                    .to_string(),
            // Validator rejects an empty key set (MissingKeys) even with auth
            // off, so supply one dummy HS256 key (the crate's own test fixture).
            jwks_json:
                r#"{"keys":[{"kid":"kid-current","kty":"oct","alg":"HS256","k":"cGhhc2U3LXRydXN0ZWQtY3VycmVudC1zZWNyZXQ"}]}"#
                    .to_string(),
            audience: RUNTIME_ID.to_string(),
        },
        console: ConsolePolicy {
            require_app_auth: false,
            ..ConsolePolicy::default()
        },
        ops: RuntimeOpsPolicy::default(),
        // The decision-state validator requires the canonical release targets
        // (crates.io/npm/pypi/github-releases); a placeholder fails with
        // MissingReleaseTarget. This mirrors the crate's own release-targets.json.
        release_metadata_json:
            r#"{"targets":["crates.io","npm","pypi","github-releases"],"support_matrix":"same-as-meerkat"}"#
                .to_string(),
    })?;

    // --- serve the reference console app on our own address ---
    // Routes: GET /console (React UI) + /console/assets/*, POST /console/rpc
    // (JSON-RPC), SSE at /console/timeline/stream, blobs at /blobs/{id},
    // GET /healthz (public).
    let app = runtime.build_reference_app_router(decisions);
    let listener = tokio::net::TcpListener::bind(BIND_ADDR).await?;
    println!("mobkit console serving on http://{BIND_ADDR}/console");
    axum::serve(listener, app).await?;
    Ok(())
}
