#!/usr/bin/env bash

source test/init
bpan:source bashplus +err +fs +sym

plan tests 102

host=localhost
url=https://localhost

source openqa-investigate

client_args=()
cli_rc=1
openqa-cli() {
    return $cli_rc
}
client_call=(openqa-cli)

fetch-vars-json() {
    local tid=$1
    if [[ $tid -eq 10020 ]]; then
        echo '{"TEST_GIT_URL": "https://github.com/os-autoinst/os-autoinst-distri-openQA.git"}'
    elif [[ $tid -eq 10026 ]]; then
        echo '{"TEST_GIT_URL": "UNKNOWN (origin remote not found)"}'
    elif [[ $tid -eq 10022 ]]; then
        echo '{"WORKER_CLASS": "foo,duh,bar"}'
    else
        echo '{}'
    fi
}
rc=0
out=$(clone 41 42 2>&1 > /dev/null) || rc=$?
is "$rc" 1 'fails when unable to query job data'
like "$out" "unable to query job data for 42: " 'query error on stderr'

cli_rc=0
consider_parallel_and_directly_chained_clusters=1
out=$(clone 41 42 2>&1 > /dev/null) || rc=$?
is "$rc" 2 'fails when no jobs could be restarted'
is "$out" "Unable to clone job 42: it is part of a directly chained cluster (not supported)" 'restart error on stderr'

