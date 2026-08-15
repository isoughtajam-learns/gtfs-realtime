#!/usr/bin/env bash
# Build, push, and roll out both app images (backend from ../gtfs-realtime,
# frontend from the sibling ../gtfs-dashboard repo) to the ECS services
# created by this Terraform config. Run after `terraform apply` has already
# created the ECR repos / cluster / services at least once.
#
# Usage: ./deploy.sh [image-tag]
#   image-tag defaults to "latest" (must match var.image_tag in Terraform,
#   which also defaults to "latest" - if you pass a different tag here,
#   update var.image_tag and re-apply so the task definitions point at it).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
FRONTEND_REPO_ROOT="$(cd "$REPO_ROOT/../gtfs-dashboard" && pwd)"
TAG="${1:-latest}"

cd "$SCRIPT_DIR"
REPO_URL=$(terraform output -raw ecr_repository_url)
FRONTEND_REPO_URL=$(terraform output -raw frontend_ecr_repository_url)
CLUSTER=$(terraform output -raw ecs_cluster_name)
REGION=$(terraform output -raw aws_region)
BACKEND_SVC=$(terraform output -raw backend_service_name)
WORKER_SVC=$(terraform output -raw celery_worker_service_name)
BEAT_SVC=$(terraform output -raw celery_beat_service_name)
FRONTEND_SVC=$(terraform output -raw frontend_service_name)

echo "==> Logging in to ECR ($REGION)"
aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "${REPO_URL%%/*}"

echo "==> Building backend $REPO_URL:$TAG"
cd "$REPO_ROOT"
docker build -t "$REPO_URL:$TAG" .

echo "==> Building frontend $FRONTEND_REPO_URL:$TAG"
cd "$FRONTEND_REPO_ROOT"
docker build -t "$FRONTEND_REPO_URL:$TAG" .

echo "==> Pushing images"
docker push "$REPO_URL:$TAG"
docker push "$FRONTEND_REPO_URL:$TAG"

echo "==> Rolling out new deployment"
for svc in "$BACKEND_SVC" "$WORKER_SVC" "$BEAT_SVC" "$FRONTEND_SVC"; do
  aws ecs update-service --cluster "$CLUSTER" --service "$svc" \
    --force-new-deployment --region "$REGION" >/dev/null
  echo "    forced new deployment: $svc"
done

echo "==> Waiting for services to stabilize (can take a few minutes)"
aws ecs wait services-stable --cluster "$CLUSTER" --region "$REGION" \
  --services "$BACKEND_SVC" "$WORKER_SVC" "$BEAT_SVC" "$FRONTEND_SVC"

cd "$SCRIPT_DIR"
echo "==> Dashboard: $(terraform output -raw app_url)"
echo "==> Backend API: $(terraform output -raw backend_api_url)"
echo "==> Deploy complete."
