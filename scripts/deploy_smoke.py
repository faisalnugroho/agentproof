#!/usr/bin/env python3
"""AgentProof — deploy to GenLayer Studionet + live consensus smoke test.

Smoke plan (skills/genlayer-live-deploy-and-smoke protocol):
  S1–S3 DETERMINISM: three consecutive consensus runs verifying a
      technically documented agent (api.github.com REST docs as evidence).
      Fresh verification each run (the seal guard makes re-runs on the
      same id revert). Success criterion: the three sealed passports
      agree on the stable status family (score band), NOT byte-identical
      prose (Equivalence Principle).
  S4 NEGATIVE: an agent whose "site" is pure marketing language with no
      endpoints/schemas/protocol evidence (a stable, well-known
      marketing-style page). Expect: capability statuses UNVERIFIED /
      INCONCLUSIVE and overall PARTIAL/UNVERIFIED/INCONCLUSIVE —
      specifically NOT VERIFIED. The assertion is: VERIFIED never comes
      out of a page with no technical evidence.
  S5 VIEWS: list_verifications/get_agent read back after consensus.

Evidence sources must be stable public pages; both fetches (leader +
validators) hit them live.

Output: docs/deployment_log.json (address, tx hashes, verdicts, timings).
Explorer: https://explorer-studio.genlayer.com/address/<addr>
"""
import json
import sys
import time
from pathlib import Path

from genlayer_py import create_client, create_account
from genlayer_py.chains import studionet
from genlayer_py.types import TransactionStatus

CODE = Path("contracts/agentproof.py").read_text()
KEYFILE = Path("scripts/smoke_deployer.json")
LOG = Path("docs/deployment_log.json")

# ---------------------------------------------------------------- smoke data
# S1-S3: agent page that DOES contain concrete technical evidence.
# Tavily's docs serve CLEAN RAW MARKDOWN at <path>.md (Mintlify) — stable,
# fetchable, unambiguous technical content: REST API endpoints (POST
# /search), request/response schemas, auth (API key) — textbook evidence
# for WEB_RESEARCH + API_ACCESS + DOCUMENTATION. The .md variant is the
# same page rendered for LLMs, and the root llms.txt is a plain-text
# index of the whole API surface.
AGENT_NAME_POS = "Tavily Web Search Agent"
AGENT_URL_POS = "https://docs.tavily.com/documentation/api-reference/endpoint/search.md"
DOCS_URL_POS = "https://docs.tavily.com/llms.txt"
CAPS_POS = json.dumps([
    {"id": "WEB_RESEARCH"},
    {"id": "API_ACCESS"},
    {"id": "DOCUMENTATION"},
])

# S4 negative: marketing-only language, no endpoints/schemas. example.com's
# page is a stable, contentless placeholder + a marketing-flavored page.
AGENT_NAME_NEG = "HypeBot 9000"
AGENT_URL_NEG = "https://example.com/"
DOCS_URL_NEG = ""   # no docs — weaker evidence on purpose
CAPS_NEG = json.dumps([
    {"id": "MCP_SUPPORT"},
    {"id": "A2A_SUPPORT"},
    {"id": "AUTONOMOUS_EXECUTION"},
])

DETERMINISM_RUNS = 3
log = {"smoke_plan": {
    "s1_s3_determinism": f"{AGENT_NAME_POS}: {DETERMINISM_RUNS} fresh consensus runs, stable status family expected",
    "s4_negative": f"{AGENT_NAME_NEG}: marketing-only evidence must never yield VERIFIED",
    "s5_views": "list_verifications + get_agent readback",
}}


def load_account():
    if KEYFILE.exists():
        data = json.loads(KEYFILE.read_text())
        return create_account(account_private_key=data["private_key"])
    acct = create_account()
    KEYFILE.parent.mkdir(exist_ok=True)
    KEYFILE.write_text(json.dumps(
        {"address": acct.address, "private_key": acct.key.hex()}))
    return acct


