"""AgentProof steward-review regression tests.

Covers the GenLayer steward's acceptance checklist:

  CONCURRENCY / EXACT-ID TRACKING
    - test_concurrent_requests_are_isolated: User A and User B submit
      verification requests "around the same time"; each receives its
      EXACT verification id; ids/owners/agent URLs/capabilities are
      distinct; results correlate id->agent with no cross-wiring.
    - reversed finalization order: A requested first, B sealed first —
      correlation stays A->A, B->B (no latest/count logic can re-wire).
    - no global-count correlation: get_verification_count is still
      exposed for the explorer, but the returned id (NOT the count) is
      what identifies the record; the tests assert id == returned value
      even when other users' requests interleave.

  USER OWNERSHIP / ISOLATION
    - every record has owner == submitter (gl.message.sender_address)
    - get_my_verification returns the caller's record ONLY; another
      user's id yields an explicit not_authorized error (no agent data,
      no evidence URLs, no result leaked)
    - get_my_verifications lists ONLY the caller's records
    - get_verification (public explorer) stays intentionally public —
      passports are public claims about public pages

  EVIDENCE PROVENANCE
    - duplicate submitted URLs (trailing slash / case) normalize to ONE
      canonical evidence entry
    - 404 / 500 / timeout URLs are never sealed as evidence
    - LLM-mentioned URLs outside the submitted set never seal
    - provenance (fetch_success, http_status, used_as_evidence) is
      recorded per source

  TAXONOMY AUTHORIZATION
    - non-admin cannot add/modify capabilities (UserError)
    - admin (deployer) can extend the taxonomy
    - taxonomy version stamped into every verification record
"""
import json

import pytest

import helpers as H
from helpers import (
    CONTRACT, AGENT_URL, DOCS_URL, DOCS_BODY_STRONG,
    mock_body, llm_caps,
)


@pytest.fixture()
def deployed(direct_vm, direct_deploy):
    """Fresh AgentProof deployment per test (deployer = the VM's default
    sender at deploy time)."""
    H.set_time(direct_vm, H.iso_now())
    return direct_deploy(CONTRACT)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def addr_str(raw):
    """Contract stores str(gl.message.sender_address) (checksummed hex).
    gltest fixtures may hand us raw bytes or an Address object."""
    from eth_utils import to_checksum_address
    if hasattr(raw, "as_bytes"):
        raw = raw.as_bytes
    if hasattr(raw, "hex"):
        try:
            raw = bytes(raw)
        except Exception:
            pass
    if isinstance(raw, str):
        return raw
    return to_checksum_address(raw)

AGENT_URL_B = "https://toolbot.example.org"
DOCS_URL_B = "https://toolbot.example.org/docs"


def request_as(vm, contract, sender, name, agent_url, docs_url, caps):
    vm.sender = sender
    return contract.request_verification(
        name, agent_url, docs_url, caps, "")


def seed_b_request(vm, contract, sender,
                   caps=None):
    if caps is None:
        caps = json.dumps(["API_ACCESS", "MCP_SUPPORT"])
    return request_as(vm, contract, sender, "ToolBot Alpha",
                      AGENT_URL_B, DOCS_URL_B, caps)


# ---------------------------------------------------------------------------
# CONCURRENCY — exact verification ID tracking + isolation
# ---------------------------------------------------------------------------

