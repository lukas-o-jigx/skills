import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {spawnSync} from 'node:child_process';
import {checkPerformance, filterPredicates, parseCreateIndex, normaliseExpr, loadSolution, markdownReport, where} from './check-performance.mjs';

// Fixtures hold unreferenced global datasources; that check has its own test.
const codes = (app, opts) => checkPerformance(app, opts).findings.map(x => x.code).filter(c => c !== 'unused-datasource').sort();
const ds = (query, entities = ['default/fileUploadQueue']) => ({type: 'datasource.sqlite', options: {entities, query}});
const PRED = "LOWER(COALESCE(json_extract(q.data, '$.status'), ''))";
const INDEX = "CREATE INDEX IF NOT EXISTS idx_fileUploadQueue_status ON [fileUploadQueue] (LOWER(COALESCE(json_extract(data, '$.status'), '')))";

test('parses expression indexes and normalises the table alias away', () => {
  const idx = parseCreateIndex(INDEX);
  assert.equal(idx.name, 'idx_fileUploadQueue_status');
  assert.equal(idx.table, 'fileUploadQueue');
  assert.equal(idx.ifNotExists, true);
  assert.deepEqual(idx.expressions, [normaliseExpr(PRED)]);
});

test('finds the outermost function around json_extract in WHERE only', () => {
  const q = `SELECT json_extract(data,'$.name') AS name FROM [fileUploadQueue] AS q WHERE ${PRED} = 'pending' AND (json_extract(q.data,'$.x') = @x) ORDER BY name`;
  const preds = filterPredicates(q);
  assert.deepEqual(preds.map(p => p.expr), [PRED, "json_extract(q.data,'$.x')"]);
  assert.equal(preds[0].wrapped, true);
  assert.equal(preds[1].wrapped, false);
});

test('a shipped matching index silences unindexed-predicate; a mismatch does not', () => {
  const action = (statement, extra = {}) => ({type: 'action.execute-sql', when: '=true', options: {entities: ['default/debug'], statements: [{statement}]}, ...extra});
  const query = `SELECT * FROM [fileUploadQueue] q WHERE ${PRED} = 'pending'`;
  assert.deepEqual(codes({datasources: {a: ds(query)}}), ['unindexed-predicate']);
  assert.deepEqual(codes({datasources: {a: ds(query)}, actions: {i: action(INDEX)}}), []);
  const typo = INDEX.replace("'$.status'", "'$.Status'");
  assert.deepEqual(codes({datasources: {a: ds(query)}, actions: {i: action(typo)}}), ['unindexed-predicate']);
});

test('flags wrapped multi-arm predicates, uncapped search and leading-wildcard LIKE is not an index lead', () => {
  const wrapped = "SELECT * FROM t WHERE TRIM(COALESCE(json_extract(data,'$.A'), json_extract(data,'$.B'))) = @v";
  assert.deepEqual(codes({datasources: {a: ds(wrapped)}}), ['wrapped-predicate']);
  const search = "SELECT * FROM t WHERE (@search IS NULL OR json_extract(data,'$.name') LIKE '%' || @search || '%') ORDER BY 1";
  assert.deepEqual(codes({datasources: {a: ds(search)}}), ['uncapped-search-list']);
  assert.deepEqual(codes({datasources: {a: ds(search + ' LIMIT 100')}}), []);
});

test('execute-sql shape, invalidation, idempotence and gating', () => {
  const bad = {type: 'action.execute-sql', options: {statements: [{statement: 'CREATE INDEX idx_a ON [fileUploadQueue] (id)'}]}};
  assert.deepEqual(codes({actions: {i: bad}}), ['ddl-ungated', 'execute-sql-entities', 'index-not-idempotent']);
  const realTable = {type: 'action.execute-sql', when: '=true', options: {entities: ['default/fileUploadQueue'], statements: [INDEX]}};
  assert.deepEqual(codes({datasources: {a: ds('SELECT 1')}, actions: {i: realTable}}), ['ddl-invalidates-datasources']);
  const errors = checkPerformance({actions: {i: bad}}).findings.filter(f => f.severity === 'error');
  assert.equal(errors.length, 1);
});