# Mocking
client-get-job() {
    local tid=$1
    # GET jobs/id
    case "$tid" in
        10024)
            echo '{"job": { "test": "vim", "priority": 50, "settings" : {} } }'
            ;;
        10030 | 10032)
            echo '{"job": { "test": "vim", "priority": "50", "settings":{"CASEDIR": "https://github.com/os-autoinst/os-autoinst-testrepo.git"} } }'
            ;;
        10027)
            echo '{"job": { "test": "vim", "clone_id" : 10028 } }'
            ;;
        3000)
            echo '{"job": { "test": "vim", "result": "failed" } }'
            ;;
        3001)
            echo '{"job": { "test": "vim:investigate:last_good_tests", "result": "failed" } }'
            ;;
        3002)
            echo '{"job": { "test": "vim", "result": "failed" } }'
            ;;
        3003)
            echo '{"job": { "test": "vim", "result": "failed" } }'
            ;;
        30001)
            echo '{"job": { "test": "vim:investigate:retry", "result": "failed", "settings": {"OPENQA_INVESTIGATE_ORIGIN": "3000"} } }'
            ;;
        30002)
            echo '{"job": { "test": "vim:investigate:retry", "result": "passed", "settings": {"OPENQA_INVESTIGATE_ORIGIN": "3000"} } }'
            ;;
        30003)
            echo '{"job": { "test": "vim:investigate:retry", "result": "softfailed", "settings": {"OPENQA_INVESTIGATE_ORIGIN": "3000"} } }'
            ;;
        30004)
            echo '{"job": { "test": "vim:investigate:retry", "result": "softfailed", "settings": {"OPENQA_INVESTIGATE_ORIGIN": "3000"} } }'
            ;;
        30005)
            echo '{"job": { "test": "vim-other:investigate:retry", "result": "failed", "settings": {"OPENQA_INVESTIGATE_ORIGIN": "3000"} } }'
            ;;
        3003[134])
            echo '{"job": { "test": "vim:investigate:retry", "result": "failed", "settings": {"OPENQA_INVESTIGATE_ORIGIN": "3003"} } }'
            ;;
        30032)
            echo '{"job": { "test": "vim:investigate:retry", "result": "passed", "settings": {"OPENQA_INVESTIGATE_ORIGIN": "3003"} } }'
            ;;
        10035)
            echo '{"job": { "test": "vim", "result": "failed", "parent_group": "foo", "group": "bar", "settings": {"OPENQA_INVESTIGATE_ORIGIN": "10035"} } }'
            ;;
        10042)
            echo '{"job": { "test": "vim", "result": "failed", "group": "42", "settings": {"NO_INVESTIGATION": "1"} } }'
            ;;
        10043)
            echo '{"job": { "test": "vim", "result": "failed", "group": "43", "settings": {"NO_INVESTIGATION": "0"} } }'
            ;;
        404)
            echo '404 Not Found'
            ;;
        *)
            echo '{"debug": "client-get-job '"$tid"'"}'
            ;;
    esac
}
client-get-job-state() {
    local tid=$1
    # GET experimental/jobs/id/status
    case "$tid" in
        10032)
            echo '{ "state": "cancelled", "test": "vim:investigate:retry", "result": "none" }'
            ;;
        30001 | 30002)
            echo '{ "state": "done", "test": "vim:investigate:retry", "result": "failed" }'
            ;;
        30003 | 30004)
            echo '{ "state": "done", "test": "vim:investigate:retry", "result": "passed" }'
            ;;
        30021 | 30022)
            echo '{ "state": "done", "test": "vim:investigate:retry", "result": "failed" }'
            ;;
        30023 | 30024)
            echo '{ "state": "running", "test": "vim:investigate:retry", "result": "none" }'
            ;;
        30031 | 30033)
            echo '{ "state": "done", "test": "vim:investigate:retry", "result": "failed" }'
            ;;
        30032 | 30034)
            echo '{ "state": "done", "test": "vim:investigate:retry", "result": "passed" }'
            ;;
        *)
            echo '{"debug": "client-get-job-state '"$tid"'"}'
            ;;
    esac
}
client-get-job-comments() {
    local tid=$1
    # GET jobs/id/comments
    case "$tid" in
        10030)
            echo '[{"id": 1234, "text":"Starting investigation for 10031"},{"id": 1235, "text":"unrelated comment"}]'
            ;;
        10032)
            echo '[{"id": 1236, "text":"Starting investigation for job 10032"},{"id": 1237, "text":"Automatic investigation jobs for job :investigate:retry*:t#10032"}]'
            ;;
        10035)
            echo '[{"id": 1236, "text":"Starting investigation for job 10035"}]'
            ;;
        10043)
            echo '[{"id": 10043, "text":"Starting investigation for job 10043"}]'
            ;;
        3000)
            echo '[{"id": 1236, "text":"Automatic investigation jobs for job\n*a:investigate:retry*: t#30001\n*a:investigate:last_good_tests:coffee*: t#30002\n*a:investigate:last_good_build:2001*: t#30003\n*a:investigate:last_good_tests_and_build:coffee+2001*: t#30004"}]'
            ;;
        3002)
            echo '[{"id": 1236, "text":"Automatic investigation jobs for job\n*a:investigate:retry*: t#30021\n*a:investigate:last_good_tests:coffee*: t#30022\n*a:investigate:last_good_build:2001*: t#30023\n*a:investigate:last_good_tests_and_build:coffee+2001*: t#34024"}]'
            ;;
        3003)
            echo '[{"id": 1236, "text":"Automatic investigation jobs for job\n*a:investigate:retry*: t#30031\n*a:investigate:last_good_tests:coffee*: t#30032\n*a:investigate:last_good_build:2001*: t#30033\n*a:investigate:last_good_tests_and_build:coffee+2001*: t#30034"}]'
            ;;
        *)
            echo '{"debug": "client-get-job-comments '"$tid"'"}'
            ;;
    esac
}
client-post-job-comment() {
    local tid=$1 text=$2
    # POST jobs/id/comments
    case "$tid" in
        10030)
            echo '{"id": 1234}'
            ;;
        10031)
            echo true > "$comment_for_job_31_created"
            ;;
        10032)
            echo '{"id": 1237}'
            ;;
        10035)
            echo '{"id": 1236}'
            ;;
        10043)
            echo '{"id": 10043}'
            ;;
        3000)
            warn "Commenting 3000 ($@)"
            exit 99
            ;;
        30002)
            warn "Commenting 30002 ($@)"
            exit 99
            ;;
        3003)
            warn "Commenting 3003 ($@)"
            exit 99
            ;;
        *)
            echo '{"debug": "client-post-job-comment '"$tid"'"}'
            ;;
    esac
}
client-put-job-comment() {
    local tid=$1 cid=$2 text=$3
    # PUT jobs/id/comments/id
    if [[ $tid -eq 10030 && $cid -eq 1234 ]]; then
        echo true > "$comment_1234_updated"
    fi
}
client-delete-job-comment() {
    local tid=$1 cid=$2
    # DELETE jobs/id/comments/id
    if [[ $tid -eq 10030 && $cid -eq 1234 ]]; then
        echo true > "$comment_1234_deleted"
    fi
}
get-dependencies-ajax() {
    local tid=$1
    # GET tests/id/dependencies_ajax
    case "$tid" in
        10027)
            echo '{"cluster":{}, "edges":[], "nodes":[{"id":10027,"state":"done","result":"passed"}]}'
            ;;
        10028)
            echo '{"cluster":{}, "edges":[], "nodes":[{"id":10028,"state":"done","result":"failed"}]}'
            ;;
        10030)
            echo '{"cluster":{"cluster_foo":[10028,10030],"cluster_bar":[29]}, "edges":[], "nodes":[{"id":10028,"state":"uploading","result":"none"},{"id":10030,"state":"done","result":"passed"}]}'
            ;;
        10032)
            echo '{"cluster":{"cluster_foo":[10028,10032],"cluster_bar":[29]}, "edges":[], "nodes":[{"id":10028,"state":"cancelled","result":"none"},{"id":10032,"state":"done","result":"failed"},{"id":29,"state":"running","result":"running"}]}'
            ;;
        10031)
            # job with cancelled job in the cluster (should be treated like a done job)
            echo '{"cluster":{"cluster_foo":[10028,10031],"cluster_bar":[29]}, "edges":[], "nodes":[{"id":10028,"state":"cancelled","result":"none"},{"id":10031,"state":"done","result":"failed"}]}'
            ;;
        10035)
            echo '{"cluster":{},"edges":[], "nodes":[{"id":10035,"state":"cancelled","result":"none"}]}'
            ;;
        10042)
            echo '{"cluster":{}, "edges":[], "nodes":[{"id":10042,"state":"done","result":"failed"}]}'
            ;;
        10043)
            echo '{"cluster":{}, "edges":[], "nodes":[{"id":10043,"state":"done","result":"failed"}]}'
            ;;
        *)
            echo '{"debug": "get-dependencies-ajax '"$tid"'"}'
            ;;
    esac
}
openqa-cli() {
    # fallback
    args="$@"
    echo '{"debug": "openqa-cli '"${args//$'\n'/ }"'"}'
}

