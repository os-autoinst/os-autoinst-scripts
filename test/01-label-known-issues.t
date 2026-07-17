#!/usr/bin/env bash

source test/init

plan tests 27

source ./_common

client_output=''
mock-client() {
    client_output+="client_call $@"$'\n'
}

client_call=(mock-client "${client_call[@]}")
logfile1=$dir/data/01-os-autoinst.txt.1
logfile2=$dir/data/01-os-autoinst.txt.2
logfile3=$dir/data/01-os-autoinst.txt.3

try-client-output() {
    out=$logfile1
    client_output=''
    try "$*"' && echo "$client_output"'
}

try-client-output comment_on_job 123 Label
is "$rc" 0 'successful comment_on_job'
is "$got" "client_call -X POST jobs/123/comments text=Label" 'comment_on_job works'

try "search_log 123 'foo.*bar'" "$logfile1"
is "$rc" 0 'successful search_log'

try "search_log 123 'foo.*bar'" "$logfile2"
is "$rc" 1 'failing search_log'

try "search_log 123 '([dD]ownload.*failed.*404)[\S\s]*Result: setup failure'" "$logfile3"
is "$rc" 0 'successful search_log'

try "search_log 123 'foo [z-a]' $logfile2"
is "$rc" 2 'search_log with invalid pattern'
has "$got" 'range out of order in character class' 'correct error message'

try-client-output label-on-issue 123 'foo.*bar' Label 1 softfailed
expected="client_call -X POST jobs/123/comments text=Label
client_call -X POST jobs/123/restart"
is "$rc" 0 'successful label-on-issue'
is "$got" "$expected" 'label-on-issue with restart and disabled force_result'

try-client-output enable_force_result=true label-on-issue 123 'foo.*bar' Label 1 softfailed
expected="client_call -X POST jobs/123/comments text=label:force_result:softfailed:Label
client_call -X POST jobs/123/restart"
is "$rc" 0 'successful label-on-issue'
is "$got" "$expected" 'label-on-issue with restart and force_result'

try-client-output label-on-issue 123 "foo.*bar" Label
expected="client_call -X POST jobs/123/comments text=Label"
is "$rc" 0 'successful label-on-issue'
is "$got" "$expected" 'label-on-issue with restart and force_result'

try-client-output "label-on-issue 123 'foo bar' Label"
is "$rc" 1 'label-on-issue did not find search term'
is "$got" "" 'label-on-issue with restart and force_result'

send-email() {
    local mailto=$1 email=$2
    echo "$mailto" >&2
    echo "$email" >&2
}
openqa-cli() {
    local id=$(basename "$4")
    cat "$dir/data/group$id.json"
}
from_email=foo@bar
client_args=(api --host http://localhost)
testurl=https://openqa.opensuse.org/api/v1/jobs/2291399
group_id=24
job_data='{"job": {"name": "foo", "result": "failed"}}'
out=$(handle_unreviewed "$testurl" "$logfile1" "no reason" "$group_id" true "$from_email" "" "$job_data" 2>&1 > /dev/null) || true
has "$out" 'Subject: Unreviewed issue (Group 24 openQA)' "send-email subject like expected"
has "$out" 'From: openqa-label-known-issues <foo@bar>' "send-email from called like expected"
has "$out" 'To: dummy@example.com.dummy' "send-email to like expected"
like "$out" '8<.*Backend process died.*>8' 'Log excerpt in mail'
has "$out" 'Content-Type: text/html' 'mail has text part'
has "$out" 'Content-Type: text/plain' 'mail has HTML part'
has "$out" '<li>Name: foo' 'mail contains job name'

out=$(handle_unreviewed "$testurl" "$logfile1" "no reason" "null" true "$from_email" 2>&1 > /dev/null) || true
is "$out" '' "send-email not called for group_id null"

group_id=25
out=$(handle_unreviewed "$testurl" "$logfile1" "no reason" "$group_id" true "$from_email" 2>&1 > /dev/null) || true
is "$out" '' "send-email not called for no email address and no fallback address"

notification_address=fallback@example.com
out=$(handle_unreviewed "$testurl" "$logfile1" "no reason" "$group_id" true "$from_email" "$notification_address" "$job_data" 2>&1 > /dev/null) || true
has "$out" 'To: fallback@example.com' "send-email to like expected"
has "$out" 'Subject: Unreviewed issue (Group 25 Lala)' "send-email subject like expected"

send-email() {
    local mailto=$1 email=$2
    echo "$mailto" >&2
    echo "$email" >&2
}
out=$(handle_unreviewed "$testurl" "$logfile1" "no reason" "$group_id" true "" "" "$job_data") || true
like "$out" "\[$testurl\].*Unknown test issue, to be reviewed" 'unknown issue found'

openqa_label_known_issues=$(dirname $0)/../openqa-label-known-issues
out=$($openqa_label_known_issues 2>&1)
like "$out" "Need.*testurl"
