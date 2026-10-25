#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
night_watch="${NIGHT_WATCH_SCRIPT:-$repo_root/scripts/night-watch.sh}"
if [ ! -r "$night_watch" ]; then
  echo "FAIL night-watch script is not readable: $night_watch" >&2
  exit 1
fi

source "$night_watch"
test_root=$(mktemp -d "${TMPDIR:-/tmp}/night-watch-test.XXXXXX")
trap 'rm -rf "$test_root"' EXIT

fail=0
check() {
  local name="$1" expected="$2" actual="$3"
  if [ "$actual" = "$expected" ]; then
    echo "ok   $name"
  else
    echo "FAIL $name: expected $expected, got $actual"
    fail=1
  fi
}

# Replace process inspection with deterministic test state.
fake_pid_state=dead
nw_alive() {
  [ "$fake_pid_state" = alive ]
}

repo="$test_root/with-commit"
mkdir -p "$repo"
git -C "$repo" init -q
git -C "$repo" -c user.email=test@example.invalid -c user.name=test \
  commit -q --allow-empty -m base
git -C "$repo" branch base
git -C "$repo" -c user.email=test@example.invalid -c user.name=test \
  commit -q --allow-empty -m result

no_commit_repo="$test_root/no-commit"
mkdir -p "$no_commit_repo"
git -C "$no_commit_repo" init -q
git -C "$no_commit_repo" -c user.email=test@example.invalid -c user.name=test \
  commit -q --allow-empty -m base
git -C "$no_commit_repo" branch base

# This matches the B report shape: its first line is a conclusion heading,
# and it does not begin with DONE.
cat > "$test_root/B-LAST.md" <<'EOF'
**結論**
- 已完成文件整理。
- 全套測試 302/302 PASS
EOF
check "B report plus a new branch commit is DONE" DONE \
  "$(nw_result "$test_root/B-LAST.md" "$repo" base)"
check "B report without a new commit is REPORTED" REPORTED \
  "$(nw_result "$test_root/B-LAST.md" "$no_commit_repo" base)"

cat > "$test_root/status-LAST.md" <<'EOF'
# 結果摘要
Status: DONE
- FAILED was an earlier retry
EOF
check "explicit result status wins over body text" DONE \
  "$(nw_result "$test_root/status-LAST.md")"

printf 'FAILED\nBuild did not finish\n' > "$test_root/failed-LAST.md"
printf 'The log contains failed during a retry\n' > "$test_root/run.log"
fake_pid_state=alive
check "live PID stays RUNNING with a FAILED report and failed log" RUNNING \
  "$(nw_state 12345 "$test_root/failed-LAST.md")"

fake_pid_state=dead
check "dead PID is classified from the result report" FAILED \
  "$(nw_state 12345 "$test_root/failed-LAST.md")"
check "a DONE report without a PID is DONE" DONE \
  "$(nw_state "" "$test_root/status-LAST.md")"

if [ "$fail" -ne 0 ]; then
  echo "NIGHT-WATCH TESTS FAILED"
  exit 1
fi
echo "NIGHT-WATCH TESTS PASS"
