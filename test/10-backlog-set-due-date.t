#!/usr/bin/env bash

source test/init

plan tests 16

script=$PWD/backlog-set-due-date
fixture=$PWD/test/data/backlog-set-due-date-issues.json
due_date=$(date -d "+0 days" +%F)

# Setting due dates: only the In Progress ticket without a due date is updated
try 'dry_run=1 duration="0 days" "$script" "$fixture"'
is "$rc" 0 "set mode succeeds"
has "$got" "curl -f " "set mode makes curl fail on HTTP errors"
has "$got" "{\"issue\":{\"due_date\":\"$due_date\",\"notes\":\"Setting due date based on mean cycle time of SUSE QE Tools\"}}" \
    "set mode emits the expected PUT payload"
has "$got" "https://progress.opensuse.org/issues/101.json" \
    "set mode updates the In Progress ticket without a due date"
hasnt "$got" "/issues/102.json" \
    "set mode does not update a Resolved ticket with a due date"
hasnt "$got" "/issues/103.json" \
    "set mode does not update a Resolved ticket without a due date"

# Clearing due dates: only the Resolved ticket with a due date is updated
try 'dry_run=1 "$script" --clear "$fixture"'
is "$rc" 0 "clear mode succeeds"
has "$got" "{\"issue\":{\"due_date\":null,\"notes\":\"Removing due date because ticket is Resolved\"}}" \
    "clear mode emits the expected PUT payload"
has "$got" "https://progress.opensuse.org/issues/102.json" \
    "clear mode clears the Resolved ticket with a due date"
hasnt "$got" "/issues/101.json" \
    "clear mode does not update the In Progress ticket"
hasnt "$got" "/issues/103.json" \
    "clear mode does not update a Resolved ticket without a due date"

# Fetching: all pages are read instead of only the first one. Redmine returns
# at most 100 issues per page, so the default limit needs two requests for 175
make_curl_stub() {
    local bindir
    bindir=$(mktemp -d)
    cat > "$bindir/curl" << 'EOF'
#!/bin/bash
limit=0
offset=0
for arg in "$@"; do
    case $arg in
        *limit=*)
            limit=${arg##*limit=}
            limit=${limit%%&*}
            ;;
    esac
    case $arg in
        *offset=*) offset=${arg##*offset=} ;;
    esac
done
count=100
[ "$offset" -gt 0 ] && count=75
jq -cn --argjson limit "$limit" --argjson offset "$offset" --argjson count "$count" \
    '{issues: [range($count) | {id: (1000 + $offset + .), status: {name: "In Progress"}, priority: {name: "Normal"}, assigned_to: {id: 1}, due_date: null}], total_count: 175, offset: $offset, limit: $limit}'
EOF
    chmod +x "$bindir/curl"
    printf '%s\n' "$bindir"
}

curl_bindir=$(make_curl_stub)
try 'PATH="'"$curl_bindir"':$PATH" dry_run=1 redmine_api_key=test host=https://redmine.example "$script"'
paginated_rc=$rc
paginated_got=$got
rm -r "$curl_bindir"
is "$paginated_rc" 0 "fetching all pages succeeds"
has "$paginated_got" "https://redmine.example/issues/1000.json" \
    "first ticket of the first page is updated"
has "$paginated_got" "https://redmine.example/issues/1099.json" \
    "last ticket of the first page is updated"
has "$paginated_got" "https://redmine.example/issues/1100.json" \
    "first ticket of the second page is updated"
has "$paginated_got" "https://redmine.example/issues/1174.json" \
    "last ticket of the second page is updated"
