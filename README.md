# AgentProof

**Trustless AI Agent Capability Verification Network** — powered by GenLayer Intelligent Contracts.

> AgentProof uses GenLayer's decentralized AI-validator consensus to verify whether
> AI agents' publicly claimed capabilities are supported by independent web evidence.

Live on GenLayer Studionet: contract `docs/deployment_log.json → deploy.address`,
dApp: `https://faisalnugroho.github.io/agentproof/`

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
- **Evidence is untrusted data.** The evaluation prompt forbids following
  instructions found inside fetched content (prompt-injection resistance);
  marketing language without endpoints, schemas, configs or protocol details
  is classified WEAK at best (R1–R9 rules in `contracts/agentproof.py`).
- **Every LLM failure mode maps to a safe outcome**: exception, non-JSON,
  malformed shape, dead fetch, missing fields → a well-formed INCONCLUSIVE
  record. Nothing can ever become VERIFIED without evidence a validator
  independently supports.
- **Validator rejection** (leader says VERIFIED, validator's own evaluation
  says UNVERIFIED) → consensus fails → the run is retried/undetermined; it
  can never silently seal a lie.
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
tests/direct/               52 direct-mode tests: 10 spec cases + adversarial
                           + validator-equivalence + deterministic units
tests/helpers.py             mock web bodies, LLM builders, time warp
frontend/                    single-page dApp (GitHub Pages, in-browser wallets)
scripts/deploy_smoke.py      Studionet deploy + consensus smoke (S1–S5)
docs/deployment_log.json     live evidence: tx hashes, verdicts, timings
```

## Testing

52/52 direct-mode tests (`genlayer-test` 0.29.2), covering the full spec:

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

Plus adversarial cases (fake JSON docs, empty page, huge page, broken JSON,
capability-count limits, unknown ids, taxonomy extension, taxonomy duplicate
rejection), registry view/pagination tests, and pure-function unit tests of
the scoring math.

Run them:

```bash
python -m venv .venv && .venv/bin/pip install genlayer-test
GENVMROOT=/tmp/genvmroot .venv/bin/python -m pytest tests/direct -q
```

## Live deployment

Deployed to GenLayer Studionet with a full consensus smoke:

- 3 consecutive consensus runs on identical evidence agree on the verdict family
  (Equivalence Principle — determinism at the decision-field level).
- A marketing-only "negative" agent never yields VERIFIED.
- Registry views (`list_verifications`, `get_agent`) read back the full history.

All tx hashes, verdicts and timings: `docs/deployment_log.json`.

## Known limitations

- Verification judges **publicly documented evidence only** — an agent may have
  a capability that is real but undocumented; that will show UNVERIFIED/INCONCLUSIVE.
- Evidence is bounded (6k/source, 16k total chars) — very long docs are truncated.
- The taxonomy ships with 8 capability classes; new ones can be added on-chain
  via `add_capability_definition` (up to 30 total).
- Consensus latency on Studionet is ~1–2 min per verification write; the dApp
  polls FINALIZED state and never fakes progress.
- INCONCLUSIVE passports are honest by design: they say "evidence could not
  safely conclude", never a synthetic low score.

## License

MIT
