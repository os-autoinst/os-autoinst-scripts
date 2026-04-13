#!/usr/bin/env bash
echo 1..1

color=always
source _common

echo "ok 1"
[[ $ENABLE_VISUAL_TESTS ]] || exit 0

log-warn "Demo warning"
log-info "Demo info" >&2
log-debug "Demo debug"
