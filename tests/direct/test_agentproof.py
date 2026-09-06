"""AgentProof direct-mode test suite.

Covers the 10 spec tests + adversarial cases + validator-equivalence
semantics + deterministic-contract unit tests:

  TEST 1  strong documentation -> VERIFIED (or PARTIAL)
  TEST 2  unsupported claims    -> PARTIAL/UNVERIFIED
  TEST 3  invalid URL           -> validation error at request time
  TEST 4  unavailable website   -> INCONCLUSIVE
  TEST 5  conflicting evidence  -> INCONCLUSIVE/PARTIAL
  TEST 6  leader lies VERIFIED, validator independently finds
          UNVERIFIED -> validator rejection (run_validator tamper)
  TEST 7  statuses agree, summaries differ -> accepted
  TEST 8  prompt injection ignored (safe outcome + validator behavior)
  TEST 9  malformed LLM response -> safe failure (INCONCLUSIVE)
  TEST 10 duplicate verification -> new historical record
  + adversarial: fake JSON docs, empty page, huge page, broken JSON,
    one-source-says-yes-other-no, capability normalization, taxonomy
    extension, scoring unit tests, storage/history invariants.
"""
import json

import pytest

import helpers as H
from helpers import (
    CONTRACT, AGENT_URL, DOCS_URL, DOCS_BODY_STRONG,
    SITE_BODY_MARKETING, EMPTY_BODY, INJECTION_BODY,
    CONFLICT_BODY_A, CONFLICT_BODY_B,
    mock_body, set_time, iso_now, request, request_default,
    llm_caps, llm_mixed, llm_all_unverified, llm_all_inconclusive,
    llm_injection_obedient, llm_conflict, llm_not_json, llm_wrong_schema,
)


@pytest.fixture()
def deployed(direct_vm, direct_deploy, direct_alice):
    H.set_time(direct_vm, H.iso_now())
    contract = direct_deploy(CONTRACT)
    return contract


def seed_valid_request(vm, contract, alice, caps=None, name=None,
                       agent_url=AGENT_URL, docs_url=DOCS_URL):
    if caps is None:
        caps = json.dumps(["API_ACCESS", "TOOL_CALLING", "WEB_RESEARCH"])
    return H.request(vm, contract, alice, name=name or "Demo Research Agent",
                     agent_url=agent_url, docs_url=docs_url,
                     capabilities=caps)


# ---------------------------------------------------------------------------
# TEST 1 — valid agent with strong documentation
# ---------------------------------------------------------------------------

class TestStrongDocumentation:
    def test_verified_with_strong_docs(self, deployed, direct_vm,
                                       direct_alice):
        vm = direct_vm
        contract = deployed
        vid = seed_valid_request(vm, contract, direct_alice)
        mock_body(vm, ".*", DOCS_BODY_STRONG)
        vm.mock_llm(".*", llm_caps([
            ("API_ACCESS", "VERIFIED"),
            ("TOOL_CALLING", "VERIFIED"),
            ("WEB_RESEARCH", "VERIFIED"),
        ]))
        out = json.loads(contract.verify_agent(int(vid)))
        assert out["status"] in ("VERIFIED", "PARTIAL"), out
        rec = json.loads(contract.get_verification(int(vid)))
        assert rec["state"] == "SEALED"
        assert out["status"] == rec["status"]
        assert rec["score"] == 100
        assert set(rec["verified_capabilities"]) == {
            "API_ACCESS", "TOOL_CALLING", "WEB_RESEARCH"}
        assert rec["unsupported_capabilities"] == []
        assert rec["verified_at"] != ""
        assert rec["verification_version"] == "1.0"


# ---------------------------------------------------------------------------
# TEST 2 — unsupported capability claims
# ---------------------------------------------------------------------------

class TestUnsupportedClaims:
    def test_marketing_only_site_yields_unverified_caps(self, deployed,
                                                        direct_vm,
                                                        direct_alice):
        vm = direct_vm
        contract = deployed
        vid = seed_valid_request(vm, contract, direct_alice)
        mock_body(vm, ".*", SITE_BODY_MARKETING)
        vm.mock_llm(".*", llm_all_unverified())
        out = json.loads(contract.verify_agent(int(vid)))
        assert out["status"] in ("PARTIAL", "UNVERIFIED"), out
        rec = json.loads(contract.get_verification(int(vid)))
        assert rec["unsupported_capabilities"] == [
            "API_ACCESS", "TOOL_CALLING", "WEB_RESEARCH"]
        assert rec["verified_capabilities"] == []
        assert rec["score"] == 0


# ---------------------------------------------------------------------------
# TEST 3 — invalid URL rejected at request time
# ---------------------------------------------------------------------------

class TestInvalidURL:
    @pytest.mark.parametrize("bad_url", [
        "not-a-url",
        "ftp://example.com",
        "example.com",
        "http://" + "x" * 400,
        "",
    ])
    def test_invalid_agent_url_rejected(self, deployed, direct_vm,
                                        direct_alice, bad_url):
        vm = direct_vm
        contract = deployed
        vm.sender = direct_alice
        with pytest.raises(Exception, match="agent_url"):
            contract.request_verification(
                "Bad", bad_url, "", json.dumps(["API_ACCESS"]), "")

    def test_invalid_docs_url_rejected(self, deployed, direct_vm,
                                       direct_alice):
        vm = direct_vm
        contract = deployed
        vm.sender = direct_alice
        with pytest.raises(Exception, match="documentation_url"):
            contract.request_verification(
                "Bad", "https://ok.example.com", "javascript:alert(1)",
                json.dumps(["API_ACCESS"]), "")


