#!/usr/bin/env bash
set -euo pipefail

if ! command -v cloudflared >/dev/null 2>&1; then
    echo "cloudflared is required. Install it first."
    exit 1
fi

PORT="${1:-8080}"
exec cloudflared tunnel --url "http://localhost:${PORT}"
