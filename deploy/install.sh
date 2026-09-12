#!/usr/bin/env bash
# Standalone installer. GITHUB_TOKEN is used only for downloading private files.
set -euo pipefail
umask 077
REPOSITORY="${GATEWAY_DEPLOY_REPOSITORY:-lingchaojie/ai_dashboard-deploy}"
REF="${GATEWAY_REF:-main}"
INSTALL_DIR="${INSTALL_DIR:-$PWD/ai-gateway}"
for command in curl python3 docker; do
    command -v "$command" >/dev/null || { echo "Missing dependency: $command" >&2; exit 1; }
done
docker compose version >/dev/null
[[ "$REPOSITORY" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || { echo 'Invalid repository' >&2; exit 1; }
[[ "$REF" =~ ^[A-Za-z0-9_.-]+$ ]] || { echo 'Use main, a version tag, or a commit SHA as GATEWAY_REF' >&2; exit 1; }
[[ "${GITHUB_TOKEN:-}" =~ ^[A-Za-z0-9_]*$ ]] || { echo 'Invalid token format' >&2; exit 1; }
TEMP_DIR="$(mktemp -d)"
trap 'rm -rf -- "$TEMP_DIR"' EXIT
fetch() {
    local url="$1" output="$2" accept="$3"
    # Read authorization from stdin, keeping the token out of process arguments.
    { if [[ -n "${GITHUB_TOKEN:-}" ]]; then printf 'header = "Authorization: Bearer %s"\n' "$GITHUB_TOKEN"; fi; } |
        curl --config - --fail --silent --show-error --location --retry 3 --connect-timeout 15 --max-time 120 \
            --proto '=https' --proto-redir '=https' -H "Accept: $accept" "$url" -o "$output"
}
fetch "https://api.github.com/repos/$REPOSITORY/commits/$REF" "$TEMP_DIR/commit.json" 'application/vnd.github+json'
SHA="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["sha"])' "$TEMP_DIR/commit.json")"
[[ "$SHA" =~ ^[a-f0-9]{40}$ ]] || { echo 'Invalid commit response' >&2; exit 1; }
FILES=(compose.yaml compose.https.yaml Caddyfile .env.example gateway.sh gateway.py README.md)
for file in "${FILES[@]}"; do
    fetch "https://api.github.com/repos/$REPOSITORY/contents/deploy/$file?ref=$SHA" "$TEMP_DIR/$file" 'application/vnd.github.raw+json'
done
bash -n "$TEMP_DIR/gateway.sh"
python3 - "$TEMP_DIR/gateway.py" <<'PY'
import ast, pathlib, sys
ast.parse(pathlib.Path(sys.argv[1]).read_text())
PY
# Download and validate all assets before replacing installed scripts.
mkdir -p -- "$INSTALL_DIR"
for file in "${FILES[@]}"; do
    if [[ -L "$INSTALL_DIR/$file" ]]; then echo "Refusing symlink: $file" >&2; exit 1; fi
done
for file in "${FILES[@]}"; do
    # Preserve customized Compose and HTTPS configuration on repeated installs.
    if [[ -e "$INSTALL_DIR/$file" && "$file" != gateway.sh && "$file" != gateway.py ]]; then continue; fi
    install -m 600 "$TEMP_DIR/$file" "$INSTALL_DIR/$file"
done
chmod 700 "$INSTALL_DIR/gateway.sh"
printf '%s\n' "$SHA" > "$INSTALL_DIR/.deployment-revision"
unset GITHUB_TOKEN
"$INSTALL_DIR/gateway.sh" install