class TestConcurrentRequestsAreIsolated:
    def test_concurrent_requests_are_isolated(self, deployed, direct_vm,
                                              direct_alice, direct_bob):
        """THE steward test: two users submit requests around the same
        time. Each caller receives the EXACT verification id assigned to
        ITS request; agent URLs, owners, capabilities and results never
        cross-wire; the global verification count cannot affect the
        correlation."""
        vm = direct_vm
        contract = deployed
        a_caps = json.dumps(["API_ACCESS", "WEB_RESEARCH"])
        b_caps = json.dumps(["MCP_SUPPORT", "A2A_SUPPORT"])

        # both requests in flight "concurrently" (same block of time,
        # interleaved submissions: A, B — then reads happen AFTER both
        # exist, so any count-based guess would already be wrong)
        vid_a = int(request_as(vm, contract, direct_alice,
                               "Demo Research Agent", AGENT_URL, DOCS_URL,
                               a_caps))
        vid_b = int(seed_b_request(vm, contract, direct_bob, b_caps))

        # ids are distinct and each is exactly what the contract returned
        assert vid_a != vid_b
        assert vid_a == 1 and vid_b == 2   # explicit, not count-derived

        # owners are distinct and stored on the record
        rec_a = json.loads(contract.get_verification(vid_a))
        rec_b = json.loads(contract.get_verification(vid_b))
        assert rec_a["owner"] == addr_str(direct_alice)
        assert rec_b["owner"] == addr_str(direct_bob)
        assert rec_a["owner"] != rec_b["owner"]

        # agent URLs + declared capabilities are correct per record
        assert rec_a["agent_url"] == AGENT_URL
        assert rec_b["agent_url"] == AGENT_URL_B
        assert rec_a["declared_capabilities"] == ["API_ACCESS",
                                                  "WEB_RESEARCH"]
        assert rec_b["declared_capabilities"] == ["MCP_SUPPORT",
                                                  "A2A_SUPPORT"]

        # finalize BOTH (A first here); results must correlate to ids
        mock_body(vm, ".*", DOCS_BODY_STRONG)
        vm.mock_llm(".*", llm_caps(
            [("API_ACCESS", "VERIFIED"), ("WEB_RESEARCH", "VERIFIED")]))
        out_a = json.loads(contract.verify_agent(vid_a))

        vm.clear_mocks()
        mock_body(vm, ".*", DOCS_BODY_STRONG)
        vm.mock_llm(".*", llm_caps(
            [("MCP_SUPPORT", "UNVERIFIED"), ("A2A_SUPPORT", "UNVERIFIED")],
            quality="WEAK"))
        out_b = json.loads(contract.verify_agent(vid_b))

        # finalized result belongs to the correct request
        assert out_a["verification_id"] == str(vid_a)
        assert out_b["verification_id"] == str(vid_b)
        sealed_a = json.loads(contract.get_verification(vid_a))
        sealed_b = json.loads(contract.get_verification(vid_b))
        assert sealed_a["agent_url"] == AGENT_URL
        assert sealed_b["agent_url"] == AGENT_URL_B
        assert "API_ACCESS" in sealed_a["verified_capabilities"]
        assert "MCP_SUPPORT" in sealed_b["unsupported_capabilities"]
        # a's VERIFIED result is NOT visible as b's result (and vice versa)
        assert sealed_a["status"] != sealed_b["status"] or \
            sealed_a["score"] != sealed_b["score"]

        # user A cannot accidentally display B's passport via the
        # user-scoped read path (and vice versa)
        vm.sender = direct_alice
        mine_a = json.loads(contract.get_my_verification(int(vid_b)))
        assert mine_a.get("error") == "not_authorized"
        vm.sender = direct_bob
        mine_b = json.loads(contract.get_my_verification(int(vid_a)))
        assert mine_b.get("error") == "not_authorized"

    def test_reversed_finalization_order_still_correlates(self, deployed,
                                                          direct_vm,
                                                          direct_alice,
                                                          direct_bob):
        """A requested FIRST, B requested SECOND — but B finalizes
        FIRST (async consensus). The correlation must stay A->A, B->B:
        exact ids, not ordering, identify records."""
        vm = direct_vm
        contract = deployed
        vid_a = int(request_as(vm, contract, direct_alice,
                               "Demo Research Agent", AGENT_URL, DOCS_URL,
                               json.dumps(["API_ACCESS"])))
        vid_b = int(seed_b_request(vm, contract, direct_bob,
                                   json.dumps(["MCP_SUPPORT"])))
        assert vid_a == 1 and vid_b == 2

        # B finalizes FIRST (reversed order)
        mock_body(vm, ".*", DOCS_BODY_STRONG)
        vm.mock_llm(".*", llm_caps([("MCP_SUPPORT", "VERIFIED")],
                                    agent_url=AGENT_URL_B,
                                    docs_url=DOCS_URL_B))
        out_b_first = json.loads(contract.verify_agent(vid_b))
        assert out_b_first["verification_id"] == str(vid_b)

        # then A finalizes
        vm.clear_mocks()
        mock_body(vm, ".*", DOCS_BODY_STRONG)
        vm.mock_llm(".*", llm_caps([("API_ACCESS", "UNVERIFIED")],
                                    quality="WEAK"))
        out_a_second = json.loads(contract.verify_agent(vid_a))
        assert out_a_second["verification_id"] == str(vid_a)

        sealed_a = json.loads(contract.get_verification(vid_a))
        sealed_b = json.loads(contract.get_verification(vid_b))
        assert sealed_a["agent_url"] == AGENT_URL
        assert sealed_b["agent_url"] == AGENT_URL_B
        assert sealed_a["declared_capabilities"] == ["API_ACCESS"]
        assert sealed_b["declared_capabilities"] == ["MCP_SUPPORT"]
        # B's earlier finalization cannot leak into A's record
        assert sealed_a["status"] == "UNVERIFIED"
        assert sealed_b["status"] in ("VERIFIED", "PARTIAL")

    def test_interleaved_third_request_cannot_rewire(self, deployed,
                                                     direct_vm,
                                                     direct_alice,
                                                     direct_bob,
                                                     direct_charlie):
        """A requests, B requests, C requests, THEN the frontend reads
        its own record. Under the old count-based logic A's UI would
        read count() == 3 and display C's record. With exact ids this
        cannot happen."""
        vm = direct_vm
        contract = deployed
        vid_a = int(request_as(vm, contract, direct_alice,
                               "Demo Research Agent", AGENT_URL, DOCS_URL,
                               json.dumps(["API_ACCESS"])))
        vid_b = int(seed_b_request(vm, contract, direct_bob,
                                   json.dumps(["MCP_SUPPORT"])))
        vid_c = int(request_as(vm, contract, direct_charlie,
                               "Gamma Agent",
                               "https://gamma.example.net", "",
                               json.dumps(["TOOL_CALLING"])))
        # the count is now 3 — a count-based frontend for A would read
        # record 3 (gamma). Exact-id correlation reads record 1.
        assert int(contract.get_verification_count()) == 3
        rec_a = json.loads(contract.get_verification(vid_a))
        assert rec_a["agent_url"] == AGENT_URL
        assert rec_a["owner"] == addr_str(direct_alice)
        rec_c = json.loads(contract.get_verification(vid_c))
        assert rec_c["agent_url"] == "https://gamma.example.net"
        assert rec_c["owner"] == addr_str(direct_charlie)


