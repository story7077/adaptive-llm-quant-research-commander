# Operations

## Prerequisites

- Python 3.12 and `uv`;
- a sanitized public source snapshot;
- a complete bounded request and evidence manifest;
- Docker with a restricted `codex-egress` network, or an explicitly reviewed
  jail adapter;
- an externally provisioned Codex runtime using `gpt-5.6-sol`.

No broker credentials or account files belong on this host.

## Prepare

First compute the source snapshot manifest and construct the request context
hash. The `prepare` command recomputes that binding and rejects mismatches.
The request must contain the current append-only Commander selection's exact
`commander_selection_id` and positive `commander_selection_version`, in
addition to `selected_commander`. If the operational plane appends a newer
selection, discard the old request and create a new cycle; do not edit or reuse
the prepared cycle.

```powershell
uv run research-commander prepare `
  --request request.json `
  --evidence evidence.json `
  --source-snapshot C:\sanitized-source `
  --runs-root .local\runs
```

A cycle ID is immutable. Reusing its directory is rejected.

## Commander

For a Codex-selected cycle:

```powershell
uv run research-commander plan `
  --run .local\runs\<cycle> `
  --role commander `
  --backend docker
```

Inspecting a plan makes no external call. Add `--execute` only after the jail,
restricted egress, and opaque authentication provider have been verified, or
execute the immutable saved plan later:

```powershell
uv run research-commander execute-plan `
  --run .local\runs\<cycle> `
  --role commander
```

For a WebGPT-selected cycle, validate the structured ingress instead. Supply
all Scout conversation IDs so they cannot be reused as Commander context.

## Builder

Only an approved `AlgorithmProposalV1` or `AlgorithmProposalV2` is passed:

```powershell
uv run research-commander plan `
  --run .local\runs\<cycle> `
  --role builder `
  --backend docker `
  --proposal approved-proposal.json
```

Historical V1 behavior is the default. A V2 proposal selects the narrower,
hash-bound V2 patch policy automatically. To stage a V1 proposal against that
policy, select it explicitly:

```powershell
uv run research-commander plan `
  --run .local\runs\<cycle> `
  --role builder `
  --backend docker `
  --proposal approved-proposal.json `
  --candidate-patch-policy candidate_patch_policy_v2
```

The V2 selection and canonical policy-contract hash are sealed into the
Builder context. V2 allows only versioned Challenger implementation paths,
`tests/candidates/**`, and `docs/research/challengers/**`; it cannot change the
trusted research controller, OOS worker, performance metrics, promotion
thresholds, persistence, execution, risk, ledger, security, broker, research
configuration, trusted research tests, migrations, or GitHub workflows. V2
also requires every changed path to be a newly added file; an existing file
cannot be modified, deleted, renamed, or copied.

The Builder sees only `input/builder_request/`: a request-binding receipt,
approved proposal, immutable Builder/policy binding, clean-source manifest,
constraints, output schemas, Candidate decision schemas, and public
instructions. It cannot see the full ResearchRequestV2, research memory,
action plan, evidence, Commander output, or any transcript. Its output is a
source tree and a schema-bound build summary. `builder_binding.json` supplies
the canonical `proposal_hash`; the Builder must echo it and must not hash
`approved_algorithm_proposal.json`.

## Finalize

After the native Builder exits and its output is host-validated, execute its
declared tests through the same network-denied Windows sandbox boundary:

```powershell
uv run research-commander test-candidate `
  --run .local\runs\<cycle>
```

Only a `PASSED` host-owned `candidate_test_manifest_v1` may be finalized:

The host projects Candidate source, declared test bytes, and the generated ABI
test into the isolated result directory before pytest starts. All three
projections are hashed before and after execution; any mutation is an integrity
failure. The test and decision scratch directories live in a
current-cycle/current-invocation namespace outside `runs/`; completed Builder
worktrees retain their deny ACLs and are not supplied to the child. These
post-Build calls use the unelevated workspace sandbox with network disabled, so
they do not require an administrator approval prompt.

Each host-run test attempt has an immutable manifest under
`candidate-test-attempts/`. A host infrastructure failure remains in history.
After the host runner itself is versioned or repaired, the same byte-identical
Candidate may receive one new append-only attempt. Repeating an unchanged
runner attempt is idempotent. Finalization accepts only the unique passing
attempt bound to the current runner hash.

```powershell
uv run research-commander finalize-candidate `
  --run .local\runs\<cycle>
```

Finalization accepts no external proposal, worktree, or test-manifest path. It
reloads the sealed Builder plan, validates every changed path and hash, and
emits the Challenger, validation, and Candidate artifact-bundle manifests.
Repeating finalization is idempotent only when every immutable byte matches.

The public trading host may invoke the finalized raw-JSON Candidate ABI with a
validated request and `candidate_execution_security_v1` attestation:

```powershell
uv run research-commander candidate-runtime-info `
  --run .local\runs\<cycle>

uv run research-commander invoke-candidate `
  --run .local\runs\<cycle> `
  --request candidate-request.json `
  --security candidate-execution-security.json
```

This uses a disposable source projection, no network, an unelevated workspace
sandbox, a kill-on-close Windows Job, and bounded output. The sealed Candidate
artifact remains unreachable and unchanged. The trading host validates the
response and retains
all PnL, order, fill, risk, and promotion authority.
The process result is stored append-only under the request/security-derived
invocation ID. A worker retry returns those identical validated bytes; an
orphaned or conflicting runtime directory fails closed.

## Recovery

Never resume or retry an invocation.

If the host wrapper timed out but the externally supervised Codex child later
exited and left `model-output.json`, the output can be adopted without another
model call:

```powershell
uv run research-commander adopt-plan-output `
  --run .local\runs\<cycle> `
  --role commander `
  --confirm-child-exited
```

Use `--role builder` for an orphaned Builder result. Before passing
`--confirm-child-exited`, the operator or process supervisor must have observed
the exact child process for that invocation exit. The run marker does not store
a process handle, so file presence alone is not sufficient confirmation.

Adoption fails closed unless the one-shot start marker matches, no completion
marker exists, sealed inputs are unchanged, the model output (and Builder
candidate tree) remains byte-stable across the fixed stability window, and all
normal schema, request-binding, proposal, and patch-policy checks pass. It then
publishes the validated role output and appends an
`execution-completed.json` marker with
`completion_mode=HOST_ADOPTED_AFTER_SUPERVISOR_TIMEOUT`. It does not execute or
resume Codex and does not finalize or promote a Challenger. Run normal candidate
tests and `finalize-candidate` after a recovered Builder output.

For a native Windows Builder, normal completion and host-only adoption first
restore host access to the exact invocation `candidate_worktree`. This happens
only after child exit is known, invokes no model and uses no credential, does
not follow reparse-point targets, and records only a hash of the host principal.
Any boundary, ACL, readability, symlink, or tree-hash failure stops validation.

If the exact child exit cannot be confirmed, or any adoption check fails,
preserve the immutable failure markers, create a new research request and
cycle, and account for the additional experiment submission. A crash before
run preparation's atomic rename leaves no visible cycle.
