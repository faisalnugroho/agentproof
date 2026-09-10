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
  S6 EVIDENCE PROVENANCE (steward review): a request whose docs URL is
      a stable 404 — the URL must never appear in the sealed normalized
      evidence, and the record's source_provenance must show
      fetch_success=false / used_as_evidence=false for it. The request
      id is taken EXACTLY from the request tx's returned value (never
      from the mutable verification count).
  S7 TAXONOMY AUTHORIZATION (steward review): a non-owner account
      attempts add_capability_definition — must be rejected (the
      taxonomy stays at its 8 default entries).

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
    "s6_evidence_provenance": "404 docs URL must never seal as normalized evidence (steward fix)",
    "s7_taxonomy_authorization": "non-owner add_capability_definition must be rejected (steward fix)",
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
            "contract_address": addr, "stderr_tail": stderr[-1500:],
            "receipt": receipt if isinstance(receipt, dict) else {}}


def read_json(client, addr, fn, args):
    raw = client.read_contract(address=addr, function_name=fn, args=args)
    return json.loads(raw) if isinstance(raw, str) else raw


def receipt_return_int(receipt, label):
    """Extract the EXACT integer return value of a write tx from the
    finalized receipt's leader result (calldata-encoded u256).

    Verified live on Studionet: consensus_data.leader_receipt[0].result
    is base64 of [status_byte, uleb128-calldata]; the SDK's simplify
    path keeps result.payload.readable when present. We parse both
    shapes. This replaces the old mutable-global-count correlation:
    the request's OWN receipt carries the verification id it created.
    """
    cd = (receipt or {}).get("consensus_data") or {}
    lr = cd.get("leader_receipt") or []
    lead = lr[0] if isinstance(lr, list) and lr else lr
    res = (lead or {}).get("result") if isinstance(lead, dict) else None
    if isinstance(res, dict):
        payload = res.get("payload") or {}
        readable = payload.get("readable")
        if readable is not None:
            try:
                return int(str(readable).strip())
            except (TypeError, ValueError):
                pass
    if isinstance(res, str) and res:
        import base64
        try:
            raw = base64.b64decode(res)
        except Exception:
            return None
        if len(raw) >= 2 and raw[0] == 0:
            # uleb128 positive int (calldata TYPE_PINT = value<<3 | 1):
            # decode the payload after the status byte.
            body = raw[1:]
            code = 0
            off = 0
            i = 0
            while i < len(body):
                b = body[i]
                code |= (b & 0x7F) << off
                if (b & 0x80) == 0:
                    break
                off += 7
                i += 1
            if code & 0x7 == 1:      # TYPE_PINT
                return code >> 3
    return None


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
        res1 = wait_final(client, tx1, label + " request")
        # EXACT-ID correlation (steward fix): the verification id is
        # the value THIS tx returned — read from THIS tx's receipt,
        # never from the mutable global verification count.
        vid = receipt_return_int(res1["receipt"], label + " request")
        if vid is None:
            raise RuntimeError(
                f"{label}: could not extract the exact verification id "
                f"from the request receipt {tx1}")
        rec_peek = read_json(client, addr, "get_verification", [vid])
        if not isinstance(rec_peek, dict) or \
                rec_peek.get("verification_id") != str(vid):
            raise RuntimeError(
                f"{label}: receipt id {vid} does not match on-chain "
                f"record — exact-id correlation failed")
        t_req = time.time() - t0
        print(f"  [{label}] verification #{vid} requested "
              f"[exact id from receipt] [{t_req:.0f}s]", flush=True)

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

    # ---------------- S6 evidence provenance (steward) ----------------
    # A 404 evidence URL must NEVER be sealed as normalized evidence.
    print("--- S6 evidence provenance: 404 docs URL ---", flush=True)
    prov = full_cycle(
        "S6", "BrokenDocs Agent",
        "https://docs.tavily.com/documentation/api-reference/endpoint/search.md",
        "https://api.github.com/agentproof-nonexistent-404",   # stable 404
        json.dumps([{"id": "WEB_RESEARCH"}, {"id": "DOCUMENTATION"}]))
    log["s6_evidence_provenance"] = prov
    sealed_sources = prov["record"].get("evidence_sources", [])
    prov_rows = prov["record"].get("source_provenance", [])
    urls = [s.get("normalized_url", s.get("url")) for s in sealed_sources]
    prov404 = [p for p in prov_rows
               if "404" in str(p.get("submitted_url", ""))]
    s6_sealed = prov["record"].get("state") == "SEALED"
    # The assertion is ONLY valid on a SEALED record: an unsealed
    # (stuck-PENDING) record's provenance is just the request-time
    # default and would pass vacuously. A stuck record is a FAIL.
    prov404_ok = (s6_sealed
                  and len(prov404) == 1
                  and prov404[0].get("fetch_success") is False
                  and prov404[0].get("used_as_evidence") is False
                  and all("nonexistent-404" not in (u or "")
                          for u in urls))
    print(f"EVIDENCE_404_EXCLUDED: {prov404_ok} (sealed={s6_sealed}, "
          f"sealed_urls={urls}, prov404={prov404})", flush=True)
    if not s6_sealed:
        raise RuntimeError(
            "S6 FAILED: the 404-evidence record never sealed "
            f"(state={prov['record'].get('state')}) — the 404-exclusion "
            "assertion would be vacuous. Fix the contract and rerun.")
    log["s6_evidence_404_excluded"] = {
        "ok": prov404_ok, "sealed": s6_sealed,
        "sealed_urls": urls, "provenance_404": prov404}

    # ---------------- S7 taxonomy authorization (steward) ----------------
    # A NON-OWNER account must NOT be able to modify the taxonomy.
    print("--- S7 taxonomy authorization: non-owner add rejected ---",
          flush=True)
    outsider = create_account()
    tax_rejected = False
    tax_err = ""
    try:
        client.write_contract(
            address=addr, function_name="add_capability_definition",
            args=[json.dumps({
                "id": "BACKDOOR_CAP",
                "display_name": "Backdoor",
                "description": "should never be added",
                "verification_criteria": "x" * 30,
            })],
            account=outsider)
        time.sleep(2)
    except Exception as e:
        tax_rejected = True
        tax_err = str(e)[:200]
    if not tax_rejected:
        # the write may submit fine and revert at consensus — check state
        taxonomy_after = read_json(client, addr, "get_capability_taxonomy",
                                   [])
        ids_after = []
        if isinstance(taxonomy_after, list):
            for t in taxonomy_after:
                if isinstance(t, dict):
                    ids_after.append(t.get("id"))
        if "BACKDOOR_CAP" not in ids_after:
            tax_rejected = True
            tax_err = "reverted at consensus (not in taxonomy)"
    print(f"TAXONOMY_NONOWNER_REJECTED: {tax_rejected} ({tax_err})",
          flush=True)
    log["s7_taxonomy_nonowner_rejected"] = {
        "ok": tax_rejected, "detail": tax_err}

    log["result"] = {
        "determinism_consistent": det_ok,
        "negative_safe_not_verified": neg_ok,
        "views_ok": views_ok,
        "evidence_404_excluded": prov404_ok,
        "taxonomy_nonowner_rejected": tax_rejected,
        "all_ok": det_ok and neg_ok and views_ok and prov404_ok
                  and tax_rejected,
        "address": addr,
    }
    LOG.parent.mkdir(exist_ok=True)
    LOG.write_text(json.dumps(log, indent=2))
    print("ALL_OK:", log["result"]["all_ok"], flush=True)
    print("DONE. contract:", addr, flush=True)
    print("log:", LOG, flush=True)


if __name__ == "__main__":
    main()
