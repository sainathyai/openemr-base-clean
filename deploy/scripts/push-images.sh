#!/usr/bin/env bash
# Push the three locally-built images to ECR. Run on your machine after
# `aws configure`. Creates the repos if missing, logs in, tags, and pushes.
#
#   AWS_REGION=us-east-1 bash push-images.sh
#
# Local image names expected (built earlier):
#   copilot-db:local  copilot-emr:local  clinical-copilot:latest
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
REGISTRY="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"

# local image -> ECR repo name
PAIRS="copilot-db:local=copilot-db copilot-emr:local=copilot-emr clinical-copilot:latest=copilot-agent"

echo ">> ensuring ECR repos exist..."
for repo in copilot-db copilot-emr copilot-agent; do
  aws ecr describe-repositories --repository-names "$repo" --region "$REGION" >/dev/null 2>&1 \
    || aws ecr create-repository --repository-name "$repo" --region "$REGION" >/dev/null
done

echo ">> docker login to $REGISTRY ..."
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REGISTRY"

for pair in $PAIRS; do
  local_img="${pair%%=*}"
  repo="${pair##*=}"
  remote="${REGISTRY}/${repo}:latest"
  echo ">> tagging + pushing $local_img -> $remote"
  docker tag "$local_img" "$remote"
  docker push "$remote"
done

echo ""
echo "Done. Put these in the box .env files:"
echo "  DB_IMAGE=${REGISTRY}/copilot-db:latest"
echo "  EMR_IMAGE=${REGISTRY}/copilot-emr:latest"
echo "  AGENT_IMAGE=${REGISTRY}/copilot-agent:latest"
