#!/bin/bash
# Blocks `git push` if it would push .py code changes that haven't been
# reviewed by staff-engineer-reviewer since the marker's last-approved
# commit. Unlike the DESIGN.md/TASKS.md file-hash markers, this one is
# keyed to a commit SHA, since a code review covers the whole diff being
# pushed, not one file's content.
#
# The check is "has any .py file changed between the marker's SHA and
# HEAD" -- not "does HEAD exactly equal the marker's SHA". The latter
# would be self-defeating: committing the marker's own update moves HEAD,
# which would immediately invalidate the value just written, and any
# later non-code commit (docs, config) would falsely re-trigger the block
# forever. This marker is intentionally gitignored/untracked -- it can
# never correctly reference "the commit it's committed in".
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
cd "$repo_root"

current_sha="$(git rev-parse HEAD 2>/dev/null)" || exit 0
upstream="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || echo "origin/main")"

# No-op if the upstream ref doesn't exist yet (nothing to diff against).
git rev-parse --verify "$upstream" >/dev/null 2>&1 || exit 0

marker_file="$repo_root/.claude/review-markers/code-push-staff-engineer.sha"
marker_sha="$(cat "$marker_file" 2>/dev/null || true)"

# Range to check .py changes over: from the marker (last-reviewed point)
# if it's a valid, known commit; otherwise fall back to the upstream ref.
if [ -n "$marker_sha" ] && git rev-parse --verify "$marker_sha" >/dev/null 2>&1; then
  range_base="$marker_sha"
else
  range_base="$upstream"
fi

changed_py_files="$(git diff --name-only "${range_base}..${current_sha}" -- '*.py' 2>/dev/null || true)"

if [ -n "$changed_py_files" ]; then
  files_list="$(printf '%s' "$changed_py_files" | tr '\n' ' ')"
  reason="Blocked: pushing code changes (${files_list}) not yet reviewed by staff-engineer-reviewer since the last recorded review. Run staff-engineer-reviewer against \`git diff ${range_base}...HEAD\`, apply any fixes, then write the current commit SHA (git rev-parse HEAD) to .claude/review-markers/code-push-staff-engineer.sha (an untracked, local-only file -- do not commit it) before pushing."
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
