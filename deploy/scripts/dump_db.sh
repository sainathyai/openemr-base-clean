#!/usr/bin/env bash
# Regenerate the DB image seed from the running local OpenEMR (dev stack up).
# Run from the repo root. The seeded DB image (deploy/images/db) bakes this in.
set -euo pipefail
OUT="deploy/images/db/initdb/01-openemr.sql.gz"
mkdir -p "$(dirname "$OUT")"
docker exec development-easy-mysql-1 mariadb-dump -uroot -proot \
  --single-transaction --routines --triggers --databases openemr | gzip > "$OUT"
echo "wrote $OUT ($(du -h "$OUT" | cut -f1))"
