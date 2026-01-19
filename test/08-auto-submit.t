#!/usr/bin/env bash

source test/init
plan tests 18

mock_osc() {
    local cmd=$1
    local args=(${@:2})
    if [[ $cmd == 'request' && ${args[0]} == 'list' && ${args[9]} == '--days' && ${args[10]} == "$throttle_days" ]]; then
        _request_list
    fi
}

_requests=(
    'submit:          devel:openQA:tested/openQA@c0f8ee6a233ed250dbc54c19dee50118 -> openSUSE:Factory'
    'maintenance_incident: devel:openQA:tested/openQA@ae3d930a703dc411e249d644ad8b6802 -> openSUSE:Maintenance (release in openSUSE:Backports:SLE-15-SP6:Update)'
)

_request_list() { echo "${_requests[@]}"; }

mock_git_obs() {
    if [[ $3 == 'repos/pool/openQA/pulls?state=open&sort=recentupdate' ]]; then
        _pr_list leap-16.0
    else
        _pr_list foo # PR targeting "foo" is supposed to be ignored
    fi
}

_pr_list() {
    local ref=$1
    echo "[{\"updated_at\":\"$two_days_ago\", \"html_url\": \"https://foo/bar\", \"user\": {\"login\": \"$git_user\"}, \"base\": {\"ref\": \"$ref\"}}]"
}

two_days_ago=$(date --iso-8601=seconds --date='-2 day')
osc=mock_osc
git_obs=mock_git_obs
source os-autoinst-obs-auto-submit

note "########### has_pending_submission"

throttle_days=0
package=os-autoinst
try has_pending_submission "$package" "$submit_target"
is "$rc" 0 "returns 0 with throttle_days=0"

throttle_days=1
try has_pending_submission "$package" "$submit_target"
is "$rc" 1 "returns 1 with existing SR"
like "$got" "Skipping submission, there is still a pending SR for package os-autoinst" "expected output"

_request_list() { echo; }
try has_pending_submission "$package" "$submit_target"
is "$rc" 0 "returns 0 without existing SRs"
like "$got" "info.*has_pending_submission" "no output"

submit_target=openSUSE:Leap:16.0
throttle_days=3 # expected to be ignored for openSUSE:Leap:16.0
throttle_days_leap_16=1
try has_pending_submission "$package" "$submit_target"
is "$rc" 0 "returns 0 without existing PRs"
like "$got" "info.*has_pending_submission\\($package, $submit_target\\)$" "no output (no PR)"

package=openQA
try has_pending_submission "$package" "$submit_target"
is "$rc" 0 "returns 0 with existing PR older than throttle config of $throttle_days days"
like "$got" "info.*has_pending_submission\\($package, $submit_target\\)$" "no output (old PR)"

throttle_days_leap_16=3
try has_pending_submission "$package" "$submit_target"
is "$rc" 1 "returns 1 with existing PR that is more recent than throttle config of $throttle_days days"
like "$got" "info.*Skipping submission.*pending PR.*https://foo/bar" "expected output (recent PR)"

_request_list() { echo "${_requests[@]}"; }
submit_target=openSUSE:Backports:SLE-15-SP6:Update
try has_pending_submission "$package" "$submit_target"
is "$rc" 1 "returns 1 with existing maintenance incident that is more recent than throttle config of $throttle_days days"
like "$got" "info.*Skipping submission.*pending SR" "expected output (skipping)"

diag "########### make_obs_submit_request"
mock_osc() {
    echo ''
}
try make_obs_submit_request openQA Factory 3.14 cmd="echo test"
is "$rc" 3 "failure when osc does not return XML"

mock_osc() {
    echo '<collection><request id="23"/></collection>'
}

try make_obs_submit_request openQA Factory 3.14 cmd="echo test"
is "$rc" 0 "success"
like "$got" "osc sr -s 23 -m Update to 3.14 Factory" "superseding as expected"

mock_osc() {
    echo '<collection></collection>'
}

try make_obs_submit_request openQA Factory 3.14 cmd="echo test"
is "$rc" 0 "success when xmlstarlet did not find anything"
like "$got" "osc sr -m Update" "creating new request as expected"
