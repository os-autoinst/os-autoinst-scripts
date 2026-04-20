#!/usr/bin/env bash

source test/init

plan tests 36
dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

source "$dir/../openqa-label-known-issues-and-investigate-hook"
client_args=(api --host "$host_url")

export INVESTIGATE_FAIL=false
export INVESTIGATE_RETRIGGER_HOOK=false

# Mocking
export LLM_INVESTIGATE_FAIL=false
export LLM_INVESTIGATE_SKIP=false

openqa-llm-investigate() {
    local testurl=$1
    warn "- openqa-llm-investigate $testurl"
    "$LLM_INVESTIGATE_FAIL" && return 1
    "$LLM_INVESTIGATE_SKIP" && return 0
    echo "$testurl"
}

openqa-trigger-bisect-jobs() {
    echo "openqa-trigger-bisect-jobs ($@)"
}
openqa-label-known-issues() {
    testurl=$1
    warn "- openqa-label-known-issues $testurl"
    echo "[$testurl]($testurl): Unknown test issue, to be reviewed -> $testurl/file/autoinst-log.txt"
}
openqa-investigate() {
    local testurl=$1
    "$INVESTIGATE_FAIL" && return 1
    "$INVESTIGATE_RETRIGGER_HOOK" && return 142
    warn "- openqa-investigate $testurl"
}
openqa-trigger-bisect-jobs() {
    warn "- openqa-trigger-bisect-jobs $1"
}
openqa-api-get() {
    local path=$1
    if [[ "$path" == "jobs/123" ]]; then
        echo '{"job":{"state":"done", "result":"failed", "test":"foo"}}'
    elif [[ "$path" == "jobs/124" ]]; then
        echo '{"job":{"state":"done", "result":"passed", "test":"foo"}}'
    elif [[ "$path" == "jobs/125" ]]; then
        echo '{"job":{"state":"done", "result":"failed", "test":"foo:investigate:retry:x"}}'
    elif [[ "$path" == "jobs/126" ]]; then
        echo '{"job":{"state":"done", "result":"failed", "test":"foo:investigate:abc:x"}}'
    fi
}

try hook 123
is "$rc" 0 'successful hook (123)'
has "$got" "- openqa-label-known-issues"
has "$got" "- openqa-llm-investigate"
has "$got" "- openqa-investigate"
has "$got" "- openqa-trigger-bisect-jobs"

try hook 124
is "$rc" 0 'successful hook (124)'
hasnt "$got" "- openqa-label-known-issues"
has "$got" "- openqa-llm-investigate"
has "$got" "- openqa-investigate"
has "$got" "- openqa-trigger-bisect-jobs"

try hook 125
is "$rc" 0 'successful hook (125)'
hasnt "$got" "- openqa-label-known-issues"
has "$got" "- openqa-llm-investigate"
has "$got" "- openqa-investigate"
has "$got" "- openqa-trigger-bisect-jobs"

try hook 126
is "$rc" 0 'successful hook (126)'
hasnt "$got" "- openqa-label-known-issues"
has "$got" "- openqa-llm-investigate"
has "$got" "- openqa-investigate"
has "$got" "- openqa-trigger-bisect-jobs"

export INVESTIGATE_FAIL=true
try hook 123
is "$rc" 1 'openqa-investigate failed'

export INVESTIGATE_FAIL=false
export INVESTIGATE_RETRIGGER_HOOK=true
try hook 123
is "$rc" 142 'openqa-investigate exit code for retriggering hook script'

# Test: No unknown issue (label returns nothing)
openqa-label-known-issues() {
    warn "- openqa-label-known-issues $1"
    echo "nothing"
}
export INVESTIGATE_RETRIGGER_HOOK=false
try hook 123
is "$rc" 0 'successful hook (no unknown issue) (123)'
has "$got" "- openqa-label-known-issues"
hasnt "$got" "- openqa-llm-investigate"
hasnt "$got" "- openqa-investigate"
hasnt "$got" "- openqa-trigger-bisect-jobs"

# Test: LLM says NO (investigate skip)
openqa-label-known-issues() {
    warn "- openqa-label-known-issues $1"
    echo "[$1]($1): Unknown test issue, to be reviewed -> $1/file/autoinst-log.txt"
}
export LLM_INVESTIGATE_SKIP=true
try hook 123
is "$rc" 0 'successful hook (LLM says NO) (123)'
has "$got" "- openqa-label-known-issues"
has "$got" "- openqa-llm-investigate"
hasnt "$got" "- openqa-investigate"
hasnt "$got" "- openqa-trigger-bisect-jobs"

# Test: LLM fails (fallback to standard investigation)
export LLM_INVESTIGATE_SKIP=false
export LLM_INVESTIGATE_FAIL=true
try hook 123
is "$rc" 0 'successful hook (LLM fails, fallback) (123)'
has "$got" "WARNING: openqa-llm-investigate failed"
has "$got" "- openqa-investigate"
has "$got" "- openqa-trigger-bisect-jobs"
