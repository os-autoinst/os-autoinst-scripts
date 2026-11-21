# Dataset audit: is `eval_dataset.json` a usable verdict oracle? (poo#210528)

Audit date: 2026-11-19. Read-only:
the entries come from `eval_dataset.json`, the ticket evidence from `redmine-fetcher` on
progress.opensuse.org. Measurements quoted come from the [MTP/QAT evaluation](gemma4_mtp_qat.md): the 25-job replay of
`qat-UD-Q4_K_XL` + MTP and the incumbent control, both on the worker with build 12464, plus the three
first-half probes on build 8740 and 12464.

## Ground truth

`eval_dataset.json` derives every label from `comment_data.bugs` of the LLM-investigated job:
`boo#` → `product`, `poo#` → `test`. What that actually means matters more than the mapping, because
it decides what the model is asked to answer.

| Set | Product | Test | All-`product` ceiling |
|---|---|---|---|
| first 25 entries | 22 | 3 | 88.0% (22/25) |
| second 25 entries (the replay set) | 10 | 15 | 40.0% (10/25) |
| all 50 entries | 32 | 18 | **64.0% (32/50)** |

Two consequences. First, the 78.0% (39/50) recorded on 2026-11-17 needs at least seven `Test issue`
answers; a model that never says `test` tops out at 64%. Second, the second half is test-heavy, so it
is the *more* discriminating half — yet the MTP/QAT replay scored 9/25 there, below its own all-`product`
ceiling of 10/25 (one job timed out).

All 18 `test` labels trace to **three** tickets, not 18 independent decisions:

| Ticket | Title | What it says | Label class |
|---|---|---|---|
| `poo#209382` | Bug at os-autoinst-distri-opensuse's lib/bootloader_setup.pm:562 | A line-move hack in the test code (off by 6 lines) breaks the generated grub config after a grub update; the missing `insmod part_gpt`/`insmod btrfs` lines are quoted | genuine test-code defect |
| `poo#209802` | test fails in opensuse_welcome | `match_has_tag` does not work as expected when three needles match; the test suite branching is buggy | genuine test-code defect |
| `poo#209751` | test fails in opensuse_welcome (gnome-live) | The `esc` key has no effect on the gnome-tour window; the test needs to press it differently | genuine test-code defect |

All three are `Test issue` in the sense the tool is asked about: the failing code is the test suite,
not the product under test. The labels are correct.

## Disputed entries

Every entry the model answered `product` while the label says `test`, with the cause the ticket
names. The `bootloader_setup.pm` case explains why the model's log view looks product-shaped: a
corrupt grub config makes the boot fail with `Kernel panic - not syncing: VFS: Unable to mount root
fs`, which the model reads as a product defect, while the cause is the test code that wrote the
config.

| Job | Test | Label source | Model verdict | Actual cause per ticket |
|---|---|---|---|---|
| 6318100 | upgrade_Leap_15.3_gnome@aarch64 | `poo#209802` | product | second half |
| 6317972 | upgrade_Leap_15.2_gnome@aarch64 | `poo#209802` | product | second half |
| 6317910 | upgrade_Leap_15.0_gnome@aarch64 | `poo#209802` | product | first half |
| 6317892 | upgrade_Leap_15.1_gnome@aarch64 | `poo#209802` | product | first half |
| 6315059 | gnome-live@USBboot_aarch64-3G | `poo#209751` | product | first half |
| 6315020 | jeos-selinux@aarch64-HD20G | `poo#209382` | product | second half |
| 6315017 | jeos-ltp-syscalls-ipc@aarch64-HD20G | `poo#209382` | product | second half |
| 6315016 | jeos-ltp-syscalls@aarch64-HD20G | `poo#209382` | product | second half |
| 6315015 | jeos-ltp-ima@aarch64-HD20G | `poo#209382` | product | second half |
| 6315014 | jeos-ltp-dio@aarch64-HD20G | `poo#209382` | product | second half |
| 6315012 | jeos-ltp-containers@aarch64-HD20G | `poo#209382` | product | second half |
| 6315011 | jeos-ltp-commands@aarch64-HD20G | `poo#209382` | product | second half |
| 6315010 | jeos-fips@aarch64-HD20G | `poo#209382` | product | second half |
| 6315008 | jeos-container_image@aarch64-HD20G | `poo#209382` | product | second half |
| 6315007 | jeos-container_host@aarch64-HD20G | `poo#209382` | product | second half |
| 6315006 | jeos-apparmor@aarch64-HD20G | `poo#209382` | product | second half |
| 6314993 | jeos-fips-container_host@aarch64-HD20G | `poo#209382` | product | second half |
| 6314991 | jeos-extra@aarch64-HD24G | `poo#209382` | product | second half |