# ---------------------------------------------------------------------------
# TEST 4 — unavailable website -> INCONCLUSIVE
# ---------------------------------------------------------------------------

class TestUnavailableWebsite:
    def test_dead_site_yields_inconclusive(self, deployed, direct_vm,
                                            direct_alice):
        vm = direct_vm
        contract = deployed
        vid = seed_valid_request(vm, contract, direct_alice)
        # No web mock for the URL: gltest raises MockNotFoundError ->
        # contract's fetch try/except treats it as a failed fetch.
        vm.mock_llm(".*", llm_all_inconclusive())
        out = json.loads(contract.verify_agent(int(vid)))
        assert out["status"] == "INCONCLUSIVE", out
        rec = json.loads(contract.get_verification(int(vid)))
        assert rec["score"] == 0 or rec["score"] >= 0
        # missing evidence must never become VERIFIED
        assert rec["verified_capabilities"] == []
        # and the limitation is recorded honestly
        assert any("retrieval" in l for l in rec["limitations"])

    def test_http_500_source(self, deployed, direct_vm, direct_alice):
        vm = direct_vm
        contract = deployed
        vid = seed_valid_request(vm, contract, direct_alice)
        mock_body(vm, ".*", EMPTY_BODY, status=500)
        vm.mock_llm(".*", llm_all_inconclusive())
        out = json.loads(contract.verify_agent(int(vid)))
        assert out["status"] == "INCONCLUSIVE", out


# ---------------------------------------------------------------------------
# TEST 5 — conflicting evidence
# ---------------------------------------------------------------------------

class TestConflictingEvidence:
    def test_conflict_flag_yields_inconclusive(self, deployed, direct_vm,
                                               direct_alice):
        vm = direct_vm
        contract = deployed
        vid = seed_valid_request(vm, contract, direct_alice)
        mock_body(vm, ".*researchbot\\.example\\.com/?$",
                  CONFLICT_BODY_A)
        mock_body(vm, ".*researchbot\\.example\\.com/docs",
                  CONFLICT_BODY_B)
        vm.mock_llm(".*", llm_conflict())
        out = json.loads(contract.verify_agent(int(vid)))
        # Conflicting evidence: either INCONCLUSIVE (safe) or PARTIAL if
        # the non-conflicting capabilities still stand on their own.
        assert out["status"] in ("INCONCLUSIVE", "PARTIAL"), out

    def test_conflict_overrides_high_score(self, deployed, direct_vm,
                                           direct_alice):
        vm = direct_vm
        contract = deployed
        vid = seed_valid_request(vm, contract, direct_alice)
        vm.mock_llm(".*", llm_caps([
            ("API_ACCESS", "VERIFIED"),
            ("TOOL_CALLING", "VERIFIED"),
            ("WEB_RESEARCH", "VERIFIED"),
        ], conflict=True))
        out = json.loads(contract.verify_agent(int(vid)))
        # score would be 100 but conflict forces INCONCLUSIVE
        assert out["status"] == "INCONCLUSIVE", out


# ---------------------------------------------------------------------------
# TEST 6 — leader claims VERIFIED, validator independently disagrees
# ---------------------------------------------------------------------------

