#!/usr/bin/env python3
"""Compare before and after load runs recorded by track-load.py.

Standard library only. Reads <folder>/*-summary.json, groups runs by label prefix
(default pre- and post-), and reports medians for the time from first scope start
to ready, the total sync work (also after ready) and the number of scope starts, each
scope, and each sync entry, with the change in percent. A slowdown
over --threshold percent (default 10) is flagged for investigation; it is a review
threshold, not an SLA. Verdicts, checked against the runs and not only the medians:

  same            under the threshold
  within noise    over the threshold in percent, but under --min-change seconds (default
                  0.5; for an entry three sampling intervals, 0.75 s at track-load.py's
                  0.25 s, since its start and end are each only as precise as one sample)
  unclear         the medians moved, but the runs overlap: some before run is faster than
                  some after run (a call whose time varies, or runs in two groups)
  parallel now    slower only because it now runs side by side with other calls; the total
                  shows whether running in parallel paid off
  not comparable  an entry of a scope that synced twice in a run on one side: the repeat
                  restarted it, so its time measures the repeat, not the call
  improved / REGRESSION   every run on one side beats every run on the other

A scope is compared by its first sync (a repeat sync before a fix moves its last start).
A run is named in the warnings as "not like the others" when it is more than twice the median
of the others on its side, the first sign-in after installing the app, a run where no login
page pause was seen although the other runs had one (password typing was then timed as sync),
or a run with a call at least three times and 5 s slower than in the other runs. Other
solutions syncing during a run are named only when that sets the run apart: on a device that
holds many solutions every sign-in syncs them, which is said once. --exclude <label> leaves a
run out (repeat it, or separate labels with commas); review-report.py leaves the same runs out.

With only --before runs in the folder (no post-* yet), or --before and --after the same, it
checks the baseline alone: medians, runs and the warnings above, with nothing compared.

Writes <folder>/comparison.md and comparison.json, and prints the markdown. Android runs
(--before pre-android --after post-android) write comparison-android.md and .json.
Exits 1 when the total time regressed past the threshold or any run did not reach
ready. Read references/run-and-track.md.

Example:
  python3 compare-runs.py <review>/runs --before pre --after post
"""
import argparse
import glob
import json
import os
import re
import statistics
import sys


def load(folder, prefix):
    """Runs labelled <prefix> or <prefix>-<anything>: "pre" takes pre-1..3 and pre-clean-1, not pre-android-1."""
    runs = []
    label = re.compile(rf"{re.escape(prefix)}(?:-(?!android)[\w.]+(?:-[\w.]+)*)?-summary\.json")
    for path in sorted(p for p in glob.glob(os.path.join(folder, f"{prefix}*-summary.json")) if label.fullmatch(os.path.basename(p))):
        with open(path, encoding="utf-8") as f:
            runs.append(json.load(f))
    return runs


def median(values):
    values = [v for v in values if v is not None]
    return round(statistics.median(values), 2) if values else None


def change(before, after, threshold, min_change=0.5):
    if before is None or after is None:
        return None, "missing"
    if before == 0:
        return None, "same" if after == 0 else "new"
    pct = round((after - before) / before * 100, 1)
    if abs(after - before) < min_change:
        return pct, "same"
    return pct, "REGRESSION" if pct > threshold else "improved" if pct < -threshold else "same"


def overlap(before, after):
    """True when the runs on the two sides overlap (2+ runs a side): no run-by-run change."""
    b, a = [v for v in before if v is not None], [v for v in after if v is not None]
    if min(len(b), len(a)) < 2:
        return False
    return not (max(a) < min(b) or min(a) > max(b))