# ---------------------------------------------------------------------------
# USER OWNERSHIP / USER-SCOPED READS
# ---------------------------------------------------------------------------

class TestUserOwnership:
    def test_every_record_has_explicit_owner(self, deployed, direct_vm,
                                             direct_alice, direct_bob):
        vm = direct_vm
        contract = deployed
        vid_a = int(request_as(vm, contract, direct_alice,
                               "Demo Research Agent", AGENT_URL, DOCS_URL,
                               json.dumps(["API_ACCESS"])))
        vid_b = int(seed_b_request(vm, contract, direct_bob,
                                   json.dumps(["MCP_SUPPORT"])))
        assert json.loads(
            contract.get_verification(vid_a))["owner"] == \
            addr_str(direct_alice)
        assert json.loads(
            contract.get_verification(vid_b))["owner"] == \
            addr_str(direct_bob)

    def test_get_my_verification_owner_only(self, deployed, direct_vm,
                                            direct_alice, direct_bob):
        vm = direct_vm
        contract = deployed
        vid_a = int(request_as(vm, contract, direct_alice,
                               "Demo Research Agent", AGENT_URL, DOCS_URL,
                               json.dumps(["API_ACCESS"])))
        # owner reads own record fine
        vm.sender = direct_alice
        mine = json.loads(contract.get_my_verification(int(vid_a)))
        assert mine.get("error") is None
        assert mine["agent_url"] == AGENT_URL
        # non-owner gets explicit authorization error — NO agent data,
        # evidence URLs, result, or passport contents leaked
        vm.sender = direct_bob
        denied = json.loads(contract.get_my_verification(int(vid_a)))
        assert denied.get("error") == "not_authorized"
        assert "agent_url" not in denied
        assert "evidence_sources" not in denied
        assert "capability_details" not in denied

    def test_get_my_verifications_scoped_per_user(self, deployed,
                                                  direct_vm,
                                                  direct_alice,
                                                  direct_bob):
        vm = direct_vm
        contract = deployed
        request_as(vm, contract, direct_alice, "Demo Research Agent",
                   AGENT_URL, DOCS_URL, json.dumps(["API_ACCESS"]))
        request_as(vm, contract, direct_alice, "Research Agent 2",
                   "https://research2.example.com", "",
                   json.dumps(["WEB_RESEARCH"]))
        seed_b_request(vm, contract, direct_bob,
                       json.dumps(["MCP_SUPPORT"]))

        vm.sender = direct_alice
        mine = json.loads(contract.get_my_verifications(50, 0))
        assert mine["total"] == 2
        urls = {v["agent_url"] for v in mine["verifications"]}
        assert urls == {AGENT_URL, "https://research2.example.com"}

        vm.sender = direct_bob
        bob_mine = json.loads(contract.get_my_verifications(50, 0))
        assert bob_mine["total"] == 1
        assert bob_mine["verifications"][0]["agent_url"] == AGENT_URL_B

    def test_public_registry_views_stay_public(self, deployed, direct_vm,
                                               direct_alice, direct_bob):
        """Passports are PUBLIC by design (public claim about a public
        page): the explorer path get_verification/list_verifications
        intentionally remains world-readable. User isolation applies
        to the per-user workflow, not to passport visibility."""
        vm = direct_vm
        contract = deployed
        request_as(vm, contract, direct_alice, "Demo Research Agent",
                   AGENT_URL, DOCS_URL, json.dumps(["API_ACCESS"]))
        request_as(vm, contract, direct_bob, "ToolBot Alpha",
                   AGENT_URL_B, DOCS_URL_B, json.dumps(["MCP_SUPPORT"]))
        listing = json.loads(contract.list_verifications(50, 0))
        assert listing["total"] == 2
        rec = json.loads(contract.get_verification(1))
        assert rec["agent_url"] == AGENT_URL