clone_call=echo
_clone_call() {
    echo "$@" >&2
}

clone_call=_clone_call
try clone 10023 10024
is "$rc" 0 "Successful clone"
testlabel="vim:investigate"
has "$got" "* *$testlabel*: " "Expected markdown output of job urls for unsupported clusters"
has "$got" '_TRIGGER_JOB_DONE_HOOK=1' "job is cloned with _TRIGGER_JOB_DONE_HOOK"

try clone 10023 10024 foo refspec
is "$rc" 0 "Successful clone"
like "$got" 'CASEDIR=.*/os-autoinst-distri-opensuse.git#refspec' "job is cloned and it uses default CASEDIR"

try clone 10026 10024 foo refspec
is "$rc" 0 "Successful clone"
like "$got" 'Can not clone refspec' "job with non-git TEST_GIT_URL is not cloned"

try clone 10020 10024 foo refspec
is "$rc" 0 "Successful clone"
like "$got" 'CASEDIR=.*/os-autoinst-distri-openQA.git#refspec' "job is cloned with sets CASEDIR from TEST_GIT_URL"

try clone 10023 10030 foo refspec
is "$rc" 0 "Successful clone"
like "$got" 'CASEDIR=.*/os-autoinst-testrepo.git#refspec' "job is cloned and CASEDIR is set correctly"

