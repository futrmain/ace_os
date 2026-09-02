#!/usr/bin/env python3
"""Deterministic RCA controller -- the SOLE WorkGraph writer.

The mob agents (coordinator/investigator/test_runner) are pure reasoning
functions with NO workgraph tools. This controller runs the RCA protocol and
makes every graph mutation itself, so "edges are always correct" and
"test_runner can never validate a hypothesis" are code invariants, not prompts.

GRAPH SHAPE
    incident
      +-- "Facts"       (group)  parent->incident
      |     +-- fact_i            parent->Facts
      +-- "Hypotheses"  (group)  parent->incident
            +-- hypothesis_j      parent->Hypotheses
                  |  (hyp --derived_from--> fact)   = a SUPPORTING fact
                  |  (hyp --related-->      fact)   = a CONFLICTING fact
                  |  (hyp --related-->      test)   = the test whose answer FALSIFIED this hyp
                  +-- test         parent->hypothesis   (open yes/no question)

Edges carry no metadata and only `parent`/`blocks` drive machine reachability,
so fact polarity is encoded in the *edge kind*: `derived_from`=supporting,
`related`=conflicting (both cosmetic). There is no unlink, so each
(hypothesis, fact) pair is classified exactly once (tracked in `Case.edged`).

OPERATOR MESSAGES (all arrive on the coordinator timeline, origin != controller)
  1. `TEST <work_id> YES|NO`  -> answer a diagnostic test:
       record the answer on the test item, close it, turn the answer into a new
       fact, and re-evaluate every open hypothesis against the fuller fact set.
  2. `NEW INCIDENT ...`       -> force a fresh incident.
  3. anything else            -> FIRST message starts an incident; later ones
       DEFAULT to adding facts to the current incident + re-evaluating.

On a NEW incident each hypothesis is (a) classified against the stated facts
(supporting/conflicting edges), (b) given a falsification verdict against the
full fact set -- closed `failed` if a present fact contradicts its mechanism,
else left `open` (NEVER `completed`) -- and (c) given 1-2 open yes/no diagnostic
questions for the operator to answer.

After every operator message the controller posts a one-line human summary back
to the chat via console/send with origin `controller:status` (the UI renders
those as the "Operator" lane; is_operator_incident ignores them so they are not
re-ingested).

Stdlib only; runs on stock python:3.12; bind-mounted, no build. Config via env:
  RPC (default http://mobkit:8090/console/rpc), HEALTH, POLL_SECONDS,
  ASK_TIMEOUT, ASK_ATTEMPTS.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

RPC = os.environ.get("RPC", "http://mobkit:8090/console/rpc")
HEALTH = os.environ.get("HEALTH", "http://mobkit:8090/healthz")
POLL = float(os.environ.get("POLL_SECONDS", "2"))
AGENTS = ["coordinator", "investigator", "test_runner"]


def log(msg):
    sys.stdout.write("controller: %s\n" % msg)
    sys.stdout.flush()


def rpc(method, params):
    body = json.dumps(
        {"jsonrpc": "2.0", "id": method, "method": method, "params": params}
    ).encode("utf-8")
    req = urllib.request.Request(
        RPC, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        d = json.loads(resp.read().decode("utf-8"))
    if d.get("error"):
        raise RuntimeError("%s -> %s" % (method, json.dumps(d["error"])))
    return d.get("result")


# --------------------------------------------------------------------------
# Timeline / request-reply over the console
# --------------------------------------------------------------------------
def timeline(identity):
    try:
        return rpc("mobkit/console/query_timeline", {"identity": identity}).get(
            "frames", []
        )
    except (urllib.error.URLError, OSError, RuntimeError):
        return []


def frame_text(frame):
    msg = (frame.get("payload", {}) or {}).get("message", {}) or {}
    parts = [
        b.get("data", {}).get("text", "")
        for b in (msg.get("blocks") or [])
        if b.get("block_type") == "text"
    ]
    return "\n".join(p for p in parts if p).strip()


def _reply_after(identity, exclude_ids, timeout):
    """Poll until a NEW assistant reply (interaction_complete) appears."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(POLL)
        for f in timeline(identity):
            if (
                f.get("id") not in exclude_ids
                and f.get("kind") == "interaction_complete"
                and f.get("status") == "completed"
            ):
                txt = frame_text(f)
                if txt:
                    return txt
    raise TimeoutError("no reply from %s within %ss" % (identity, timeout))


def send(identity, content, origin="controller"):
    key = "ctl-%s-%d" % (identity, int(time.time() * 1000))
    rpc(
        "mobkit/console/send",
        {
            "identity": identity,
            "origin": origin,
            "idempotency_key": key,
            "handling_mode": "queue",
            "content": content,
        },
    )