# ---------------------------------------------------------------------------
# EVIDENCE PROVENANCE — unique, successfully fetched, SUBMITTED urls only
# ---------------------------------------------------------------------------

class TestEvidenceProvenance:
    def test_duplicate_urls_collapse_to_one_entry(self, deployed,
                                                  direct_vm,
                                                  direct_alice):
        """agent URL and docs URL differing only by trailing slash (or
        case) normalize to the SAME canonical URL -> ONE evidence
        entry, ONE fetch, no double counting."""
        vm = direct_vm
        contract = deployed
        vm.sender = direct_alice
        vid = int(contract.request_verification(
            "Dedup Agent", "https://dup.example.com/",
            "https://dup.example.com",
            json.dumps(["API_ACCESS"]), ""))
        rec = json.loads(contract.get_verification(vid))
        # submitted two urls; normalized to one canonical source
        assert len(rec["sources"]) == 1
        assert rec["sources"][0]["normalized_url"] == \
            "https://dup.example.com"

        mock_body(vm, ".*", DOCS_BODY_STRONG)
        vm.mock_llm(".*", llm_caps([("API_ACCESS", "VERIFIED")],
                                   agent_url="https://dup.example.com/",
                                   docs_url=""))
        contract.verify_agent(vid)
        sealed = json.loads(contract.get_verification(vid))
        urls = [s["normalized_url"] for s in sealed["evidence_sources"]]
        assert len(urls) == 1
        assert urls[0] == "https://dup.example.com"
        # provenance: single entry, fetch success, used as evidence
        prov = sealed["source_provenance"]
        assert len(prov) == 1
        assert prov[0]["fetch_success"] is True
        assert prov[0]["http_status"] == 200
        assert prov[0]["used_as_evidence"] is True

    def test_404_url_never_sealed_as_evidence(self, deployed, direct_vm,
                                              direct_alice):
        vm = direct_vm
        contract = deployed
        vid = int(request_as(vm, contract, direct_alice,
                             "Demo Research Agent", AGENT_URL, DOCS_URL,
                             json.dumps(["API_ACCESS"])))
        mock_body(vm, ".*researchbot\\.example\\.com/?$",
                  "<html>Not Found</html>", status=404)
        mock_body(vm, ".*researchbot\\.example\\.com/docs",
                  DOCS_BODY_STRONG)
        # LLM (obedient to the page) still describes 404 source:
        vm.mock_llm(".*", llm_caps(
            [("API_ACCESS", "VERIFIED")],
            sources=[H._src(AGENT_URL, "STRONG"),
                     H._src(DOCS_URL, "STRONG")]))
        contract.verify_agent(vid)
        sealed = json.loads(contract.get_verification(vid))
        sealed_urls = [s["normalized_url"]
                       for s in sealed["evidence_sources"]]
        assert sealed_urls == ["https://researchbot.example.com/docs"]
        # provenance records the 404 honestly
        prov = {p["normalized_url"]: p
                for p in sealed["source_provenance"]}
        assert prov[AGENT_URL]["http_status"] == 404
        assert prov[AGENT_URL]["fetch_success"] is False
        assert prov[AGENT_URL]["used_as_evidence"] is False

    def test_404_docs_seals_inconclusive_not_stuck(self, deployed,
                                                   direct_vm,
                                                   direct_alice):
        """LIVE S6 regression (first smoke run failed here): a
        DOCUMENTATION capability whose submitted docs URL 404s must
        SEAL with DOCUMENTATION=INCONCLUSIVE — R10 makes fetch failure
        authoritative, so 'unreachable docs' can never be ruled
        UNVERIFIED (a dead URL proves nothing) and the record can
        never get stuck PENDING on leader/validator disagreement."""
        vm = direct_vm
        contract = deployed
        vid = int(request_as(vm, contract, direct_alice,
                             "BrokenDocs Agent", AGENT_URL, DOCS_URL,
                             json.dumps(["DOCUMENTATION"])))
        mock_body(vm, ".*researchbot\\.example\\.com/?$",
                  DOCS_BODY_STRONG)                     # agent page 200
        mock_body(vm, ".*researchbot\\.example\\.com/docs",
                  "<html>Not Found</html>", status=404)  # docs URL 404
        # honest evaluation per R10: fetch failure -> INCONCLUSIVE
        vm.mock_llm(".*", llm_caps(
            [("DOCUMENTATION", "INCONCLUSIVE")],
            sources=[H._src(AGENT_URL, "STRONG"),
                     H._src(DOCS_URL, "NONE")]))
        out = json.loads(contract.verify_agent(vid))
        sealed = json.loads(contract.get_verification(vid))
        # the record MUST seal (never stuck PENDING) — the live-S6 bug
        assert sealed["state"] == "SEALED", sealed
        # fetch failure is authoritative: DOCUMENTATION is INCONCLUSIVE
        # (never UNVERIFIED — R10), never VERIFIED
        assert sealed["inconclusive_capabilities"] == ["DOCUMENTATION"]
        assert sealed["unsupported_capabilities"] == []
        assert sealed["verified_capabilities"] == []
        assert out["status"] in ("INCONCLUSIVE", "UNVERIFIED", "PARTIAL")
        # the 404 URL is provenance-recorded and not sealed evidence
        prov = {p["normalized_url"]: p
                for p in sealed["source_provenance"]}
        assert prov[DOCS_URL]["http_status"] == 404
        assert prov[DOCS_URL]["fetch_success"] is False
        assert prov[DOCS_URL]["used_as_evidence"] is False
        sealed_urls = [s["normalized_url"]
                       for s in sealed["evidence_sources"]]
        assert DOCS_URL not in sealed_urls

    def test_validator_rejects_unverified_from_fetch_failure(self, deployed,
                                                             direct_vm,
                                                             direct_alice):
        """LIVE S6 regression, validator side: a leader that rules a
        capability UNVERIFIED because its URL failed to fetch (the
        pre-R10 behavior seen on Studionet) must be REJECTED — the
        validator's own honest evaluation says INCONCLUSIVE (R4+R10)
        and the capability statuses disagree."""
        vm = direct_vm
        contract = deployed
        vid = int(request_as(vm, contract, direct_alice,
                             "BrokenDocs Agent", AGENT_URL, DOCS_URL,
                             json.dumps(["DOCUMENTATION"])))
        mock_body(vm, ".*researchbot\\.example\\.com/?$",
                  DOCS_BODY_STRONG)
        mock_body(vm, ".*researchbot\\.example\\.com/docs",
                  "<html>Not Found</html>", status=404)
        # honest LLM (what validators independently produce): 404 ->
        # INCONCLUSIVE
        vm.mock_llm(".*", llm_caps(
            [("DOCUMENTATION", "INCONCLUSIVE")],
            sources=[H._src(AGENT_URL, "STRONG"),
                     H._src(DOCS_URL, "NONE")]))
        contract.verify_agent(int(vid))   # capture leader+validator
        # the pre-R10 leader lie: 'docs unreachable -> UNVERIFIED'
        unverified_from_fetch_failure = {
            "capabilities": [
                {"id": "DOCUMENTATION", "status": "UNVERIFIED",
                 "evidence": "docs URL failed to resolve",
                 "reason": "unreachable documentation URL"},
            ],
            "sources": [
                {"url": AGENT_URL, "type": "AGENT_WEBSITE",
                 "quality": "STRONG", "note": "ok"},
                {"url": DOCS_URL, "type": "DOCUMENTATION",
                 "quality": "NONE", "note": "404"},
            ],
            "http_statuses": [200, 404],
            "overall_inconclusive": False,
            "retrieval_failed": False,
            "conflict": False,
            "summary": "docs unreachable so DOCUMENTATION is unsupported",
        }
        accepted = vm.run_validator(
            leader_result=unverified_from_fetch_failure)
        assert accepted is False, (
            "validator must reject UNVERIFIED derived from a fetch "
            "failure (R4/R10: a dead URL proves nothing)")

    def test_500_url_never_sealed_as_evidence(self, deployed, direct_vm,
                                              direct_alice):
        vm = direct_vm
        contract = deployed
        vid = int(request_as(vm, contract, direct_alice,
                             "Demo Research Agent", AGENT_URL, DOCS_URL,
                             json.dumps(["API_ACCESS"])))
        mock_body(vm, ".*researchbot\\.example\\.com/?$",
                  "<html>Server Error</html>", status=500)
        mock_body(vm, ".*researchbot\\.example\\.com/docs",
                  DOCS_BODY_STRONG)
        vm.mock_llm(".*", llm_caps(
            [("API_ACCESS", "VERIFIED")],
            sources=[H._src(AGENT_URL, "STRONG"),
                     H._src(DOCS_URL, "STRONG")]))
        contract.verify_agent(vid)
        sealed = json.loads(contract.get_verification(vid))
        sealed_urls = [s["normalized_url"]
                       for s in sealed["evidence_sources"]]
        assert sealed_urls == ["https://researchbot.example.com/docs"]
        prov = {p["normalized_url"]: p
                for p in sealed["source_provenance"]}
        assert prov[AGENT_URL]["http_status"] == 500
        assert prov[AGENT_URL]["fetch_success"] is False

    def test_timeout_url_never_sealed_as_evidence(self, deployed,
                                                  direct_vm,
                                                  direct_alice):
        """No web mock for the agent URL: gltest raises
        MockNotFoundError -> contract's fetch try/except -> failed
        fetch (http_status 0, fetch_success False)."""
        vm = direct_vm
        contract = deployed
        vid = int(request_as(vm, contract, direct_alice,
                             "Demo Research Agent", AGENT_URL, DOCS_URL,
                             json.dumps(["API_ACCESS"])))
        mock_body(vm, ".*researchbot\\.example\\.com/docs",
                  DOCS_BODY_STRONG)
        vm.mock_llm(".*", llm_caps(
            [("API_ACCESS", "INCONCLUSIVE")],
            sources=[H._src(AGENT_URL, "STRONG"),
                     H._src(DOCS_URL, "STRONG")]))
        out = json.loads(contract.verify_agent(vid))
        sealed = json.loads(contract.get_verification(vid))
        sealed_urls = [s["normalized_url"]
                       for s in sealed["evidence_sources"]]
        assert sealed_urls == ["https://researchbot.example.com/docs"]
        prov = {p["normalized_url"]: p
                for p in sealed["source_provenance"]}
        assert prov[AGENT_URL]["fetch_success"] is False
        assert prov[AGENT_URL]["used_as_evidence"] is False
        # dead evidence never becomes VERIFIED
        assert out["status"] != "VERIFIED"

    def test_unsubmitted_url_mentioned_by_llm_excluded(self, deployed,
                                                       direct_vm,
                                                       direct_alice):
        """The LLM 'discovers' a URL in the page text (or hallucinates
        one). It was never SUBMITTED -> never sealed as evidence."""
        vm = direct_vm
        contract = deployed
        vid = int(request_as(vm, contract, direct_alice,
                             "Demo Research Agent", AGENT_URL, DOCS_URL,
                             json.dumps(["API_ACCESS"])))
        mock_body(vm, ".*", DOCS_BODY_STRONG)
        discovered = "https://evil.example.com/agent-card.json"
        vm.mock_llm(".*", llm_caps(
            [("API_ACCESS", "VERIFIED")],
            sources=[H._src(AGENT_URL, "STRONG"),
                     H._src(discovered, "STRONG"),
                     H._src(DOCS_URL, "STRONG")]))
        contract.verify_agent(vid)
        sealed = json.loads(contract.get_verification(vid))
        sealed_urls = [s["normalized_url"]
                       for s in sealed["evidence_sources"]]
        assert discovered not in sealed_urls
        assert set(sealed_urls) == {"https://researchbot.example.com",
                                    "https://researchbot.example.com/docs"}
        # discovered URL is not in provenance either (never submitted)
        prov_urls = {p["normalized_url"]
                     for p in sealed["source_provenance"]}
        assert discovered not in prov_urls

    def test_http_status_gate_blocks_none_quality_inflation(self, deployed,
                                                            direct_vm,
                                                            direct_alice):
        """A 404 page's HTML body ("Not Found") must not be treated as
        retrievable content: the source yields nothing (MIN_RETRIEVED
        policy applies to gated fetches only)."""
        vm = direct_vm
        contract = deployed
        vid = int(request_as(vm, contract, direct_alice,
                             "Doomed Agent",
                             "https://doomed.example.com", "",
                             json.dumps(["API_ACCESS"])))
        mock_body(vm, ".*", "<html>404 page content</html>", status=404)
        vm.mock_llm(".*", llm_caps([("API_ACCESS", "VERIFIED")],
                                    agent_url="https://doomed.example.com",
                                    docs_url="", quality="STRONG"))
        out = json.loads(contract.verify_agent(vid))
        # 404 body gated out -> no content -> INCONCLUSIVE overall
        assert out["status"] == "INCONCLUSIVE", out