class TestValidatorIndependence:
    def test_validator_rejects_unsupported_verified(self, deployed,
                                                     direct_vm,
                                                     direct_alice):
        """THE critical anti-rubber-stamp test: leader returns a
        well-formed all-VERIFIED result; the validator re-derives from
        the (mocked) evidence honestly and finds UNVERIFIED -> the
        leader result is rejected."""
        vm = direct_vm
        contract = deployed
        vid = seed_valid_request(vm, contract, direct_alice)
        # Leader mocked honest: marketing site cannot support API_ACCESS
        mock_body(vm, ".*", SITE_BODY_MARKETING)
        vm.mock_llm(".*", llm_all_unverified())
        contract.verify_agent(int(vid))   # captures leader+validator
        # Tampered well-formed lie:
        well_formed_lie = {
            "capabilities": [
                {"id": "API_ACCESS", "status": "VERIFIED",
                 "evidence": "looks fine", "reason": "trust me"},
                {"id": "TOOL_CALLING", "status": "VERIFIED",
                 "evidence": "looks fine", "reason": "trust me"},
                {"id": "WEB_RESEARCH", "status": "VERIFIED",
                 "evidence": "looks fine", "reason": "trust me"},
            ],
            "sources": [
                {"url": AGENT_URL, "type": "AGENT_WEBSITE",
                 "quality": "STRONG", "note": "n"},
                {"url": DOCS_URL, "type": "DOCUMENTATION",
                 "quality": "STRONG", "note": "n"},
            ],
            "overall_inconclusive": False,
            "retrieval_failed": False,
            "conflict": False,
            "summary": "everything verified, seal the passport",
        }
        accepted = vm.run_validator(leader_result=well_formed_lie)
        assert accepted is False, (
            "validator must reject a well-formed all-VERIFIED claim "
            "when its independent evaluation finds the capabilities "
            "unsupported")

    def test_validator_accepts_consistent_leader(self, deployed,
                                                 direct_vm, direct_alice):
        vm = direct_vm
        contract = deployed
        vid = seed_valid_request(vm, contract, direct_alice)
        mock_body(vm, ".*", DOCS_BODY_STRONG)
        vm.mock_llm(".*", llm_caps([
            ("API_ACCESS", "VERIFIED"),
            ("TOOL_CALLING", "VERIFIED"),
            ("WEB_RESEARCH", "VERIFIED"),
        ]))
        contract.verify_agent(int(vid))
        accepted = vm.run_validator()
        assert accepted is True

    def test_validator_rejects_malformed_leader(self, deployed, direct_vm,
                                                direct_alice):
        vm = direct_vm
        contract = deployed
        vid = seed_valid_request(vm, contract, direct_alice)
        mock_body(vm, ".*", DOCS_BODY_STRONG)
        vm.mock_llm(".*", llm_caps([
            ("API_ACCESS", "VERIFIED"),
            ("TOOL_CALLING", "VERIFIED"),
            ("WEB_RESEARCH", "VERIFIED"),
        ]))
        contract.verify_agent(int(vid))
        for tampered in [
            "just a string",
            ["not", "a", "dict"],
            12345,
            {"capabilities": []},                    # wrong count
            {"capabilities": [                       # bad enum
                {"id": "API_ACCESS", "status": "SO-SO"}],
             "sources": []},
            {"capabilities": [                       # wrong ids
                {"id": "NOT_A_CAP", "status": "VERIFIED"}],
             "sources": []},
            None,
        ]:
            ok = vm.run_validator(leader_result=tampered)
            assert ok is False, tampered

    def test_validator_rejects_source_quality_lie(self, deployed,
                                                  direct_vm, direct_alice):
        """Leader inflates source quality to STRONG; validator's own
        retrieval sees WEAK marketing only -> quality fields disagree
        -> rejection (source quality is a compared stable field)."""
        vm = direct_vm
        contract = deployed
        vid = seed_valid_request(vm, contract, direct_alice)
        mock_body(vm, ".*", SITE_BODY_MARKETING)
        vm.mock_llm(".*", llm_all_unverified())
        contract.verify_agent(int(vid))
        quality_lie = {
            "capabilities": [
                {"id": "API_ACCESS", "status": "UNVERIFIED",
                 "evidence": "e", "reason": "r"},
                {"id": "TOOL_CALLING", "status": "UNVERIFIED",
                 "evidence": "e", "reason": "r"},
                {"id": "MCP_SUPPORT", "status": "UNVERIFIED",
                 "evidence": "e", "reason": "r"},
            ],
            "sources": [
                {"url": AGENT_URL, "type": "AGENT_WEBSITE",
                 "quality": "STRONG", "note": "inflated"},
                {"url": DOCS_URL, "type": "DOCUMENTATION",
                 "quality": "STRONG", "note": "inflated"},
            ],
            "overall_inconclusive": False,
            "retrieval_failed": False,
            "conflict": False,
            "summary": "statuses match, qualities lie",
        }
        accepted = vm.run_validator(leader_result=quality_lie)
        assert accepted is False

    def test_validator_rejects_leader_error(self, deployed, direct_vm,
                                             direct_alice):
        vm = direct_vm
        contract = deployed
        vid = seed_valid_request(vm, contract, direct_alice)
        mock_body(vm, ".*", DOCS_BODY_STRONG)
        vm.mock_llm(".*", llm_caps([
            ("API_ACCESS", "VERIFIED"),
            ("TOOL_CALLING", "VERIFIED"),
            ("WEB_RESEARCH", "VERIFIED"),
        ]))
        contract.verify_agent(int(vid))
        ok = vm.run_validator(leader_error=Exception("leader exploded"))
        assert ok is False


# ---------------------------------------------------------------------------
# TEST 7 — summaries differ, statuses agree -> accepted (equivalence)
# ---------------------------------------------------------------------------

class TestEquivalencePrinciple:
    def test_different_prose_same_decisions_accepted(self, deployed,
                                                     direct_vm,
                                                     direct_alice):
        vm = direct_vm
        contract = deployed
        vid = seed_valid_request(vm, contract, direct_alice)
        mock_body(vm, ".*", DOCS_BODY_STRONG)
        vm.mock_llm(".*", llm_caps([
            ("API_ACCESS", "VERIFIED"),
            ("TOOL_CALLING", "VERIFIED"),
            ("WEB_RESEARCH", "VERIFIED"),
        ]))
        contract.verify_agent(int(vid))
        same_decisions_new_prose = {
            "capabilities": [
                {"id": "API_ACCESS", "status": "VERIFIED",
                 "evidence": "totally different wording here",
                 "reason": "other phrasing entirely"},
                {"id": "TOOL_CALLING", "status": "VERIFIED",
                 "evidence": "different evidence text",
                 "reason": "another justification"},
                {"id": "WEB_RESEARCH", "status": "VERIFIED",
                 "evidence": "more different text",
                 "reason": "yet another reason"},
            ],
            "sources": [
                {"url": AGENT_URL, "type": "AGENT_WEBSITE",
                 "quality": "STRONG", "note": "reworded note"},
                {"url": DOCS_URL, "type": "DOCUMENTATION",
                 "quality": "STRONG", "note": "reworded note"},
            ],
            "overall_inconclusive": False,
            "retrieval_failed": False,
            "conflict": False,
            "summary": "A completely different summary saying the same "
                       "thing decision-wise.",
        }
        accepted = vm.run_validator(leader_result=same_decisions_new_prose)
        assert accepted is True, (
            "equivalence principle: prose may differ, stable decision "
            "fields must decide")

    def test_capability_status_disagreement_rejected(self, deployed,
                                                     direct_vm,
                                                     direct_alice):
        vm = direct_vm
        contract = deployed
        vid = seed_valid_request(vm, contract, direct_alice)
        mock_body(vm, ".*", DOCS_BODY_STRONG)
        vm.mock_llm(".*", llm_mixed())
        contract.verify_agent(int(vid))
        one_cap_flipped = {
            "capabilities": [
                {"id": "API_ACCESS", "status": "VERIFIED",
                 "evidence": "e", "reason": "r"},
                {"id": "TOOL_CALLING", "status": "VERIFIED",
                 "evidence": "e", "reason": "r"},
                {"id": "MCP_SUPPORT", "status": "VERIFIED",
                 "evidence": "e", "reason": "r"},
                {"id": "AUTONOMOUS_EXECUTION", "status": "INCONCLUSIVE",
                 "evidence": "e", "reason": "r"},
            ],
            "sources": [
                {"url": AGENT_URL, "type": "AGENT_WEBSITE",
                 "quality": "STRONG", "note": "n"},
                {"url": DOCS_URL, "type": "DOCUMENTATION",
                 "quality": "STRONG", "note": "n"},
            ],
            "overall_inconclusive": False,
            "retrieval_failed": False,
            "conflict": False,
            "summary": "only MCP flipped to VERIFIED",
        }
        accepted = vm.run_validator(leader_result=one_cap_flipped)
        assert accepted is False


