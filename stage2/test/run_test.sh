#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p ../ssl_logs

echo "Bringing up container and running TLS test..."
docker compose up --build --abort-on-container-exit

echo
echo "Contents of ../ssl_logs:" 
ls -lah ../ssl_logs || true
echo
echo "--- sslkey.log ---"
cat ../ssl_logs/sslkey.log || true

echo
echo "Tearing down containers..."
docker compose down
