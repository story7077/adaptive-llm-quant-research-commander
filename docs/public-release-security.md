# Public release security

This repository must begin from a sanitized working tree and a new Git root.
Do not copy the private repository's `.git` directory or commit history.

Before a public push:

```powershell
uv run research-commander public-scan . `
  --expected-repository story7077/adaptive-llm-quant-research-commander
uv run pytest
uv run ruff check .
uv run pyright
```

The root commit must contain `.public-root.json`. Later public feature commits
are allowed, but the history must retain exactly one independent root and the
root marker must use `PublicRootMarkerV1`, bind the public repository name, and
state that private history was not included. The scanner checks every reachable
ref, commit metadata, tree path, and historical blob, not only `HEAD` or the
current checkout. It rejects:

- credential-shaped values and assignments;
- email addresses and personal home paths;
- account identifiers;
- `.env`, `.local`, raw, credential, cookie, and browser-profile material;
- symlinks, non-UTF-8 content, binaries, and Git LFS pointers;
- any public root whose history is not exactly one clean-root commit.

Scanner output contains only a rule name and relative path. It never echoes a
matched secret.

Runs are gitignored and cannot be release artifacts. Evidence manifests contain
hashes and bounded metadata, never full copyrighted source text or browser
captures.

The GitHub release workflow uses a full-depth checkout with persisted checkout
credentials disabled, then runs the scanner, tests, Ruff, and Pyright.
