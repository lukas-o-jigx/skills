#!/usr/bin/env python3
"""Write the before/after report for a /jigx-performance review.

Standard library only. Reads a review folder laid out as:

  <folder>/before/static.json   check-performance.mjs output before changes
  <folder>/before/plans.json    check-plans.py output before changes
  <folder>/after/static.json    the same after changes (optional until applied)
  <folder>/after/plans.json
  <folder>/runs/comparison.json compare-runs.py output (optional)
  <folder>/runs/pre*-summary.json, post*-summary.json   track-load.py runs: pre, pre-1, pre-clean-2 …
                                (pre-android-… is the Android side)
  <folder>/before/timings.json  time-queries.mjs output (optional; --timings BEFORE AFTER for other files)
  <folder>/after/timings.json   time-queries.mjs --baseline before/timings.json (optional)
  <folder>/screens/pre.json     time-screens.py --label pre (optional)
  <folder>/screens/post.json    time-screens.py --label post --compare pre (optional)
  <folder>/runs/comparison-android.json, screens/pre-android.json, post-android.json
                                the same on Android (optional)
  <folder>/changes.json         [{"section": "B", "change": "...", "files": [...]}] (optional)

and writes <folder>/report.md (and prints it): an overview of every area, then sync,
function (sync step), query, action and screen times
before and after, findings per section A-H before and after, the changes applied per
section, and what was not measured. With only the before files it writes the overview used for the proposal.
Runs that compare-runs.py left out with --exclude (recorded in comparison.json) are left out
here too, so both show the same medians; --exclude adds more. A query that is slower only at
the scaled data (time-queries.mjs --scale with --baseline) is listed with the queries.

  python3 review-report.py <review> --change "B: TRIM() indexes for the time-activities join" --files datasources/a.jigx

appends one change to changes.json (the section letter, then what changed) and exits.
Read references/customer-review.md.

Example:
  python3 review-report.py <review> --solution my-solution
"""
import argparse
import glob
import json
import os
import re
import statistics
import sys

SECTIONS = {
    "A": "Measure and track",
    "B": "Queries and indexes",
    "C": "Lists and rendering",
    "D": "State and expressions",
    "E": "Screens and start-up",
    "F": "Sync",
    "G": "Writes and duplicates",
    "H": "Security and logs",
}

# What each section means to the user; kept in step with SECTION_MEANING in check-performance.mjs.
SECTION_MEANING = {
    "A": "How the app is timed and compared; makes the numbers trustworthy, not the app faster.",
    "B": "How fast the app reads its local database: screens, lists and actions waiting for SQL.",
    "C": "How lists are built and drawn: lists that open late, scroll badly or redraw every row.",
    "D": "Work redone in expressions and state: screens that lag, or expressions that fail and blank a field.",
    "E": "Work done when the app starts or a screen opens: cold start and screen open.",
    "F": "How data comes from the server: sign-in, refresh and screens waiting for a sync.",
    "G": "Saving and sending records: duplicate or lost records, not speed.",
    "H": "Credentials, logging and errors: security risk or failures nobody sees.",
}
CODE_SECTION = {
    "unindexed-predicate": "B", "wrapped-predicate": "B", "execute-sql-entities": "B",
    "ddl-invalidates-datasources": "B", "index-not-idempotent": "B", "ddl-ungated": "B",
    "per-row-datasource-read": "C", "uncapped-search-list": "C",
    "datasource-not-equals": "D", "filter-inside-map": "D",
}


def read_path(path):
    return read(os.path.dirname(path) or ".", os.path.basename(path))


def read(folder, *parts):
    path = os.path.join(folder, *parts)
    if not os.path.exists(path) and parts[-1] == "static.json":
        path = os.path.join(folder, *parts[:-1], "audit.json")  # the name an older SKILL.md used
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        text = f.read()
    try:
        return json.loads(text) if text.strip() else None
    except ValueError:
        # A script that failed half way leaves an empty or partial file; report without it.
        print(f"Skipped {path}: not valid JSON (did the script that writes it fail?)", file=sys.stderr)
        return None


