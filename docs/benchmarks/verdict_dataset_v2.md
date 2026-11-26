# Audited, ticket-diverse verdict dataset (v2)

Built 2026-11-26 for `tasks/022_poo210528_verdict_label_reaudit.md` (Task 1.4). Replaces the
50-row / 7-ticket oracle in `tests/data/eval_dataset.json`, which derives every label from the tracker
a bug reference lives in. The v2 rows carry the class the written policy
(`docs/research/verdict_label_policy.md`) gives the referenced ticket, its confidence, and the
rationale the audit tool recorded.

## How it is built

```sh
# corpus rows: one row per referenced job, capped per ticket, no per-job requests
python3 scripts/benchmarking/fetch_eval_dataset.py --limit 5000 --groups <all o3 group ids> \
  --since 2026-05-26 --until 2026-11-26 --max-per-ticket 5 --corpus-output /tmp/corpus_v2.json

# ticket audit and v2 dataset
python3 scripts/benchmarking/audit_ticket_labels.py --from-dataset /tmp/corpus_v2.json \
  --annotate-dataset /tmp/corpus_v2.json --output tests/data/eval_dataset_v2.json \
  --force-unclear boo#1289464 --force-unclear poo#209751
```

The corpus tool lives in the o3-config checkout (`scripts/benchmarking/fetch_eval_dataset.py`), the
ticket audit in this repository (`scripts/benchmarking/audit_ticket_labels.py`). `--force-unclear` is
the policy's contradiction rule: the two `esc` tickets describe one symptom with opposite classes and
stay unscored until a human rules. Ticket answers are cached under
`~/.cache/o3-config/audit_ticket_labels/`, so a re-run needs `--refresh` to query the trackers again.

## Corpus

`tests/list_ajax?comment=LLM` answers only the 5000 newest matches and takes no date filter, so the
corpus was assembled from one query per o3 job group (112 groups) and filtered on `finished`:

| Measure | Value |
|---|---|
| LLM-commented jobs in the 6-month window | 35878 |
| Jobs carrying a bug reference | 616 (1.7%) |
| Distinct references | 195 |
| Rows after "first reference per job" and `--max-per-ticket 5` | 409 |
| Tickets in the dataset | 191 |

A job with several references contributes to the first one only, and the cap keeps at most five rows per
ticket, so the dataset covers every reference the corpus offered and spends at most five rows on it.
Four busy groups (1, 3, 38, 132) hit the server's row cap before the window start, so their oldest rows
are missing; see `docs/benchmarks/llm_corpus_inventory.md` in the o3-config checkout.

## Classes and confidence

Labels are the audit tool's proposals from the ticket's summary, description and first three comments,
plus its state. Scored rows only:

| Label | Rows | Tickets |
|---|---|---|
| `test` | 109 | 46 |
| `product` | 81 | 34 |
| `infra` | 7 | 2 |
| **Scored total** | **197** | **82** |

By confidence: 102 rows `certain`, 95 `plausible`. All-`product` ceiling of the scored rows:
**41.1% (81/197)** — lower than the v1 ceiling of 64.0%, because the `test` class now dominates.

Scored rows by tracker: `poo` 130, `boo` 64, `bsc` 3. The tracker no longer decides the class: 59 of the
82 scored tickets carry a `poo` reference, and the `poo` tickets labelled `product` are the openQA jobs
whose product under test is openQA itself.

## Unscored rows

212 of the 409 rows are `unclear` and stay in the file with their rationale; the scoring tool must skip
them (`scored: false`). Their reasons:

| Reason | Rows |
|---|---|
| Ticket content names neither a product nor a test defect | 111 |
| GitHub issue, while the audit reads Bugzilla and Redmine only | 40 |
| Closed `RESOLVED/INVALID` without a conclusion | 12 |
| The ticket leaves the cause open ("needs to be investigated") | 10 |
| Reference is not a ticket (`kde#…`, `bootloader_uefi#…`) | 10 |
| Closed `Rejected` without a conclusion | 10 |
| Contradiction pending a human ruling (`boo#1289464`, `poo#209751`) | 7 |
| Closed `RESOLVED/MOVED` (a redirect to another ticket) | 5 |
| Ticket could not be read (unknown ids, one `bsc` 404) | 4 |
| Closed `RESOLVED/WONTFIX` without a conclusion | 3 |

The dominant reason is the honest one: the majority of referenced tickets say only "openQA test fails in
`<module>`" plus the job link, which names a symptom and not the class. 109 of the 191 tickets therefore
do not decide their label, and the dataset's scorable size is 82 tickets, not 191.

## Per-ticket rows

The 12 tickets that contribute five scored rows each:

| Ticket | Rows | Label | Confidence | State | Rationale |
|---|---|---|---|---|---|
| `boo#1170723` | 5 | product | certain | NEW | names a product defect: crash |
| `boo#1230849` | 5 | product | certain | NEW | names a product defect: Failed to load |
| `boo#1246013` | 5 | product | certain | IN_PROGRESS | names a product defect: incomplete |
| `boo#1263652` | 5 | product | certain | RESOLVED/FIXED | names a product defect: crash |
| `boo#1283446` | 5 | product | certain | NEW | names a product defect: conflicts with |
| `boo#1288770` | 5 | product | certain | NEW | names a product defect: fails to |
| `poo#199868` | 5 | test | certain | In Progress | names test code or test data: `opensuse/lib/utils.pm` |
| `poo#202368` | 5 | test | plausible | Resolved | names test code or test data: `os-autoinst-distri` |
| `poo#203406` | 5 | test | plausible | New | names test code or test data: `os-autoinst-distri` |
| `poo#205077` | 5 | test | plausible | Feedback | names test code or test data: `os-autoinst-distri` |
| `poo#208527` | 5 | infra | plausible | Resolved | names only an infrastructure fault: worker |
| `poo#208620` | 5 | test | plausible | New | names test code or test data: needle |

