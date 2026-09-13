#!/usr/bin/env bash
# Public standalone installer. No GitHub API or registry credentials required.
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
TEMP_DIR="$(mktemp -d)"
trap 'rm -rf -- "$TEMP_DIR"' EXIT
# One archive gives a consistent commit without GitHub's anonymous API quota.
curl --fail --silent --show-error --location --retry 3 --connect-timeout 15 --max-time 120 \
    --max-filesize 5242880 --proto '=https' --proto-redir '=https' \
    "https://codeload.github.com/$REPOSITORY/tar.gz/$REF" -o "$TEMP_DIR/deployment.tar.gz"
FILES=(compose.yaml compose.https.yaml compose.updates.yaml Caddyfile .env.example gateway.sh gateway.py README.md)
SHA="$(python3 - "$TEMP_DIR/deployment.tar.gz" "$TEMP_DIR" "${FILES[@]}" <<'PYARCHIVE'
from pathlib import Path
import re, sys, tarfile
archive_path, destination, *names = sys.argv[1:]
with tarfile.open(archive_path, 'r:gz') as archive:
    revision = archive.pax_headers.get('comment', '')
    if not re.fullmatch(r'[a-f0-9]{40}', revision):
        raise ValueError('Archive does not identify its Git commit')
    roots = {member.name.split('/')[0] for member in archive.getmembers()}
    if len(roots) != 1 or next(iter(roots)) in ('', '.', '..'):
        raise ValueError('Unexpected deployment archive root')
    root = next(iter(roots))
    selected = []
    for name in names:
        member = archive.getmember(root + '/deploy/' + name)
        if not member.isfile() or member.size > 2 * 1024 * 1024:
            raise ValueError('Invalid deployment file: ' + name)
        selected.append((name, member))
    for name, member in selected:
        with archive.extractfile(member) as source:
            (Path(destination) / name).write_bytes(source.read())
    print(revision)
PYARCHIVE
)"
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