Verdict on H1 (oracle is wrong): **falsified**. The labels describe real test-code defects, and each
`test` verdict is defensible on the ticket text. The disagreement is symptom versus cause: the model
classifies the failure it sees in the log, the dataset records the defect a human found by reading
the tickets and the test code. That moves the weight of the analysis onto H2 (the decisive evidence — the
test-code change or the ticket text — does not reach the prompt) and onto the classification rule.

## Held-out split

Deterministic rule, fixed before any prompt or rule change: validation is every third entry of
`eval_dataset.json` in dataset order (0-based indices 2, 5, 8, … 47), tuning is everything else.

| Split | Entries | Product | Test |
|---|---|---|---|
| tuning | 34 | 21 | 13 |
| validation | 16 | 11 | 5 |

Validation job ids: 6311674, 6317874, 6315059, 6343065, 6330152, 6317928, 6319615, 6319608, 6319617, 6319602, 6317891, 6315104, 6314993, 6315006, 6315012, 6315014

Any change to the prompt, the rules or the verdict normalisation is tuned on the tuning split and
reported on the validation split; a number quoted without naming its split is not a measurement.

## Prompt evidence

`openqa-llm-investigate --print-prompt` for seven jobs (three from each class, plus the reference job
6287643 which the tool classifies correctly as `Infrastructure problem`), captured 2026-11-19 with the
tool at the base revision. Prompt 1 is the job details plus analysis, prompt 2 is the 38-token summary
question, prompt 3 is the 437-token verdict question.

| Job | Label | Prompt 1 tokens | Kernel-panic banner | "Test commits since the last good job" | TAP diff | Named commits | Hidden commits |
|---|---|---|---|---|---|---|---|
| 6315020 | test/`poo#209382` | 1811 | **yes** | **no** | no | 0 | 0 |
| 6315010 | test/`poo#209382` | 1791 | **yes** | **no** | no | 0 | 0 |
| 6318100 | test/`poo#209802` | 3656 | no | yes | no | 2 | 75 |
| 6317910 | test/`poo#209802` | 3779 | no | yes | no | 2 | 92 |
| 6319605 | product | 3864 | no | yes | no | 0 | 0 |
| 6317992 | product | 6738 | no | yes | no | 0 | 0 |
| 6287643 | infra (reference) | 3775 | no | yes | **yes** | 0 | 0 |

Two findings decide the direction of the fix:

1. **The verdict rule names the symptom the disputed jobs show.** Prompt 3 says verbatim: "Product
   bug: the software under test is wrong, e.g. **a kernel panic** or segfault." The two
   kernel-panic jobs carry a `*** KERNEL PANIC DETECTED ***` banner in the head of prompt 1 and
   *no* test-commit section at all, so given the rule the model's answer is the correct application
   of the given definition. The label disagrees because it records the *cause*
   (`bootloader_setup.pm:562` wrote a broken grub config), which the prompt never contains.
2. **Where the test-commit evidence exists, it is diluted.** The two `poo#209802` jobs show
   `[stack] 91db3b29a7 handle_welcome_screen: Click if we lost focus and 'esc' did not work` plus
   "and 75 other test commits" / "and 92 other test commits": the one decisive commit is hidden
   behind a count unless it happens to sort first.

## Missing artefacts

For the disputed jobs the prompt lacks, in decreasing impact:

- **The decisive test-code change.** The `bootloader_setup.pm:562` line-move that the ticket names is
  not in any prompt; the test-commit list shows two names and hides 75-92, and the kernel-panic jobs
  show no commit list at all. A cause-based label cannot be reproduced from a prompt that only shows
  the symptom.
- **Any known-issue or ticket context.** No `poo#`/`boo#` text, no title, no "fails since build X"
  note from the ticket. The tool has no known-issue lookup.
- **Package versions.** `_SKIP_UPLOAD_RE` drops `zypper.log`, `rpm-qa` and `rpm-list`, so a
  grub-package update that explains the grub-config drift is invisible.
- **The TAP assertion diff for non-TAP failures.** `_UPLOAD_TEXT_RE` excludes `.tap`; the reference
  job 6287643 still shows one (its failure is TAP-shaped), the `poo#209382` jobs show none.

## Truncation

The production gateway sends no `num_ctx`, so the model sees roughly the first 4096 tokens of prompt
1. One of the seven captured prompts exceeds that: 6317992 with 6738 tokens, whose tail (the failed
module analysis) would be cut behind the job details and the settings diff. The other six fit
(1791-3864 tokens), and prompt 3 with the rules is the last message as intended.