40 of the 82 scored tickets contribute exactly one row, so a row-level percentage still overstates the
independent sample: the unit for any interval is the ticket.

## What the audit does to the v1 rows

Of the 50 v1 rows (7 tickets), 25 keep their tracker class under the policy and 25 change:

| v1 ticket | Rows | Tracker class | Policy outcome |
|---|---|---|---|
| `poo#209382` | 13 | test | test, certain |
| `boo#1246013` | 5 | product | product, certain |
| `poo#209802` | 4 | test | test, plausible |
| `boo#1288617` | 3 | product | product, certain |
| `boo#1289464` | 21 | product | **unclear** (contradiction with `poo#209751`) |
| `boo#1289038` | 3 | product | **unclear** (`RESOLVED/WONTFIX`, no conclusion) |
| `poo#209751` | 1 | test | **unclear** (contradiction with `boo#1289464`) |

H1 ("at most half of the current rows keep their class") therefore holds at the boundary: 25 of 50 rows
keep it, 50%. It is fragile, because the outcome turns on the `esc` ruling: if a human rules that pair
`test` — the policy's product-behaviour-change rule points that way — 26 of 50 rows keep their class
(52%) and H1 is falsified by one row. Either way the tracker mapping is not a label, and the v1
comparison that rested on the 21-row ticket is not defensible.

## Row schema

Each entry carries `job_id`, `test_name`, `reference`, `ticket_state`, `label`, `confidence`,
`rationale` and `scored`. The `metadata` block records the row and ticket counts, the label and
confidence counts, the all-`product` ceiling, the cap, the forced-unclear references and the per-ticket
row counts.
## Reproducibility

H4 asks whether a second reading of the same evidence reaches the same class. Measured on deterministic
20-row samples (`random.Random(<seed>).sample` over the rows sorted by job id): the first pass writes a
blinded file with the ticket's summary, description and first three comments, without the recorded
label, and the second reading is fixed before the comparison runs.

| Pass | Sample seed | Tool state | Agreement |
|---|---|---|---|
| 1 | 20261126 | initial heuristic table | 14/20 |
| 2 | 20261128 | after the corrections below | 13/20 |
| 2b | 20261128 | post hoc, same sample, final rules | 19/20 |
| 3 | 20261129 | final rules, untouched sample | 16/20 |

Pass 3 is the only clean post-correction measurement: 16/20, i.e. 20% of the classes change against a
10% bar. **H4 is falsified.** Passes 1 and 2 were read blind; for pass 1 five rows had had their label
printed earlier in the working session, so its 14/20 is an upper bound on the disagreement.

The disagreements are one-sided:

| Pass | Ticket | Tool | Second reading | Resolution |
|---|---|---|---|---|
| 1 | `poo#202767` | product | unclear | tool over-read "fails to"; the ticket says the cause "needs to be investigated" |
| 1 | `poo#205569` | unclear | test | "this test module needs work" missed by the adaptation rule |
| 1 | `poo#203088` | unclear | test | stale test data after grafana 11 → 12; rule 3 applies, the table does not see it |
| 1 | `boo#1272188` | unclear | product | "terminates on signal 11" not a product phrase in the table |
| 1 | `poo#209454` | unclear | test | "yast is no longer to be preinstalled" missed |
| 2 | `poo#203226` | test | unclear | a `*.yaml` path in the log counted as test code although it is a product path |
| 2 | `poo#210111` | unclear | product | kernel regression phrasing ("helper failed") missed |
| 2 | `poo#210456` | unclear | test | "no longer to detect the expected strings" missed |
| 2 | `boo#1276489` | unclear | product | "requirement cannot be provided" missed |
| 3 | `poo#209943` | product | test | "broken" in prose; the ticket says the failure is sporadic and upstream calls it not a kernel issue |
| 3 | `bsc#1234398` | unclear | product | "TimeoutSec=5s" hardcoded in a product unit; the table has no phrase for it |
| 3 | `poo#187110` | unclear | test | an upstream xfstests update fixes the test; not visible in the table |
| 3 | `boo#1271391` | unclear | product | SELinux `allow` rules from `audit2allow`, not denials |

The corrections the first two passes forced, all of them general rules rather than sample exceptions:

- test evidence is a source file (`.pm`, `.py`) or a `schedule/`/`data/` path, never a bare `.yaml` path
  taken from a log;
- the adaptation rule covers "no longer supported / available / preinstalled / to detect" and "module
  needs work";
- a ticket that says the cause needs investigation is `unclear` before any class rule fires;
- the product rule knows `signal N`, `helper failed`, `cannot be provided`, `failed to load`,
  `audit denied` and `conflicts with`;
- `RESOLVED/MOVED` is a redirect, so the row is unscored like a duplicate.

What remains is not a policy disagreement but a detection limit: three of the four remaining
disagreements are the tool answering `unclear` where a reader names a class, which costs sample size but
never scores a wrong class, and one is the tool asserting `product` on a flaky-test ticket, which does.
The written policy was stable across all three passes: no disagreement was about which rule applies, only
about whether the table noticed the evidence.

Consequence for plan 022: the labels are the tool's proposals with their confidence, not a human's
verdict, and H4's bar is not met. A decision that hangs on a 12-point difference therefore needs the
second instrument the plan names — a human spot-check of the disputed class — rather than this dataset
alone.
