# os-autoinst scripts ![](https://github.com/os-autoinst/os-autoinst-scripts/workflows/ci/badge.svg)


## Communication

If you have questions, visit us on IRC in
[#opensuse-factory](irc://chat.freenode.net/opensuse-factory)

## How to use

Checkout the individual scripts and either call them manually or automatically,
e.g. in CI jobs find all the dependencies which are required in
[dependencies.yaml](https://github.com/os-autoinst/os-autoinst-scripts/blob/master/dependencies.yaml).
You can install all dependencies by installing the `os-autoinst-scripts-deps`
package under openSUSE. You need to add the
[openQA development repository](https://open.qa/docs/#_development_version_repository)
for this.

### auto-review - Automatically detect known issues in openQA jobs, label openQA jobs with ticket references and optionally retrigger

Based on simple regular expressions in the subject line of tickets on
progress.opensuse.org within https://progress.opensuse.org/projects/openqav3 or
any subproject of it, commonly
https://progress.opensuse.org/projects/openqatests/ , openQA jobs can be
automatically labeled with the corresponding ticket and optionally retriggered
where it makes sense.

For this the subject line of a ticket must include text following the format
`auto_review:"<search_term>"[:retry][:force_result:<result>]`

Note that the `force_result` feature is disabled by default.

* `<search_term>`: the perl extended regex to search for
* `:retry`: (optional) boolean switch after the quoted search term to instruct
  for retriggering the according openQA job.
* `:force_result:<result>`: (optional) give the job a special label which
  forces the result to be the given string

Examples:
* `auto_review:"error 42 found"`.
* `auto_review:"error 42 found":retry`.
* `auto_review:"error 42 found":force_result:softfailed`.
* `auto_review:"error 42 found":retry:force_result:softfailed`.

The search terms are crosschecked against the logfiles and "reason" field of
the openQA jobs. A multi-line search is possible, for example using the
`<search_term>`

  `something to.*match earlier[\S\s]*something to match some lines further down`

Other double quotes in the subject line than around the search term should be
avoided. Also **avoid generic search terms** to prevent false matches of job
failures unrelated to the specified ticket.

* [openqa-monitor-incompletes](https://github.com/os-autoinst/os-autoinst-scripts/blob/master/openqa-monitor-incompletes)
  queries the database of an openQA instance (ssh access is necessary) and
  output the list of "interesting" incompletes, where "interesting" means not
  all incompletes but the ones likely needing actions by admins, e.g.
  unreviewed, no clones, no obvious "setup failure", etc.
* [openqa-label-known-issues-multi](https://github.com/os-autoinst/os-autoinst-scripts/blob/master/openqa-label-known-issues-multi)
  can take a list of openQA jobs, for example output from
  "openqa-monitor-incompletes" and look for matching "known issues", for
  example from progress.opensuse.org, label the job and retrigger if specified
  in the issue (see the source code for details how to mark tickets)

For tickets referencing "auto_review" it is suggested to add a text section
based on the following template:

```
## Steps to reproduce

Find jobs referencing this ticket with the help of
https://raw.githubusercontent.com/os-autoinst/os-autoinst-scripts/master/openqa-query-for-job-label ,
for example to look for ticket 12345 call `openqa-query-for-job-label poo#12345`
```

*Notes:* the regex used in `<search_term>` is very powerful but also can be
expensive when `(?s)line1.*line2.*line3` is used. Every `.*` can span over
multiple lines and that involves a lot of backtracking and might not be needed.
Use `[^\n]*` over `.*` where possible or use line spanning matches with
`[\S\s]*` instead of `(?s)`.

### Unreviewed issues

If none of your configured searches above are matching, you can configure a
notification for those failed jobs, for example to an email address or a
Slack channel. You can also configure a [fallback address](#Configuration).

Just put the following into the description of the Job Group:

    MAILTO: some-email@address.example,other@address.example

Each Slack channel has its own email address which you can find out by clicking
on the title and then on "Integrations".

The email will have the subject "Unreviewed issue (Group 123 Foobar)".
It will contain the link to the job and a small log excerpt, possibly
already pointing to the error. The sender email address can be
[configured](#Configuration).

### openqa-investigate - Automatic investigation jobs with failure analysis in openQA

openQA can be configured to automatically trigger investigation jobs whenever
there is no carry-over and no automatic ticket assignment by auto-review.

* [openqa-monitor-investigation-candidates](https://github.com/os-autoinst/os-autoinst-scripts/blob/master/openqa-monitor-investigation-candidates)
  queries the dabase of an openQA instance (ssh access is necessary) and
  output the list of failed jobs that are suitable for triggering
  investigation jobs on, compare to "openqa-monitor-incompletes"

* [openqa-investigate-multi](https://github.com/os-autoinst/os-autoinst-scripts/blob/master/openqa-investigate-multi)
  can take a list of openQA jobs, for example output of
  "openqa-monitor-investigation-candidates" and trigger "investigation jobs",
  e.g. a plain retrigger, using the "last good" tests as well as "last good"
  build and a combination of both. The results of all these four jobs should
  give a pretty good indication if something is a test regression, a product
  regression, an infrastructure problem or a sporadic issue.


### Combine auto-review and openqa-investigate

A possible approach to combine handling known issues and unreviewed issues is to
run "openqa-label-known-issues-multi" against all "investigation candidates" and
pass all unreviewed issues to "openqa-investigate-multi":

```
./openqa-review-failed
```

which does the equivalent of:

```
./openqa-monitor-investigation-candidates | ./openqa-label-known-issues-multi | ./openqa-investigate-multi
```

with minor changes to the input/output format used between the commands.

### openQA hook scripts - Call auto-review or investigation steps in openQA after every job is done

openQA supports custom job done hook scripts that can be called whenever a job
is done, see
http://open.qa/docs/#_enable_custom_hook_scripts_on_job_done_based_on_result
for details. For the purpose of being called as these hook scripts here the
following scripts are provided:

* [openqa-label-known-issues-hook](https://github.com/os-autoinst/os-autoinst-scripts/blob/master/openqa-label-known-issues-hook)
  calls "openqa-label-known-issues" on the specified job
* [openqa-label-known-issues-and-investigate-hook](https://github.com/os-autoinst/os-autoinst-scripts/blob/master/openqa-label-known-issues-and-investigate-hook)
  calls "openqa-label-known-issues" on the specified job and if no label was
  found, "openqa-investigate". Compare to section
  "Combine auto-review and openqa-investigate"

### More details and examples about openqa-investigate comments

Let's say we have a failed test `openqa-Tumbleweed-dev-x86_64-Build:TW.315-vim`.
openqa-investigate will trigger several jobs and write a comment:

```
Automatic investigation jobs for job 123:
* vim:investigate:retry: https://openqa/t124
* vim:investigate:last_good_tests:abc123: https://openqa/t126
* vim:investigate:last_good_build:TW.314: https://openqa/t125
* vim:investigate:last_good_tests_and_build:abc123+TW.314: https://openqa/t127
```

The first job `vim:investigate:retry` is a simple retry with the same settings,
in order to find out if the failure is reproducible consistently.

The `vim:investigate:last_good_tests:abc123` is a retry which uses the last
good test code. It searches for the last good test, and if there were changes
in the test code, it will trigger the test using the git sha from that last good
test, e.g. `CASEDIR=orginal_git_url#abc123`.

The `vim:investigate:last_good_build:TW.314` is a retry using the last good
build. It searches for the last good test, and if it has a different build
than the current failing test, it triggers the original test with that build,
e.g. `BUILD=TW.314`.

The `vim:investigate:last_good_tests_and_build:abc123+TW.314` is a combination
of the other two, i.e. a retry of the last good build (`BUILD=TW.314`) with the
last good test code (`CASEDIR=orginal_git_url#abc123`).

With the results of all those tests it can be seen quickly whether something is
an issue with the new build or the changes in the test code or something else.

As an example, if the `retry` fails, `last_good_tests` fails, but
`last_good_build` and `last_good_tests_and_build` pass, it's likely an issue
with the product.

### Configuration

`openqa-label-known-issues-and-investigate-hook` recognizes the following
environment variables:
* `notification_address` - If set, unreviewed issues will be sent to this address
   unless a job group has an address configured
* `from_email` - The From address for notification emails
* `force_result` - If set to `1` tickets in the [tracker openqa-force-result](https://progress.opensuse.org/projects/openqav3/issues?query_id=700) can override job results

## Auto submission scripts for `devel:openQA`

There are three scripts/pipelines responsible for managing submissions in OBS:
* Trigger: `trigger-openqa_in_openqa`
* Monitor: `monitor-openqa_job`
* Submit: `os-autoinst-obs-auto-submit` / `cleanup-obs-project`

They are called in [Jenkins](http://jenkins.qa.suse.de/)

There are three projects in OBS:
* [devel:openQA](https://build.opensuse.org/project/show/devel:openQA)
* [devel:openQA:testing](https://build.opensuse.org/project/show/devel:openQA:testing)
* [devel:openQA:tested](https://build.opensuse.org/project/show/devel:openQA:tested)

[devel:openQA](https://build.opensuse.org/project/show/devel:openQA) is the
project where the packages are automatically updated with every commit in git.

[devel:openQA:tested](https://build.opensuse.org/project/show/devel:openQA:tested)
contains the devel projects for `openSUSE:Factory`. They should always be
present and can be created like this:

```
osc linkpac openSUSE:Factory openQA devel:openQA:tested
osc changedevelrequest openSUSE:Factory openQA devel:openQA:tested openQA -m "assign a devel project"
osc linktobranch devel:openQA:tested openQA
```

### `trigger-openqa_in_openqa`

This script is called first. Its primary purpose is to regularly schedule
[`os-autoinst-distri-openqa` tests](https://openqa.opensuse.org/group_overview/24)
using `devel:openQA`. The monitor and autosubmit pipeline will be called
but having no effect.

Only if there are no packages in `devel:openQA:testing`, it will `osc release`
all packages present in `devel:openQA:tested` from `devel:openQA` to
`devel:openQA:testing`.

Then it triggers `os-autoinst-distri-openqa` tests using `devel:openQA:testing`,
logging to `job_post_response`.

### `monitor-openqa_job`

This monitors `job_post_response` until all jobs are finished. If all jobs
passed, the auto submit pipeline will be run.

Otherwise, it will delete packages from `devel:openQA:testing` again and
return an error.

### `os-autoinst-obs-auto-submit`

This script will copy contents from each `devel:openQA` package to
`devel:openQA:tested`, generate the changelog, and
submit the packages to `openSUSE:Factory` and potentially other target
projects.

In order to give reviewers time to review existing submit requests it will not
create new ones in a certain timeframe.

After creating submit requests, it calls `cleanup-obs-project` to delete all
packages in `devel:openQA:testing`.

## Contribute

This project lives in https://github.com/os-autoinst/os-autoinst-scripts

Feel free to add issues in github or send pull requests.

### Rules for commits

* For git commit messages use the rules stated on
  [How to Write a Git Commit Message](http://chris.beams.io/posts/git-commit/) as
  a reference

If this is too much hassle for you feel free to provide incomplete pull
requests for consideration or create an issue with a code change proposal.

### Local testing

#### Functional testing

This is done with the [test-tap-bash
library](https://github.com/bpan-org/test-tap-bash).
It will be automatically cloned.


    # only a few functions from openqa-label-known-issues so far
    make test

#### Style checks

    make checkstyle

Run

    make shfmt

to automatically format shell files.

#### openqa-label-known-issues
Generate a list of recent incomplete jobs of your local openQA instance. Here's an example using `psql`:

```
psql \
 --dbname=openqa-local \
 --command="select concat('http://localhost:9526/tests/', id, ' ', TEST) from jobs where result = 'incomplete' order by t_finished desc limit 10;" \
 --csv \
 | tail -n +2 > tests/local_incompletes
```

Perform a dry run on the local instance using the generated job list:
```
cat tests/local_incompletes | env scheme=http host=localhost:9526 dry_run=1 sh -ex ./openqa-label-known-issues-multi
```

## License

This project is licensed under the MIT license, see LICENSE file for details.
