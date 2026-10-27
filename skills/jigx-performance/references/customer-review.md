# Customer Review

The end-to-end flow for `/jigx-performance review <solution path>`: measure the
solution as it is, propose changes by section, apply only what the user approves,
measure again and report what improved. Everything lives in `perf-review/` in the
solution project.

## 0. Before starting

Confirm with the user, once:

- the solution folder and that it builds (`build/output.json`)
- the **test** solution to publish to, and the app to run it in. Never publish to or
  time against production or a working user's phone
- the journey to time: fresh sign-in and initial sync, a refresh, or opening a screen
- realistic test data: a load that is fast because the test user has ten rows proves
  nothing

Work on a branch in the customer repo. Commit nothing until the user approves changes.

## 1. Baseline

```sh
mkdir -p perf-review/before
node <skill-directory>/scripts/check-performance.mjs build/output.json > perf-review/before/static.json
python3 <skill-directory>/scripts/check-plans.py build/output.json > perf-review/before/plans.json
```

Publish the current build to the test solution (`jigx_debug` `create_debug_session`) and
record three loads with `track-load.py --out perf-review/runs --label pre-1` (then
`pre-2`, `pre-3`). Arm the tracker, then start the journey: drive the simulator if a UI
tool is available, otherwise ask the user to sign in or refresh while it runs. If a load
cannot be recorded, say so and continue with the static baseline.

## 2. Overview and proposal

Run `review-report.py perf-review --solution <name>` for the baseline overview. Read the
references for each section and review what the scripts cannot see: state, expressions,
screen size, start-up, sync shape, writes, credentials. Then present:

1. **Overview**: the time to ready and the slowest sync steps from the baseline runs,
   and the findings table by section.
2. **Proposed changes, grouped by section A–H** (`recipe-index.md`). For each change:
   what and where (file and line), why (finding and measured cost), expected effect,
   risk, and how it will be verified. Order sections by measured cost, largest first.
3. **Not proposed**: findings left alone and why (unreachable path, needs the API owner,
   needs a product decision).

Then stop and ask which sections or changes to apply. Do not change any file before the
user agrees. Approval for one section does not cover another.

## 3. Apply

For each approved section, in order:

- make the changes with the section's reference, one commit per section
- keep every index or rewrite equivalence-checked (`verifying.md`)
- add a `check-plans.py` guard for each per-row or per-change lookup you fixed
- append each change to `perf-review/changes.json` as
  `{"section": "B", "change": "Index orders by reference", "files": ["actions/create-indexes.jigx"]}`

Rebuild and run the `jigx` runtime checker after each section. Stop and report if it
fails.

## 4. Measure again

```sh
mkdir -p perf-review/after
node <skill-directory>/scripts/check-performance.mjs build/output.json > perf-review/after/static.json
python3 <skill-directory>/scripts/check-plans.py build/output.json > perf-review/after/plans.json
```

Publish to the same test solution, record `post-1..3` with the same journey, data and
simulator, then:

```sh
python3 <skill-directory>/scripts/compare-runs.py perf-review/runs --before pre --after post
python3 <skill-directory>/scripts/review-report.py perf-review --solution <name>
```

Check final row counts match between pre and post runs; a faster load that fetched
less is a regression.

## 5. Report

Show the user `perf-review/report.md`: time to ready before and after, the steps that
got faster or slower, snapshot timeouts and errors, findings per section before and
after, the changes applied, and what was not verified. Offer it as a page for the
customer. Say plainly when a step was not run or a number got worse.
