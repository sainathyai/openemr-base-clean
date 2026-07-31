#!/usr/bin/env bash
# DEV ONLY. Registers a confidential OAuth2 client, enables it (simulating the
# admin-approval control CTRL-2), and writes copilot/.env. Local dev uses the
# password grant (D-2); production uses SMART authorization-code + EHR launch.
set -euo pipefail

BASE="${OPENEMR_BASE:-https://localhost:9300}"
MYSQL_CONTAINER="${MYSQL_CONTAINER:-development-easy-mysql-1}"
ENV_FILE="$(dirname "$0")/../.env"

echo "Registering confidential client at $BASE ..."
REG=$(curl -sk -X POST "$BASE/oauth2/default/registration" \
  -H 'Content-Type: application/json' \
  --data '{
    "client_name": "Clinical Co-Pilot (service)",
    "application_type": "private",
    "redirect_uris": ["'"$BASE"'/callback"],
    "token_endpoint_auth_method": "client_secret_post",
    "scope": "openid offline_access api:fhir user/Patient.read user/Condition.read user/MedicationRequest.read user/Observation.read user/Encounter.read user/AllergyIntolerance.read"
  }')

CID=$(printf '%s' "$REG" | python -c "import sys,json;print(json.load(sys.stdin)['client_id'])")
CSEC=$(printf '%s' "$REG" | python -c "import sys,json;print(json.load(sys.stdin)['client_secret'])")
[ -n "$CID" ] && [ -n "$CSEC" ] || { echo "registration failed: $REG" >&2; exit 1; }

echo "Enabling client (admin approval) ..."
docker exec "$MYSQL_CONTAINER" mariadb -uroot -proot -e \
  "UPDATE openemr.oauth_clients SET is_enabled=1 WHERE client_id='$CID';"

cat > "$ENV_FILE" <<EOF
OPENEMR_BASE=$BASE
OPENEMR_VERIFY_TLS=false
COPILOT_CLIENT_ID=$CID
COPILOT_CLIENT_SECRET=$CSEC
COPILOT_DEV_USER=drhouse
COPILOT_DEV_PASS=DocPass123!
COPILOT_LAB_LOOKBACK_MONTHS=18
COPILOT_LAB_MAX=200
EOF

echo "Wrote $ENV_FILE (client_id=$CID)"