def findings(static, plans):
    """{section: {label: count}} from the static scan and the plan check."""
    out = {}
    if static and static.get("findings") is not None:
        for f in static["findings"]:
            section = out.setdefault(f.get("section") or CODE_SECTION.get(f["code"], "B"), {})
            section[f["code"]] = section.get(f["code"], 0) + 1
    elif static:
        for code, n in (static.get("byCode") or {}).items():
            out.setdefault(CODE_SECTION.get(code, "B"), {})[code] = n
    if plans:
        b = out.setdefault("B", {})
        b["queries with full scans"] = plans.get("queries_with_full_scans", 0)
        b["unused indexes"] = len(plans.get("indexes_unused", []))
        b["index DDL that fails"] = len(plans.get("indexes_failed", []))
    return out


def section_rows(before, after):
    rows = []
    for s in SECTIONS:
        labels = sorted(set(before.get(s, {})) | set(after.get(s, {}) if after else set()))
        for label in labels:
            b = before.get(s, {}).get(label, 0)
            a = after.get(s, {}).get(label, 0) if after is not None else None
            rows.append((s, label, b, a))
    return rows


def load_runs(folder, prefix, excluded=()):
    """track-load.py summaries labelled <prefix> or <prefix>-<anything> in <folder>/runs (pre-1, pre-clean-2),
    but not <prefix>-android-…, which is the Android side, nor a run whose label is in `excluded`."""
    label = re.compile(rf"{re.escape(prefix)}(?:-(?!android)[\w.]+(?:-[\w.]+)*)?-summary\.json")
    out = []
    for path in sorted(glob.glob(os.path.join(folder, "runs", f"{prefix}*-summary.json"))):
        if label.fullmatch(os.path.basename(path)):
            with open(path, encoding="utf-8") as f:
                run = json.load(f)
            if run.get("label") not in excluded:
                out.append(run)
    return out


def med(values):
    values = [v for v in values if v is not None]
    return round(statistics.median(values), 2) if values else None


def entry_medians(runs):
    names = {n for r in runs for n in r.get("entries", {})}
    return {n: med([r["entries"].get(n, {}).get("duration_s") for r in runs]) for n in names}


def function_lines(pre, post):
    """Each sync step runs one function (REST or OData call plus the write to the local table)."""
    if not pre:
        return ["Not measured. Run track-load.py for the sign-in sync (references/run-and-track.md)."]
    before, after = entry_medians(pre), entry_medians(post) if post else {}
    rows = sorted(((n, v) for n, v in before.items() if v is not None), key=lambda x: -x[1])[:12]
    lines = (["| Function (sync step) | Before | After |", "| --- | ---: | ---: |"] if post
             else ["| Function (sync step) | Median |", "| --- | ---: |"])
    for n, v in rows:
        lines.append(f"| {n} | {v} s | {after.get(n, '-')} s |" if post else f"| {n} | {v} s |")
    return lines + ["", f"Median of {len(pre)} run(s){f' before and {len(post)} after' if post else ''}; a step's time is "
                        "its call and the write of its rows, with the sampling interval's resolution. Calls outside sync "
                        "(create, update, submit) are timed as screen journeys: tap the button, ready = the confirmation."]


