# Threat model

## Protected assets

- credentials and browser authentication;
- private account and order data;
- locked OOS observations;
- Champion source and manifests;
- operational risk, execution, ledger, broker, database, and release controls;
- experiment budgets and immutable failed-candidate history.

## Untrusted inputs

Web pages, social media, evidence summaries, WebGPT output, Commander output,
Algorithm Proposals, generated patches, test claims, and model confidence are
all untrusted.

## Main threats and controls

| Threat | Control |
|---|---|
| Prior-session contamination | fresh process, ephemeral Codex, no resume, ignored user config |
| Sibling-run access | current-run-only mounts and jail policy |
| Prompt injection from evidence | structured manifests, no raw transcript, schema and binding checks |
| Credential exfiltration | scrubbed process environment and no credential mounting by this host |
| Champion overwrite | new version required, protected-path policy, deterministic patch validation |
| Trading authority expansion | no broker dependency, order schema, or operational runtime |
| OOS overfitting | lockbox aggregate-only contract and experiment-budget accounting |
| Self-approval | Commander/Builder separation and no automatic promotion |
| Unsafe patch | allowlist plus denylist; denylist wins |
| Public data leak | clean-root export and fail-closed public scanner |
| False alpha | mandatory falsification, costs, placebo, stability, and later shadow gates |

## Residual risks

A container alone does not restrict network destinations. Deployment must
provide a domain-restricted egress network or proxy suitable for Codex. An
opaque authentication mechanism must prevent model-executed tools from reading
client credentials. Neither capability is emulated by unit tests.

LLM-generated code can still be wrong after schema validation. Passing this
repository's boundary checks means only that the Challenger is eligible for
the separate falsification pipeline, not that it has alpha or may trade.