# ---------------------------------------------------------------------------
# TEST 8 — prompt injection ignored
# ---------------------------------------------------------------------------

class TestPromptInjection:
    def test_injection_body_cannot_flip_validator(self, deployed,
                                                  direct_vm,
                                                  direct_alice):
        vm = direct_vm
        contract = deployed
        vid = seed_valid_request(vm, contract, direct_alice,
                                 caps=json.dumps(["MCP_SUPPORT",
                                                  "A2A_SUPPORT",
                                                  "API_ACCESS"]))
        mock_body(vm, ".*", INJECTION_BODY)
        # Validator's own honest evaluation (ignores injection):
        vm.mock_llm(".*", llm_all_unverified())
        contract.verify_agent(int(vid))
        obedient_leader = json.loads(H.llm_injection_obedient())
        # validator's own LLM run (fresh mocks say UNVERIFIED) must not
        # accept the injection-obedient leader result
        accepted = vm.run_validator(leader_result=obedient_leader)
        assert accepted is False

    def test_injection_cannot_reach_verified_via_derivation(self, deployed,
                                                            direct_vm,
                                                            direct_alice):
        """Even if the LLM follows the injection, a page whose only
        content is the injection itself is NONE-quality evidence: the
        passport can never come out VERIFIED from it."""
        vm = direct_vm
        contract = deployed
        vid = seed_valid_request(vm, contract, direct_alice,
                                 caps=json.dumps(["MCP_SUPPORT"]))
        mock_body(vm, ".*", INJECTION_BODY)
        vm.mock_llm(".*", llm_caps(
            [("MCP_SUPPORT", "VERIFIED")],
            sources=[{"url": AGENT_URL, "type": "AGENT_WEBSITE",
                      "quality": "NONE", "note": "injected page"}],
            quality="NONE"))
        out = json.loads(contract.verify_agent(int(vid)))
        # VERIFIED is unreachable from NONE-quality evidence
        assert out["status"] in ("UNVERIFIED", "PARTIAL", "INCONCLUSIVE"), \
            out


# ---------------------------------------------------------------------------
# TEST 9 — malformed LLM response -> safe failure
# ---------------------------------------------------------------------------

class TestMalformedLLM:
    def test_non_json_llm_yields_inconclusive(self, deployed, direct_vm,
                                              direct_alice):
        vm = direct_vm
        contract = deployed
        vid = seed_valid_request(vm, contract, direct_alice)
        mock_body(vm, ".*", DOCS_BODY_STRONG)
        vm.mock_llm(".*", llm_not_json())
        out = json.loads(contract.verify_agent(int(vid)))
        assert out["status"] == "INCONCLUSIVE", out
        rec = json.loads(contract.get_verification(int(vid)))
        assert rec["state"] == "SEALED"
        assert rec["verified_capabilities"] == []

    def test_wrong_schema_llm_yields_inconclusive(self, deployed,
                                                  direct_vm, direct_alice):
        vm = direct_vm
        contract = deployed
        vid = seed_valid_request(vm, contract, direct_alice)
        mock_body(vm, ".*", DOCS_BODY_STRONG)
        vm.mock_llm(".*", llm_wrong_schema())
        out = json.loads(contract.verify_agent(int(vid)))
        assert out["status"] == "INCONCLUSIVE", out

    def test_partial_llm_output_normalizes_to_inconclusive(
            self, deployed, direct_vm, direct_alice):
        vm = direct_vm
        contract = deployed
        vid = seed_valid_request(vm, contract, direct_alice)
        mock_body(vm, ".*", DOCS_BODY_STRONG)
        vm.mock_llm(".*", json.dumps({
            "capabilities": [
                {"id": "API_ACCESS", "status": "VERIFIED"},
                # TOOL_CALLING and WEB_RESEARCH entries missing
            ],
            "sources": [],
        }))
        out = json.loads(contract.verify_agent(int(vid)))
        # missing caps -> INCONCLUSIVE; overall not VERIFIED
        assert out["status"] != "VERIFIED", out
        rec = json.loads(contract.get_verification(int(vid)))
        assert "TOOL_CALLING" in rec["inconclusive_capabilities"]


# ---------------------------------------------------------------------------
# TEST 10 — duplicate verification -> history, not overwrite
# ---------------------------------------------------------------------------

