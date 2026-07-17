#!/usr/bin/env bash

source test/init

plan tests 19

source _common

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

# list_packages: 'osc' is invoked via the $osc variable, so mock it there
# list_packages: returns real package names, filtering out '*-test' entries
mock_osc_success() {
    shift
    printf '%s\n' openQA os-autoinst openQA-test
}
osc=mock_osc_success
try list_packages devel:openQA
is "$rc" 0 "list_packages success"
is "$got" $'openQA\nos-autoinst' "list_packages returns packages without -test entries"

# list_packages: empty project yields no output and success
mock_osc_empty() { return 0; }
osc=mock_osc_empty
try list_packages devel:openQA:testing
is "$rc" 0 "list_packages empty project success"
is "$got" "" "list_packages empty project has no output"

# list_packages: failed 'osc ls' (e.g. HTTP 5xx printing to stdout) must not
# be mistaken for a package list; propagate failure and emit nothing
mock_osc_fail() {
    printf '%s\n' "Request: https://api.opensuse.org/source/devel:openQA:testing?deleted=0" "Headers:"
    return 1
}
osc=mock_osc_fail
try list_packages devel:openQA:testing
is "$rc" 1 "list_packages propagates osc failure"
is "$got" "" "list_packages emits no output on osc failure"
