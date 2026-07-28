# Runner image contract

Build with an explicitly reviewed Codex CLI version:

```text
docker build --build-arg CODEX_CLI_VERSION=<reviewed-version> \
  -t adaptive-llm-quant-codex-runner:local -f containers/Dockerfile .
```

The image deliberately contains no authentication. Deployment must provide an
opaque authentication facility that is unavailable to model tools and is not
represented in repository files, command arguments, environment inheritance,
or run logs.

The `codex-egress` Docker network name is a contract, not a firewall by itself.
Configure its gateway or proxy to allow only the required Codex service
destinations and deny broker, market-data, browser, and arbitrary web access.