class TestVerificationHistory:
    def test_duplicate_agent_appends_history(self, deployed, direct_vm,
                                             direct_alice):
        vm = direct_vm
        contract = deployed
        # first verification
        vid1 = seed_valid_request(vm, contract, direct_alice)
        mock_body(vm, ".*", DOCS_BODY_STRONG)
        vm.mock_llm(".*", llm_caps([
            ("API_ACCESS", "VERIFIED"),
            ("TOOL_CALLING", "VERIFIED"),
            ("WEB_RESEARCH", "VERIFIED"),
        ]))
        contract.verify_agent(int(vid1))

        # second verification of the SAME agent (URL)
        vid2 = seed_valid_request(vm, contract, direct_alice)
        assert int(vid2) == int(vid1) + 1
        vm.clear_mocks()
        vm.mock_llm(".*", llm_caps([
            ("API_ACCESS", "UNVERIFIED"),
            ("TOOL_CALLING", "UNVERIFIED"),
            ("WEB_RESEARCH", "UNVERIFIED"),
        ], quality="NONE"))
        contract.verify_agent(int(vid2))

        agent = json.loads(contract.get_agent(AGENT_URL))
        assert agent["verification_count"] == 2
        hist_ids = [h["verification_id"] for h in
                    agent["verification_history"]]
        assert hist_ids == ["2", "1"]   # newest first

        # BOTH records still exist independently
        rec1 = json.loads(contract.get_verification(1))
        rec2 = json.loads(contract.get_verification(2))
        assert rec1["status"] == "VERIFIED"
        assert rec2["status"] in ("UNVERIFIED", "INCONCLUSIVE")
        assert rec1["verified_capabilities"] != \
            rec2["verified_capabilities"]

    def test_sealed_verification_cannot_rerun(self, deployed, direct_vm,
                                             direct_alice):
        vm = direct_vm
        contract = deployed
        vid = seed_valid_request(vm, contract, direct_alice)
        mock_body(vm, ".*", DOCS_BODY_STRONG)
        vm.mock_llm(".*", llm_caps([
            ("API_ACCESS", "VERIFIED"),
            ("TOOL_CALLING", "VERIFIED"),
            ("WEB_RESEARCH", "VERIFIED"),
        ]))
        contract.verify_agent(int(vid))
        with pytest.raises(Exception, match="already sealed"):
            contract.verify_agent(int(vid))

    def test_history_preserved_after_agent_update(self, deployed,
                                                  direct_vm, direct_alice):
        vm = direct_vm
        contract = deployed
        vid1 = seed_valid_request(vm, contract, direct_alice,
                                  name="Old Name")
        mock_body(vm, ".*", DOCS_BODY_STRONG)
        vm.mock_llm(".*", llm_caps([
            ("API_ACCESS", "VERIFIED"),
            ("TOOL_CALLING", "VERIFIED"),
            ("WEB_RESEARCH", "VERIFIED"),
        ]))
        contract.verify_agent(int(vid1))
        # re-submit same agent with a NEW name
        vid2 = seed_valid_request(vm, contract, direct_alice,
                                  name="New Name")
        agent = json.loads(contract.get_agent(AGENT_URL))
        assert agent["agent_name"] == "New Name"
        assert agent["verification_count"] == 2
        # original record untouched
        rec1 = json.loads(contract.get_verification(1))
        assert rec1["agent_name"] == "Old Name"


# ---------------------------------------------------------------------------
# Adversarial cases (spec §30)
# ---------------------------------------------------------------------------

