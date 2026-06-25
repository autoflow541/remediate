#!/usr/bin/env bash
# Start the remediation engine container.
#
# Usage:
#   ./run-engine.sh                                      # CORS open (temporary)
#   CORS_ORIGINS=https://studio.yourdomain.com ./run-engine.sh  # locked down
#
# Run this AFTER the image is built:
#   docker build -f docker/Dockerfile -t remediation-engine .
#
# To update later: stop + remove the old container, then re-run this script.
#   docker stop remediation-engine && docker rm remediation-engine

set -euo pipefail

CORS_ORIGINS="${CORS_ORIGINS:-*}"

docker run -d \
  --name remediation-engine \
  --restart unless-stopped \
  -p 127.0.0.1:8000:8000 \
  -e "CORS_ORIGINS=${CORS_ORIGINS}" \
  remediation-engine

echo "Engine started. Health check:"
sleep 2
curl -sf http://localhost:8000/health | python3 -m json.tool || echo "(curl failed — container may still be starting)"
