# Adaptive LLM Quant Research Commander

Public-safe, isolated execution host for the Adaptive LLM Quant Research
Plane. It converts a bounded research request into either a structured research
decision or a versioned Challenger patch.

This is a research-only paper-trading component:

- no guarantee of profit;
- no broker client or order-routing code;
- no credentials, account values, browser profiles, or raw web payloads;
- no access to locked OOS observations;
- no in-place Champion edits;
- no automatic promotion;
- all examples are synthetic.

The operational trading plane remains a separate repository. Only a
human-approved, versioned strategy release may later be imported by that plane.

## Roles

1. **Research Commander** — receives `ResearchRequestV1`, returns
   `ResearchDecisionV1` and, where applicable, `AlgorithmProposalV1`. It cannot
   edit code.
2. **Candidate Builder** — a second, fresh Codex invocation that receives the
   approved proposal and a sanitized clean snapshot. It may produce a patch and
   tests only within allowed paths.
3. **WebGPT ingress** — validates a structured result from a fresh
   GPT-5.6 Sol Pro/xhigh conversation. Conversation transcripts are never
   forwarded to the Builder.

Both Codex roles require `gpt-5.6-sol`, reasoning `max`, non-interactive
`codex exec`, a fresh process, `--ephemeral`, and `--ignore-user-config`.
Every request and model output is also bound to the exact append-only Commander
selection record by `commander_selection_id` and
`commander_selection_version`; matching only the model kind is insufficient.

Candidates expose only a raw-JSON decision ABI:
`candidate_decision_request_v1` in and
`candidate_decision_response_v1` out. The isolated callable may return scores,
long-only target weights, and diagnostics for host-supplied symbols. It cannot
return orders, fills, returns/PnL, broker actions, or promotion decisions.

## Isolation model

Each cycle is prepared as:

```text
runs/<research-cycle-id>/
  request/
    research_request.json
    evidence_manifest.json
    output.schema.json
    constraints.json
  input/
    clean_source_snapshot/
  work/
    commander/
    builder/
  output/
    research_decision.json
    algorithm_proposal.json
    candidate_manifest.json
    candidate_artifact_bundle.json
    patch.diff
    validation_request.json
  logs/
    sanitized-run.log
```

The model process must not see the repository's `runs/` parent. A
Docker/container backend is preferred. Native Windows model invocations use
explicit least-privilege `research_commander` and `research_builder` profiles,
elevated sandboxing, and a live credential-free probe that must prove sibling
reads are denied before any request is exposed. If the installed Codex/Windows
combination provides only a write boundary, model execution fails closed and
Docker, WSL2, or another independently reviewed OS jail is required. All
copied request/input files are
hash-sealed before and after the invocation.

## Local development

```powershell
uv sync --all-groups
uv run pytest
uv run ruff check .
uv run pyright
```

Preparing and inspecting an invocation does not contact Codex:

```powershell
uv run research-commander prepare `
  --request examples/research-request.example.json `
  --evidence examples/evidence-manifest.example.json `
  --source-snapshot <sanitized-source-directory> `
  --runs-root .local/runs

uv run research-commander plan `
  --run .local/runs/cycle-example-001 `
  --role commander `
  --backend docker
```

Native Windows additionally requires a one-time ChatGPT login into an empty,
private runner home backed by the OS keyring. That home may not contain
`auth.json`, configuration, skills, memories, sessions, or `AGENTS.md`.

```powershell
.\scripts\bootstrap-codex-runner.ps1
```

The login is deployment infrastructure. It is never copied into a run,
manifest, prompt, output, log, fixture, or public repository.

When the native read-jail preflight is supported:

```powershell
uv run research-commander plan `
  --run .local/runs/cycle-example-001 `
  --role commander `
  --backend native_windows
```

Executing a plan is deliberately a separate explicit operation and requires an
installed, configured jail backend. Current native Windows builds that allow a
sibling read will stop before launching the model; do not bypass that result
with the weaker unelevated write sandbox. Post-Build Candidate tests and
decision calls are different: they receive only disposable, hash-checked
source/test projections and therefore use the non-interactive unelevated
workspace sandbox with network disabled. This project does not bundle
credentials or invoke external services during tests.

```powershell
uv run research-commander execute-plan `
  --run .local/runs/cycle-example-001 `
  --role commander
```

## Public release

Run
`uv run research-commander public-scan . --expected-repository story7077/adaptive-llm-quant-research-commander`
before publishing. It rejects secrets, personal paths, account-shaped data,
hidden environment files, raw payloads, symlinks, and Git history that is not a
single repository-bound clean root.