try clone 10022 10030 last_good_tests_and_build:123 refspec
has "$got" 'WORKER_CLASS:vim=foo,duh,bar' "job assigns the WORKER_CLASS only to the origin job clone"

try clone 10021 10030 last_good_tests_and_build:123 refspec
has "$got" 'unidentified worker class in vars.json' "info shown in the name is WORKER_CLASS is empty"

curl() {
    if [[ $@ =~ investigation_ajax ]]; then
        echo -n '{ }'$'\n'200
    else
        echo -n 200
    fi
}

try investigation_gid=888 investigate 10035
is "$rc" 0 'success when clone with non-default _GROUP_ID'
has "$got" "_GROUP_ID=888" "clone with correct group_id"

clone_call=echo
try investigate 10027
is "$rc" 0 'success regardless of actually triggered jobs'
is "$got" "Job 10027 already has a clone, skipping investigation. Use the env variable 'force=true' to trigger investigation jobs"

try force=true investigate 10028
is "$rc" 0 'still success when job is skipped (because of exclude_no_group)'
has "$got" "exclude_no_group is set, skipping investigation"

try investigate 10030
is "$rc" 142 'investigation postponed because other job in cluster is not done'
is "$got" "Postponing to investigate job 10030: waiting until 1 pending parallel job(s) finished"

try main 10030
is "$rc" 142 'return code (for postponing) passed by main function'
is "$got" "Postponing to investigate job 10030: waiting until 1 pending parallel job(s) finished" 'output passed by main function'

try investigate 10032
is "$rc" 0 'investigation not postponed if other job in dependency tree not done but cluster itself is done'

try force=true investigate 10031
is "$rc" 0 'success when job is skipped (because of exclude_no_group and job w/o group)'
has "$got" 'Job w/o job group, $exclude_no_group is set, skipping investigation'

try investigate 10042
is "$rc" 0 'NO_INVESTIGATION=1, a failed job with this variable should be skipped'
has "$got" 'NO_INVESTIGATION=1 detected, skipping'

try investigate 10043
is "$rc" 0 "Successful clone, NO_INVESTIGATION != 1 should not change default behavior"
has "$got" '_TRIGGER_JOB_DONE_HOOK=1' "job is cloned with _TRIGGER_JOB_DONE_HOOK"

