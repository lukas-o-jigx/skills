# Sync Orchestration

How the inbound sync is shaped: what runs at start, what runs later, and what stops a
bad run from cascading. The patterns come from a mature offline field-service solution
and from audits where one oversized sync caused most production errors. Outbound writes
and retries are in `writes-and-duplicates.md`.

## Gate on required context first

Before any data operation, validate the identifiers every later call filters on (user,
employee number, branch, territory):

```yaml
expressions:
  initialized: |
    =$count(@ctx.datasources.loggedinuser) > 0
    and $length($trim(@ctx.datasources.loggedinuser.employeeID)) > 0
```

When the gate fails, stop and show a clear message ("Your user has no employee number").
Do not run the sync with an empty filter: it produces full-table pulls or a 400 on every
call. Guard calls that need a later choice (customer, branch) until that choice exists.
Never mark a skipped or failed step as complete.

## Tier the sync

One action with dozens of concurrent `sync-entities` makes optional failures fail the
whole sync, contends for the device, and invites users to press refresh mid-run. Split:

| Tier | Contents | When |
| --- | --- | --- |
| Essential | user, today's work, the records the first screen lists | at start, sequential |
| Background | reference data and feature data, one group per feature | after the first screen is usable |
| On demand | a feature's own data | when that feature opens |

Within a stage, run steps in dependency order and gate each on no earlier failure:

```yaml
when: =$count(@ctx.datasources.sync-status[state='failed']) = 0
```

Use feature flags from configuration to include or skip feature groups, so a customer who
does not use a feature does not download it.

## Delta, not full pulls

Pull mutable entities by modified date, keyed on the newest value stored locally:

```sql
SELECT json_extract(data, '$.LastModifiedDateTime') AS LastModifiedDateTime
FROM [records]
ORDER BY LastModifiedDateTime DESC
LIMIT 1
```

Fall back to an early date on the first sync, or to a bounded window (the current month)
for heavy entities such as files. A pure delta misses records removed from the user's
scope; use a fixed recent window for those and reconcile deletions. Ask the backend owner
for created and last-modified fields early: changed-records sync depends on them and they
are a backend change, not an app change.

Page large child sets with a continuation function in small batches (about 30) instead of
one large request; it avoids URL length limits and keeps requests short on slow networks.

## Refresh on an interval, not a timer

Store the last run per refresh action and compare on focus:

```sql
SELECT CASE
  WHEN count(*) = 0 OR max(json_extract(data, '$.LastRunDateTime')) IS NULL THEN 1
  WHEN datetime(max(json_extract(data, '$.LastRunDateTime')))
       < datetime('now', 'localtime', '-' || @intervalMinutes || ' minutes') THEN 1
  ELSE 0 END AS shouldRefresh
FROM [refreshRuns] WHERE id = @actionId
```

Run the refresh from `onFocus` only when `shouldRefresh = 1`, the device is online and no
earlier refresh error is unresolved; write `LastRunDateTime` with an upsert on success.
Make the interval configurable. Server push (`onTableChanged`) complements this; the
interval catches what push missed while offline.

## Online guard

Wrap every sync action and remote read in `when: =@ctx.system.isOnline`. Offline, the app
keeps working from local tables.

## Track progress with sync scopes

`action.start-sync-scope` records each `instanceId` with state `not started`, `syncing`,
`synced` or `failed` and its failure details. Drive progress, "in progress" and error
banners from datasources over that state rather than from solution-state flags. Report
the failing child entity, its URL and status, not just a parent summary.

## Run once at a time

Initialisation and sync must be safe to start twice (app start plus a refresh button, two
screens). Make the work idempotent and guard it:

- Seed or reset tables with one upsert, not a delete followed by many inserts in a
  non-sequential group. That pattern caused hundreds of `UNIQUE constraint failed`
  errors in one audit.

  ```yaml
  statement: >-
    INSERT OR REPLACE INTO [steps] (id, data)
    SELECT json_extract(value, '$.id'), json_remove(value, '$.id') FROM json_each(@steps)
  parameters:
    steps: =$string([ {"id": "S1", "title": "Customers"}, … ])
  ```

  Pass the list as a JSON string; an array bound as one parameter can fail on a device.
- Guard with a running flag and disable the refresh control while a sync is active.

## Endpoints that fail permanently

Do not keep an endpoint that returns 404 in the general sync and never retry a 404: fix or
remove it, or move it to on-demand. Verify against the deployed API before calling it
missing.

## Escape hatch

Provide a hard reset that clears the sync-scope state and re-runs a full sync without delta
filters, for when delta state is corrupted. Keep it behind a confirmation.

## Checklist

- [ ] Context gate before any sync; clear message when it fails.
- [ ] Essential, background and on-demand tiers; feature groups isolated.
- [ ] Every step gated on no earlier failure and on `isOnline`.
- [ ] Delta by modified date for every mutable entity; bounded first window for heavy ones.
- [ ] Interval refresh on focus from a stored last-run time, no timers.
- [ ] Idempotent initialisation; run-once guard; refresh disabled while running.
- [ ] No 404 endpoints in the general sync; failures name the child entity.
- [ ] Hard reset path.
