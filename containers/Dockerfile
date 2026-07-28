FROM node:24-bookworm-slim

ARG CODEX_CLI_VERSION
RUN test -n "${CODEX_CLI_VERSION}" \
    && npm install --global --ignore-scripts "@openai/codex@${CODEX_CLI_VERSION}" \
    && npm cache clean --force

RUN useradd --create-home --uid 65532 runner
USER 65532:65532
WORKDIR /workspace

# Authentication and restricted egress are deployment concerns. Never bake
# auth.json, API keys, user config, or history into this image.
ENTRYPOINT []
CMD ["codex", "--version"]