test('per-row datasource reads, != against a datasource field, and filters inside $map', () => {
  const item = {type: 'component.list-item', options: {title: '=@ctx.current.item.name', color: '=@ctx.datasources.status[id=@ctx.current.item.id].color', label: '="@ctx.datasources.literal"'}};
  const found = checkPerformance({jigs: {list: {item}}}).findings;
  assert.deepEqual(found.map(f => f.code), ['per-row-datasource-read']);
  assert.match(found[0].message, /status/);
  assert.doesNotMatch(found[0].message, /literal/);
  assert.deepEqual(codes({jigs: {a: {when: "=@ctx.datasources.queue.state != 'done'"}}}), ['datasource-not-equals']);
  assert.deepEqual(codes({jigs: {a: {expressions: {rows: '=$map(@ctx.datasources.forms, function($f) { @ctx.datasources.subs[formId = $f.id] })'}}}}), ['filter-inside-map']);
  assert.deepEqual(codes({jigs: {list: {item: {type: 'component.list-item', options: {title: '=@ctx.current.item.name'}}}}}), []);
  assert.deepEqual(codes({jigs: {a: {data: '=$map(@ctx.datasources.files[id != null], function($v) { $v.id })'}}}), []);
});

test('every finding carries a priority, a fix and a readable location, high first', () => {
  const bad = {type: 'action.execute-sql', options: {statements: [{statement: 'CREATE INDEX idx_a ON [t] (id)'}]}};
  const {findings} = checkPerformance({actions: {i: bad}, datasources: {a: ds("SELECT * FROM t WHERE json_extract(data,'$.x') = 1")}});
  for (const f of findings) {
    assert.ok(['high', 'medium', 'low'].includes(f.priority), f.code);
    assert.ok(f.fix && f.where && f.message, f.code);
  }
  assert.equal(findings[0].priority, 'high');
  assert.equal(where('$.jigs.list-orders.datasources.a.options.query', {'$.jigs.list-orders': 'jigs/Orders/list-orders.jigx'}),
    'jigs/Orders/list-orders.jigx › datasources.a.options.query');
  assert.equal(where('$.onLoad.options'), 'index › onLoad.options');
});

test('unused global datasources; a reference from anywhere but itself counts', () => {
  const app = {datasources: {used: ds('SELECT 1'), unused: ds('SELECT 1'), self: {...ds('SELECT 1'), note: '=@ctx.datasources.self'}},
    jigs: {home: {title: '=@ctx.datasources.used.name'}}};
  const unused = checkPerformance(app).findings.filter(f => f.code === 'unused-datasource').map(f => f.path).sort();
  assert.deepEqual(unused, ['$.datasources.self', '$.datasources.unused']);
});

test('sync: unread tables, full pulls, missing $select, sized by device stats', () => {
  const sync = entities => ({type: 'action.sync-entities', options: {provider: 'DATA_PROVIDER_REST', entities}});
  const app = {
    functions: {
      'get-orders': {method: 'GET', url: 'https://{u}Order', parameters: {$filter: {location: 'query'}, $expand: {location: 'query'}}},
      'get-types': {method: 'GET', url: 'https://{u}Type', parameters: {$select: {value: 'ID'}}},
    },
    actions: {s: {action: sync([
      {entity: 'default/orders', function: 'get-orders', parameters: {$filter: "=LastModifiedDateTime gt @ctx.datasources.lastmod"}},
      {entity: 'default/types', function: 'get-types'},
      {entity: 'default/notes', function: 'get-types'},
    ])}},
    datasources: {orders: ds('SELECT * FROM [default/orders]', ['default/orders']), types: ds('SELECT 1 FROM [types] t', [])},
  };
  assert.deepEqual(codes(app), ['sync-full-pull', 'sync-full-pull', 'sync-no-select', 'synced-table-unread']);
  const unread = checkPerformance(app).findings.find(f => f.code === 'synced-table-unread');
  assert.match(unread.message, /^notes /);
  const stats = {orders: {rows: 30000, bytes: 90 * 1024 * 1024}, types: {rows: 1500, bytes: 1000}, notes: {rows: 5, bytes: 10}};
  const sized = checkPerformance(app, {stats}).findings;
  assert.equal(sized.find(f => f.code === 'large-synced-table').priority, 'high');
  assert.equal(sized.find(f => f.code === 'sync-full-pull' && /types/.test(f.message)).priority, 'high');
  assert.equal(sized.find(f => f.code === 'sync-no-select').priority, 'medium');
});

