#!/usr/bin/env python3
"""Seed the RCA mob roster once the mobkit host is healthy.

Why this exists
---------------
The library host (mobkit-host/src/main.rs) reconciles ONLY the mob.toml
orchestrator (`coordinator`) + wiring on boot -- it does not auto-spawn the
other profiles. So `investigator` and `test_runner` are materialized here, at
`docker compose up` time, via the console JSON-RPC `mobkit/ensure_member`.

`ensure_member` is idempotent (upsert/resume of a durable identity), so it is
safe to re-run on every restart; existing members just resume. Members are
created `turn_driven` (no kickoff; the controller respawns per query), matching the profiles in mob.toml.

Stdlib only -- runs on stock python:3.12-slim, no build, bind-mounted like the
llmshim sidecar. Config via env:
  RPC     JSON-RPC endpoint   (default http://mobkit:8090/console/rpc)
  HEALTH  health probe URL    (default http://mobkit:8090/healthz)

The script logs every response and always exits 0 so a partial failure never
wedges the compose `up`; check its output to confirm the roster.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

RPC = os.environ.get("RPC", "http://mobkit:8090/console/rpc")
HEALTH = os.environ.get("HEALTH", "http://mobkit:8090/healthz")

# Durable identities to ensure. The orchestrator (coordinator) is already
# reconciled on boot; re-ensuring it is a harmless idempotent resume that also
# pins turn_driven. role == profile name in mob.toml.
MEMBERS = ["coordinator", "investigator", "test_runner"]


def log(msg):
    sys.stdout.write("seed: %s\n" % msg)
    sys.stdout.flush()


def rpc(method, params):
    """POST a single JSON-RPC call; return the parsed response dict."""
    body = json.dumps(
        {"jsonrpc": "2.0", "id": method, "method": method, "params": params}
    ).encode("utf-8")
    req = urllib.request.Request(
        RPC, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def wait_healthy(timeout_s=180):
    """Block until /healthz answers 2xx, or give up after timeout_s."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(HEALTH, timeout=5) as resp:
                if 200 <= resp.status < 300:
                    log("host healthy (%s)" % HEALTH)
                    return True
        except (urllib.error.URLError, OSError) as e:
            log("waiting for host... (%s)" % e)
        time.sleep(3)
    log("gave up waiting for host after %ss" % timeout_s)
    return False


def ensure(name):
    """Ensure one durable, turn_driven member (controller respawns it per query). Tolerate per-member errors."""
    params = {
        "member_id": name,
        "role": name,            # role == mob.toml profile name
        "agent_identity": name,
        "profile": name,
        "runtime_mode": "turn_driven",
    }
    try:
        resp = rpc("mobkit/ensure_member", params)
    except (urllib.error.URLError, OSError, ValueError) as e:
        log("ensure_member %s FAILED to call: %s" % (name, e))
        return
    if resp.get("error"):
        log("ensure_member %s -> error: %s" % (name, json.dumps(resp["error"])))
    else:
        log("ensure_member %s -> ok: %s" % (name, json.dumps(resp.get("result"))))


def main():
    log("RPC=%s HEALTH=%s" % (RPC, HEALTH))
    if not wait_healthy():
        sys.exit(0)  # never wedge compose up

    for name in MEMBERS:
        ensure(name)

    # Apply mob.toml [wiring].role_wiring against the now-complete roster.
    try:
        resp = rpc("mobkit/reconcile_edges", {})
        log("reconcile_edges -> %s" % json.dumps(resp.get("result", resp.get("error"))))
    except (urllib.error.URLError, OSError, ValueError) as e:
        log("reconcile_edges call failed: %s" % e)

    # Roster readback for the log.
    try:
        resp = rpc("mobkit/list_members", {})
        log("list_members -> %s" % json.dumps(resp.get("result", resp.get("error"))))
    except (urllib.error.URLError, OSError, ValueError) as e:
        log("list_members call failed: %s" % e)

    log("done")
    sys.exit(0)


if __name__ == "__main__":
    main()