def wait_final(client, tx_hash, label, strict=True):
    # FINALIZED != success: a reverted or DISAGREE'd execution also
    # finalizes. On studionet the receipt carries BOTH:
    #   - result_name: consensus VOTING result (MAJORITY_AGREE /
    #     NO_MAJORITY / MAJORITY_DISAGREE / DETERMINISTIC_VIOLATION…)
    #   - tx_execution_result_name: leader GenVM execution
    #     (FINISHED_WITH_RETURN / FINISHED_WITH_ERROR / NOT_VOTED)
    # Success = MAJORITY_AGREE (voting) + FINISHED_WITH_RETURN (exec).
    # The stderr TAIL carries the contract's revert reason (AssertionError
    # sits at the END of the runner traceback) — keep the tail, not prefix.
    last_err = None
    for _ in range(6):
        try:
            receipt = client.wait_for_transaction_receipt(
                transaction_hash=tx_hash,
                status=TransactionStatus.FINALIZED,
                retries=100, interval=3000)
            break
        except Exception as e:
            last_err = e
            print(f"  [{label}] rpc error: {str(e)[:180]} — backing off 15s", flush=True)
            time.sleep(15)
    else:
        raise RuntimeError(f"{label} rpc failed: {last_err}")

    if isinstance(receipt, dict):
        data = receipt.get("data") or {}
        addr = data.get("contract_address") if isinstance(data, dict) else None
        if addr is None:
            addr = receipt.get("to_address")
        leader = (receipt.get("consensus_data") or {}).get("leader_receipt", [{}])
        lead = leader[0] if leader else {}
        exec_result = lead.get("execution_result")
        vote_result = receipt.get("result_name") or "UNKNOWN"
        exec_name = receipt.get("tx_execution_result_name")
        if exec_name is not None:
            exec_result = exec_name
        if exec_result is None:
            exec_result = receipt.get("result_name")
        stderr = str((lead.get("genvm_result") or {}).get("stderr") or "")
    else:
        addr = getattr(receipt, "contract_address", None)
        exec_result = None
        vote_result = "UNKNOWN"
        stderr = ""

    print(f"  [{label}] finalized vote={vote_result} exec={exec_result}", flush=True)
    ok_names = (None, "SUCCESS", "FINISHED_WITH_RETURN")
    if exec_result not in ok_names or vote_result not in ("MAJORITY_AGREE", None):
        print("EXECUTION FAILED — consensus data:", flush=True)
        print(json.dumps(receipt.get("consensus_data"), default=str)[:3000], flush=True)
        if stderr:
            print("STDERR tail:", stderr[-1500:], flush=True)
        if strict:
            raise RuntimeError(
                f"{label} failed: vote={vote_result} exec={exec_result}")
        print(f"  [{label}] consensus mismatch (vote={vote_result}) — "
              "state change discarded, record remains PENDING", flush=True)
    return {"execution_result": exec_result or "SUCCESS",
            "vote_result": vote_result,
            "ok": exec_result in ok_names
                 and vote_result in ("MAJORITY_AGREE", None),
            "contract_address": addr, "stderr_tail": stderr[-1500:]}


def read_json(client, addr, fn, args):
    raw = client.read_contract(address=addr, function_name=fn, args=args)
    return json.loads(raw) if isinstance(raw, str) else raw