def timing_lines(comparison, pre=None):
    if comparison and (comparison.get("baseline_only") or comparison.get("before_runs") == comparison.get("after_runs")):
        # compare-runs.py checked the baseline alone (or it against itself): nothing was compared.
        notes = [f"- Warning: {w}" for w in comparison.get("warnings", [])]
        return (timing_lines(None, pre) if pre else ["Baseline only; no after runs were compared."]) + notes
    if not comparison and pre:
        ready = med([r.get("first_start_to_ready_s") for r in pre])
        timeouts = sum(sum(r.get("snapshot_timeouts", {}).values()) for r in pre)
        busy = med([r.get("sync_busy_s") for r in pre])
        starts = med([sum(r["scope_starts"].values()) for r in pre if "scope_starts" in r])
        repeated = sorted({sid for r in pre for sid in r.get("repeated_scopes", [])})
        starters = sorted({x for r in pre for xs in (r.get("repeat_starters") or {}).values() for x in xs})
        lines = [f"**Time to ready: {ready} s** (median of {len(pre)} run(s) on {pre[0].get('machine', '?')})", ""]
        if busy is not None:
            lines += [f"Sync work, also after ready: {busy} s in {starts} scope start(s)."
                      + (f" **{', '.join(repeated)} synced more than once in a run**: "
                         + (f"the source starts the sync from {', '.join(starters)}." if starters
                            else "look for an onFocus or onLoad starting the same sync again.") if repeated else ""), ""]
        return lines + [
                f"Timeout for first snapshot: {timeouts}. Runtime log errors: {sum(r.get('log_errors', 0) for r in pre)}. "
                f"Expression errors: {sum(e['count'] for r in pre for e in r.get('expression_errors', []))}. "
                f"Rows hashed for lack of an id: {sum(r.get('hash_fallbacks', 0) for r in pre)}."]
    if not comparison:
        return ["Not measured. Run track-load.py and compare-runs.py (references/run-and-track.md)."]
    rows = comparison["rows"]
    total = rows[0]
    pct = "-" if total["change_pct"] is None else f"{total['change_pct']}%"
    lines = [f"**Time to ready: {total['before_s']} s → {total['after_s']} s ({pct}, {total['verdict']})**", ""]
    for r in rows[1:3]:
        if r["metric"].startswith("total:") and r["before_s"] is not None:
            unit = "" if "count" in r["metric"] else " s"
            lines += [f"{r['metric'][7:].capitalize()}: {r['before_s']}{unit} → {r['after_s']}{unit} "
                      f"({'-' if r['change_pct'] is None else str(r['change_pct']) + '%'}, {r['verdict']})", ""]
    lines += [
             f"Medians of {len(comparison['before_runs'])} runs before and {len(comparison['after_runs'])} after "
             f"on {'; '.join(comparison['machine'])}.", ""]
    # Every step whose verdict says something; "same" and "within noise" are noise. The verdict
    # goes with it: a bare -89% hides "not comparable", "unclear" or "parallel now".
    moved = [r for r in rows[1:] if not r["metric"].startswith("total:") and r["change_pct"] is not None
             and r["verdict"] not in ("same", "within noise")]
    order = {"REGRESSION": 0, "improved": 1}
    moved.sort(key=lambda r: (order.get(r["verdict"], 2), r["change_pct"]))
    if moved:
        runs = lambda vs: ", ".join(f"{v:g}" for v in vs or []) or "-"
        lines += ["| Step | Before | After | Change | Verdict | Runs before → after |", "| --- | ---: | ---: | ---: | --- | --- |"]
        for r in moved[:12] + [r for r in moved[12:] if r["verdict"] == "REGRESSION"]:
            lines.append(f"| {r['metric']} | {r['before_s']} s | {r['after_s']} s | {r['change_pct']}% | {r['verdict']} | "
                         f"{runs(r.get('before_runs'))} → {runs(r.get('after_runs'))} |")
        lines += ["", "Steps that moved by less than the noise floor are left out (comparison.md lists every step).", ""]
    lines.append(f"Timeout for first snapshot: {comparison['snapshot_timeouts']['before']} → "
                 f"{comparison['snapshot_timeouts']['after']}. Runtime log errors: "
                 f"{comparison['log_errors']['before']} → {comparison['log_errors']['after']}."
                 + (f" Rows hashed for lack of an id: {comparison['hash_fallbacks']['before']} → {comparison['hash_fallbacks']['after']}."
                    if "hash_fallbacks" in comparison else "")
                 + (f" Expression errors: {comparison['expression_errors']['before']} → {comparison['expression_errors']['after']}."
                    if "expression_errors" in comparison else ""))
    for w in comparison.get("warnings", []):
        lines.append(f"- Warning: {w}")
    return lines


KIND = {"queries": "query", "actions": "execute-sql", "indexes": "index"}