def metrics(runs):
    """{metric: [value per run]} for total, each scope and each entry."""
    out = {"total: first start to ready": [r.get("first_start_to_ready_s") for r in runs],
           "total: sync busy (all sync work, also after ready)": [r.get("sync_busy_s") for r in runs],
           "total: scope starts (count)": [sum(r["scope_starts"].values()) if "scope_starts" in r else None for r in runs]}
    for key, section in (("scope", "scopes"), ("entry", "entries")):
        names = sorted({n for r in runs for n in r.get(section, {})})
        for n in names:
            # A scope by its first sync: a repeat sync (before a fix) moves its last start.
            field = lambda d: d.get("first_duration_s", d.get("duration_s")) if key == "scope" else d.get("duration_s")
            out[f"{key}: {n}"] = [field(r.get(section, {}).get(n, {})) for r in runs]
    return out


def overlapped(run, name):
    """True when the entry ran side by side with another entry in this run."""
    e = run.get("entries", {}).get(name)
    if not e or e.get("end_s") is None:
        return False
    return any(o.get("end_s") is not None and o["start_s"] < e["end_s"] and o["end_s"] > e["start_s"]
               for n, o in run["entries"].items() if n != name)


def scope_overlapped(run, scope):
    """True when an entry of this scope ran side by side with an entry of another scope."""
    entries = run.get("entries", {})
    mine = [e for n, e in entries.items() if n.split("/")[0] == scope and e.get("end_s") is not None]
    other = [e for n, e in entries.items() if n.split("/")[0] != scope and e.get("end_s") is not None]
    return any(o["start_s"] < e["end_s"] and o["end_s"] > e["start_s"] for e in mine for o in other)


def share_parallel(before, after, name, test=overlapped):
    """Most after runs ran it in parallel, most before runs did not."""
    frac = lambda runs: sum(test(r, name) for r in runs) / len(runs) if runs else 0
    return frac(after) > 0.5 >= frac(before)


def odd_runs(before, after):
    """Warnings for runs that are not like the others on their side, with why and how to leave
    them out, and one line for what every run shares (other solutions syncing on the device)."""
    warnings, everything = [], before + ([] if after is before else after)
    others = [bool(r.get("other_solutions_synced")) for r in everything]
    shared = len(everything) >= 2 and sum(others) > len(everything) / 2  # the device's normal, not a difference
    paused = [r for r in everything if (r.get("interrupted_s") or 0) > 0]
    names = lambda xs: ", ".join(xs[:3]) + (f" and {len(xs) - 3} more" if len(xs) > 3 else "")
    # An install has one first sign-in: when several runs carry the mark, only the earliest is it.
    marked = [r for r in everything if r.get("first_sign_in_after_install")]
    first = min(marked, key=lambda r: r.get("armed_at") or "") if marked else None
    for side, runs in (("before", before),) + ((("after", after),) if after is not before else ()):
        for r in runs:
            why = []
            if r is first:
                why.append("the first sign-in after installing the app")
            if r.get("other_solutions_synced") and not shared:
                why.append(f"{names(r['other_solutions_synced'])} synced during it")
            # A sign-in normally pauses on the login page (interrupted). When at least two runs in
            # three show that pause and this one does not, the runtime never reported it here and
            # the typing was timed.
            if "interrupted_s" in r and not r["interrupted_s"] and len(paused) >= 2 * (len(everything) - len(paused)):
                why.append("no login page pause was seen in it, but in the other runs: typing the password was "
                           "probably timed as sync")
            slow = []
            for name, e in (r.get("entries") or {}).items():
                rest = [o["entries"][name]["duration_s"] for o in runs
                        if o is not r and (o.get("entries") or {}).get(name, {}).get("duration_s") is not None]
                d = e.get("duration_s")
                if d is not None and len(rest) >= 2 and d >= 3 * statistics.median(rest) and d - statistics.median(rest) >= 5:
                    slow.append((d, f"{name} took {d:g}s against {statistics.median(rest):g}s"))
            if slow:
                why.append("; ".join(text for _, text in sorted(slow, reverse=True)[:2])
                           + " in the other runs (a login page that was not detected, or a slow call)")
            v, rest = r.get("first_start_to_ready_s"), [o.get("first_start_to_ready_s") for o in runs if o is not r]
            rest = [x for x in rest if x is not None]
            if v is not None and len(rest) >= 2 and v > 2 * statistics.median(rest):
                why.append(f"{v}s against {', '.join(f'{x:g}' for x in rest)} for the others")
            if why:
                warnings.append(f"{side} run {r['label']} is not like the others ({'; '.join(why)}): run it again, "
                                f"or leave it out with --exclude {r['label']}")
    if shared:
        seen = sorted({n for r in everything for n in r.get("other_solutions_synced") or []})
        warnings.append(f"other solutions on this device synced during {sum(others)} of {len(everything)} runs ({names(seen)}): "
                        "the same for both sides, so the comparison holds, but the times include their load; a device "
                        "with only the test solution gives cleaner numbers")
    return warnings


