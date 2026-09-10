# AgentProof

**Trustless AI Agent Capability Verification Network** — powered by GenLayer Intelligent Contracts.

> AgentProof uses GenLayer's decentralized AI-validator consensus to verify whether
> AI agents' publicly claimed capabilities are supported by independent web evidence.

Live on GenLayer Studionet: contract `0xd25Cc0910d67CBe06E228CAFD5382638DF9d9Ca4`
(full smoke S1–S7 ALL_OK — see `docs/deployment_log.json`),
dApp: `https://faisalnugroho.github.io/agentproof/`

> Steward remediation (v3): exact verification-ID correlation (no
> global-count reads), per-user record isolation, evidence restricted
> to unique successfully-fetched SUBMITTED urls, owner-only taxonomy.
> Older contracts remain on-chain as history only (readable, unused by
> the dApp): pre-remediation `0x87303D30DA71a47221A5fe5aD6aBC7350384B770`
> and the first remediation attempt
> `0x4239311F2eC2d3964ff114819A6e4c716Ef8594c` (superseded: its S6
> smoke exposed that a 404-evidence record could not seal — fixed by
> prompt rule R10 and redeployed).

---

## The problem

Today, an AI agent can simply *claim* capabilities:

- "I can execute web searches."
- "I support tool calling."
- "I expose an API."
- "I support MCP / A2A."
- "I provide autonomous task execution."

There is no trust-minimized way to verify those claims against publicly available
evidence. Agent marketplaces, directories and "agent registries" take these claims
at face value. Buyers of agent services — humans and other agents — have no way to
distinguish a well-documented agent from a landing page with "API-ready" in the hero.

## What AgentProof does

A **capability verification registry** — not an escrow, not a marketplace, no
payments anywhere in the product. The primary asset is the verification record:
an **Agent Passport**.

1. **SUBMIT** — anyone registers an AI agent: name, agent URL, optional
   documentation URL, and declared capabilities drawn from an on-chain taxonomy
   (Web Research, Tool Calling, API Access, Structured Output, Autonomous
   Execution, MCP, A2A, Documentation — plus up to 3 custom capabilities).
2. **RETRIEVE** — the Intelligent Contract fetches public evidence from the
   submitted URLs (bounded: 6,000 chars/source, 16,000 total, deterministic
   budget redistribution).
3. **EVALUATE** — the *leader* LLM evaluates every declared capability
   independently against explicit per-capability criteria, returning a strict
   structured verdict (VERIFIED / UNVERIFIED / INCONCLUSIVE per capability, plus
   per-source quality labels STRONG / MODERATE / WEAK / NONE).
4. **VERIFY** — GenLayer *validators independently re-fetch and re-evaluate the
   same evidence*, then compare the leader's result against their own. Only
   stable decision fields are compared (capability statuses, source qualities,
   failure flags) — never prose.
5. **SEAL** — after consensus, *deterministic contract code* derives the score
   and the overall status, and the passport is sealed on-chain. History is
   append-only: re-verifying the same agent adds a new record, never overwrites.

## Why GenLayer (and not an ordinary blockchain / oracle)

- **Ordinary smart contracts cannot judge web evidence.** A deterministic
  contract can store a URL and hash a response, but it cannot decide whether a
  page *actually documents* an MCP integration versus merely mentioning "MCP"
  in a marketing paragraph. That judgement is subjective, natural-language
  work — impossible in Solidity.
