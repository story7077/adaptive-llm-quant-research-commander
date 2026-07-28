# Research contracts

The canonical contracts live in `schemas/`.

## ResearchRequestV1

The request contains versioned Champion and Challenger summaries, failure
clusters, regimes, costs, capacity, bounded evidence references, experiment
budget, and an `available_data_catalog`.

The catalog is generic: it may list any eligible US-listed equity or ETF with
point-in-time eligibility. Nothing in this repository hardcodes a semiconductor
universe, leveraged ETF pair, factor, or risk-only objective.

`context_manifest_hash` is the public host's canonical hash of the request
without that field. Evidence, constraints, source commit, and clean-snapshot
bytes are separately sealed by the immutable run and Builder binding.

Every request carries `selected_commander`, `commander_selection_id`, and the
positive integer `commander_selection_version`. The ID and version identify one
exact record in the operational plane's append-only Commander selection log.
They are part of the context hash and are required even when the selected model
kind is unchanged from a previous selection.

## ResearchRequestV2

V2 preserves all V1 identity, time, source, Commander-selection, catalog, and
authority bindings. It replaces caller-authored performance/failure/regime
summaries with two typed host artifacts:

- `ResearchMemorySnapshotV1`, an immutable point-in-time aggregate of verified,
  mature experiment outcomes; and
- `ResearchActionPlanV1`, a deterministic, budgeted Meta Controller ranking.

The request context hash covers both objects. The plan must reference the
embedded snapshot and cycle, its submission budget must fit the request budget,
and its hashes, timestamps, and action ranking must validate. Raw returns,
locked OOS samples, transcripts, and hidden reasoning are not request fields.
Memory text is untrusted data, never a prompt-control channel.

## ResearchDecisionV1 and AlgorithmProposalV1

Both `CODEX_SOL_MAX` and `WEBGPT_SOL_PRO` return the same decision schema.
Proposal decisions must contain a complete `AlgorithmProposalV1`; other
decisions must not.

The decision, Builder result, WebGPT ingress binding, and Builder request
binding must echo all three Commander-selection fields. A nested proposal is
instead identified by its canonical `proposal_id` and `proposal_hash`.
A missing value or any ID, version, or model-kind mismatch is rejected as a
stale selection. Switching away from and later back to the same model kind
therefore cannot revive an older request.

The proposal describes a falsifiable alpha hypothesis, economic mechanism,
failure modes, invalidation, placebo tests, stress tests, target universe,
data, formula/rule changes, expected capacity, turnover, and minimum effect.
`raw_confidence` is audit metadata only.

## ResearchDecisionV2 and AlgorithmProposalV2

Both Commander implementations use the same V2 schema. A proposal must select
one funded `primary_action_id` from the bound action plan, match its canonical
action kind and failure tags, and keep the predicted portfolio delta-Sharpe
lower/central/upper values ordered and inside the host-computed action bounds.
These predictions are auditable forecasts only; they cannot affect capital,
OOS results, or promotion.

V2 proposals select `candidate_patch_policy_v2`. Model-facing output still uses
trusted-host placeholders for receipt time and canonical hashes. The host
recomputes them and validates the same snapshot, plan, selection, expiry,
catalog, evidence, and request bindings before accepting the result.

The proposed `target_universe` is a normalized symbol list and must be a subset
of the request's versioned catalog. Every symbol must have point-in-time
research history and shadow execution support. Evidence IDs must belong to the
bounded request.

Model-facing Commander output uses trusted-host placeholders for
`created_at`, `output_hash`, and any nested `proposal_hash`. The host replaces
them with receipt time and canonical hashes before authoritative validation.

## CandidateBuildResultV1

The Builder reports what it implemented and tested. It cannot report a
promotion decision; `promotion_decision` is always `NOT_PERMITTED`.

Before a Builder invocation, the host canonicalizes the approved proposal,
computes its canonical JSON `proposal_hash`, and writes an immutable
`BuilderInvocationBindingV1` to `builder_binding.json`. The binding joins the
proposal hash to the exact request binding and has its own
`builder_context_hash`. It also records the raw proposal-file SHA-256 for audit;
that file hash is intentionally not the value returned by the Builder.