test-post-investigate() {
    # job is one of the other investigation types, e.g. :investigate:last_good_tests
    try investigate 3001
    is "$rc" 0 'success (3001)'
    has "$got" "Job is ':investigate:' already, skipping investigation" "skip investigation, not a retry (3001)"

    # retry failed
    # product issue
    try investigate 30001
    is "$rc" 2 'mocked function returned failure (30001)'
    has "$got" "Commenting 3000" "Posting comment on OPENQA_INVESTIGATE_ORIGIN (30001)"
    has "$got" "Investigate retry job *vim:investigate:retry*" "retry test name appears in comment(30001)"
    has "$got" "likely a product issue" "product issue (30001)"

    # retry passed
    try investigate 30003
    is "$rc" 2 'mocked function returned failure (30003)'
    has "$got" "Commenting 3000" "Posting comment on OPENQA_INVESTIGATE_ORIGIN (30003)"
    has "$got" "Likely a sporadic" "sporadic (passed) (30003)"

    # retry softfailed
    try investigate 30003
    is "$rc" 2 'mocked function returned failure (30003)'
    has "$got" "Commenting 3000" "Posting comment on OPENQA_INVESTIGATE_ORIGIN (30003)"
    has "$got" "Likely a sporadic" "sporadic (softfailed) (30003)"

    # retry softfailed
    try investigate 30005
    is "$rc" 0 'early return (other test in cluster) (30005)'
    has "$got" "is not a retry" "vim-other vs. vim (30005)"

    # test issue
    try investigate 30031
    is "$rc" 2 'mocked function returned failure (30031)'
    has "$got" "Commenting 3003" "Posting comment on OPENQA_INVESTIGATE_ORIGIN (30031)"
    has "$got" "Investigate retry job *vim:investigate:retry*" "retry test name appears in comment(30031)"
    has "$got" "likely a test issue" "test issue (30031)"

    # 142 not finished yet
    local job_data='{"job": { "result": "failed", "settings": { "OPENQA_INVESTIGATE_ORIGIN": "https://localhost/t3002" } } }'
    try post-investigate 2039 "vim:investigate:retry"
    is "$rc" 142 'post-investigate returned 142 (not all jobs finished yet) (2039)'

    # test does not exist anymore
    local job_data='{"job": { "result": "failed", "settings": { "OPENQA_INVESTIGATE_ORIGIN": "https://localhost/t404" } } }'
    try post-investigate 2040 "vim:investigate:retry"
    is "$rc" 0 'post-investigate returned 0 if the original job does not exist anymore'

    local job_data='{"job": { "result": "failed", "settings": { "OPENQA_INVESTIGATE_ORIGIN": "https://localhost/t10032" } } }'
    post-investigate 10032 'vim:investigate:retry'
    is "$rc" 0 'cancelled job handled without error'
    has "$comment" "t#10032 cancelled" "comment mentions that job was cancelled"

    # various combinations of investigation results
    t1="fail"

    t2="passed" t3="passed" t4="passed"
    fetch-investigation-results() {
        echo "retry|1|$t1
last_good_tests|2|$t2
last_good_build|3|$t3
last_good_tests_and_build|4|$t4"
    }
    product_issue=false
    test_issue=false
    infra_issue=false
    identify-issue-type 999
    is "$product_issue" "false" "$t1+$t2+$t3+$t4 -> false"
    is "$test_issue" "false" "$t1+$t2+$t3+$t4 -> false"

    t2="fail" t3="passed" t4="passed"
    identify-issue-type 999
    is "$product_issue" "true" "$t1+$t2+$t3+$t4 -> true"
    is "$test_issue" "false" "$t1+$t2+$t3+$t4 -> false"

    t2="fail" t3="fail" t4="passed"
    identify-issue-type 999
    is "$product_issue" "false" "$t1+$t2+$t3+$t4 -> false"
    is "$test_issue" "false" "$t1+$t2+$t3+$t4 -> false"

    t2="" t3="passed" t4="passed"
    identify-issue-type 999
    is "$product_issue" "true" "$t1+$t2+$t3+$t4 -> true"
    is "$test_issue" "false" "$t1+$t2+$t3+$t4 -> false"

    t2="" t3="passed" t4=""
    identify-issue-type 999
    is "$product_issue" "true" "$t1+$t2+$t3+$t4 -> true"
    is "$test_issue" "false" "$t1+$t2+$t3+$t4 -> false"

    t2="fail" t3="" t4="fail"
    identify-issue-type 999
    is "$product_issue" "false" "$t1+$t2+$t3+$t4 -> false"
    is "$test_issue" "false" "$t1+$t2+$t3+$t4 -> false"

    t2="passed" t3="" t4=""
    identify-issue-type 999
    is "$product_issue" "false" "$t1+$t2+$t3+$t4 -> false"
    is "$test_issue" "true" "$t1+$t2+$t3+$t4 -> true"

    t2="passed" t3="passed" t4=""
    identify-issue-type 999
    is "$product_issue" "false" "$t1+$t2+$t3+$t4 -> false"
    is "$test_issue" "false" "$t1+$t2+$t3+$t4 -> false"

    t2="passed" t3="" t4="passed"
    identify-issue-type 999
    is "$product_issue" "false" "$t1+$t2+$t3+$t4 -> false"
    is "$test_issue" "true" "$t1+$t2+$t3+$t4 -> true"

    t2="passed" t3="failed" t4="passed"
    identify-issue-type 999
    is "$product_issue" "false" "$t1+$t2+$t3+$t4 -> false"
    is "$test_issue" "true" "$t1+$t2+$t3+$t4 -> true"

    t2="passed" t3="failed" t4="failed"
    identify-issue-type 999
    is "$product_issue" "false" "$t1+$t2+$t3+$t4 -> false"
    is "$test_issue" "false" "$t1+$t2+$t3+$t4 -> false"

    t2="failed" t3="failed" t4="failed"
    identify-issue-type 999
    is "$product_issue" "false" "$t1+$t2+$t3+$t4 -> false"
    is "$test_issue" "false" "$t1+$t2+$t3+$t4 -> false"
    is "$infra_issue" "true" "$t1+$t2+$t3+$t4 -> true"

    t2="failed" t3="" t4=""
    identify-issue-type 999
    is "$infra_issue" "false" "$t1+$t2+$t3+$t4 -> false"

    # test when last good tests or build do not exist
    fetch-investigation-results() {
        echo "retry|1|failed
last_good_build|3|passed"
    }
    identify-issue-type 999
    is "$product_issue" "true" "failed+n/a+failed+n/a -> true"
    is "$test_issue" "false" "failed+n/a+failed+n/a -> false"

    fetch-investigation-results() {
        echo "retry|1|failed
last_good_tests|2|passed"
    }
    identify-issue-type 999
    is "$product_issue" "false" "failed+failed+n/a+n/a -> false"
    is "$test_issue" "true" "failed+failed+n/a+n/a -> true"
}

