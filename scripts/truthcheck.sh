#!/bin/sh
# Truth pin — fail CI when product facts drift from ground truth.
#
# Checks tracked text files for copy that is KNOWN to be wrong (stale quota
# numbers, the wrong docs host, a personal handle, the midnight-UTC myth) and,
# wherever quotas are stated at all, that the current FREE quota and the docs
# host are present. CHANGELOG.md is exempt from the forbidden-string checks:
# its entries legitimately describe history.
#
# POSIX sh, no dependencies beyond git and grep.
set -u

cd "$(dirname "$0")/.." || exit 1

fail=0
flag() {
    echo "truthcheck: FAIL — $1" >&2
    fail=1
}

# Tracked text files, minus the changelog (history entries may describe old
# facts) and this script (its own messages name the forbidden strings).
FILES=$(git ls-files '*.py' '*.md' '*.toml' '*.yml' '*.yaml' '*.sh' '*.cfg' '*.ini' \
    | grep -v '^CHANGELOG\.md$' | grep -v '^scripts/truthcheck\.sh$')
[ -n "$FILES" ] || exit 0

# 1) Stale day-quota numbers (the pre-2026-08-06 grid) next to day context.
if echo "$FILES" | xargs grep -inE '100[, ]?000[^0-9]{0,24}(/ *day|per[- ]day|daily)|100k[^a-zA-Z]{0,24}(/ *day|per[- ]day|daily)' 2>/dev/null; then
    flag "stale 100,000/day quota copy (FREE is 100/day since 2026-08-06)"
fi

# 2) FREE paired with the old 1,000/day quota.
if echo "$FILES" | xargs grep -inE '(free[^|.]{0,60}1,?000[^0-9]{0,12}(/ *day|per[- ]day|requests/day))|(1,?000[^|.]{0,30}(/ *day|per[- ]day)[^.]{0,30}free tier)' 2>/dev/null; then
    flag "FREE tier paired with 1,000/day (FREE is 100/day; 1,000/day is BASIC)"
fi

# 3) The docs live at docs.livetennisapi.com, never livetennisapi.com/docs.
if echo "$FILES" | xargs grep -in 'livetennisapi\.com/docs' 2>/dev/null; then
    flag "wrong docs URL — use https://docs.livetennisapi.com"
fi

# 4) No personal handle in the repo or its metadata.
if echo "$FILES" | xargs grep -in 'bensynapse' 2>/dev/null; then
    flag "personal handle found — use the livetennisapi org identity"
fi

# 5) The daily reset is an absolute instant (resets_at), not midnight UTC.
if echo "$FILES" | xargs grep -in 'midnight UTC' 2>/dev/null; then
    flag "daily reset is not 'midnight UTC' — the daily-429 resets_at is the truth"
fi

# 6) If quota copy exists at all, the current FREE quota and docs host must too.
if echo "$FILES" | xargs grep -ilE 'per[- ]day|/ *day|per minute' >/dev/null 2>&1; then
    if ! echo "$FILES" | xargs grep -qE '100( requests)?/day' 2>/dev/null; then
        flag "quota copy present but the FREE quota '100/day' is missing"
    fi
    if ! echo "$FILES" | xargs grep -q 'docs\.livetennisapi\.com' 2>/dev/null; then
        flag "quota copy present but docs.livetennisapi.com is missing"
    fi
fi

if [ "$fail" -eq 0 ]; then
    echo "truthcheck: OK"
fi
exit "$fail"
