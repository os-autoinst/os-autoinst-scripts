#!/usr/bin/env bash

source test/init

plan tests 11

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

# Clearing due dates: the query already selects the tickets to clear, so every
# ticket it returns is updated regardless of status or existing due date
try 'dry_run=1 "$script" --clear "$fixture"'
is "$rc" 0 "clear mode succeeds"
has "$got" "{\"issue\":{\"due_date\":null,\"notes\":\"Removing due date because ticket is Resolved\"}}" \
    "clear mode emits the expected PUT payload"
has "$got" "/issues/101.json" \
    "clear mode updates every ticket returned by the query (101)"
has "$got" "/issues/102.json" \
    "clear mode updates every ticket returned by the query (102)"
has "$got" "/issues/103.json" \
    "clear mode updates every ticket returned by the query (103)"