def post_status(text):
    """Post a human-readable status line to the chat (UI "Operator" lane).

    origin `controller:status` -> is_operator_incident() skips it (starts with
    "controller"), so it is displayed but never re-ingested as an incident.
    """
    try:
        send("coordinator", text, origin="controller:status")
        log("STATUS: %s" % text)
    except (urllib.error.URLError, OSError, RuntimeError) as e:
        log("post_status failed (non-fatal): %s" % e)


ASK_TIMEOUT = float(os.environ.get("ASK_TIMEOUT", "40"))
ASK_ATTEMPTS = int(os.environ.get("ASK_ATTEMPTS", "3"))


def respawn(identity):
    """Give the member a fresh runtime incarnation (a new session)."""
    try:
        rpc("mobkit/respawn_member", {"member_id": identity})
    except (urllib.error.URLError, OSError, RuntimeError) as e:
        log("respawn %s failed (non-fatal): %s" % (identity, e))


def wait_member_active(identity, timeout=25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            members = {m.get("agent_identity"): m for m in rpc("mobkit/list_members", {})}
            m = members.get(identity)
            if m and m.get("status") == "active":
                return True
        except (urllib.error.URLError, OSError, RuntimeError):
            pass
        time.sleep(1)
    return False


def ask(identity, content):
    """Prompt an agent and block for its text reply.

    A mob member answers only its FIRST console turn per session (a 2nd message
    returns empty; autonomous_host's kickoff also eats the slot). So we RESPAWN
    the member first -> fresh session -> our prompt is its first message. Each
    turn also emits an instant empty interaction_complete before the real text,
    which `_reply_after` skips. Retry the whole cycle up to ASK_ATTEMPTS times.
    """
    last = None
    for attempt in range(1, ASK_ATTEMPTS + 1):
        respawn(identity)
        wait_member_active(identity)
        exclude = {f.get("id") for f in timeline(identity)}
        send(identity, content)
        try:
            return _reply_after(identity, exclude, ASK_TIMEOUT)
        except TimeoutError as err:
            last = err
            log("no text reply from %s (attempt %d/%d); respawning + retry" % (identity, attempt, ASK_ATTEMPTS))
    raise last or TimeoutError("no reply from %s" % identity)


# --------------------------------------------------------------------------
# WorkGraph writes (controller is the ONLY writer)
# --------------------------------------------------------------------------
def wg_create(title, description, labels, status=None):
    params = {"title": title[:120], "description": description, "labels": labels}
    if status:
        params["status"] = status
    return rpc("mobkit/workgraph/create", params)["item"]


def wg_link(kind, from_id, to_id):
    rpc("mobkit/workgraph/link", {"kind": kind, "from_id": from_id, "to_id": to_id})


def wg_get(item_id):
    return rpc("mobkit/workgraph/get", {"id": item_id})["item"]


def wg_update(item_id, **fields):
    item = wg_get(item_id)
    params = {"id": item_id, "expected_revision": item["revision"]}
    params.update(fields)
    return rpc("mobkit/workgraph/update", params)["item"]


def wg_close(item_id, status):
    item = wg_get(item_id)
    rpc(
        "mobkit/workgraph/close",
        {"id": item_id, "status": status, "expected_revision": item["revision"]},
    )


def wg_add_evidence(item_id, summary):
    # The evidence OBJECT itself requires an `id` (the evidence-ref id), not just
    # the top-level item id. Non-fatal: evidence is nice-to-have; a failure here
    # must never block the verdict close.
    try:
        item = wg_get(item_id)
        rpc(
            "mobkit/workgraph/evidence/add",
            {
                "id": item_id,
                "expected_revision": item["revision"],
                "evidence": {
                    "kind": "summary",
                    "id": "rca-verdict-%s" % item_id[-8:],
                    "summary": summary[:500],
                },
            },
        )
    except (urllib.error.URLError, OSError, RuntimeError) as e:
        log("add_evidence on %s failed (non-fatal): %s" % (item_id[-6:], e))


def wg_snapshot():
    return rpc("mobkit/workgraph/snapshot", {"include_terminal": True})


# --------------------------------------------------------------------------
# Busy/idle signal for the UI. A single "control" WorkGraph item whose
# description is "busy" while the controller is mid-message and "idle" otherwise.
# It is just a graph write (NO LLM turn), and the UI already polls the snapshot,
# so it locks its composer while this reads "busy" -- stopping the operator from
# firing a second message into the middle of a long generation.
# --------------------------------------------------------------------------
CONTROL_ID = None


def control_item_id():
    global CONTROL_ID
    if CONTROL_ID:
        return CONTROL_ID
    try:
        for it in wg_snapshot().get("items", []):
            if (it.get("labels") or [None])[0] == "control":
                CONTROL_ID = it["id"]
                return CONTROL_ID
        CONTROL_ID = wg_create("controller", "idle", ["control"])["id"]
    except (urllib.error.URLError, OSError, RuntimeError) as e:
        log("control item init failed (non-fatal): %s" % e)
        CONTROL_ID = None
    return CONTROL_ID


def set_working(on):
    """Flip the control item's description to busy/idle (the UI reads it to lock)."""
    cid = control_item_id()
    if not cid:
        return
    try:
        wg_update(cid, description=("busy" if on else "idle"))
    except (urllib.error.URLError, OSError, RuntimeError) as e:
        log("set_working(%s) failed (non-fatal): %s" % (on, e))


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------
def extract_json(text):
    """Pull the first {...} JSON object out of an agent reply."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        obj = json.loads(text[start : end + 1])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def heuristic_facts(incident_text):
    """Fallback: split an 'INCIDENT: ... FACTS: (1).. (2)..' style report."""
    title = "Incident"
    m = re.search(r"incident[:\-]\s*(.+?)(?:\.|;|\bfacts?\b|$)", incident_text, re.I)
    if m:
        title = m.group(1).strip()[:80] or "Incident"
    facts_part = re.split(r"\bfacts?\b[:\-]?", incident_text, maxsplit=1, flags=re.I)
    tail = facts_part[1] if len(facts_part) > 1 else incident_text
    chunks = re.split(r"\(\d+\)|;|\n|(?<=\.)\s+(?=[A-Z(])", tail)
    facts = [{"title": c.strip(" .;-")[:100], "detail": c.strip()} for c in chunks if c and len(c.strip(" .;-")) > 4]
    return {"title": title, "facts": facts[:8] or [{"title": incident_text[:80], "detail": incident_text}]}


def parse_facts(text, fallback_text):
    """Parse a coordinator reply into (title, [facts]); heuristic on failure."""
    data = extract_json(text) or heuristic_facts(fallback_text)
    facts = data.get("facts") or heuristic_facts(fallback_text)["facts"]
    title = (data.get("title") or "Incident")[:80]
    return title, facts


def structured_input(text):
    """If the operator message is our JSON schema -- an object with a `sources`
    list whose entries carry a `fact` -- convert it to the internal
    {title, facts:[{title, detail}]} shape so we can skip the LLM parse. Only the
    summary and the source facts matter; extra keys (requested_handoff, caution,
    kind, ...) are ignored. Returns None if the text is not that schema.

    Works for both incident.json (has `summary`) and late-evidence.json (no
    summary -- title falls back, and amend_incident ignores the title anyway)."""
    obj = extract_json(text or "")
    if not isinstance(obj, dict) or not isinstance(obj.get("sources"), list):
        return None
    facts = []
    for s in obj["sources"]:
        if not isinstance(s, dict):
            continue
        fact = (s.get("fact") or "").strip()
        if not fact:
            continue
        when = (s.get("observed_at") or "").strip()
        detail = "%s (observed %s)" % (fact, when) if when else fact
        facts.append({"title": fact[:120], "detail": detail})
    if not facts:
        return None
    title = (obj.get("summary") or obj.get("incident_id") or "Incident").strip()[:80]
    return {"title": title, "facts": facts}


def clean_lines(text, limit):
    lines = []
    for raw in text.splitlines():
        s = raw.strip().lstrip("-*0123456789. )\t").strip()
        if len(s) > 6:
            lines.append(s)
    return lines[:limit]


def parse_nums(text, key):
    """Pull the number list after a 'KEY:' label out of an assess reply."""
    m = re.search(r"%s\s*:\s*([0-9,\s]*)" % key, text, re.I)
    if not m:
        return []
    return [int(n) for n in re.findall(r"\d+", m.group(1))]


# --------------------------------------------------------------------------
# A live "case" = one incident and everything hanging off it.
# --------------------------------------------------------------------------
class Case:
    def __init__(self, incident_id, facts_group_id, hyps_group_id, title):
        self.incident_id = incident_id
        self.facts_group_id = facts_group_id
        self.hyps_group_id = hyps_group_id
        self.title = title
        self.facts = []   # list of (title, id)  -- 1-based order is the assess numbering
        self.hyps = []    # list of dicts {id, title, status}
        self.tests = {}   # test_id -> {"hyp_id":.., "question":..}
        self.edged = set()  # (hyp_id, fact_id) pairs that already have a polarity edge
        self.falsif_edged = set()  # (hyp_id, test_id) pairs already linked as falsifier

    def facts_block(self):
        return "\n".join("%d. %s" % (i + 1, t) for i, (t, _) in enumerate(self.facts))

    def open_hyps(self):
        return [h for h in self.hyps if h["status"] == "open"]


def add_facts(case, facts):
    """Create fact items under the case's Facts group; return the new ones."""
    added = []
    for f in facts:
        ft = (f.get("title") or "").strip() or (f.get("detail") or "fact")[:80]
        fi = wg_create(ft, f.get("detail", ""), ["fact"])
        wg_link("parent", fi["id"], case.facts_group_id)  # fact -> Facts group
        case.facts.append((ft, fi["id"]))
        added.append(ft)
    return added


def assess_hypothesis(case, hyp):
    """Classify facts as supporting/conflicting + get a falsification verdict.

    Creates `derived_from` (supporting) / `related` (conflicting) edges from the
    hypothesis to each fact -- once per (hyp, fact) pair (append-only edges).
    Closes the hypothesis `failed` if a PRESENT fact contradicts its mechanism.
    NEVER `completed`. Returns True if it just became falsified.
    """
    h = hyp["title"]
    try:
        reply = ask(
            "test_runner",
            "Hypothesis: %s\nFacts (numbered):\n%s\n\n"
            "Classify EACH numbered fact for THIS hypothesis as SUPPORTING (makes "
            "it more likely), CONFLICTING (a PRESENT fact contradicts its "
            "mechanism), or NEUTRAL (irrelevant or merely absent data). Then give "
            "a VERDICT: FALSIFIED if any fact CONFLICTS the mechanism, else "
            "CONSISTENT. You NEVER confirm a hypothesis.\n"
            "Reply EXACTLY three lines:\nSUPPORTING: <nums or none>\n"
            "CONFLICTING: <nums or none>\nVERDICT: FALSIFIED|CONSISTENT"
            % (h, case.facts_block()),
        )
    except TimeoutError:
        reply = "SUPPORTING:\nCONFLICTING:\nVERDICT: CONSISTENT"
        log("  no assessment for '%s...'; treating as consistent" % h[:32])

    def link_facts(nums, kind):
        for n in nums:
            if 1 <= n <= len(case.facts):
                fid = case.facts[n - 1][1]
                if (hyp["id"], fid) not in case.edged:
                    wg_link(kind, hyp["id"], fid)  # hyp -> fact (from=hyp, to=fact)
                    case.edged.add((hyp["id"], fid))

    link_facts(parse_nums(reply, "SUPPORTING"), "derived_from")
    link_facts(parse_nums(reply, "CONFLICTING"), "related")

    falsified = bool(re.search(r"VERDICT\s*:\s*FALSIFIED", reply, re.I))
    if falsified and hyp["status"] == "open":
        wg_add_evidence(hyp["id"], reply.strip()[:500])
        wg_close(hyp["id"], "failed")  # controller ONLY ever sets failed / open
        hyp["status"] = "failed"
        log("  '%s...' -> FAILED" % h[:40])
        return True
    return False


def open_test_for(case, hyp_id):
    for tid, t in case.tests.items():
        if t.get("hyp_id") == hyp_id and t.get("status") == "open":
            return tid
    return None


def asked_questions(case, hyp_id):
    return [t.get("question", "") for t in case.tests.values() if t.get("hyp_id") == hyp_id]


def propose_tests(case, hyp, limit=2):
    """Create up to `limit` diagnostic tests for the hypothesis: the most
    discriminating NEW yes/no questions, each judged against the CURRENT facts.
    A question the facts already settle becomes an answered test (FACT_ANSWER,
    closed) -- so a hypothesis the facts falsify still shows answered tests;
    otherwise the test is left OPEN for the operator. Skips generating a new
    batch while the hypothesis still has an unanswered test (caps at ~`limit`
    open per hypothesis). Returns the number of tests created."""
    if open_test_for(case, hyp["id"]):
        return 0  # still has an unanswered test; wait for it to be answered
    priors = asked_questions(case, hyp["id"])
    excl = "\n".join("- %s" % q for q in priors) or "(none)"
    try:
        reply = ask(
            "investigator",
            "Hypothesis: %s\nFacts:\n%s\nAlready-asked questions (do NOT repeat):\n%s\n\n"
            "Propose up to 2 of the most discriminating NEW yes/no questions whose "
            "answers would most strongly CONFIRM or FALSIFY this hypothesis, and for "
            "each judge whether the CURRENT facts already answer it. Reply with one "
            "or two blocks, each EXACTLY two lines:\n"
            "QUESTION: <yes/no question ending with '?', or NONE>\nANSWER: YES|NO|UNKNOWN"
            % (hyp["title"], case.facts_block(), excl),
        )
    except TimeoutError:
        log("  no test questions for '%s...'" % hyp["title"][:32])
        return 0

    # Pair sequential QUESTION/ANSWER lines into (question, fact-answer) tuples.
    pairs, cur = [], None
    for line in reply.splitlines():
        qm = re.match(r"\s*QUESTION\s*:\s*(.+)", line, re.I)
        am = re.match(r"\s*ANSWER\s*:\s*(YES|NO|UNKNOWN)", line, re.I)
        if qm:
            cur = qm.group(1).strip().strip('"')
        elif am and cur is not None:
            pairs.append((cur, am.group(1).upper()))
            cur = None

    made = 0
    for question, ans in pairs:
        if made >= limit:
            break
        if not question or question.upper().startswith("NONE") or "?" not in question:
            continue
        if question in priors:  # skip repeats (across batches and within one)
            continue
        if ans in ("YES", "NO"):
            desc, tstatus = "FACT_ANSWER: %s" % ans.lower(), "completed"
        else:
            desc, tstatus = "OPERATOR_ANSWER:", "open"
        test = wg_create(question, desc, ["test"], status="open")
        wg_link("parent", test["id"], hyp["id"])  # test -> hypothesis
        if tstatus == "completed":
            wg_close(test["id"], "completed")
        case.tests[test["id"]] = {"hyp_id": hyp["id"], "question": question, "status": tstatus}
        priors.append(question)
        made += 1
        log("  test for '%s...': %s (%s)" % (hyp["title"][:30], question[:50], ans))
    return made


def summarize(case):
    failed = [h["title"] for h in case.hyps if h["status"] == "failed"]
    standing = [h["title"] for h in case.hyps if h["status"] == "open"]
    log("SUMMARY: RCA '%s': %d falsified, %d standing | RULED OUT: %s | STANDING: %s" % (
        case.title, len(failed), len(standing),
        "; ".join(failed) or "(none)", "; ".join(standing) or "(none)",
    ))


def generate_more_hypotheses(case):
    """When EVERY hypothesis has been falsified, propose a fresh batch from ALL
    current facts so the investigation can continue. The investigator is told
    which hypotheses were already ruled out and asked for genuinely different
    ones. Each new hypothesis is assessed + given tests like an initial one.

    No-op unless at least one hypothesis exists AND none is still standing.
    Generates exactly ONE batch per call (the caller invokes it once per operator
    message), so it can never loop indefinitely. Returns the count added."""
    if not case.hyps or case.open_hyps():
        return 0
    ruled_out = "\n".join("- %s" % h["title"] for h in case.hyps)
    post_status("Bah dis donc, all %d hypotheses are dead. Alright, cooking up fresh ones from the facts…"
                % len(case.hyps))
    try:
        hreply = ask(
            "investigator",
            "Incident: %s\nFacts:\n%s\n\nEvery prior hypothesis has been FALSIFIED:\n%s\n\n"
            "Propose 2-5 NEW distinct candidate root-cause hypotheses that are CONSISTENT "
            "with ALL the facts above and are genuinely DIFFERENT from the falsified ones "
            "(one causal claim per line, grounded strictly in the stated facts)."
            % (case.title, case.facts_block(), ruled_out),
        )
    except TimeoutError:
        post_status("Bof, nothing new coming to me right now.")
        return 0

    existing = {h["title"] for h in case.hyps}
    fresh = []
    for h in clean_lines(hreply, 5):
        if h in existing:            # skip exact repeats of already-tried ones
            continue
        hi = wg_create(h, "candidate root cause", ["hypothesis"])
        wg_link("parent", hi["id"], case.hyps_group_id)
        hyp = {"id": hi["id"], "title": h, "status": "open"}
        case.hyps.append(hyp)
        existing.add(h)
        fresh.append(hyp)
    if not fresh:
        post_status("Rien de neuf — no new distinct hypotheses, désolé.")
        return 0

    post_status("Allez, %d fresh hypotheses. Checking them against the facts…" % len(fresh))
    for hyp in fresh:
        assess_hypothesis(case, hyp)
    post_status("Hop, tests for the new ones…")
    for hyp in fresh:
        propose_tests(case, hyp)
    post_status("Voilà, %d new hypotheses (%d still standing). On continue."
                % (len(fresh), len(case.open_hyps())))
    return len(fresh)


# --------------------------------------------------------------------------
# The protocol entry points
# --------------------------------------------------------------------------
def new_incident(incident_text, coordinator_reply):
    post_status("Bon, un nouvel incident. Lemme pull out the facts…")
    title, facts = parse_facts(coordinator_reply, incident_text)

    incident = wg_create(title, incident_text, ["incident"])
    facts_group = wg_create("Facts", "Observed facts for this incident", ["group"])
    hyps_group = wg_create("Hypotheses", "Candidate root causes for this incident", ["group"])
    wg_link("parent", facts_group["id"], incident["id"])
    wg_link("parent", hyps_group["id"], incident["id"])
    case = Case(incident["id"], facts_group["id"], hyps_group["id"], title)
    log("incident %s: %s (Facts=%s Hypotheses=%s)" % (
        incident["id"][-6:], title, facts_group["id"][-6:], hyps_group["id"][-6:]))

    added = add_facts(case, facts)
    log("created %d fact items: %s" % (len(added), "; ".join(added)))
    post_status("Voilà, %d facts in the bag. Now, brainstorming some hypotheses…" % len(added))

    hreply = ask(
        "investigator",
        "Incident: %s\nFacts:\n%s\n\nList 2-5 distinct candidate root-cause "
        "hypotheses (one causal claim per line, grounded in the stated facts)."
        % (title, case.facts_block()),
    )
    for h in clean_lines(hreply, 5):
        hi = wg_create(h, "candidate root cause", ["hypothesis"])
        wg_link("parent", hi["id"], case.hyps_group_id)  # hypothesis -> Hypotheses group
        case.hyps.append({"id": hi["id"], "title": h, "status": "open"})
    log("investigator returned %d hypotheses" % len(case.hyps))
    post_status("Alright, %d hypotheses on the table. Let's see which ones survive the facts, hein…" % len(case.hyps))

    for hyp in case.hyps:
        assess_hypothesis(case, hyp)                 # supporting/conflicting edges + verdict
    post_status("Hop, whipping up some diagnostic tests…")

    for hyp in case.hyps:
        propose_tests(case, hyp)                     # up to 2 tests per hypothesis (open OR failed)

    generate_more_hypotheses(case)  # if the stated facts already ruled everything out

    summarize(case)
    failed = sum(1 for h in case.hyps if h["status"] == "failed")
    post_status(
        "Et voilà, mon vieux. '%s': %d facts, %d hypotheses (%d already shot down), %d tests to run."
        % (title, len(case.facts), len(case.hyps), failed, len(case.tests)))
    return case


def amend_incident(case, message_text, coordinator_reply):
    """DEFAULT path for a free-text follow-up: add facts + re-test open hyps."""
    post_status("Ah, du nouveau — adding these facts…")
    _title, facts = parse_facts(coordinator_reply, message_text)
    added = add_facts(case, facts)
    log("amend '%s': +%d facts: %s" % (case.title, len(added), "; ".join(added) or "(none parsed)"))

    open_before = case.open_hyps()
    log("re-evaluating %d open hypotheses against %d total facts" % (
        len(open_before), len(case.facts)))
    post_status("Bon, +%d facts. Re-checking the %d hypotheses still standing, attends…"
                % (len(added), len(open_before)))
    newly = sum(1 for hyp in open_before if assess_hypothesis(case, hyp))
    # Refresh open tests for still-open hypotheses (no-op while one is unanswered).
    post_status("Freshening up the tests, deux secondes…")
    for hyp in case.open_hyps():
        propose_tests(case, hyp)

    generate_more_hypotheses(case)  # if the new facts ruled out the last standing one

    summarize(case)
    post_status("Voilà. +%d facts, re-checked %d hypotheses (%d just bit the dust)."
                % (len(added), len(open_before), newly))


def answer_test(case, test_id, answer):
    """Operator answered a diagnostic test YES/NO (from the UI's YES/NO buttons).

    Records the answer on the test item, closes it, turns the answer into a new
    fact, and re-evaluates every open hypothesis against the fuller fact set.
    """
    ans = "yes" if answer.lower() == "yes" else "no"
    info = case.tests.get(test_id)
    try:
        item = wg_get(test_id)
    except (urllib.error.URLError, OSError, RuntimeError):
        log("answer_test: test %s not found" % test_id[-6:])
        post_status("Hmm, can't find that test, désolé.")
        return
    question = (info or {}).get("question") or item.get("title") or "diagnostic test"
    post_status("Nickel, noting your %s on '%s'…" % (ans.upper(), question[:50]))

    # Record the answer on the test item, then close it.
    try:
        wg_update(test_id, description="OPERATOR_ANSWER: %s" % ans)
        wg_close(test_id, "completed")
    except (urllib.error.URLError, OSError, RuntimeError) as e:
        log("answer_test: recording answer failed: %s" % e)
    if test_id in case.tests:
        case.tests[test_id]["status"] = "completed"

    # The answer becomes a fact for downstream reasoning (unambiguous phrasing).
    fact_title = 'Operator answered %s to: "%s"' % (ans.upper(), question)
    add_facts(case, [{"title": fact_title[:100], "detail": fact_title}])
    log("answered test %s = %s -> new fact" % (test_id[-6:], ans.upper()))
    post_status("Bon, that's a fact now. Re-checking the hypotheses…")

    # Re-evaluate open hypotheses. A hypothesis that flips to `failed` in THIS
    # pass was falsified by the fact this answer just added -> link THIS test to
    # it (hyp --related--> test) as the precise falsifier the UI displays.
    open_before = case.open_hyps()
    newly = 0
    for hyp in open_before:
        if assess_hypothesis(case, hyp):
            newly += 1
            if (hyp["id"], test_id) not in case.falsif_edged:
                wg_link("related", hyp["id"], test_id)  # from=hyp, to=test
                case.falsif_edged.add((hyp["id"], test_id))
    # Progressive: give the related hypothesis its NEXT batch if it is still open.
    hyp_id = (info or {}).get("hyp_id")
    for hyp in case.open_hyps():
        if hyp["id"] == hyp_id:
            propose_tests(case, hyp)

    generate_more_hypotheses(case)  # if this answer ruled out the last standing one

    summarize(case)
    post_status("Voilà — '%s': %s. Re-checked %d hypotheses (%d newly shot down)."
                % (question[:60], ans.upper(), len(open_before), newly))


# --------------------------------------------------------------------------
# Rebuild the active case from the graph (survives a controller-only restart)
# --------------------------------------------------------------------------
def rebuild_case_from_graph():
    """If the graph already holds an incident (e.g. controller restarted while
    mobkit kept running), reconstruct the Case so TEST answers still map."""
    try:
        snap = wg_snapshot()
    except (urllib.error.URLError, OSError, RuntimeError):
        return None
    items = snap.get("items", [])
    edges = snap.get("edges", [])
    by_id = {it["id"]: it for it in items}

    def label(it):
        return (it.get("labels") or [None])[0]

    # Ignore incidents from before the latest RESET marker (see reset_board).
    resets = [it for it in items if label(it) == "reset"]
    reset_epoch = max((it.get("created_at", "") for it in resets), default="")
    incidents = [it for it in items
                 if label(it) == "incident" and it.get("created_at", "") > reset_epoch]
    if not incidents:
        return None
    incident = sorted(incidents, key=lambda it: it.get("created_at", ""))[-1]

    parent = {}  # child_id -> parent_id
    for e in edges:
        if e["kind"] == "parent":
            parent[e["from_id"]] = e["to_id"]

    groups = {it["title"]: it for it in items
              if label(it) == "group" and parent.get(it["id"]) == incident["id"]}
    facts_group = groups.get("Facts")
    hyps_group = groups.get("Hypotheses")
    if not (facts_group and hyps_group):
        return None

    case = Case(incident["id"], facts_group["id"], hyps_group["id"], incident.get("title", "Incident"))
    for it in sorted(items, key=lambda x: x.get("created_at", "")):
        lb = label(it)
        if lb == "fact" and parent.get(it["id"]) == facts_group["id"]:
            case.facts.append((it.get("title", ""), it["id"]))
        elif lb == "hypothesis" and parent.get(it["id"]) == hyps_group["id"]:
            status = "failed" if it.get("status") == "failed" else "open"
            case.hyps.append({"id": it["id"], "title": it.get("title", ""), "status": status})
        elif lb == "test":
            tstatus = "open" if it.get("status") == "open" else "completed"
            case.tests[it["id"]] = {"hyp_id": parent.get(it["id"]),
                                    "question": it.get("title", ""), "status": tstatus}
    # Rebuild the classified-pair set + falsifier links from existing edges so a
    # controller-only restart does not duplicate them. hyp--related-->fact is a
    # conflicting fact (case.edged); hyp--related-->test is a falsifier marker.
    for e in edges:
        frm, to = by_id.get(e["from_id"]), by_id.get(e["to_id"])
        if not (frm and to) or label(frm) != "hypothesis":
            continue
        if e["kind"] in ("derived_from", "related") and label(to) == "fact":
            case.edged.add((e["from_id"], e["to_id"]))
        elif e["kind"] == "related" and label(to) == "test":
            case.falsif_edged.add((e["from_id"], e["to_id"]))
    log("rebuilt case '%s' from graph: %d facts, %d hyps, %d tests" % (
        case.title, len(case.facts), len(case.hyps), len(case.tests)))
    return case


# --------------------------------------------------------------------------
# Main watch loop
# --------------------------------------------------------------------------
def wait_healthy(timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(HEALTH, timeout=5) as r:
                if 200 <= r.status < 300:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(2)
    return False


def wait_for_agents(timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            members = {m.get("agent_identity") for m in rpc("mobkit/list_members", {})}
            if all(a in members for a in AGENTS):
                return True
        except (urllib.error.URLError, OSError, RuntimeError):
            pass
        time.sleep(2)
    return False


def is_operator_incident(frame):
    if frame.get("kind") != "user_input":
        return False
    origin = (frame.get("payload", {}) or {}).get("origin", "") or ""
    return not origin.startswith("controller")  # operator/console, not our own sends


def operator_text(frame):
    return (frame.get("payload", {}) or {}).get("content", "") or ""


def wants_new_incident(text):
    """Escape hatch: a message beginning 'NEW INCIDENT' forces a fresh incident;
    everything else defaults to amending the current one."""
    return re.match(r"\s*new incident\b", text or "", re.I) is not None


def parse_test_answer(text):
    """A `TEST <work_id> YES|NO` message from the UI's YES/NO buttons."""
    m = re.match(r"\s*TEST\s+(\S+)\s+(YES|NO)\b", text or "", re.I)
    return (m.group(1), m.group(2)) if m else None


def wants_reset(text):
    """The UI's RESET button sends the bare word RESET (exact match only, so an
    incident that merely mentions 'reset' is never mistaken for the command)."""
    return re.match(r"\s*RESET\s*$", text or "", re.I) is not None


def reset_board():
    """Soft reset to a clean board. We CANNOT delete graph items or wipe the
    timeline (no delete/unlink RPC, and reset_all is off-limits), so we instead
    drop an epoch boundary that the UI hides everything before:
      * a `reset` marker item -- its created_at is the graph epoch; the UI shows
        only items created AFTER the latest reset marker.
      * a chat frame with origin `controller:reset` -- the UI shows only chat
        after the last such frame.
    The caller also clears the in-memory case so follow-ups start a fresh one."""
    try:
        wg_create("session reset", "", ["reset"])
    except (urllib.error.URLError, OSError, RuntimeError) as e:
        log("reset marker failed (non-fatal): %s" % e)
    send("coordinator", "-- new session --", origin="controller:reset")
    post_status("Table rase, mon ami. Describe an incident and we're off.")
    log("RESET: board cleared (new epoch)")


def main():
    log("RPC=%s" % RPC)
    if not wait_healthy():
        log("host never became healthy; exiting")
        return
    wait_for_agents()
    set_working(False)  # create the control item and start idle
    # Prime: ignore incidents already in the timeline before we started.
    seen = {f.get("id") for f in timeline("coordinator") if is_operator_incident(f)}
    case = rebuild_case_from_graph()  # survive a controller-only restart
    log("ready; watching coordinator for operator messages (%d pre-existing ignored)" % len(seen))

    while True:
        try:
            for f in timeline("coordinator"):
                if f.get("id") in seen or not is_operator_incident(f):
                    continue
                seen.add(f.get("id"))
                text = operator_text(f)
                log("operator message: %s" % text[:90])

                if wants_reset(text):
                    reset_board()
                    case = None
                    continue

                # Everything else is a long, LLM-heavy job -> mark BUSY so the UI
                # locks its composer, and clear it no matter how the job ends.
                set_working(True)
                try:
                    ta = parse_test_answer(text)
                    if ta:
                        if case is None:
                            post_status("Attends — there's no incident to attach that to.")
                        else:
                            answer_test(case, ta[0], ta[1])
                    else:
                        # If the operator pasted our JSON schema, parse it
                        # deterministically and skip the LLM. Otherwise drive the
                        # coordinator (a member answers only its FIRST console
                        # turn per session).
                        struct = structured_input(text)
                        if struct is not None:
                            reply = json.dumps(struct)
                            log("structured input: %d facts, title=%r"
                                % (len(struct["facts"]), struct["title"]))
                        else:
                            try:
                                reply = ask("coordinator", text)
                            except TimeoutError:
                                log("coordinator did not parse; using heuristic")
                                reply = ""
                        if case is None or wants_new_incident(text):
                            case = new_incident(text, reply)
                        else:
                            amend_incident(case, text, reply)
                finally:
                    set_working(False)
        except Exception as e:  # never let the watch loop die
            log("loop error: %s" % e)
        time.sleep(POLL)


if __name__ == "__main__":
    main()