test('markdown report groups findings by priority and code with the fix once', () => {
  const findings = checkPerformance({jigs: {a: {item: {type: 'component.list-item', options: {t: '=@ctx.datasources.x.y'}}},
    b: {item: {type: 'component.list-item', options: {t: '=@ctx.datasources.x.y'}}}}}).findings;
  const md = markdownReport({title: 'sol', findings});
  assert.match(md, /\*\*0 high · 2 medium · 0 low\*\*/);
  assert.match(md, /### per-row-datasource-read \(2\) · C Lists and rendering/);
  assert.equal(md.split('**Fix:**').length, 2);
});

test('loads a YAML solution folder into the output.json shape', {skip: spawnSync('ruby', ['-v']).status !== 0 && 'ruby not installed'}, () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'jigx-sol-'));
  const write = (rel, text) => { fs.mkdirSync(path.dirname(path.join(dir, rel)), {recursive: true}); fs.writeFileSync(path.join(dir, rel), text); };
  write('index.jigx', 'name: demo\ntitle: Demo\n');
  write('datasources/Lists/orders.jigx', 'type: datasource.sqlite\noptions:\n  provider: DATA_PROVIDER_LOCAL\n  entities: [default/orders]\n  query: SELECT * FROM [default/orders]\n');
  write('jigs/Orders/list-orders.jigx', 'type: jig.list\ntitle: =@ctx.datasources.orders.name\n');
  write('_backup/jigs/old.jigx', 'broken: [\n');
  write('jigs/broken.jigx', 'a: [\n');
  const {app, sources, parseErrors} = loadSolution(dir);
  assert.equal(app.name, 'demo');
  assert.equal(app.datasources.orders.options.query, 'SELECT * FROM [default/orders]');
  assert.equal(sources['$.jigs.list-orders'], 'jigs/Orders/list-orders.jigx');
  assert.deepEqual(parseErrors.map(e => e.file), ['jigs/broken.jigx']);
  assert.deepEqual(checkPerformance(app, {sources}).findings, []);
});

test('targeted pulls, tables pulled by delta elsewhere and expression entity lists are not flagged', () => {
  const sync = entities => ({type: 'action.sync-entities', options: {entities}});
  const app = {
    functions: {'get-appts': {method: 'GET', url: 'https://{u}Appointment', parameters: {$filter: {required: false}}}},
    actions: {
      one: {action: sync([{entity: 'appts', function: 'get-appts', parameters: {$filter: "=\"AppointmentNbr eq '\" & @ctx.action.parameters.nbr & \"'\""}}])},
      delta: {action: sync([{entity: 'appts', function: 'get-appts', parameters: {$filter: '=@ctx.datasources.lastmod-appts.LastModifiedDateTime'}}])},
      full: {action: sync([{entity: 'appts', function: 'get-appts'}])},
      dynamic: {action: sync('=$map(@ctx.action.parameters.files, function($v) { {"entity": "files", "function": "get-file"} })')},
    },
    datasources: {appts: ds('SELECT * FROM [default/appts]', ['default/appts'])},
  };
  assert.deepEqual(codes(app).filter(c => c !== 'sync-no-select'), []);
});

test('index steps: listed tables cannot be missing, one bound table is medium, several are high', () => {
  const step = (entities, tables, extra = {}) => ({type: 'action.execute-sql', ...extra, options: {entities,
    statements: tables.map(t => `CREATE INDEX IF NOT EXISTS idx_${t} ON ${t} (json_extract(data, '$.LastModifiedDateTime'))`)}});
  const datasources = {a: ds('SELECT 1', ['employees']), b: ds('SELECT 1', ['vendor'])};
  const run = action => checkPerformance({datasources, actions: {i: action}}).findings.filter(f => f.code.startsWith('ddl-'));
  assert.deepEqual(run(step(['employees'], ['employees'])).map(f => [f.code, f.priority]), [['ddl-invalidates-datasources', 'medium']]);
  assert.deepEqual(run(step(['employees', 'vendor'], ['employees', 'vendor'])).map(f => [f.code, f.priority]), [['ddl-invalidates-datasources', 'high']]);
  const ungated = run(step(['default/debug'], ['employees']));
  assert.deepEqual(ungated.map(f => f.code), ['ddl-ungated']);
  assert.match(ungated[0].message, /employees/);
});