def main():
    account = load_account()
    client = create_client(chain=studionet, account=account)
    print("deployer:", account.address, flush=True)

    # ---------------- deploy ----------------
    print(f"deploying AgentProof ({len(CODE)} bytes)…", flush=True)
    tx = client.deploy_contract(code=CODE, account=client.local_account,
                                args=[], leader_only=True)
    res = wait_final(client, tx, "deploy")
    addr = res["contract_address"]
    if not addr:
        raise RuntimeError("no contract address in deploy receipt")
    log["deploy"] = {"tx_hash": tx, "address": addr,
                     "deployer": account.address}
    print("CONTRACT:", addr, flush=True)
    print("explorer: https://explorer-studio.genlayer.com/address/" + addr, flush=True)

    info = read_json(client, addr, "get_contract_info", [])
    print("contract info:", json.dumps(info)[:300], flush=True)
    log["contract_info"] = info

    def full_cycle(label, name, agent_url, docs_url, caps, verify_attempts=3):
        """request -> (read id) -> verify (crank w/ retry) -> read passport.

        A NO_MAJORITY/DISAGREE consensus round discards the state change
        (record stays PENDING) — the honest crank pattern is to resubmit
        verification on the SAME record until it seals or attempts run
        out. Determinism is then judged on the sealed status family.
        """
        t0 = time.time()
        tx1 = client.write_contract(
            address=addr, function_name="request_verification",
            args=[name, agent_url, docs_url, caps, ""],
            account=client.local_account)
        wait_final(client, tx1, label + " request")
        cnt = read_json(client, addr, "get_verification_count", [])
        vid = int(cnt)
        t_req = time.time() - t0
        print(f"  [{label}] verification #{vid} requested [{t_req:.0f}s]", flush=True)

        t1 = time.time()
        rec = None
        votes_seen = []
        for attempt in range(1, verify_attempts + 1):
            tx2 = client.write_contract(
                address=addr, function_name="verify_agent", args=[vid],
                account=client.local_account)
            res = wait_final(client, tx2, f"{label} verify #{attempt}",
                             strict=False)
            votes_seen.append(res["vote_result"])
            rec = read_json(client, addr, "get_verification", [vid])
            if rec.get("state") != "PENDING":
                break
            if attempt < verify_attempts:
                print(f"  [{label}] still PENDING after {attempt} "
                      f"consensus round(s) {votes_seen} — re-cranking", flush=True)
        t_ver = time.time() - t1
        print(f"  [{label}] consensus rounds: {votes_seen} [{t_ver:.0f}s]", flush=True)
        rec = read_json(client, addr, "get_verification", [vid])
        print(f"  [{label}] passport #{vid}: status={rec.get('status')} "
              f"score={rec.get('score')} verified={rec.get('verified_capabilities')} "
              f"unsupported={rec.get('unsupported_capabilities')} "
              f"strong_sources={len([s for s in rec.get('evidence_sources', []) if s.get('quality') in ('STRONG','MODERATE')])} "
              f"[verify {t_ver:.0f}s]", flush=True)
        print(f"  [{label}] summary: {str(rec.get('summary'))[:200]}", flush=True)
        for d in rec.get("capability_details", []):
            print(f"     {d.get('id')}: {d.get('status')} — {str(d.get('reason'))[:110]}", flush=True)
        return {
            "verification_id": vid,
            "request_tx": tx1, "verify_tx": tx2,
            "consensus_rounds": votes_seen,
            "status": rec.get("status"), "score": rec.get("score"),
            "state": rec.get("state"),
            "verified": rec.get("verified_capabilities"),
            "unsupported": rec.get("unsupported_capabilities"),
            "inconclusive": rec.get("inconclusive_capabilities"),
            "strong_sources": len([s for s in rec.get("evidence_sources", [])
                                   if s.get("quality") in ("STRONG", "MODERATE")]),
            "verify_secs": round(t_ver, 1),
            "record": rec,
        }

    # ---------------- S1–S3 determinism ----------------
    results = []
    for i in range(DETERMINISM_RUNS):
        print(f"--- S{i+1} determinism run {i+1}/{DETERMINISM_RUNS} ---", flush=True)
        r = full_cycle(f"S{i+1}", AGENT_NAME_POS, AGENT_URL_POS,
                       DOCS_URL_POS, CAPS_POS)
        log[f"s{i+1}_determinism"] = r
        results.append(r)

    families = {r["status"] for r in results}
    # Equivalence principle: identical inputs -> same verdict FAMILY.
    det_ok = len(families) == 1
    print(f"DETERMINISM_CONSISTENT: {det_ok} (statuses: {sorted(families)})", flush=True)
    log["determinism"] = {
        "consistent": det_ok,
        "statuses": sorted(families),
        "scores": [r["score"] for r in results],
    }

    # ---------------- S4 negative ----------------
    print("--- S4 negative: marketing-only evidence ---", flush=True)
    neg = full_cycle("S4", AGENT_NAME_NEG, AGENT_URL_NEG,
                     DOCS_URL_NEG, CAPS_NEG)
    log["s4_negative"] = neg
    neg_ok = neg["status"] != "VERIFIED"
    print(f"NEGATIVE_SAFE (not VERIFIED): {neg_ok} (status={neg['status']})", flush=True)
    log["negative_safe"] = {"ok": neg_ok, "status": neg["status"]}

    # ---------------- S5 views ----------------
    lst = read_json(client, addr, "list_verifications", [20, 0])
    agent = read_json(client, addr, "get_agent", [AGENT_URL_POS.strip().lower()])
    views_ok = (lst.get("total", 0) >= 4
                and (agent.get("verification_count") or 0) >= 3)
    print(f"VIEWS_OK: {views_ok} (total={lst.get('total')}, "
          f"agent history={agent.get('verification_count')})", flush=True)
    log["s5_views"] = {
        "ok": views_ok,
        "total": lst.get("total"),
        "agent": {k: agent.get(k) for k in
                  ("agent_name", "verification_count",
                   "last_status", "last_score")},
        "list_sample": [
            {k: v.get(k) for k in ("verification_id", "agent_name", "status", "score")}
            for v in (lst.get("verifications") or [])[:6]],
    }

    log["result"] = {
        "determinism_consistent": det_ok,
        "negative_safe_not_verified": neg_ok,
        "views_ok": views_ok,
        "all_ok": det_ok and neg_ok and views_ok,
        "address": addr,
    }
    LOG.parent.mkdir(exist_ok=True)
    LOG.write_text(json.dumps(log, indent=2))
    print("ALL_OK:", log["result"]["all_ok"], flush=True)
    print("DONE. contract:", addr, flush=True)
    print("log:", LOG, flush=True)


if __name__ == "__main__":
    main()
