#!/usr/bin/env bash
set -euo pipefail

REGISTRY="localhost:5000"
IMAGE_NAME="cabotage-app"
NAMESPACE="cabotage"
DEPLOYMENTS=("cabotage-app-web" "cabotage-app-worker" "cabotage-app-worker-beat")
SERVICE="cabotage-app"
APP_CONTAINER="cabotage-app"
PORT_LOCAL=8001
PORT_REMOTE=80

# Determine next image tag by finding the highest existing local-N
LAST_TAG=$(docker images --format '{{.Tag}}' "${REGISTRY}/${IMAGE_NAME}" 2>/dev/null \
    | sed -n 's/^local-\([0-9]*\)$/\1/p' \
    | sort -n \
    | tail -1)
NEXT_NUM=$(( ${LAST_TAG:--1} + 1 ))
TAG="local-${NEXT_NUM}"

echo "==> Building ${IMAGE_NAME}:${TAG}..."
docker build -t "${IMAGE_NAME}:local" -t "${REGISTRY}/${IMAGE_NAME}:${TAG}" .

echo "==> Pushing ${REGISTRY}/${IMAGE_NAME}:${TAG}..."
docker push "${REGISTRY}/${IMAGE_NAME}:${TAG}"

# Merge .env into the cabotage-config configmap
ENV_FILE="$(cd "$(dirname "$0")" && pwd)/.env"
if [ -f "${ENV_FILE}" ]; then
    echo "==> Patching configmap/cabotage-config with ${ENV_FILE}..."
    PATCH='{"data":{'
    FIRST=true
    while IFS= read -r line || [ -n "$line" ]; do
        # Skip blank lines and comments
        case "$line" in
            ''|\#*) continue ;;
        esac
        KEY="${line%%=*}"
        VALUE="${line#*=}"
        if [ "$FIRST" = true ]; then
            FIRST=false
        else
            PATCH="${PATCH},"
        fi
        # Escape double quotes in value for JSON
        VALUE=$(printf '%s' "$VALUE" | sed 's/"/\\"/g')
        PATCH="${PATCH}\"${KEY}\":\"${VALUE}\""
    done < "${ENV_FILE}"
    PATCH="${PATCH}}}"
    kubectl --context minikube-cabotage -n "${NAMESPACE}" patch configmap/cabotage-config \
        --type merge -p "${PATCH}"
fi

for DEPLOY in "${DEPLOYMENTS[@]}"; do
    case "${DEPLOY}" in
        cabotage-app-web) CONTAINER="cabotage-app" ;;
        *)                CONTAINER="${DEPLOY}" ;;
    esac
    echo "==> Updating deployment/${DEPLOY} container ${CONTAINER} to ${REGISTRY}/${IMAGE_NAME}:${TAG}..."
    kubectl --context minikube-cabotage -n "${NAMESPACE}" set image "deployment/${DEPLOY}" \
        "${CONTAINER}=${REGISTRY}/${IMAGE_NAME}:${TAG}"
done

echo "==> Waiting for rollouts..."
for DEPLOY in "${DEPLOYMENTS[@]}"; do
    kubectl --context minikube-cabotage -n "${NAMESPACE}" rollout status "deployment/${DEPLOY}" --timeout=120s
done

echo "==> Running migrations..."
POD=$(kubectl --context minikube-cabotage -n "${NAMESPACE}" get pod -l app=cabotage-app,component=web --field-selector=status.phase=Running --sort-by=.metadata.creationTimestamp -o jsonpath='{.items[-1:].metadata.name}')
kubectl --context minikube-cabotage -n "${NAMESPACE}" wait --for=jsonpath='{.status.containerStatuses[?(@.name=="'"${APP_CONTAINER}"'")].ready}'=true "pod/${POD}" --timeout=60s
kubectl --context minikube-cabotage -n "${NAMESPACE}" exec "${POD}" -c "${APP_CONTAINER}" -- python3 -m flask db upgrade