def baseline(runs, threshold=10.0, min_change=0.5):
    """The baseline alone, before anything is changed: medians and runs per metric, and the
    warnings about its runs. Nothing is compared, so there are no verdicts."""
    m = metrics(runs)
    rows = [{"metric": name, "before_s": median(vs), "after_s": None, "change_pct": None, "verdict": "baseline",
             "before_runs": [v for v in vs if v is not None], "after_runs": []} for name, vs in m.items()]
    warnings = []
    if len(runs) < 3:
        warnings.append(f"only {len(runs)} before run(s); use at least 3 per side")
    bad = [r["label"] for r in runs if r.get("result") != "ready"]
    if bad:
        warnings.append(f"before runs not ready: {', '.join(bad)}")
    repeated = sorted({sid for r in runs for sid in r.get("repeated_scopes", [])})
    if repeated:
        warnings.append(f"before: {', '.join(repeated)} synced more than once in a run")
    warnings += odd_runs(runs, runs)
    total = lambda f: {"before": sum(f(r) for r in runs), "after": 0}
    return {"baseline_only": True, "threshold_pct": threshold, "min_change_s": min_change,
            "before_runs": [r["label"] for r in runs], "after_runs": [], "rows": rows, "warnings": warnings,
            "snapshot_timeouts": total(lambda r: sum(r.get("snapshot_timeouts", {}).values())),
            "log_errors": total(lambda r: r.get("log_errors", 0)), "hash_fallbacks": total(lambda r: r.get("hash_fallbacks", 0)),
            "expression_errors": total(lambda r: sum(e["count"] for e in r.get("expression_errors", []))),
            "machine": sorted({r.get("machine") or "?" for r in runs})}