def query_lines(before, after):
    if not before:
        return ["Not measured. Run time-queries.mjs on the test user's database (references/customer-review.md)."]
    total = lambda t, kind: round(sum(r.get("ms") or 0 for r in t.get(kind, [])), 1)
    lines = []
    for kind, label in (("queries", "Queries"), ("actions", "execute-sql steps"), ("indexes", "Index builds")):
        b = total(before, kind)
        lines.append(f"- {label}: {len(before.get(kind, []))}, {b} ms in total" + (f" → {total(after, kind)} ms" if after else ""))
    meta = before.get("meta", {})
    lines += ["", f"Host timings on {meta.get('host', 'the host')} against {meta.get('rows', '?')} rows; a phone is several times slower."]
    scale = (after or {}).get("meta", {}).get("scale") or meta.get("scale")
    blocks = [("", (after or {}).get("comparison", []))]
    if (after or {}).get("comparisonScaled"):
        blocks.append((f" at {scale}x data", after["comparisonScaled"]))
    rewritten = False
    for at, compared in blocks if after else []:
        moved = [r for r in compared if r["verdict"] != "same"]
        slower = sum(r["verdict"] == "SLOWER" for r in moved)
        lines.append(f"\n{'**' if slower else ''}Before and after{at}: {sum(r['verdict'] == 'faster' for r in moved)} faster, "
                     f"{slower} slower, {len(compared) - len(moved)} the same.{'**' if slower else ''}")
        rows = [r for r in moved if r["verdict"] == "SLOWER"] + [r for r in moved if r["verdict"] != "SLOWER"]
        if rows:
            lines += ["", "| What | Where | Before | After | Change | |", "| --- | --- | ---: | ---: | ---: | --- |"]
            for r in rows[:12]:
                note = r["verdict"]
                if r.get("rewritten") and r["verdict"] == "SLOWER":
                    note, rewritten = "SLOWER (query rewritten)", True
                lines.append(f"| {KIND[r['kind']]} | `{r['where']}` | {r['before_ms']} ms | {r['after_ms']} ms | {r['change_pct']}% | {note} |")
    if rewritten:
        lines += ["", "Query rewritten: its text changed between the two runs. When the new query does work that expressions "
                      "did per row before, the before time left that work out (expressions cannot be timed here), so the two "
                      "times are not like for like: judge the change by the screen time, and fix what the plan shows (a missing index)."]
    if after and (before.get("growth") or after.get("growth")) and not after.get("comparisonScaled"):
        lines += ["", "The scaled data was not compared before and after (run time-queries.mjs with the same --scale on both "
                      "sides and --baseline on the second): a regression that shows only with more data would be missed."]
    if not after:
        rows = sorted([dict(r, kind=k) for k in ("queries", "actions") for r in before.get(k, []) if r.get("ms") is not None],
                      key=lambda r: -r["ms"])[:8]
        if rows:
            lines += ["", "| What | Where | Median | Rows |", "| --- | --- | ---: | ---: |"]
            lines += [f"| {KIND[r['kind']]} | `{r['where']}` | {r['ms']} ms | {r.get('rows', '-')} |" for r in rows]
    grow = [r for r in before.get("growth", []) if r["verdict"] == "GROWS FASTER"]
    if before.get("growth"):
        grow_after = [r for r in (after or {}).get("growth", []) if r["verdict"] == "GROWS FASTER"] if (after or {}).get("growth") else None
        lines += ["", f"**At {meta.get('scale', 10)}x data: {len(grow)} grow faster than the data"
                      + (f" before, {len(grow_after)} after" if grow_after is not None else "") + ".**"]
        if grow:
            lines += ["", "| What | Where | 1x | Scaled | Time |", "| --- | --- | ---: | ---: | ---: |"]
            lines += [f"| {KIND[r['kind']]} | `{r['where']}` | {r['ms_1x']} ms | {r['ms_nx']} ms | {r.get('time_x')}x |" for r in grow[:10]]
    return lines


def slowest(timings, kind):
    rows = [r for r in (timings or {}).get(kind, []) if r.get("ms") is not None]
    return max(rows, key=lambda r: r["ms"]) if rows else None