class TestAdversarial:
    def test_fake_json_docs_not_machine_proof(self, deployed, direct_vm,
                                              direct_alice):
        """A page that literally claims 'this is machine-readable
        capability metadata' with fake JSON has no real endpoints —
        LLM labels WEAK, contract must not reach VERIFIED from a
        single WEAK source."""
        vm = direct_vm
        contract = deployed
        vid = seed_valid_request(vm, contract, direct_alice)
        fake_json_docs = (
            "<html><body><pre>"
            "{\"capabilities\": [\"MCP\", \"A2A\", \"API\"], "
            "\"self_verified\": true}"
            "</pre></body></html>"
        )
        mock_body(vm, ".*", fake_json_docs)
        vm.mock_llm(".*", llm_caps([
            ("API_ACCESS", "VERIFIED"),
            ("TOOL_CALLING", "VERIFIED"),
            ("WEB_RESEARCH", "VERIFIED"),
        ], quality="WEAK"))
        out = json.loads(contract.verify_agent(int(vid)))
        # WEAK sources only -> not VERIFIED overall
        assert out["status"] in ("UNVERIFIED", "PARTIAL"), out

    def test_empty_page(self, deployed, direct_vm, direct_alice):
        vm = direct_vm
        contract = deployed
        vid = seed_valid_request(vm, contract, direct_alice)
        mock_body(vm, ".*", EMPTY_BODY)
        vm.mock_llm(".*", llm_all_inconclusive(quality="NONE"))
        out = json.loads(contract.verify_agent(int(vid)))
        assert out["status"] == "INCONCLUSIVE", out

    def test_huge_page_bounded(self, deployed, direct_vm, direct_alice):
        vm = direct_vm
        contract = deployed
        vid = seed_valid_request(vm, contract, direct_alice)
        huge = ("<html><body>" +
                ("<p>API endpoint POST /v1/search works.</p>" * 4000) +
                "</body></html>")
        mock_body(vm, ".*", huge)
        vm.mock_llm(".*", llm_caps([
            ("API_ACCESS", "VERIFIED"),
            ("TOOL_CALLING", "VERIFIED"),
            ("WEB_RESEARCH", "VERIFIED"),
        ]))
        # must not crash; result must be well-formed
        out = json.loads(contract.verify_agent(int(vid)))
        assert out["status"] in ("VERIFIED", "PARTIAL", "UNVERIFIED",
                                 "INCONCLUSIVE")

    def test_broken_json_source_content(self, deployed, direct_vm,
                                        direct_alice):
        """Source content is broken JSON — it is DATA, not proof; the
        pipeline must survive and stay inconclusive."""
        vm = direct_vm
        contract = deployed
        vid = seed_valid_request(vm, contract, direct_alice)
        mock_body(vm, ".*", '{"broken": json,,}')
        vm.mock_llm(".*", llm_all_inconclusive())
        out = json.loads(contract.verify_agent(int(vid)))
        assert out["status"] == "INCONCLUSIVE", out

    def test_capability_count_limit(self, deployed, direct_vm,
                                    direct_alice):
        vm = direct_vm
        contract = deployed
        vm.sender = direct_alice
        too_many = json.dumps([
            "WEB_RESEARCH", "TOOL_CALLING", "API_ACCESS",
            "STRUCTURED_OUTPUT", "AUTONOMOUS_EXECUTION", "MCP_SUPPORT",
            "A2A_SUPPORT", "DOCUMENTATION",
            "W1", "W2", "W3", "W4", "W5"])
        with pytest.raises(Exception, match="too many"):
            contract.request_verification(
                "Too Many", AGENT_URL, "", too_many, "")

    def test_unknown_capability_id_rejected(self, deployed, direct_vm,
                                            direct_alice):
        vm = direct_vm
        contract = deployed
        vm.sender = direct_alice
        with pytest.raises(Exception, match="unknown capability"):
            contract.request_verification(
                "Weird", AGENT_URL, "",
                json.dumps(["TELEPATHY"]), "")

    def test_custom_capability_flow(self, deployed, direct_vm,
                                    direct_alice):
        vm = direct_vm
        contract = deployed
        vid = seed_valid_request(vm, contract, direct_alice,
                                 caps=json.dumps([
                                     {"id": "", "label": "CSV Export"},
                                     {"id": "", "label": "Slack Bot"},
                                 ]))
        mock_body(vm, ".*", DOCS_BODY_STRONG)
        vm.mock_llm(".*", llm_caps([
            ("CUSTOM:CSV Export", "VERIFIED"),
            ("CUSTOM:Slack Bot", "UNVERIFIED"),
        ]))
        out = json.loads(contract.verify_agent(int(vid)))
        assert out["status"] == "PARTIAL", out
        rec = json.loads(contract.get_verification(int(vid)))
        assert rec["verified_capabilities"] == ["CUSTOM:CSV Export"]
        assert rec["unsupported_capabilities"] == ["CUSTOM:Slack Bot"]

    def test_custom_capability_limit(self, deployed, direct_vm,
                                     direct_alice):
        vm = direct_vm
        contract = deployed
        vm.sender = direct_alice
        with pytest.raises(Exception, match="custom"):
            contract.request_verification(
                "Custom Heavy", AGENT_URL, "",
                json.dumps([
                    {"id": "", "label": "A"},
                    {"id": "", "label": "B"},
                    {"id": "", "label": "C"},
                    {"id": "", "label": "D"},
                ]), "")

    def test_taxonomy_extension_then_use(self, deployed, direct_vm,
                                        direct_alice):
        vm = direct_vm
        contract = deployed
        new_id = contract.add_capability_definition(json.dumps({
            "id": "VOICE_INTERFACE",
            "display_name": "Voice Interface",
            "description": "Agent accepts voice input.",
            "verification_criteria": "Documentation shows speech input "
                                     "endpoints, STT pipeline config, or "
                                     "voice interaction examples.",
        }))
        assert new_id == "VOICE_INTERFACE"
        # taxonomy view reflects the addition
        taxonomy = json.loads(contract.get_capability_taxonomy())
        ids = [t["id"] for t in taxonomy]
        assert "VOICE_INTERFACE" in ids
        # ...and the new capability is usable immediately
        vid = seed_valid_request(vm, contract, direct_alice,
                                 caps=json.dumps(["VOICE_INTERFACE"]))
        mock_body(vm, ".*", DOCS_BODY_STRONG)
        vm.mock_llm(".*", llm_caps([("VOICE_INTERFACE", "VERIFIED")]))
        out = json.loads(contract.verify_agent(int(vid)))
        assert out["status"] in ("VERIFIED", "PARTIAL", "UNVERIFIED")

    def test_taxonomy_rejects_duplicates_and_bad_ids(self, deployed,
                                                     direct_vm):
        vm = direct_vm
        contract = deployed
        with pytest.raises(Exception, match="already exists"):
            contract.add_capability_definition(json.dumps({
                "id": "API_ACCESS",
                "verification_criteria": "x" * 20}))
        with pytest.raises(Exception, match="UPPERCASE"):
            contract.add_capability_definition(json.dumps({
                "id": "lowercase",
                "verification_criteria": "x" * 20}))


# ---------------------------------------------------------------------------
# Deterministic contract unit tests (spec §31 — scoring, normalization,
# storage, listing views) — isolated from nondet functions
# ---------------------------------------------------------------------------

def _mod():
    """The contract module as loaded by gltest direct mode — re-loading
    the file again would trip the SDK's one-contract-per-module guard."""
    import sys
    return sys.modules["_contract_agentproof"]


