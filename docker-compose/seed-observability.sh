#!/usr/bin/env bash
# Pushes fake metrics into Mimir and fake logs into Loki for local dev testing.
#
# Automatically reads org/project/app/env from the local DB to compute the
# correct k8s namespace, pod prefix, and Loki label selectors.
#
# Usage: bash docker-compose/seed-observability.sh [org_slug] [project_slug] [app_slug] [env_slug]
#   Defaults: admin-org admin-proj admin-app blah

set -euo pipefail

MIMIR_URL="${MIMIR_URL:-http://localhost:9009}"
LOKI_URL="${LOKI_URL:-http://localhost:3100}"

ORG_SLUG="${1:-admin-org}"
PROJECT_SLUG="${2:-admin-proj}"
APP_SLUG="${3:-admin-app}"
ENV_SLUG="${4:-blah}"

# Query DB for k8s_identifiers (use | delimiter to handle NULLs)
DB_ROW="$(docker-compose exec -T db psql -U postgres cabotage_dev -t -A -F '|' -c "
    SELECT o.k8s_identifier, p.k8s_identifier, a.k8s_identifier,
           ae.k8s_identifier, pe.k8s_identifier
    FROM organizations o
    JOIN projects p ON p.organization_id = o.id
    JOIN project_applications a ON a.project_id = p.id
    LEFT JOIN application_environments ae ON ae.application_id = a.id
    LEFT JOIN project_environments pe ON pe.id = ae.environment_id
    WHERE o.slug = '${ORG_SLUG}' AND p.slug = '${PROJECT_SLUG}'
      AND a.slug = '${APP_SLUG}' AND (pe.slug = '${ENV_SLUG}' OR pe.slug IS NULL)
    LIMIT 1
" 2>/dev/null | tr -d '\r')"

IFS='|' read -r ORG_K8S PROJ_K8S APP_K8S AE_K8S ENV_K8S <<< "$DB_ROW"

echo "    DB row: org_k8s=${ORG_K8S} proj_k8s=${PROJ_K8S} app_k8s=${APP_K8S} ae_k8s=${AE_K8S:-NULL} env_k8s=${ENV_K8S:-NULL}"

# Compute namespace: if ae has k8s_identifier (non-empty = non-legacy),
# namespace is safe_k8s_name(org, env); otherwise just org k8s_identifier
if [ -n "$AE_K8S" ]; then
    NAMESPACE="${ORG_K8S}-${ENV_K8S}"
else
    NAMESPACE="${ORG_K8S}"
fi

# Compute prefix: project-app (legacy = just slugs joined)
PREFIX="${PROJECT_SLUG}-${APP_SLUG}"

PROCESS="web"
POD_NAME="${PREFIX}-${PROCESS}-abc12-def34"

NOW=$(date +%s)

echo "==> Computed labels from DB:"
echo "    namespace=${NAMESPACE}  prefix=${PREFIX}  pod=${POD_NAME}"
echo "    loki labels: project=${PROJECT_SLUG} application=${APP_SLUG} environment=${ENV_SLUG}"
echo ""

echo "==> Seeding Mimir metrics..."

for i in $(seq 60 -1 0); do
    ts_ns=$(( (NOW - i * 60) * 1000000000 ))
    cpu_val=$(echo "scale=4; 0.15 + ($RANDOM % 100) / 1000" | bc)
    mem_val=$(( 104857600 + RANDOM * 1000 ))  # ~100MB +/- jitter

    curl -s -X POST "${MIMIR_URL}/otlp/v1/metrics" \
        -H "Content-Type: application/json" \
        -d "{
  \"resourceMetrics\": [{
    \"resource\": {\"attributes\": []},
    \"scopeMetrics\": [{
      \"metrics\": [
        {
          \"name\": \"container_cpu_usage_seconds_total\",
          \"sum\": {
            \"dataPoints\": [{
              \"asDouble\": $(echo "scale=4; $cpu_val * (60 - $i)" | bc),
              \"timeUnixNano\": \"${ts_ns}\",
              \"attributes\": [
                {\"key\": \"namespace\", \"value\": {\"stringValue\": \"${NAMESPACE}\"}},
                {\"key\": \"pod\", \"value\": {\"stringValue\": \"${POD_NAME}\"}}
              ]
            }],
            \"isMonotonic\": true,
            \"aggregationTemporality\": 2
          }
        },
        {
          \"name\": \"container_memory_working_set_bytes\",
          \"gauge\": {
            \"dataPoints\": [{
              \"asDouble\": ${mem_val},
              \"timeUnixNano\": \"${ts_ns}\",
              \"attributes\": [
                {\"key\": \"namespace\", \"value\": {\"stringValue\": \"${NAMESPACE}\"}},
                {\"key\": \"pod\", \"value\": {\"stringValue\": \"${POD_NAME}\"}}
              ]
            }]
          }
        }
      ]
    }]
  }]
}" > /dev/null 2>&1 || true
done