def look(before, after, measured_after):
    """The "where to look" note for both sides: a note that only held before says so."""
    if not measured_after:
        return before
    if before == after:
        return before
    return "; ".join(x for x in (f"before: {before}" if before else "", f"after: {after or 'none'}") if x)


def counts(static):
    """'H high · M medium' and the expression-safety count, which audit.md keeps apart."""
    pri = (static or {}).get("byPriority") or {}
    safety = sum(((static or {}).get("expressionSafety") or {}).values())
    return f"{pri.get('high', 0)} high · {pri.get('medium', 0)} medium" + (f" (+{safety} expression safety)" if safety else "")


def overview(folder, pre, post, skip_android, timings_before, timings_after, screens, static_before, static_after):
    """One row per area: the number that matters before, after, and where to look."""
    fmt_s = lambda v: "-" if v is None else f"{v} s"
    rows = []
    for label, p, q in (("Sync, time to ready", pre, post), ("Sync on Android", load_runs(folder, "pre-android", skip_android), load_runs(folder, "post-android", skip_android))):
        if p:
            b, a = med([r.get("first_start_to_ready_s") for r in p]), med([r.get("first_start_to_ready_s") for r in q]) if q else None
            def slowest_fn(runs):
                top = sorted(((n, v) for n, v in entry_medians(runs).items() if v is not None), key=lambda x: -x[1])[:1]
                return f"slowest function: {top[0][0]} {top[0][1]} s" if top else ""
            rows.append((label, fmt_s(b), fmt_s(a), look(slowest_fn(p), slowest_fn(q) if q else "", bool(q))))
            busy = med([r.get("sync_busy_s") for r in p])
            if busy is not None:
                def twice(runs):
                    repeated = sorted({sid for r in runs for sid in r.get("repeated_scopes", [])})
                    return f"synced more than once: {', '.join(repeated)}" if repeated else ""
                rows.append((f"{label.split(',')[0]}, all sync work (also after ready)", fmt_s(busy),
                             fmt_s(med([r.get("sync_busy_s") for r in q])) if q else "-", look(twice(p), twice(q) if q else "", bool(q))))
    for kind, label in (("queries", "Queries"), ("actions", "Actions (execute-sql)")):
        if timings_before:
            tot = lambda t: round(sum(r.get("ms") or 0 for r in (t or {}).get(kind, [])), 1)
            def where(t):
                if not t:
                    return ""
                top = slowest(t, kind)
                grow = sum(1 for r in t.get("growth", []) if r["kind"] == kind and r["verdict"] == "GROWS FASTER")
                slower = sum(1 for r in t.get("comparisonScaled", []) if r["kind"] == kind and r["verdict"] == "SLOWER")
                return "; ".join(x for x in (f"slowest: `{top['where']}` {top['ms']} ms" if top else "",
                                             f"{grow} grow faster than the data" if grow else "",
                                             f"**{slower} slower at {t.get('meta', {}).get('scale', 'scaled')}x data**" if slower else "") if x)
            rows.append((f"{label}, {len(timings_before.get(kind, []))} in total", f"{tot(timings_before)} ms",
                         f"{tot(timings_after)} ms" if timings_after else "-", look(where(timings_before), where(timings_after), bool(timings_after))))
    for label, (p, q) in screens.items():
        if p:
            verdicts = {r["screen"]: r for r in ((q or {}).get("comparison") or {}).get("rows", [])}
            for name, s in p["screens"].items():
                after = q["screens"].get(name, {}).get("median_ms") if q else None
                v = verdicts.get(name)
                note = ""
                if v:
                    note = v["verdict"] + (f": runs {', '.join(f'{x / 1000:.2f}' for x in v.get('before_runs', []))} → "
                                           f"{', '.join(f'{x / 1000:.2f}' for x in v.get('after_runs', []))}"
                                           if v["verdict"].startswith("unclear") else "")
                rows.append((f"{label}: {name}", "-" if s["median_ms"] is None else f"{s['median_ms'] / 1000:.2f} s",
                             "-" if after is None else f"{after / 1000:.2f} s", note))
    if static_before:
        rows.append(("Audit findings", counts(static_before), counts(static_after) if static_after else "-",
                     "by section below; fixes in audit.md"))
    if not rows:
        return []
    return (["## Overview", "", "| Area | Before | After | Where to look |", "| --- | ---: | ---: | --- |"]
            + [f"| {a} | {b} | {c} | {d} |" for a, b, c, d in rows]
            + ["", "Expressions are inside screen times; Jigx exposes no separate expression timing.", ""])