# ---------------------------------------------------------------------------
# TAXONOMY AUTHORIZATION
# ---------------------------------------------------------------------------

class TestTaxonomyAuthorization:
    def test_non_admin_cannot_add_capability(self, deployed, direct_vm,
                                             direct_alice):
        vm = direct_vm
        contract = deployed
        vm.sender = direct_alice   # a regular user (not the deployer)
        with pytest.raises(Exception, match="owner"):
            contract.add_capability_definition(json.dumps({
                "id": "ESPIONAGE",
                "verification_criteria": "x" * 30,
            }))

    def test_non_admin_cannot_modify_capability(self, deployed, direct_vm,
                                                direct_alice):
        """No update method exists; the only mutation path
        (add_capability_definition) is owner-gated, and duplicates are
        rejected anyway — a non-admin trying to redefine WEB_RESEARCH
        via add hits BOTH walls."""
        vm = direct_vm
        contract = deployed
        vm.sender = direct_alice
        with pytest.raises(Exception, match="owner"):
            contract.add_capability_definition(json.dumps({
                "id": "WEB_RESEARCH",
                "verification_criteria": "just trust marketing pages "
                                         "please, this agent is great",
            }))

    def test_admin_can_modify_taxonomy(self, deployed, direct_vm):
        """The deployer (vm.sender at init = default_sender) can still
        extend the taxonomy — protected, not frozen."""
        vm = direct_vm
        contract = deployed
        # deployer is the fixture's default sender (direct_owner)
        vm.sender = vm._sender  # deployer context: default_sender set at deploy
        new_id = contract.add_capability_definition(json.dumps({
            "id": "VOICE_INTERFACE",
            "display_name": "Voice Interface",
            "description": "Agent accepts voice input.",
            "verification_criteria": "Documentation shows speech input "
                                     "endpoints or STT pipeline config.",
        }))
        assert new_id == "VOICE_INTERFACE"
        taxonomy = json.loads(contract.get_capability_taxonomy())
        assert "VOICE_INTERFACE" in [t["id"] for t in taxonomy]
        info = json.loads(contract.get_contract_info())
        assert info["taxonomy_updates"] == "1"

    def test_verification_uses_expected_taxonomy_version(self, deployed,
                                                          direct_vm,
                                                          direct_alice):
        vm = direct_vm
        contract = deployed
        vid = int(request_as(vm, contract, direct_alice,
                             "Demo Research Agent", AGENT_URL, DOCS_URL,
                             json.dumps(["API_ACCESS"])))
        rec = json.loads(contract.get_verification(vid))
        assert rec["taxonomy_version"] == "1.0"
        info = json.loads(contract.get_contract_info())
        assert info["taxonomy_version"] == rec["taxonomy_version"]
        # sealing preserves the stamp
        mock_body(vm, ".*", DOCS_BODY_STRONG)
        vm.mock_llm(".*", llm_caps([("API_ACCESS", "VERIFIED")]))
        contract.verify_agent(vid)
        sealed = json.loads(contract.get_verification(vid))
        assert sealed["taxonomy_version"] == "1.0"

    def test_taxonomy_is_immutable_for_regular_users(self, deployed,
                                                     direct_vm,
                                                     direct_alice,
                                                     direct_bob):
        """Every regular caller path to taxonomy mutation is blocked."""
        vm = direct_vm
        contract = deployed
        for user in (direct_alice, direct_bob):
            vm.sender = user
            with pytest.raises(Exception, match="owner"):
                contract.add_capability_definition(json.dumps({
                    "id": "USER_ADDED_%s" % ("A" if user is direct_alice
                                             else "B"),
                    "verification_criteria": "y" * 30,
                }))
        # and the taxonomy is unchanged
        ids = [t["id"] for t in
               json.loads(contract.get_capability_taxonomy())]
        assert "USER_ADDED_A" not in ids and "USER_ADDED_B" not in ids
        assert len(ids) == 8