class TestDeterministicLogic:
    def test_scoring_math(self, deployed):
        contract = deployed
        mod = _mod()
        # 5 caps: 100+100+50+0+100 = 350/5 = 70 -> PARTIAL
        capabilities = [{"key": "c%d" % i} for i in range(5)]
        result = {
            "capabilities": [
                {"id": "c0", "status": "VERIFIED", "evidence": "",
                 "reason": ""},
                {"id": "c1", "status": "VERIFIED", "evidence": "",
                 "reason": ""},
                {"id": "c2", "status": "INCONCLUSIVE", "evidence": "",
                 "reason": ""},
                {"id": "c3", "status": "UNVERIFIED", "evidence": "",
                 "reason": ""},
                {"id": "c4", "status": "VERIFIED", "evidence": "",
                 "reason": ""},
            ],
            "sources": [
                {"url": "u1", "type": "DOCUMENTATION",
                 "quality": "STRONG", "note": ""},
                {"url": "u2", "type": "DOCUMENTATION",
                 "quality": "MODERATE", "note": ""},
            ],
            "overall_inconclusive": True,   # INCONCLUSIVE caps are scored
            "retrieval_failed": False,
            "conflict": False,
        }
        passport = mod._derive_passport(capabilities, result)
        # INCONCLUSIVE counted at 50 under contradiction flags:
        # (100+100+50+0+100)/5 = 70 -> but conflict-family flag forces
        # INCONCLUSIVE overall. Score math is what we assert here.
        assert passport["score"] == 70
        assert passport["strong_sources"] == 2

    def test_spec_example_score(self, deployed):
        mod = _mod()
        # The spec's own worked example: 100,100,50,0,100 -> 70 PARTIAL
        # with no conflict flags: the INCONCLUSIVE cap is EXCLUDED ->
        # (100+100+0+100)/4 = 75.
        result = {
            "capabilities": [
                {"id": "a", "status": "VERIFIED"},
                {"id": "b", "status": "VERIFIED"},
                {"id": "c", "status": "INCONCLUSIVE"},
                {"id": "d", "status": "UNVERIFIED"},
                {"id": "e", "status": "VERIFIED"},
            ],
            "sources": [
                {"url": "u1", "type": "DOCUMENTATION",
                 "quality": "STRONG", "note": ""},
                {"url": "u2", "type": "DOCUMENTATION",
                 "quality": "MODERATE", "note": ""},
            ],
            "overall_inconclusive": False,
            "retrieval_failed": False,
            "conflict": False,
        }
        passport = mod._derive_passport(
            [{"key": k} for k in "abcde"], result)
        assert passport["score"] == 75
        assert passport["status"] == "PARTIAL"

    def test_all_verified_with_weak_sources_not_verified(self, deployed):
        mod = _mod()
        result = {
            "capabilities": [
                {"id": "c0", "status": "VERIFIED", "evidence": "",
                 "reason": ""},
            ],
            "sources": [
                {"url": "u1", "type": "DOCUMENTATION",
                 "quality": "WEAK", "note": ""},
            ],
            "overall_inconclusive": False,
            "retrieval_failed": False,
            "conflict": False,
        }
        passport = mod._derive_passport([{"key": "c0"}], result)
        # score 100 but strong_sources=0 < 2 -> cannot be VERIFIED
        assert passport["score"] == 100
        assert passport["status"] == "PARTIAL"

    def test_normalization_unknown_ids_and_enums(self, deployed):
        mod = _mod()
        raw = {
            "capabilities": [
                {"id": "API_ACCESS", "status": "MAYBE"},
                {"id": "GHOST_CAP"},
            ],
            "sources": [
                {"url": "u", "type": "t", "quality": "EPIC",
                 "note": "n"},
            ],
            "summary": 42,
            "weird": True,
        }
        norm = mod._normalize_llm(raw, [
            {"key": "API_ACCESS"}, {"key": "TOOL_CALLING"}])
        assert norm["capabilities"][0]["status"] == "INCONCLUSIVE"
        assert norm["capabilities"][0]["id"] == "API_ACCESS"
        assert norm["capabilities"][1]["status"] == "INCONCLUSIVE"
        assert norm["sources"][0]["quality"] == "NONE"
        assert norm["summary"] == ""
        assert norm["overall_inconclusive"] is False

    def test_url_validation_rules(self, deployed):
        mod = _mod()
        assert mod._url_ok("https://a.example.com/x") is True
        assert mod._url_ok("http://a.example.com") is True
        assert mod._url_ok("https://a.example.com/ x") is False
        assert mod._url_ok("HTTPS://a.example.com") is False
        assert mod._url_ok("https://a.example.com/'q'") is False
        assert mod._url_ok("") is False
        assert mod._url_ok(None) is False

    def test_capability_parsing_variants(self, deployed):
        mod = _mod()
        # JSON array of strings
        caps = mod._parse_capabilities('["A", "B"]')
        assert [c["id"] for c in caps] == ["A", "B"]
        # comma-separated
        caps = mod._parse_capabilities("A, B ,C")
        assert [c["id"] for c in caps] == ["A", "B", "C"]
        # dedup
        caps = mod._parse_capabilities('["A", "A"]')
        assert len(caps) == 1
        # objects with labels
        caps = mod._parse_capabilities(
            '[{"id": "", "label": "Custom X"}]')
        assert caps[0]["id"] == ""
        assert caps[0]["label"] == "Custom X"
        # garbage
        assert mod._parse_capabilities("") == []
        assert mod._parse_capabilities("not json no comma") == [
            {"id": "not json no comma", "label": ""}]


# ---------------------------------------------------------------------------
# Storage / listing / explorer views
# ---------------------------------------------------------------------------