def screen_lines(pre, post):
    if not pre:
        return ["Not measured. Run time-screens.py with a screens.json (references/customer-review.md)."]
    fmt = lambda v: "-" if v is None else f"{v / 1000:.2f} s"
    runs = lambda vs: ", ".join(f"{v / 1000:.2f}" for v in vs or []) or "-"
    until = lambda s: (f"id {s['ready']['id']}" if isinstance(s.get("ready"), dict) and "id" in s["ready"]
                       else str((s.get("ready") or {}).get("text", "-")) if isinstance(s.get("ready"), dict)
                       else s.get("ready") or "-")
    if post and post.get("comparison"):
        lines = ["| Screen | Timed until | Before | After | Change | | Runs before → after |",
                 "| --- | --- | ---: | ---: | ---: | --- | --- |"]
        lines += [f"| {r['screen']} | `{r.get('ready', '-')}` | {fmt(r['before_ms'])} | {fmt(r['after_ms'])} | "
                  f"{'-' if r['change_pct'] is None else str(r['change_pct']) + '%'} | {r['verdict']} | "
                  f"{runs(r.get('before_runs'))} → {runs(r.get('after_runs'))} |" for r in post["comparison"]["rows"]]
    else:
        lines = ["| Screen | Timed until | Median | Runs |", "| --- | --- | ---: | --- |"]
        lines += [f"| {n} | `{until(s)}` | {fmt(s['median_ms'])} | {runs([r['ms'] for r in s.get('runs', []) if r.get('ms') is not None])} |"
                  for n, s in pre["screens"].items()]
    return lines + ["", f"Tap to ready element on {pre['device']}, {pre['runs']} runs a screen; includes datasources, "
                        "expressions and rendering, plus Maestro's overhead. A screen's time covers what renders before its "
                        "ready element, not what loads after it. unclear: the medians moved but the runs overlap; read the runs."]


def keep_lines(comparison, timings_after, screens):
    """What the second timing says about the changes: what paid off, what got slower and what did
    not move. A tester found forms 'slower' after the fixes with no word on what to do about it."""
    faster, slower, same, unclear = [], [], [], []
    put = lambda verdict, text: (faster if verdict in ("faster", "improved") else slower if verdict in ("SLOWER", "REGRESSION")
                                 else same if verdict in ("same", "within noise") else unclear).append(text)
    if comparison and not comparison.get("baseline_only") and comparison.get("rows"):
        t = comparison["rows"][0]
        if t.get("after_s") is not None:
            put(t["verdict"], f"sync, time to ready ({t['before_s']} → {t['after_s']} s)")
    sec = lambda v: "-" if v is None else f"{v / 1000:.2f}"
    for label, post in screens:
        for r in ((post or {}).get("comparison") or {}).get("rows", []):
            put(r["verdict"], f"{label} {r['screen']} ({sec(r.get('before_ms'))} → {sec(r.get('after_ms'))} s)")
    for at, key in (("", "comparison"), (" at scaled data", "comparisonScaled")):
        for r in (timings_after or {}).get(key) or []:
            if r["verdict"] != "same":
                put(r["verdict"], f"{KIND[r['kind']]} `{r['where']}`{at} ({r['before_ms']} → {r['after_ms']} ms)")
    if not (faster or slower or same or unclear):
        return []
    lines = ["## Keep or take out", ""]
    if faster:
        lines.append(f"- **Faster, keep:** {'; '.join(faster)}.")
    if slower:
        lines.append(f"- **Slower:** {'; '.join(slower)}. Time these again once. If they are still slower, take out the "
                     "change behind them (one commit per section) and time again; do not merge it as a performance fix.")
    if unclear:
        lines.append(f"- **Unclear:** {'; '.join(unclear)}. The runs overlap or cannot be compared: read the runs, time again.")
    if same:
        lines.append(f"- **No measurable change:** {'; '.join(same)}. A change that touches only these did not pay off on "
                     "this data. Keep it only if it fixes something else (an error, a risk, growth at more data), and say which.")
    return lines + [""]