echo "    Pushed 61 metric samples for cpu + memory"

echo "==> Seeding Loki logs..."

BATCH_SIZE=20
values=""
count=0

for i in $(seq 120 -1 0); do
    ts_ns="$(( (NOW - i * 30) ))000000000"
    status_codes=("200" "200" "200" "200" "200" "200" "200" "201" "301" "404" "500")
    status=${status_codes[$(( RANDOM % ${#status_codes[@]} ))]}
    paths=("/" "/projects" "/orgs" "/static/main.css" "/static/main.js" "/login" "/healthz")
    path=${paths[$(( RANDOM % ${#paths[@]} ))]}
    ms=$(( RANDOM % 500 ))

    line="2026-03-09T$(printf '%02d:%02d:%02d' $(( (NOW - i*30) / 3600 % 24 )) $(( (NOW - i*30) / 60 % 60 )) $(( (NOW - i*30) % 60 ))).000Z stdout F ${status} GET ${path} ${ms}ms"

    if [ -n "$values" ]; then values+=","; fi
    values+="[\"${ts_ns}\",\"${line}\"]"
    count=$(( count + 1 ))

    if [ "$count" -ge "$BATCH_SIZE" ]; then
        curl -s -X POST "${LOKI_URL}/loki/api/v1/push" \
            -H "Content-Type: application/json" \
            -d "{\"streams\":[{
                \"stream\":{
                    \"namespace\":\"${NAMESPACE}\",
                    \"project\":\"${PROJECT_SLUG}\",
                    \"application\":\"${APP_SLUG}\",
                    \"environment\":\"${ENV_SLUG}\",
                    \"pod_name\":\"${POD_NAME}\",
                    \"pod_container_name\":\"${PROCESS}\",
                    \"process\":\"${PROCESS}\"
                },
                \"values\":[${values}]
            }]}" > /dev/null 2>&1 || true
        values=""
        count=0
    fi
done

# Push remaining
if [ -n "$values" ]; then
    curl -s -X POST "${LOKI_URL}/loki/api/v1/push" \
        -H "Content-Type: application/json" \
        -d "{\"streams\":[{
            \"stream\":{
                \"namespace\":\"${NAMESPACE}\",
                \"project\":\"${PROJECT_SLUG}\",
                \"application\":\"${APP_SLUG}\",
                \"environment\":\"${ENV_SLUG}\",
                \"pod_name\":\"${POD_NAME}\",
                \"pod_container_name\":\"${PROCESS}\",
                \"process\":\"${PROCESS}\"
            },
            \"values\":[${values}]
        }]}" > /dev/null 2>&1 || true
fi

echo "    Pushed ~121 log entries"

echo ""
echo "==> Verify:"
echo "    Mimir: curl -sG '${MIMIR_URL}/prometheus/api/v1/query' --data-urlencode 'query=container_memory_working_set_bytes{namespace=\"${NAMESPACE}\"}'"
echo "    Loki:  curl -sG '${LOKI_URL}/loki/api/v1/query_range' --data-urlencode 'query={namespace=\"${NAMESPACE}\",application=\"${APP_SLUG}\"}' --data-urlencode 'start=$(( NOW - 3600 ))000000000' --data-urlencode 'end=${NOW}000000000' --data-urlencode 'limit=5'"
echo ""
echo "Done!"
