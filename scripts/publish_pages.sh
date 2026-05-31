#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

python3 scripts/policy_tracker.py dashboard

mkdir -p docs/data/exports
cp dashboard/index.html docs/index.html
cp data/exports/*.csv docs/data/exports/
cp data/exports/status_report.md docs/data/exports/
touch docs/.nojekyll

perl -0pi -e 's/href="priority_review\.html"/href="data\/exports\/review_priority.csv"/g; s/href="review\.html"/href="data\/exports\/review_values.csv"/g; s/href="\.\.\/data\/exports\/policy_values_daily\.csv"/href="data\/exports\/policy_values_daily.csv"/g' docs/index.html

git add docs dashboard data/exports data/manual README.md scripts .gitignore .nojekyll index.html

if git diff --cached --quiet; then
  echo "No publish changes."
  exit 0
fi

stamp="$(date '+%Y-%m-%d %H:%M:%S %Z')"
git commit -m "Update policy dashboard ${stamp}"
git push