def compare(before, after, threshold=10.0, min_change=0.5):
    b, a = metrics(before), metrics(after)
    # Entry times come from samples: start and end are each off by up to one interval, so
    # an entry change under three intervals is sampling noise.
    interval = max([r.get("interval_s") or 0.25 for r in before + after] or [0.25])
    repeated = {side: {sid for r in runs for sid in r.get("repeated_scopes", [])}
                for side, runs in (("before", before), ("after", after))}
    rows = []
    for name in list(dict.fromkeys(list(b) + list(a))):
        bs, as_ = b.get(name, []), a.get(name, [])
        mb, ma = median(bs), median(as_)
        kind, _, item = name.partition(": ")
        floor = max(min_change, 3 * interval) if kind == "entry" else min_change
        pct, verdict = change(mb, ma, threshold, floor)
        if verdict == "same" and pct is not None and abs(pct) > threshold:
            verdict = "within noise"  # a large percentage of a time under the noise floor
        if verdict in ("REGRESSION", "improved") and kind == "entry":
            scope = item.split("/")[0]
            twice = [side for side in ("before", "after") if scope in repeated[side]]
            if twice:
                verdict = f"not comparable ({scope} synced twice in the {' and '.join(twice)} runs)"
        if verdict == "REGRESSION" and kind in ("entry", "scope") and share_parallel(
                before, after, item, overlapped if kind == "entry" else scope_overlapped):
            verdict = "parallel now"  # it shares the network with other calls now; the total says if that paid off
        if verdict in ("REGRESSION", "improved") and overlap(bs, as_):
            verdict = "unclear (runs overlap)"
        rows.append({"metric": name, "before_s": mb, "after_s": ma, "change_pct": pct, "verdict": verdict,
                     "before_runs": [v for v in bs if v is not None], "after_runs": [v for v in as_ if v is not None]})
    warnings = []
    for side, runs in (("before", before), ("after", after)):
        if len(runs) < 3:
            warnings.append(f"only {len(runs)} {side} run(s); use at least 3 per side")
        bad = [r["label"] for r in runs if r.get("result") != "ready"]
        if bad:
            warnings.append(f"{side} runs not ready: {', '.join(bad)}")
    for side, runs in (("before", before), ("after", after)):
        repeated = sorted({sid for r in runs for sid in r.get("repeated_scopes", [])})
        if repeated:
            warnings.append(f"{side}: {', '.join(repeated)} synced more than once in a run")
    # A run that is not like the rest of its side distorts the median: name it, with why when
    # known, and how to leave it out.
    warnings += odd_runs(before, after)
    machines = {r.get("machine") for r in before + after}
    if len(machines) > 1:
        warnings.append("runs come from different machines: " + "; ".join(sorted(m or "?" for m in machines)))
    timeouts = {side: sum(sum(r.get("snapshot_timeouts", {}).values()) for r in runs)
                for side, runs in (("before", before), ("after", after))}
    errors = {side: sum(r.get("log_errors", 0) for r in runs) for side, runs in (("before", before), ("after", after))}
    hashed = {side: sum(r.get("hash_fallbacks", 0) for r in runs) for side, runs in (("before", before), ("after", after))}
    expr = {side: sum(e["count"] for r in runs for e in r.get("expression_errors", [])) for side, runs in (("before", before), ("after", after))}
    return {"threshold_pct": threshold, "min_change_s": min_change, "before_runs": [r["label"] for r in before],
            "after_runs": [r["label"] for r in after], "rows": rows, "warnings": warnings,
            "snapshot_timeouts": timeouts, "log_errors": errors, "hash_fallbacks": hashed, "expression_errors": expr,
            "machine": sorted(m or "?" for m in machines)}


def fmt(v, suffix=""):
    return "-" if v is None else f"{v}{suffix}"


def baseline_markdown(report):
    runs = lambda vs: ", ".join(f"{v:g}" for v in vs) or "-"
    lines = ["# Load baseline", "", f"Runs: {', '.join(report['before_runs']) or 'none'}  ",
             f"Machine: {'; '.join(report['machine'])}  ", "Medians in seconds. Nothing is compared yet: this checks the "
             "baseline runs themselves before any change.", ""]
    if report["warnings"]:
        lines += ["**Warnings**", ""] + [f"- {w}" for w in report["warnings"]] + [""]
    lines += ["| Metric | Median | Runs |", "| --- | ---: | --- |"]
    lines += [f"| {r['metric']} | {fmt(r['before_s'])} | {runs(r['before_runs'])} |" for r in report["rows"]]
    lines += ["", f"Timeout for first snapshot: {report['snapshot_timeouts']['before']}. Log errors: {report['log_errors']['before']}. "
                  f"Rows hashed for lack of an id: {report['hash_fallbacks']['before']}. "
                  f"Expression errors: {report['expression_errors']['before']}.",
              "", "Simulator timings show relative change on this machine, not device timings."]
    return "\n".join(lines) + "\n"