- **A single oracle is just another trusted party.** One LLM call behind an
  oracle means whoever operates the oracle (or biases the model, or poisons the
  prompt with the agent's own page content) decides every "verified" badge.
  AgentProof's core security property — *the leader is never trusted* — needs
  *independent* evaluations by multiple nodes.
- **GenLayer's Optimistic Democracy** gives exactly that: a leader proposes,
  validators independently verify, and the Equivalence Principle defines what
  "the same result" means for non-deterministic AI judgement:
  - **What IS compared:** per-capability statuses, per-source quality labels,
    failure/conflict flags — the decision fields that determine the outcome.
  - **What is NOT compared:** evidence prose, reasons, summaries — two honest
    evaluations may word the same conclusion differently.
- **The LLM never picks the final score.** After consensus, deterministic
  contract code computes the score (integer math): VERIFIED=100,
  UNVERIFIED=0, contradictory-inconclusive=50; overall = sum/evaluable;
  ≥80 **and** ≥2 STRONG/MODERATE sources ⇒ VERIFIED; 40–79 ⇒ PARTIAL;
  <40 ⇒ UNVERIFIED. Broad retrieval failure ⇒ INCONCLUSIVE overall —
  *silence is not disproof, and a dead URL never becomes a fake low score.*

## Security architecture

- **The nondet block is pure**: no storage reads/writes, no transfers, no
  events inside `run_nondet` — it only returns a normalized structure.
- **Exact-ID correlation**: `request_verification` returns the exact
  `verification_id` it created; the dApp reads it from its OWN tx receipt
  (`consensus_data.leader_receipt[0].result`) and polls
  `get_verification(<that id>)` only. The mutable global verification
  count is never used for correlation (concurrency-safe by construction).
- **User isolation**: every record stores `owner = gl.message.sender_address`.
  `get_my_verification` / `get_my_verifications` return ONLY the caller's
  records (explicit `not_authorized` otherwise — no agent data, evidence
  URLs, results or passport contents leak). Registry views remain public
  by design: a passport is a public claim about a public page.
- **Evidence provenance**: submitted URLs are deterministically
  normalized (fragment, trailing slash, scheme/host case) and deduped;
  fetches are HTTP-status-gated (2xx only); sealed evidence is
  restricted to unique, successfully fetched, SUBMITTED urls — URLs the
  LLM mentions but were never submitted, and dead (404/500/timeout)
  urls, are never sealed. Every record carries per-source provenance
  (`submitted_url`, `normalized_url`, `fetch_success`, `http_status`,
  `used_as_evidence`). Validators independently re-confirm the submitted
  urls' fetch statuses (a compared stable field).
- **Taxonomy authorization**: `add_capability_definition` is owner-only
  (deployer). Regular users can request verifications and select existing
  capabilities but cannot redefine what "verified" means. Each record is
  stamped with `taxonomy_version`.
- **Evidence is untrusted data.** The evaluation prompt forbids following
  instructions found inside fetched content (prompt-injection resistance);
  marketing language without endpoints, schemas, configs or protocol details
  is classified WEAK at best (R1–R10 rules in `contracts/agentproof.py`),
  and a fetch failure is authoritative (R10): a dead or erroring URL can
  never make a capability UNVERIFIED, only INCONCLUSIVE.
- **Every LLM failure mode maps to a safe outcome**: exception, non-JSON,
  malformed shape, dead fetch, missing fields → a well-formed INCONCLUSIVE
  record. Nothing can ever become VERIFIED without evidence a validator
  independently supports.
- **Validator rejection** (leader says VERIFIED, validator's own evaluation
  says otherwise; or leader derives a status from a fetch failure) →
  consensus fails → the run is retried/undetermined; it can never
  silently seal a lie.
- **Bounded everything**: URL/name/description/capability limits, content
  caps, 12-cap max, 3 custom-cap max, registry size cap.

## Scoring model (deterministic, post-consensus)

| Per capability | Score |
|---|---|
| VERIFIED | 100 |
| UNVERIFIED | 0 |
| INCONCLUSIVE (evidence existed but contradictory) | 50 |
| INCONCLUSIVE (no usable evidence) | excluded from score |

Overall = sum / evaluable-count. Status buckets: ≥80 VERIFIED (requires ≥2
STRONG/MODERATE sources), 40–79 PARTIAL, <40 UNVERIFIED, retrieval-dead ⇒
INCONCLUSIVE overall.

## Repository layout

```
contracts/agentproof.py     Intelligent Contract (leader/validator, registry)
tests/direct/               72 direct-mode tests: 10 spec cases + adversarial
                           + validator-equivalence + deterministic units
                           + 20 steward-remediation regressions
tests/helpers.py             mock web bodies, LLM builders, time warp
tests/frontend/              live exact-ID receipt extraction test (node)
frontend/                    single-page dApp (GitHub Pages, in-browser wallets)
scripts/deploy_smoke.py      Studionet deploy + consensus smoke (S1–S7)
docs/deployment_log.json     live evidence: tx hashes, verdicts, timings
```

## Testing

72/72 direct-mode tests (`genlayer-test` 0.29.2): the original 52-test
spec suite plus 20 steward-remediation tests covering exact-ID
correlation, concurrent-request isolation (two/three users, reversed
finalization order, interleaved requests), user ownership and
user-scoped reads (cross-user denial), evidence provenance (duplicate
URL collapse, 404/500/timeout exclusion, unsubmitted-URL exclusion,
fetch-failure sealing, validator rejection of fetch-failure-derived
statuses), and taxonomy authorization (non-admin add/modify rejected,
admin add works, taxonomy version preserved).

Core spec coverage:

1. strong documentation → VERIFIED
2. marketing-only claims → PARTIAL/UNVERIFIED
3. invalid URL → validation error at request time
4. unavailable website → INCONCLUSIVE
5. conflicting evidence → INCONCLUSIVE (conflict flag overrides high score)
6. leader lies VERIFIED, validator independently finds otherwise → rejection
7. same statuses, different prose → accepted (Equivalence Principle)
8. prompt injection in evidence → ignored (safe outcome both paths)
9. malformed LLM responses → safe INCONCLUSIVE
10. duplicate verification → appended history, old records preserved

Run them:

```bash
python -m venv .venv && .venv/bin/pip install genlayer-test
GENVMROOT=/tmp/genvmroot .venv/bin/python -m pytest tests/direct -q
```

Frontend integration test (exact-ID receipt extraction against live
receipts + structural no-global-count checks):

```bash
node tests/frontend/test_concurrent_isolation.mjs
```

## Live deployment

Deployed to GenLayer Studionet with a full consensus smoke (S1–S7):

- S1–S3: three consecutive consensus runs on identical evidence agree on
  the verdict family (Equivalence Principle — determinism at the
  decision-field level). Each run's verification id is read from its own
  request receipt (exact-ID correlation).
- S4: a marketing-only "negative" agent never yields VERIFIED.
- S5: registry views (`list_verifications`, `get_agent`) read back the
  full history.
- S6: a request whose docs URL is a stable 404 — the record seals, the 404
  URL never becomes normalized/sealed evidence, and its provenance shows
  `fetch_success=false`, `used_as_evidence=false`.
- S7: a non-owner `add_capability_definition` is rejected on-chain (the
  taxonomy keeps its 8 default entries).

All tx hashes, verdicts and timings: `docs/deployment_log.json`.

## Known limitations

- Verification judges **publicly documented evidence only** — an agent may have
  a capability that is real but undocumented; that will show UNVERIFIED/INCONCLUSIVE.
- Evidence is bounded (6k/source, 16k total chars) — very long docs are truncated.
- The taxonomy ships with 8 capability classes; the deployer can extend it
  on-chain via owner-only `add_capability_definition` (up to 30 total);
  regular users cannot modify it.
- Consensus latency on Studionet is ~1–2 min per verification write; the dApp
  polls FINALIZED state and never fakes progress.
- INCONCLUSIVE passports are honest by design: they say "evidence could not
  safely conclude", never a synthetic low score.

## License

MIT