test-post-investigate

# test syncing via investigation comment; we are first
try force=true sync_via_investigation_comment 10031 10030
is "$rc" 255 'do not skip if we own first investigation comment'
has "$got" '1234' 'comment ID returned'

# test syncing via investigation comment; we are second
try force=true sync_via_investigation_comment 10032 10032
is "$rc" 0 'skip with success if we do not own first investigation comment'
# XXX What is this testing?
like "$got" '' 'no output when skipping'

# Make auto-deleting temp files for testing subprocesses:
+fs:mktemp
comment_1234_updated=$temp
+fs:mktemp
comment_1234_deleted=$temp
+fs:mktemp
comment_for_job_31_created=$temp

# test finalizing investigation comment when no investigation jobs were needed
ok "$(force=true finalize_investigation_comment 10031 10030 1234 '' 2>&1)" \
    'success if no investigation jobs needed to be created after all'
ok "$([[ -s $comment_1234_deleted ]])" \
    'comment on job 10030 deleted'
ok "$([[ ! -s $comment_for_job_31_created ]])" \
    'no comment on job 10031 created'

# test finalizing investigation comment when investigation jobs had been created
ok "$(force=true finalize_investigation_comment 10031 10030 1234 'foo' 2>&1)" \
    'success if we write an investigation comment'
ok "$([[ -s $comment_1234_updated ]])" \
    'comment on job 10030 updated'
ok "$([[ -s $comment_for_job_31_created ]])" \
    'comment on job 10031 created as well'

trigger_jobs() {
    echo "simulated failure from trigger_jobs" >&2
    return 1
}

try investigate 10035
is "$rc" 1 'investigate fails when trigger_jobs fails'
has "$got" "Unexpected error encountered when triggering job 10035" "error message contains job id"
has "$got" "simulated failure from trigger_jobs" "original error message from trigger_jobs included"

trigger_jobs() {
    echo "Current job 10035 will fail, because the repositories for the below updates are unavailable" >&2
    return 1
}

try investigate 10035
is "$rc" 0 'investigate passes when trigger_jobs fails with known error'
has "$got" "Ignoring known error when triggering job 10035" "error message contains job id"
like "$got" "Current job 10035 will fail.*repositories.*available" "Got message about unavailable repositories"
