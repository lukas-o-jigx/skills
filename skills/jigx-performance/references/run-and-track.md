# Run And Track

Prove a change made the app faster by running the real load before and after it, with
a live timeline. `scripts/track-load.py` records a run; `scripts/compare-runs.py`
compares runs. Both are read-only on the device database.

## What can be timed where

| What | How | Where |
| --- | --- | --- |
| Sync and load timeline: each scope and entry, queue size, row counts | `track-load.py`, sampling the solution database read-only every 0.25 s | iOS Simulator |
| Slow datasources and errors during the run | `track-load.py` tails the simulator's `jigx.log.db` for WARNING/ERROR lines such as `Timeout for first snapshot` | iOS Simulator |
| Same signals on a phone | `jigx_debug` `track_logs` before the run, read after | real device |
| Request start, finish and overlap per sync wave | a proxy capture (e.g. Proxyman HAR export) of the test solution only | simulator or device |
| Screen ready: tap to the first row visible | a UI flow (e.g. Maestro) that taps and waits for a stable accessibility id | simulator |
| One query's cost at 1x and 10x | `profile-query.py` on an exported database | host |

Jigx exposes no render timing. A screen's time is tap to its ready element, measured by
the UI flow, not by the runtime. A real phone's files are not reachable from the host
and `query_device_db` takes about 5 s per call, so a device gives logs and spot checks,
not a timeline.

## Run

1. **Publish to a test solution only.** Use `jigx_debug` `create_debug_session` with the
   test solution's path and target app; never time against production or a working
   user's phone. Same simulator, fixture data, network and build settings for every run.
2. **Find the database.** `python3 track-load.py --list` lists solution databases in the
   local simulators, newest first.
3. **Arm, then start the load.** Arm before logging in, refreshing or opening the screen:

   ```sh
   python3 <skill-directory>/scripts/track-load.py --solution <test-solution> \
     --label pre-1 --scopes <scope-a>,<scope-b> --table <main-table>
   ```

   Name the scopes that define "ready" (the essential tier); without `--scopes` every
   scope started after arming counts. It prints a line per change, stops when every
   tracked scope is synced and `_commandQueue` is empty for `--settle` seconds (or on a
   failed entry, or `--timeout`), then prints a waterfall of entries and writes
   `perf-runs/<label>-summary.json` and `-samples.jsonl`.
4. **Three runs per side.** `pre-1..3` on the old build, `post-1..3` on the new one, each
   from the same starting state (fresh login, or the same refresh).
5. **Compare.**

   ```sh
   python3 <skill-directory>/scripts/compare-runs.py perf-runs --before pre --after post
   ```

   It reports medians for first start to ready, each scope and each entry, flags any
   slowdown over 10% (a review threshold, not an SLA), and warns on fewer than three runs
   a side, runs that did not reach ready, or runs from different machines. It writes
   `comparison.md` for the PR.

## Reading the result

- **First start to ready** is the user's wait. Scope durations use the runtime's own
  `lastStartedAt`/`lastSyncedAt`; entry times have the sampling interval's resolution.
- **The waterfall shows waves.** Entries that should overlap but run one after another
  point at a sequential action; an entry that starts only after an unrelated one ends
  points at a needless dependency (`sync-orchestration.md` in the `jigx` skill).
- **A faster total with new `Timeout for first snapshot` lines or errors is not an
  improvement.** Compare the log signals as well as the times.
- **Check the data, not only the time.** Equal final row counts before and after; a load
  that is fast because it fetched less is a regression.

## Rules

- Record build, simulator, solution and data set with every run; `compare-runs.py`
  refuses to hide a machine change, but it cannot see a data change.
- Median of three or more, never one run. Report the spread when runs disagree widely.
- Simulator timings show relative change on that machine. Do not quote them as device
  timings; confirm a large win on the slowest supported device.
- Keep evidence sanitised: the scripts keep only scope and entry states, counts and
  redacted log lines. Do not add request bodies or headers to saved runs.
