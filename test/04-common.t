#!/usr/bin/env bash

source test/init

plan tests 21

source ./_common

success() {
    echo "SUCCESS $@"
}

failure() {
    warn "oh noe!"
    return 23
}

try runcli success a b c
is $rc 0 "runcli success"
is "$got" "SUCCESS a b c" "runcli successful output"

caller() (builtin caller 2)
try runcli failure a b c
unset -f caller
is $rc 23 "runcli failure"
like "$got" "test/04-common.t.*failure a b c.*stderr.*oh noe" "runcli failure output"

tw_openqa_host=foo
get_image() {
    echo "opensuse-Tumbleweed-i386-20380101-Tumbleweed@32bit-3G.qcow2"
}
latest_published_tw_builds() {
    echo "20380101 20390101"
}

try find_latest_published_tumbleweed_image "23" "i386" "32bit" qcow
is $rc 0 "find_latest_published_tumbleweed_image success (qcow)"
image=opensuse-Tumbleweed-i386-20380101-Tumbleweed@32bit-3G.qcow2
is "$got" "$image" "Found expected image (qcow)"

try find_latest_published_tumbleweed_image "23" "i386" "32bit" iso
is $rc 0 "find_latest_published_tumbleweed_image success (iso)"
image=opensuse-Tumbleweed-i386-20380101-Tumbleweed@32bit-3G.qcow2
is "$got" "$image" "Found expected image (iso)"

get_image() {
    echo "null"
}
try find_latest_published_tumbleweed_image "23" "i386" "32bit" qcow
is "$rc" 2 "find_latest_published_tumbleweed_image failure (qcow)"
has "$got" "Unable to determine qcow image" "Expected error message (qcow)"

latest_published_tw_builds() {
    echo ""
}
try find_latest_published_tumbleweed_image "23" "i386" "32bit" qcow
is "$rc" 1 "find_latest_published_tumbleweed_image failure (no builds)"
has "$got" "Unable to find latest published Tumbleweed builds" "Expected error message (no builds)"

somecommand() {
    echo "STDOUT"
    echo "STDERR" >&2
}

try runcli somecommand
like "$got" "somecommand.*>>>STDERR<<<.*STDOUT" "somecommand stdout and stderr"

try "bash -c 'command() { if [[ \"\$1\" == \"-v\" && \"\$2\" == \"jq\" ]]; then return 1; fi; if [[ \"\$1\" == \"-v\" && ( \"\$2\" == \"retry\" || \"\$2\" == \"osc\" || \"\$2\" == \"openqa-cli\" ) ]]; then return 0; fi; builtin command \"\$@\"; }; export -f command; source ./_common'"
is "$rc" 1 "exits with 1 when jq is missing"
like "$got" "ERROR: 'jq' command not found" "prints correct error message when jq is missing"

try "bash -c 'command() { if [[ \"\$1\" == \"-v\" && \"\$2\" == \"retry\" ]]; then return 1; fi; if [[ \"\$1\" == \"-v\" && ( \"\$2\" == \"jq\" || \"\$2\" == \"osc\" || \"\$2\" == \"openqa-cli\" ) ]]; then return 0; fi; builtin command \"\$@\"; }; export -f command; source ./_common'"
is "$rc" 1 "exits with 1 when retry is missing"
like "$got" "ERROR: 'retry' command not found" "prints correct error message when retry is missing"

try "bash -c 'command() { if [[ \"\$1\" == \"-v\" && \"\$2\" == \"osc\" ]]; then return 1; fi; if [[ \"\$1\" == \"-v\" && ( \"\$2\" == \"jq\" || \"\$2\" == \"retry\" || \"\$2\" == \"openqa-cli\" ) ]]; then return 0; fi; builtin command \"\$@\"; }; export -f command; source ./_common'"
is "$rc" 1 "exits with 1 when osc is missing"
like "$got" "ERROR: 'osc' command not found" "prints correct error message when osc is missing"

try "bash -c 'command() { if [[ \"\$1\" == \"-v\" && \"\$2\" == \"openqa-cli\" ]]; then return 1; fi; if [[ \"\$1\" == \"-v\" && ( \"\$2\" == \"jq\" || \"\$2\" == \"retry\" || \"\$2\" == \"osc\" ) ]]; then return 0; fi; builtin command \"\$@\"; }; export -f command; source ./_common'"
is "$rc" 1 "exits with 1 when openqa-cli is missing"
like "$got" "ERROR: 'openqa-cli' command not found" "prints correct error message when openqa-cli is missing"
