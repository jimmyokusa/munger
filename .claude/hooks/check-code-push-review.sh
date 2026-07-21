#!/bin/bash
# Blocks `git push` if it would push .py code changes that haven't been
# reviewed by staff-engineer-reviewer since the marker's last-approved
# commit. Unlike the DESIGN.md/TASKS.md file-hash markers, this one is
# keyed to a commit SHA, since a code review covers the whole diff being
# pushed, not one file's content.
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
cd "$repo_root"

current_sha="$(git rev-parse HEAD 2>/dev/null)" || exit 0
upstream="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || echo "origin/main")"

# No-op if the upstream ref doesn't exist yet (nothing to diff against).
git rev-parse --verify "$upstream" >/dev/null 2>&1 || exit 0

changed_py_files="$(git diff --name-only "${upstream}..HEAD" -- '*.py' 2>/dev/null || true)"

if [ -n "$changed_py_files" ]; then
  marker_file="$repo_root/.claude/review-markers/code-push-staff-engineer.sha"
  marker_sha="$(cat "$marker_file" 2>/dev/null || true)"
  if [ "$current_sha" != "$marker_sha" ]; then
    files_list="$(printf '%s' "$changed_py_files" | tr '\n' ' ')"
    reason="Blocked: pushing code changes (${files_list}) not yet reviewed by staff-engineer-reviewer for this commit. Run staff-engineer-reviewer against \`git diff ${upstream}...HEAD\`, apply any fixes, then write the current commit SHA to .claude/review-markers/code-push-staff-engineer.sha (git rev-parse HEAD) before pushing."
    jq -n --arg reason "$reason" '{
      hookSpecificOutput: {
        hookEventName: "PreToolUse",
        permissionDecision: "deny",
        permissionDecisionReason: $reason
      }
    }'
    exit 0
  fi
fi

exit 0