The Builder must echo the supplied `proposal_hash` exactly. It must never hash
the proposal file or reimplement canonical JSON. Plan loading, normal output
validation, and timeout adoption all validate the host-supplied binding.
It also declares one `python.module:function` entrypoint. The callable accepts
one raw `candidate_decision_request_v1` JSON object and returns one raw
`candidate_decision_response_v1` JSON object. Orders, fills, returns/PnL,
broker actions, and new symbols are outside this ABI.

### Candidate patch policy versions

Historical `AlgorithmProposalV1` builds continue to use
`candidate_patch_policy_v1` unless the trusted host explicitly selects V2.
`AlgorithmProposalV2` selects V2 automatically at Commander and WebGPT ingress.

`candidate_patch_policy_v2` has one canonical
`candidate_patch_policy_contract_v1` document and hash. It permits Candidate
implementation only under:

- `src/trading/strategies/challengers/**`
- `src/trading/features/challengers/**`
- `src/trading/calibration/challengers/**`
- `src/trading/experiments/challengers/**`
- `config/strategies/challengers/**`
- `tests/candidates/**`
- `docs/research/challengers/**`

It forbids the trusted research controller, persistence, execution, risk,
ledger, security, broker, research configuration, trusted research tests,
migrations, and `.github/**`. Relative-path escapes, binary patches, and
symbolic-link patch modes fail closed. Every V2 unified-diff section must add a
new file from `/dev/null`; modifying, deleting, renaming, or copying an
existing file is forbidden even within an allowed Challenger namespace. V2
requires both a Candidate implementation and a `tests/candidates/**` test.

For an explicit V2 build, the trusted host binds the policy version and
canonical contract hash into `builder_context_hash`, the persisted invocation
plan, and `builder_binding.json`. Output validation, timeout adoption,
host-owned Candidate tests, and finalization reload that same immutable
selection. V1 binding bytes and patch semantics remain unchanged.

The authoritative request and response field contracts are copied into every
prepared run as `candidate-decision-request-v1.schema.json` and
`candidate-decision-response-v1.schema.json`. The Builder must consume
host-supplied point-in-time instrument features and constraints, echo all
request bindings, return one sorted target per supplied symbol, and compute the
canonical output hash. Canonicalization recursively sorts object keys, preserves
array order and JSON scalar types, normalizes each finite float through
`float(format(value, ".12g"))`, rejects non-finite values, serializes without
spaces using UTF-8 and `ensure_ascii=false`, and hashes the response after
removing only `output_hash`. Raw bars and outcomes are not Candidate inputs.

Before Builder launch, the host materializes `input/builder_request/`. It
contains only the immutable request-binding receipt, approved proposal,
Builder/patch-policy binding, constraints, clean-source manifest, output
schemas, Candidate decision schemas, and public instructions. The Builder
mount excludes the full research request, memory snapshot, action plan,
evidence bundle, Commander result, and every conversation transcript.

## ChallengerManifestV1

The manifest binds source, proposal, patch, code, config, and test hashes.
Its identifier is derived from deterministic inputs. Initial status is
`PROPOSED`, never `PROMOTED`.

## CandidateTestManifestV1 and CandidateArtifactBundleV1

`test-candidate` copies Candidate source and the changed, Builder-declared tests
into host-owned disposable projections, runs the projected tests plus the
host-owned exact ABI test under a network-denied Windows sandbox and
kill-on-close Job limits, and requires the original Candidate tree and every
projection hash to remain unchanged. It records hashes and bounded counts,
never raw test output. Attempts are append-only and bound to the host runner
hash; this permits recovery from a host-infrastructure failure without
discarding either the failed attempt or rebuilding an unchanged Candidate.

Finalization accepts no caller-supplied proposal, worktree, or test manifest.
It reloads host-owned artifacts, rechecks every source/candidate/patch/proposal
binding, and emits `candidate_artifact_bundle_v1`. The bundle binds the
request/response ABI, declared entrypoint, source snapshot, candidate
tree/code/config/test/proposal hashes, and all broker, credential, network,
filesystem-write, and real-order capabilities as false.

Candidate decision results are idempotent immutable records keyed by the
request hash and security-contract hash. The sealed artifact is never writable;
only a disposable projection exists inside the unelevated workspace, and any
projection mutation prevents a result from being accepted.

## ValidationRequestV1

Validation receives only a lockbox request and mandatory test IDs.
`raw_oos_access_permitted=false` and
`automatic_promotion_permitted=false` are schema constants.