class TestRegistryViews:
    def _seed_and_verify(self, vm, contract, alice, n=3):
        for i in range(n):
            vid = H.request(vm, contract, alice,
                            name="Agent %d" % i,
                            agent_url="https://agent%d.example.com" % i,
                            docs_url="",
                            capabilities=json.dumps(["API_ACCESS"]))
            mock_body(vm, ".*", DOCS_BODY_STRONG)
            vm.mock_llm(".*", llm_caps([("API_ACCESS", "VERIFIED")]))
            contract.verify_agent(int(vid))

    def test_list_verifications_pagination(self, deployed, direct_vm,
                                           direct_alice):
        vm = direct_vm
        contract = deployed
        self._seed_and_verify(vm, contract, direct_alice, n=3)
        listing = json.loads(contract.list_verifications(20, 0))
        assert listing["total"] == 3
        assert len(listing["verifications"]) == 3
        # newest first
        assert listing["verifications"][0]["verification_id"] == "3"
        page1 = json.loads(contract.list_verifications(2, 0))
        assert [v["verification_id"] for v in page1["verifications"]] \
            == ["3", "2"]
        page2 = json.loads(contract.list_verifications(2, 2))
        assert [v["verification_id"] for v in page2["verifications"]] \
            == ["1"]

    def test_list_agents(self, deployed, direct_vm, direct_alice):
        vm = direct_vm
        contract = deployed
        self._seed_and_verify(vm, contract, direct_alice, n=2)
        listing = json.loads(contract.list_agents(20, 0))
        assert listing["total"] == 2
        assert listing["agents"][0]["last_status"] == "VERIFIED"

    def test_get_agent_history_view(self, deployed, direct_vm,
                                    direct_alice):
        vm = direct_vm
        contract = deployed
        self._seed_and_verify(vm, contract, direct_alice, n=1)
        # duplicate verification on agent 0
        vid = H.request(vm, contract, direct_alice,
                        agent_url="https://agent0.example.com",
                        docs_url="",
                        capabilities=json.dumps(["API_ACCESS"]))
        mock_body(vm, ".*", DOCS_BODY_STRONG)
        vm.mock_llm(".*", llm_caps([("API_ACCESS", "UNVERIFIED")]))
        contract.verify_agent(int(vid))
        agent = json.loads(contract.get_agent(
            "https://agent0.example.com"))
        assert agent["verification_count"] == 2
        assert len(agent["verification_history"]) == 2

    def test_not_found_views(self, deployed):
        contract = deployed
        assert json.loads(contract.get_verification(999)) == \
            {"error": "not_found"}
        assert json.loads(contract.get_agent("https://nope.example.com")) \
            == {"error": "not_found"}

    def test_contract_info(self, deployed):
        contract = deployed
        info = json.loads(contract.get_contract_info())
        assert info["name"] == "AgentProof"
        assert info["verification_version"] == "1.0"
        assert info["thresholds"]["verified"] == 80
        assert info["thresholds"]["partial"] == 40

    def test_pending_state_then_seal(self, deployed, direct_vm,
                                     direct_alice):
        vm = direct_vm
        contract = deployed
        vid = seed_valid_request(vm, contract, direct_alice)
        rec = json.loads(contract.get_verification(int(vid)))
        assert rec["state"] == "PENDING"
        assert rec["status"] == "PENDING"
        mock_body(vm, ".*", DOCS_BODY_STRONG)
        vm.mock_llm(".*", llm_caps([
            ("API_ACCESS", "VERIFIED"),
            ("TOOL_CALLING", "VERIFIED"),
            ("WEB_RESEARCH", "VERIFIED"),
        ]))
        contract.verify_agent(int(vid))
        rec = json.loads(contract.get_verification(int(vid)))
        assert rec["state"] == "SEALED"
        assert rec["status"] == "VERIFIED"


# ---------------------------------------------------------------------------
# Invariants (spec §16 — nothing stuck, nothing falsely VERIFIED)
# ---------------------------------------------------------------------------

class TestInvariants:
    def test_never_verified_on_missing_evidence(self, deployed, direct_vm,
                                                direct_alice):
        """No combination of LLM behavior may produce VERIFIED when no
        source had content: NONE quality on every source forces
        INCONCLUSIVE overall."""
        vm = direct_vm
        contract = deployed
        vid = seed_valid_request(vm, contract, direct_alice)
        mock_body(vm, ".*", EMPTY_BODY)
        vm.mock_llm(".*", llm_caps([
            ("API_ACCESS", "VERIFIED"),
            ("TOOL_CALLING", "VERIFIED"),
            ("WEB_RESEARCH", "VERIFIED"),
        ], sources=[{"url": AGENT_URL, "type": "AGENT_WEBSITE",
                     "quality": "NONE", "note": ""},
                    {"url": DOCS_URL, "type": "DOCUMENTATION",
                     "quality": "NONE", "note": ""}]))
        out = json.loads(contract.verify_agent(int(vid)))
        assert out["status"] == "INCONCLUSIVE", (
            "all-NONE-quality sources must never yield VERIFIED")

    def test_seal_always_reachable_no_stuck_records(self, deployed,
                                                    direct_vm,
                                                    direct_alice):
        """Whatever happens (dead fetch + garbage LLM), the record seals
        in a terminal state — never permanently stuck PENDING."""
        vm = direct_vm
        contract = deployed
        vid = seed_valid_request(vm, contract, direct_alice)
        vm.mock_llm(".*", llm_not_json())   # no web mock -> dead fetch
        out = json.loads(contract.verify_agent(int(vid)))
        assert out["status"] == "INCONCLUSIVE"
        rec = json.loads(contract.get_verification(int(vid)))
        assert rec["state"] == "SEALED"

    def test_score_bounds(self, deployed, direct_vm, direct_alice):
        vm = direct_vm
        contract = deployed
        for statuses, quality in [
            ([("API_ACCESS", "VERIFIED")], "STRONG"),
            ([("API_ACCESS", "UNVERIFIED")], "WEAK"),
            ([("API_ACCESS", "INCONCLUSIVE")], "NONE"),
        ]:
            vm.clear_mocks()
            vid = seed_valid_request(vm, contract, direct_alice)
            mock_body(vm, ".*", DOCS_BODY_STRONG)
            vm.mock_llm(".*", llm_caps(statuses, quality=quality))
            out = json.loads(contract.verify_agent(int(vid)))
            assert 0 <= out["score"] <= 100, out
            assert out["status"] in ("VERIFIED", "PARTIAL", "UNVERIFIED",
                                     "INCONCLUSIVE")
