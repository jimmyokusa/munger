#!/bin/bash
# Blocks `git commit` if DESIGN.md or TASKS.md is staged with content that
# doesn't match its last recorded review marker. Convention: DESIGN.md
# changes require a staff-engineer-reviewer pass; TASKS.md changes require
# a pm-reviewer pass. After applying review fixes, update the matching
# marker file in .claude/review-markers/ with sha256(final file content)
# before committing.
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
cd "$repo_root"

staged_files="$(git diff --cached --name-only)"
markers_dir="$repo_root/.claude/review-markers"
blocked=""

check_marker() {
  local file="$1" marker_file="$2" reviewer="$3"
  if printf '%s\n' "$staged_files" | grep -qx "$file"; then
    local current_hash marker_hash
    current_hash="$(git show ":$file" 2>/dev/null | shasum -a 256 | awk '{print $1}')"
    marker_hash="$(cat "$markers_dir/$marker_file" 2>/dev/null || true)"
    if [ "$current_hash" != "$marker_hash" ]; then
      blocked="${blocked}${file} (needs ${reviewer}); "
    fi
  fi
}

check_marker "DESIGN.md" "design-staff-engineer.sha256" "staff-engineer-reviewer"
check_marker "TASKS.md" "tasks-pm-reviewer.sha256" "pm-reviewer"

if [ -n "$blocked" ]; then
  reason="Blocked: ${blocked}Run the required reviewer subagent against the diff, apply any fixes, then update the marker in .claude/review-markers/ (sha256 of the final file content) before committing."
  jq -n --arg reason "$reason" '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: $reason
    }
  }'
  exit 0
fi

exit 0
