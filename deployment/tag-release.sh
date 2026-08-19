#!/usr/bin/env bash
# Tags origin/main's current tip with the next semantic version and pushes
# the tag. Only ever tags origin/main - never a local-only commit or another
# branch - since deploy.sh only trusts tags reachable from origin/main.
#
# Usage: ./tag-release.sh [patch|minor|major]
#   Defaults to a patch bump. Existing tags must look like vX.Y.Z.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BUMP="${1:-patch}"

case "$BUMP" in
  patch|minor|major) ;;
  *) echo "Usage: $0 [patch|minor|major]" >&2; exit 1 ;;
esac

cd "$REPO_ROOT"

echo "==> Fetching origin"
git fetch --tags origin main >/dev/null

MAIN_SHA=$(git rev-parse origin/main)
MAIN_SUBJECT=$(git log -1 --format=%s "$MAIN_SHA")

LATEST_TAG=$(git tag -l 'v[0-9]*.[0-9]*.[0-9]*' --sort=-v:refname | head -1)
if [ -z "$LATEST_TAG" ]; then
  LATEST_TAG="v0.0.0"
fi

IFS='.' read -r MAJOR MINOR PATCH <<< "${LATEST_TAG#v}"
case "$BUMP" in
  patch) PATCH=$((PATCH + 1)) ;;
  minor) MINOR=$((MINOR + 1)); PATCH=0 ;;
  major) MAJOR=$((MAJOR + 1)); MINOR=0; PATCH=0 ;;
esac
NEW_TAG="v${MAJOR}.${MINOR}.${PATCH}"

if git rev-parse "$NEW_TAG" >/dev/null 2>&1; then
  echo "Error: tag $NEW_TAG already exists." >&2
  exit 1
fi

echo "Latest tag:      $LATEST_TAG"
echo "origin/main HEAD: $MAIN_SHA ($MAIN_SUBJECT)"
echo "Bump:             $BUMP"
echo "New tag:          $NEW_TAG"
echo

read -r -p "Tag origin/main HEAD as $NEW_TAG and push? [y/N] " CONFIRM
if [ "$CONFIRM" != "y" ] && [ "$CONFIRM" != "Y" ]; then
  echo "Aborted."
  exit 1
fi

git tag -a "$NEW_TAG" "$MAIN_SHA" -m "$NEW_TAG"
git push origin "$NEW_TAG"
echo "==> Tagged and pushed $NEW_TAG"
