#!/usr/bin/env python3
"""Write the before/after report for a /jigx-performance review.

Standard library only. Reads a review folder laid out as:

  <folder>/before/static.json   check-performance.mjs output before changes
  <folder>/before/plans.json    check-plans.py output before changes
  <folder>/after/static.json    the same after changes (optional until applied)
  <folder>/after/plans.json
  <folder>/runs/comparison.json compare-runs.py output (optional)
  <folder>/changes.json         [{"section": "B", "change": "...", "files": [...]}] (optional)

and writes <folder>/report.md (and prints it): load times before and after, findings
per section A-H before and after, the changes applied per section, and what was not
measured. With only the before files it writes the overview used for the proposal.
Read references/customer-review.md.

Example:
  python3 review-report.py perf-review --solution my-solution
"""
import argparse
import json
import os
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
CODE_SECTION = {
    "unindexed-predicate": "B", "wrapped-predicate": "B", "execute-sql-entities": "B",
    "ddl-invalidates-datasources": "B", "index-not-idempotent": "B", "ddl-ungated": "B",
    "per-row-datasource-read": "C", "uncapped-search-list": "C",
    "datasource-not-equals": "D", "filter-inside-map": "D",
}


def read(folder, *parts):
    path = os.path.join(folder, *parts)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def findings(static, plans):
    """{section: {label: count}} from the static scan and the plan check."""
    out = {}
    if static:
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


def timing_lines(comparison):
    if not comparison:
        return ["Not measured. Run track-load.py and compare-runs.py (references/run-and-track.md)."]
    rows = comparison["rows"]
    total = rows[0]
    pct = "-" if total["change_pct"] is None else f"{total['change_pct']}%"
    lines = [f"**Time to ready: {total['before_s']} s → {total['after_s']} s ({pct}, {total['verdict']})**", "",
             f"Medians of {len(comparison['before_runs'])} runs before and {len(comparison['after_runs'])} after "
             f"on {'; '.join(comparison['machine'])}.", ""]
    moved = [r for r in rows[1:] if r["change_pct"] is not None and r["verdict"] != "same"]
    moved.sort(key=lambda r: r["change_pct"])
    if moved:
        lines += ["| Step | Before | After | Change |", "| --- | ---: | ---: | ---: |"]
        for r in moved[:8] + [r for r in moved[8:] if r["verdict"] == "REGRESSION"]:
            lines.append(f"| {r['metric']} | {r['before_s']} s | {r['after_s']} s | {r['change_pct']}% |")
        lines.append("")
    lines.append(f"Timeout for first snapshot: {comparison['snapshot_timeouts']['before']} → "
                 f"{comparison['snapshot_timeouts']['after']}. Runtime log errors: "
                 f"{comparison['log_errors']['before']} → {comparison['log_errors']['after']}.")
    for w in comparison.get("warnings", []):
        lines.append(f"- Warning: {w}")
    return lines


def report(folder, solution=None):
    before = findings(read(folder, "before", "static.json"), read(folder, "before", "plans.json"))
    after_static, after_plans = read(folder, "after", "static.json"), read(folder, "after", "plans.json")
    after = findings(after_static, after_plans) if (after_static or after_plans) else None
    comparison = read(folder, "runs", "comparison.json")
    changes = read(folder, "changes.json") or []
    title = "Performance review" + (f": {solution}" if solution else "")
    lines = [f"# {title}", ""]
    lines += ["## Load time", ""] + timing_lines(comparison) + [""]
    lines += ["## Findings by section", ""]
    if after is None:
        lines += ["Baseline only; nothing has been changed yet.", "",
                  "| § | Section | Finding | Count |", "| --- | --- | --- | ---: |"]
        lines += [f"| {s} | {SECTIONS[s]} | {label} | {b} |" for s, label, b, _ in section_rows(before, None)]
    else:
        lines += ["| § | Section | Finding | Before | After |", "| --- | --- | --- | ---: | ---: |"]
        lines += [f"| {s} | {SECTIONS[s]} | {label} | {b} | {a} |" for s, label, b, a in section_rows(before, after)]
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
    if not comparison:
        lines.append("- No before/after load timing was recorded.")
    return "\n".join(lines) + "\n"


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("folder", nargs="?", default="perf-review")
    p.add_argument("--solution")
    args = p.parse_args(argv)
    if not os.path.exists(os.path.join(args.folder, "before", "static.json")):
        p.error(f"{args.folder}/before/static.json is missing; capture the baseline first")
    text = report(args.folder, args.solution)
    with open(os.path.join(args.folder, "report.md"), "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