def report(folder, solution=None, timings=(None, None), exclude=()):
    before = findings(read(folder, "before", "static.json"), read(folder, "before", "plans.json"))
    after_static, after_plans = read(folder, "after", "static.json"), read(folder, "after", "plans.json")
    after = findings(after_static, after_plans) if (after_static or after_plans) else None
    comparison = read(folder, "runs", "comparison.json")
    changes = read(folder, "changes.json") or []
    title = "Performance review" + (f": {solution}" if solution else "")
    lines = [f"# {title}", ""]
    timings_before = read_path(timings[0]) if timings[0] else read(folder, "before", "timings.json")
    timings_after = read_path(timings[1]) if timings[1] else read(folder, "after", "timings.json")
    screens_pre, screens_post = read(folder, "screens", "pre.json"), read(folder, "screens", "post.json")
    # The runs compare-runs.py left out stay out here, or the two would show different medians.
    skip = set(exclude) | set((comparison or {}).get("excluded", []))
    android_sync = read(folder, "runs", "comparison-android.json")
    skip_android = set(exclude) | set((android_sync or {}).get("excluded", []))
    pre, post = load_runs(folder, "pre", skip), load_runs(folder, "post", skip)
    android_screens = (read(folder, "screens", "pre-android.json"), read(folder, "screens", "post-android.json"))
    lines += overview(folder, pre, post, skip_android, timings_before, timings_after,
                      {"Screen": (screens_pre, screens_post), "Android screen": android_screens},
                      read(folder, "before", "static.json"), after_static)
    lines += keep_lines(comparison, timings_after, [("screen", screens_post), ("Android screen", android_screens[1])])
    lines += ["## Sync", ""] + timing_lines(comparison, pre) + [""]
    lines += ["## Functions", ""] + function_lines(pre, post) + [""]
    lines += ["## Queries and actions", ""] + query_lines(timings_before, timings_after) + [""]
    lines += ["## Screens", ""] + screen_lines(screens_pre, screens_post) + [""]
    # Android, when it was timed too (track-load.py --android, time-screens.py --platform android).
    android_pre, android_post = read(folder, "screens", "pre-android.json"), read(folder, "screens", "post-android.json")
    android_pre_runs = load_runs(folder, "pre-android", skip_android)
    if android_sync or android_pre_runs:
        lines += ["## Sync (Android)", ""] + timing_lines(android_sync, android_pre_runs) + [""]
        lines += ["## Functions (Android)", ""] + function_lines(android_pre_runs, load_runs(folder, "post-android", skip_android)) + [""]
    if android_pre:
        lines += ["## Screens (Android)", ""] + screen_lines(android_pre, android_post) + [""]
    lines += ["## Findings by section", ""]
    pri = (read(folder, "before", "static.json") or {}).get("byPriority")
    if pri:
        after_pri = (after_static or {}).get("byPriority")
        safety = lambda st: sum(((st or {}).get("expressionSafety") or {}).values())
        sb, sa = safety(read(folder, "before", "static.json")), safety(after_static)
        lines += [f"Priority before: {pri['high']} high, {pri['medium']} medium, {pri['low']} low"
                  + (f", plus {sb} expression-safety" if sb else "")
                  + (f"; after: {after_pri['high']} high, {after_pri['medium']} medium, {after_pri['low']} low"
                     + (f", plus {sa} expression-safety" if sa else "") if after_pri else "")
                  + ". The full list with locations and fixes is in `audit.md` (`check-performance.mjs --report`).", ""]
    if after is None:
        lines += ["Baseline only; nothing has been changed yet.", "",
                  "| § | Section | Finding | Count |", "| --- | --- | --- | ---: |"]
        lines += [f"| {s} | {SECTIONS[s]} | {label} | {b} |" for s, label, b, _ in section_rows(before, None)]
    else:
        lines += ["| § | Section | Finding | Before | After |", "| --- | --- | --- | ---: | ---: |"]
        lines += [f"| {s} | {SECTIONS[s]} | {label} | {b} | {a} |" for s, label, b, a in section_rows(before, after)]
    shown = sorted({s for s, _, _, _ in section_rows(before, after)})
    if shown:
        lines += ["", "What the sections mean:", ""] + [f"- **{s} {SECTIONS[s]}**: {SECTION_MEANING[s]}" for s in shown]
    lines += ["", "Static counts are leads, not timings; sections D-H also need review the scripts cannot do.", ""]
    if changes:
        lines += ["## Changes applied", ""]
        for s in SECTIONS:
            items = [c for c in changes if c.get("section") == s]
            if items:
                lines.append(f"**{s} · {SECTIONS[s]}**")
                lines += [f"- {c['change']}" + (f" ({', '.join(c['files'])})" if c.get("files") else "") for c in items]
                lines.append("")
    lines += ["## Not verified", "",
              "- Simulator timings show relative change on that machine, not timings on a customer's device.",
              "- Confirm large wins on the slowest supported device with production-sized data."]
    if skip | skip_android:
        lines.append(f"- Runs left out of every median: {', '.join(sorted(skip | skip_android))}.")
    if not comparison or comparison.get("baseline_only"):
        lines.append("- No before/after load timing was recorded.")
    if not timings_after:
        lines.append("- Query and action times were not compared before and after.")
    elif (timings_before or {}).get("growth") and not timings_after.get("comparisonScaled"):
        lines.append("- Query times at the scaled data were not compared before and after.")
    if not (screens_post and screens_post.get("comparison")):
        lines.append("- Screen open times were not compared before and after.")
    lines.append("- Expressions have no runtime timing; their cost shows only inside screen open time.")
    return "\n".join(lines) + "\n"


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("folder", nargs="?", default=os.path.expanduser("~/jigx-perf-reviews"))
    p.add_argument("--solution")
    p.add_argument("--timings", nargs=2, metavar=("BEFORE", "AFTER"),
                   help="time-queries.mjs JSON files to use instead of before/ and after/timings.json")
    p.add_argument("--exclude", action="append", default=[], metavar="LABEL",
                   help="run label to leave out, besides the ones compare-runs.py left out; repeat it, or separate with commas")
    p.add_argument("--change", metavar='"B: what changed"', help="append one change to changes.json (section letter A-H first) and exit")
    p.add_argument("--files", nargs="*", default=[], help="with --change: the files it touched")
    args = p.parse_args(argv)
    if args.change:
        m = re.match(r"\s*([A-Ha-h])\s*[:.)-]\s*(.+)", args.change, re.S)
        if not m:
            p.error('--change needs the section letter first, e.g. --change "F: skip the focus sync right after a sync" '
                    f"({', '.join(f'{k} {v}' for k, v in SECTIONS.items())})")
        path = os.path.join(args.folder, "changes.json")
        changes = read(args.folder, "changes.json") or []
        changes.append({"section": m.group(1).upper(), "change": m.group(2).strip(), **({"files": args.files} if args.files else {})})
        with open(path, "w", encoding="utf-8") as f:
            json.dump(changes, f, indent=2)
        print(f"{path}: {len(changes)} change(s); added to {m.group(1).upper()} {SECTIONS[m.group(1).upper()]}")
        return 0
    if not any(os.path.exists(os.path.join(args.folder, "before", n)) for n in ("static.json", "audit.json")):
        p.error(f"{args.folder}/before/static.json is missing; capture the baseline first")
    skip = [x.strip() for value in args.exclude for x in value.split(",") if x.strip()]
    text = report(args.folder, args.solution, tuple(args.timings or (None, None)), skip)
    with open(os.path.join(args.folder, "report.md"), "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