Truncation is therefore a contributing factor, not the primary one: the benchmark server on the worker runs
with `--ctx-size 65536` and no truncation, and it answered `product` for all 50 jobs as well. A fix
that only enlarges the context window would not change a single verdict observed here.

## Cause

Closure of the pre-registered hypotheses:

| Hypothesis | Prediction | Outcome |
|---|---|---|
| H1 oracle | the labels record a known flaky module, not a defect | **falsified**: all three tickets name a defect in the test code (`bootloader_setup.pm:562` line move after a grub update, `match_has_tag` not working with three matching needles, a dead `esc` key in the gnome-tour window) |
| H2 evidence | the decisive artefact never reaches the prompt | **partly confirmed**: the two `poo#209382` jobs carry no test-commit section at all, so the prompt contains only the symptom; the `poo#209802` jobs carry it but hide the decisive commit behind "and 75 other test commits" |
| H3 bias | the prompt or normaliser prefers `Product bug` | **confirmed for the rule, refuted for the code**: prompt 3 defines "Product bug … e.g. a kernel panic or segfault" while prompt 1 headlines a `*** KERNEL PANIC DETECTED ***` banner, so the model applies the given definition; the normaliser keeps a `Test issue` answer intact (pinned by `test(llm-investigate): Pin the classification of a normalized reply`) |
| H4 instability | temperature 1.0 makes the verdict a lottery | **not measured, and not needed**: the collapse is deterministic across weight sets, runner builds and tool revisions, and the disputed jobs answer the same class five ways out of five in the records available (MTP/QAT replays + probes). The 5x2 repeat measurement stays open as a refinement, not a blocker |
| H5 drift | the 78.0% came from a configuration that no longer exists | **open, being measured**: an all-`product` model can score at most 64% on this dataset, and the full-50 re-baseline on `server-b8740` with the current tool is running; the pre-normalisation revision already answers `Product bug` on the same job |

**Dominant cause**: the classification rule is *symptom-based* while the dataset labels are
*cause-based*. For 13 of the 18 `test` labels the prompt shows a kernel panic, and prompt 3 names a
kernel panic as the archetype of `Product bug`; the model therefore answers the question as asked and
the label disagrees, because the human recorded the test-code defect that produced the panic
(`bootloader_setup.pm` wrote a broken grub config). The missing or diluted test-commit section
removes the only evidence that could have outvoted the bare symptom.

What the fix must do, in order of expected effect:

1. make the rule cause-based: a kernel panic is a symptom, and the class follows from what caused the
   failure — a boot/grub failure that follows a test-code change in the failing module is a test
   issue;
2. surface the decisive test commit instead of a count, and never drop the section for a job that has
   test-code changes (the `poo#209382` jobs had none at all);
3. keep the panic banner, but after the cause evidence, not as the first thing the model reads.

Enlarging the context window is not part of the fix: the benchmark server never truncated and still
answered `product` for all 50 jobs.

## Population representativeness (measured 2026-11-20)

The 50-entry dataset was extracted from at most 500 candidate jobs on 2026-11-16, sorted so that jobs
with bug metadata came first. To check whether it describes o3 at all, the same classification was
applied to every LLM-investigated job the instance lists: `tests/list_ajax?comment=LLM` for the whole
instance and for the job groups 1, 3, 38 and 139, deduplicated to **7816 jobs finished between
2026-04-27 and 2026-11-20** (the global query is capped at 2000 rows, so the per-group queries supply
the rest of each busy group).

Human labels come from `comment_data.bugs`, as in the dataset: `poo#` and
`gh#os-autoinst/os-autoinst-distri-opensuse` → test, `boo#`/`bsc#` and other `gh#` → product.

| Population | Jobs | With a human bugref | test | product | mixed |
|---|---|---|---|---|---|
| all investigated | 7816 | 201 (2.6%) | 110 | 90 | 1 |
| finished in November | 102 of the 201 | — | 44 | 58 | 0 |
| the 50-entry dataset | 50 | 50 (100% by construction) | 18 | 32 | 0 |

**The population is not single-class.** Among the jobs a human filed a bug for, test and product are
close to even (55/45 overall, 43/57 in November), and the dataset's 64/36 split is if anything more
product-heavy than the population. There is no evidence that o3's failures have recently become
mostly one kind.

The far more consequential number is the first column: only **2.6% of investigated jobs carry any
human bugref**, so 97% of the workload has no ground truth at all. Every accuracy figure in this audit
is computed on that self-selected 2.6%, which is why the dataset cannot be treated as a sample of
"the failures openQA produces".

