# Codex context isolation

Each Commander or Builder is a new operating-system process and a new Codex
session. The fixed command contract is:

```text
codex exec
  --model gpt-5.6-sol
  -c model_reasoning_effort="max"
  --ephemeral
  --ignore-user-config
  --ignore-rules
  -c default_permissions="research_commander|research_builder"
  --output-schema <role-specific-schema>
```

The Commander profile can read only `.research`. The Builder profile can read
`.research` and write only `candidate_worktree`. Both may use a per-invocation
temporary directory. Native Windows also fixes
`windows.sandbox="elevated"`. Before the model receives any request, a
credential-free probe must successfully write inside a disposable workspace
and fail to read a sibling sentinel. A failed probe terminates the cycle.

`resume` is forbidden. A role invocation creates an exclusive started marker;
the same invocation cannot be retried or resumed after success, timeout,
interruption, or failure.

## Visible filesystem

The preferred Docker backend mounts only:

- the current invocation's work directory as writable;
- the current role-specific request projection as read-only;
- the current clean source snapshot as read-only.

The parent `runs/` directory and all sibling cycles are absent. Commander and
Builder use different writable mounts. The Builder receives a copy of the
clean snapshot; the Commander does not.

The Builder additionally receives a read-only, host-created
`input/builder_request/` projection. Its `builder_binding.json` covers the
request context and canonical proposal hash; `request_binding.json` exposes
only immutable identity, selection, source, and context receipts. The
projection also contains the approved proposal, clean-source manifest,
constraints, schemas, and public instructions. It explicitly excludes the full
research request, research memory, action plan, evidence, Commander output, and
transcripts. Native Windows copies this projection into the hashed sealed input
tree; the other backends expose the same immutable mount. The Builder echoes
the supplied proposal hash and performs no hashing itself.

There is no unrestricted direct-host fallback. Native Windows is accepted only
when its live probe proves the same sibling-read boundary. Codex releases that
provide workspace write isolation but still allow sibling reads are rejected.
An installation without a passing native probe or container must provide an
explicit, independently reviewed jail adapter.

Native Windows sandbox files can retain a sandbox-SID ACL after the child
exits. For a Builder only, the host may run a credential-free sandboxed
`icacls` recovery after confirmed child exit. Recovery is restricted to that
invocation's exact `candidate_worktree`, rejects a symlinked boundary, uses
`/L` so reparse targets are not followed, and must produce a host-readable,
symlink-free tree hash before validation proceeds. It never changes ACLs on the
run root, source repository, sibling runs, or credentials.

Candidate tests and Candidate decisions never make the completed Builder work
directory writable again. The host copies only Candidate `src`, declared test
bytes, changed Candidate configuration, a minimal host-owned
`repository_root` fixture, and the generated ABI test into a disposable
current-cycle/current-invocation namespace outside `runs/`. The original
Builder tree is not supplied to the child. The child uses the unelevated
workspace sandbox with network disabled, and the host hashes every projection
before and after execution. Any mutation is an integrity failure. This avoids
both an elevation prompt and the Builder deny ACL without exposing any sibling
research cycle.

## Environment and history

The host launcher retains only operating-system process essentials. It removes
`HOME`, API keys, broker variables, cookies, tokens, browser state, and the
normal user Codex home. Model subprocesses also exclude `CODEX_*`, `OPENAI_*`,
credential-shaped variables, and user profile paths. Model event streams and
stderr are discarded; the audit record contains only status, duration, hashes,
and exit code.

Authentication is deployment infrastructure. Native Windows uses a dedicated,
context-free `CODEX_HOME` whose credential is stored in the OS keyring. The
home may not contain file-backed credentials, configuration, skills, memories,
sessions, or instruction files. `--ignore-user-config` remains mandatory, and
SQLite/session state is redirected to an invocation-local disposable path.
The repository never reads or copies authentication material. Other production
jails must provide an equivalent opaque mechanism unavailable to model tools,
files, prompts, outputs, and logs.

## Binding

Every accepted output must exactly echo:

- request ID;
- research cycle ID;
- context manifest hash;
- source snapshot commit;
- Champion version;
- experiment family;
- selected Commander;
- exact append-only Commander selection ID;
- exact append-only Commander selection version.

A missing or changed selection ID/version fails closed even if the selected
model kind is unchanged. Because those values are also covered by the context
manifest hash, an older request cannot be rebound to a later selection record.
An expired request, mismatched context, or reused conversation also fails
closed.
