#!/usr/bin/env bash
# Build a self-contained exodus-agent bundle for transfer to an air-gapped machine.
#
# Usage:
#   ./scripts/bundle.sh              # Docker image only (default)
#   ./scripts/bundle.sh --pyinstaller  # also build a standalone binary (same OS/arch only)
#
# Output:
#   dist/exodus-agent-docker.tar.gz   Docker image — load with: docker load < exodus-agent-docker.tar.gz
#   dist/exodus                       PyInstaller binary (if --pyinstaller was passed)
#   dist/TRANSFER.md                  Quick-reference for the target machine

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DIST="$REPO_ROOT/dist"
IMAGE="exodus-agent:latest"
PYINSTALLER=0

for arg in "$@"; do
  case "$arg" in
    --pyinstaller) PYINSTALLER=1 ;;
    *) echo "Unknown argument: $arg"; exit 1 ;;
  esac
done

mkdir -p "$DIST"

echo "==> Building Docker image $IMAGE"
docker build -t "$IMAGE" "$REPO_ROOT"

echo "==> Saving Docker image → dist/exodus-agent-docker.tar.gz"
docker save "$IMAGE" | gzip > "$DIST/exodus-agent-docker.tar.gz"
echo "    Size: $(du -sh "$DIST/exodus-agent-docker.tar.gz" | cut -f1)"

if [[ "$PYINSTALLER" -eq 1 ]]; then
  echo "==> Building PyInstaller binary → dist/exodus"
  echo "    NOTE: this binary runs ONLY on $(uname -s) $(uname -m)"
  pip install --quiet pyinstaller
  pyinstaller \
    --onefile \
    --name exodus \
    --distpath "$DIST" \
    --workpath "$REPO_ROOT/build/pyinstaller" \
    --specpath "$REPO_ROOT/build" \
    "$REPO_ROOT/exodus_agent/cli.py"
  echo "    Size: $(du -sh "$DIST/exodus" | cut -f1)"
fi

# Write a quick-reference for the target machine
cat > "$DIST/TRANSFER.md" <<'EOF'
# Exodus Agent — Air-Gap Transfer

## Prerequisites on the target machine
- Docker (for the .tar.gz image) **or** nothing (for the standalone binary)

## Docker — recommended

```bash
# 1. Load the image (run once)
docker load < exodus-agent-docker.tar.gz

# 2. Verify
docker run --rm exodus-agent exodus --help

# 3. Run a migration command
#    Mount your working directory as /workspace; pass secrets via env vars.
docker run --rm \
  -e WEBEX_ACCESS_TOKEN=your_token_here \
  -v "$(pwd)":/workspace \
  exodus-agent \
  doctor --config /workspace/migration.toml
```

The workspace path inside the container is `/workspace`. Put your
`migration.toml`, `identity-map.json`, and `conversation-map.json` there.
The archive and job store are written to `<workspace>/.exodus/` by default.

## PyInstaller binary (if included)

The `exodus` binary (or `exodus.exe` on Windows) runs without Docker or Python.
**It only works on the same OS and CPU architecture it was built on.**

```bash
chmod +x ./exodus        # Linux/macOS only
./exodus --help
WEBEX_ACCESS_TOKEN=... ./exodus doctor --config migration.toml
```

## Windows — install from source (no Docker, no binary)

Use this when Docker Desktop is not available on the target Windows machine.
Requires **Python 3.11 or later** (download from python.org — check "Add to PATH").

Transfer the repo source to the Windows machine (zip it or copy the folder via USB),
then open **Command Prompt** or **PowerShell** in the repo root:

```powershell
# 1. Verify Python version (must be 3.11+)
python --version

# 2. Create and activate a virtual environment (keeps the install isolated)
python -m venv .venv
.venv\Scripts\activate

# 3. Install exodus-agent and its dependencies
pip install -e .

# 4. Verify the CLI is available
exodus --help

# 5. Set secrets as environment variables (PowerShell syntax)
$env:WEBEX_ACCESS_TOKEN      = "your-webex-token"
$env:MICROSOFT_TENANT_ID     = "your-tenant-id"
$env:MICROSOFT_CLIENT_ID     = "your-client-id"
$env:MICROSOFT_CLIENT_SECRET = "your-client-secret"

# 6. Run the migration (same commands as Docker, without the docker run wrapper)
exodus webex-teams-dry-run `
  --config migration.toml `
  --identity-map identity-map.json `
  --conversation-map conversation-map.json
```

**Note:** The virtual environment must be activated (`.venv\Scripts\activate`) each time
you open a new terminal before running `exodus`. All workspace files are written to the
`workspace` path in your `migration.toml`, defaulting to `.exodus/` in the current folder.

## Quick-start for Webex → Teams dry-run

See `docs/quickstart-webex-to-teams.md` in the repo for the full walkthrough.
Short version:

```bash
# 1. Validate
docker run --rm -e WEBEX_ACCESS_TOKEN=... -v $(pwd):/workspace \
  exodus-agent doctor --config /workspace/migration.toml

# 2. Extract
docker run --rm -e WEBEX_ACCESS_TOKEN=... -v $(pwd):/workspace \
  exodus-agent export-dry-run --config /workspace/migration.toml

# 3. Identity map (fill in entra_user_id values, then re-mount)
docker run --rm -v $(pwd):/workspace \
  exodus-agent teams-identity-map-template \
  --config /workspace/migration.toml --output /workspace/identity-map.json

# 4. Conversation map (fill in target values, then re-mount)
docker run --rm -v $(pwd):/workspace \
  exodus-agent teams-conversation-map-template \
  --config /workspace/migration.toml \
  --identity-map /workspace/identity-map.json \
  --output /workspace/conversation-map.json

# 5. All-in-one (after maps are filled)
docker run --rm -e WEBEX_ACCESS_TOKEN=... -v $(pwd):/workspace \
  exodus-agent webex-teams-dry-run \
  --config /workspace/migration.toml \
  --identity-map /workspace/identity-map.json \
  --conversation-map /workspace/conversation-map.json
```
EOF

echo ""
echo "==> Bundle ready in $DIST/"
ls -lh "$DIST/"
echo ""
echo "Transfer to target machine via USB, then follow dist/TRANSFER.md"