def markdown(report):
    if report.get("baseline_only"):
        return baseline_markdown(report)
    lines = ["# Load comparison", "",
             f"Before: {', '.join(report['before_runs']) or 'none'}  ",
             f"After: {', '.join(report['after_runs']) or 'none'}  ",
             f"Machine: {'; '.join(report['machine'])}  ",
             f"Medians in seconds; slowdowns over {report['threshold_pct']}% and "
             f"{report.get('min_change_s', 0.5)} s are flagged.", ""]
    if report["warnings"]:
        lines += ["**Warnings**", ""] + [f"- {w}" for w in report["warnings"]] + [""]
    runs = lambda vs: ", ".join(f"{v:g}" for v in vs) or "-"
    lines += ["| Metric | Before | After | Change | Verdict | Runs before → after |", "| --- | ---: | ---: | ---: | --- | --- |"]
    for r in report["rows"]:
        lines.append(f"| {r['metric']} | {fmt(r['before_s'])} | {fmt(r['after_s'])} | "
                     f"{fmt(r['change_pct'], '%')} | {r['verdict']} | {runs(r.get('before_runs', []))} → {runs(r.get('after_runs', []))} |")
    lines += ["", "**improved** / **REGRESSION**: every run on one side beats every run on the other. "
              "**unclear**: the medians moved but the runs overlap, so there is no change run by run. "
              "**within noise**: a large percentage of a time under the noise floor "
              f"({report.get('min_change_s', 0.5)} s; three sampling intervals for an entry). "
              "**parallel now**: slower because it now runs beside other calls; read the total. "
              "**not comparable**: its scope synced twice in a run, so the repeat restarted it."]
    lines += ["", f"Timeout for first snapshot: before {report['snapshot_timeouts']['before']}, "
                  f"after {report['snapshot_timeouts']['after']}. "
                  f"Log errors: before {report['log_errors']['before']}, after {report['log_errors']['after']}. "
                  f"Rows hashed for lack of an id: before {report['hash_fallbacks']['before']}, after {report['hash_fallbacks']['after']}. "
                  f"Expression errors: before {report['expression_errors']['before']}, after {report['expression_errors']['after']}.",
              "", "Simulator timings show relative change on this machine, not device timings."]
    return "\n".join(lines) + "\n"


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("folder", nargs="?", default=os.path.expanduser("~/jigx-perf-reviews/runs"))
    p.add_argument("--before", default="pre")
    p.add_argument("--after", default="post")
    p.add_argument("--threshold", type=float, default=10.0, help="percent slowdown to flag")
    p.add_argument("--exclude", action="append", default=[], metavar="LABEL",
                   help="run label to leave out (e.g. a first sign-in after install); repeat it, or separate labels with commas")
    p.add_argument("--min-change", type=float, default=0.5,
                   help="seconds; smaller changes are 'same', or 'within noise' when over --threshold percent (sampling resolution)")
    args = p.parse_args(argv)
    skip = {x.strip() for value in args.exclude for x in value.split(",") if x.strip()}
    before, after = ([r for r in load(args.folder, side) if r.get("label") not in skip] for side in (args.before, args.after))
    if not before:
        p.error(f"need {args.before}*-summary.json in {args.folder}")
    # No after runs yet, or the baseline named on both sides: check the baseline alone.
    alone = not after or args.before == args.after
    report = baseline(before, args.threshold, args.min_change) if alone else compare(before, after, args.threshold, args.min_change)
    if skip:
        report["excluded"] = sorted(skip)
        report["warnings"].append(f"left out: {', '.join(sorted(skip))}")
    md = markdown(report)
    name = "comparison-android" if args.before.endswith("-android") else "comparison"
    with open(os.path.join(args.folder, f"{name}.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    with open(os.path.join(args.folder, f"{name}.md"), "w", encoding="utf-8") as f:
        f.write(md)
    print(md)
    if alone:
        return 1 if any("not ready" in w for w in report["warnings"]) else 0
    total = report["rows"][0]
    totals = [r for r in report["rows"] if r["metric"].startswith("total:")]
    failed = (total["verdict"] in ("REGRESSION", "missing") or any(r["verdict"] == "REGRESSION" for r in totals)
              or any("not ready" in w for w in report["warnings"]))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