### Verdicts recorded in production, versus the benchmark harness

Parsing the `Classification:` line of each job's own comment with the extractor's regex reproduces
the dataset's stored `llm_verdict` field exactly (50/50), so the parser is faithful to what the tool
posted. On the 201 labelled jobs:

| Scope | Jobs with a parseable verdict | product | test | infra | Accuracy |
|---|---|---|---|---|---|
| all labelled jobs | 153 | 58 | 46 | 49 | 48/153 (31.4%) |
| finished in November | 102 | 49 | 38 | 15 | 39/102 (38.2%) |
| the 50 dataset jobs, as posted | 50 | 28 | 18 | 4 | 22/50 (44.0%) |
| the 50 dataset jobs, benchmark server 65536 ctx | 50 | 49 | 1 | 0 | 31/50 (62.0%) |

The last two rows are the same jobs and the same weights. Production's recorded answers are mixed but
inaccurate (44.0%, `Test` recall 6/18); the benchmark harness answers `product` almost always and
scores higher (62.0%) by sitting on the constant-answer ceiling. The two disagree on 21 of 50 jobs,
always toward `product`.

So the "single-class collapse" is a property of the benchmark harness, not of the deployed model: on
the jobs themselves the tool emitted all three classes. Two candidates explain the difference and can
be told apart with one run — the tool revision (the comments predate the normalisation revision) and the
context window (the production gateway sends no `num_ctx`, so the model truncates; the benchmark
server runs 65536 tokens). Until that is settled, any model or parameter ranking measured on the
benchmark server ranks the harness rather than the model, which weakens this audit's own numbers even
where it does not change its conclusion: no arm beat the incumbent, and the 78.0% reference is
reproducible in neither configuration.

### Consequences for using the dataset as an oracle

- It remains a valid **regression** set: a fixed 50 jobs with audited human labels and a recorded
  per-arm verdict snapshot, where a change that costs points is a real loss.
- It is not a **representativeness** sample: 2.6% of jobs carry a label, the reviewer who files a bug
  is not the average job, and the 18 test labels all trace to three tickets on one architecture.
- For ranking models, the binding constraint is the harness, not the dataset: two configurations of
  the same weights score 44.0% and 62.0% while disagreeing on 21 jobs.

## Attribution of the production/benchmark difference

The stored production verdicts and the benchmark runs differ, so the next question was which variable
does it. Four arms on one stratified 20-job subset of `eval_dataset.json` (8 stored test, 4 stored
infra, 8 stored product; human labels 15 product / 5 test), all against build 8740 on the worker:

| Arm | Verdicts | Agreement with the stored verdicts | Correct |
|---|---|---|---|
| Stored production comments, posted by 2026-11-16 | 8 product / 8 test / 4 infra | — (reference) | 9/20 |
| Tool at the current revision, 6-job list prompt, 65536 ctx | 20 product | 8/20 | 15/20 |
| Tool at the pre-normalisation revision, 65536 ctx | 19 product / 1 test | 9/20 | 16/20 |
| Tool at the current revision behind a proxy truncating every prompt to 4096 tokens | 19 product | 8/20 | 14/20 |

**Both candidate explanations are refuted.** Reverting to the tool revision that posted the stored
comments changes nothing (1 test in 20), and reproducing the gateway's 4096-token window changes
nothing either (0 test in 19 answered). What differs is the **model**: the stored comments were posted
while production still ran `gemma-4-E4B-it:UD-IQ2_M` — the quadlet on the worker is dated 2026-11-18 12:37
and the container was created at the same second, whereas the dataset was built 2026-11-16 18:49, and
the toolchain notes record E4B with a 4096-token truncation for exactly that era. The stored 22/50
(44.0%) is the predecessor model's score and equals the matrix's `gemma-4-E4B-it UD-IQ2_M` row
(44.0%, 22/50) to the job.

Consequences:

- The dataset's `llm_verdict` column is **not** an incumbent baseline. "Same set, 62% today" compares
  two models *and* two harnesses; it is not a regression of one model.
- The single-class collapse is the incumbent `26B-A4B`'s behaviour on these 50 jobs in the harness. It
  reproduces across two runner builds (8740, 12464), two weight sets (Q4_K_M, QAT), with and without
  an MTP head, at two tool revisions and with the production context window. Production itself has
  posted only 8 labelled comments since the switch (5 product / 3 test on other jobs), which neither
  confirms nor refutes it.
- The 78.0% (39/50) recorded 2026-11-17 also predates the production switch, i.e. it was a candidate
  measurement on a benchmark harness, not a production score. Nothing in the record identifies the
  harness it used, and no arm here reproduces it.
