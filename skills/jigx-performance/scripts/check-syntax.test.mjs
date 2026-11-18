import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {checkSyntax, findJsonata, forParser} from './check-syntax.mjs';

const solution = files => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'jigx-syntax-'));
  for (const [f, text] of Object.entries({'index.jigx': 'name: demo\ntitle: Demo\n', ...files})) {
    fs.mkdirSync(path.dirname(path.join(dir, f)), {recursive: true});
    fs.writeFileSync(path.join(dir, f), text);
  }
  return dir;
};
const found = findJsonata(process.cwd());

test('Jigx @ctx and @. read as a variable for the parser', () => {
  assert.equal(forParser('$count(@ctx.datasources.x) > 0 and @.user.email'), '$count($ctx.datasources.x) > 0 and $ctx.user.email');
  assert.equal(forParser('"a@b.c"'), '"a@b.c"');
  // a hyphenated name is one name in Jigx (chkYes-3, big-list-10k), a subtraction in JSONata
  assert.equal(forParser('@ctx.components.chkYes-3.state.value'), '$ctx.components.`chkYes-3`.state.value');
  assert.equal(forParser('$count(@ctx.datasources.big-list-10k) - 1'), '$count($ctx.datasources.`big-list-10k`) - 1');
  assert.equal(forParser('@ctx.inputs.1position-team-avatar'), '$ctx.inputs.`1position-team-avatar`');
  assert.equal(forParser('@ctx.inputs.list[0].name'), '$ctx.inputs.list[0].name');
});

test('a " : " in a plain YAML value is a YAML error in that file (Project Central, a ternary in when:)', () => {
  const dir = solution({'jigs/a.jigx': 'title: A\ntype: jig.default\nwhen: =@ctx.jig.state.x ? true : false\n',
    'jigs/b.jigx': 'title: B\ntype: jig.default\nwhen: |\n  =@ctx.jig.state.x ? true : false\n'});
  const r = checkSyntax(dir);
  assert.deepEqual(r.yaml.map(e => e.file), ['jigs/a.jigx']);
  assert.deepEqual(checkSyntax(dir, {files: [path.join(dir, 'jigs/b.jigx')]}).yaml, []);
});

test('an expression that does not parse is named with its place (KOI: a ternary with no then-branch)', {skip: !found && 'no jsonata package on this machine'}, () => {
  const dir = solution({'jigs/photos.jigx': 'title: P\ntype: jig.default\nplaceholders:\n  - title: Loading\n    when: =$count(@ctx.datasources.file) > 0 = false :true\n' +
    'children:\n  - type: component.text\n    options:\n      value: =$uppercase(@ctx.inputs.title)\n'});
  const r = checkSyntax(dir, {jsonata: found.jsonata});
  assert.equal(r.expressions, 2);
  assert.deepEqual(r.errors.map(e => [e.where, e.code]), [['jigs/photos.jigx › placeholders.0.when', 'S0201']]);
});

// Peter's end-to-end rerun: without jsonata it printed "NOT checked" and exited 0, on a broken copy too.
test('expressions that could not be checked are not a pass: exit 2, unless --yaml-only', () => {
  const dir = solution({'jigs/a.jigx': 'title: A\ntype: jig.default\nwhen: =$count(@ctx.datasources.file) > 0 = false :true\n'});
  const script = fileURLToPath(new URL('./check-syntax.mjs', import.meta.url));
  // No jsonata anywhere: an empty npm prefix hides the global one, and the folder has none of its own.
  const env = {...process.env, npm_config_prefix: fs.mkdtempSync(path.join(os.tmpdir(), 'jigx-no-npm-'))};
  const run = (...args) => spawnSync(process.execPath, [script, dir, ...args], {encoding: 'utf8', env});
  const none = run();
  assert.equal(none.status, 2);
  assert.match(none.stdout, /1 expressions NOT checked/);
  assert.match(none.stderr, /NOT CHECKED \(exit 2\)/);
  const yaml = run('--yaml-only');
  assert.equal(yaml.status, 0);
  assert.match(yaml.stdout, /not parsed \(--yaml-only\)/);
});

test('--jsonata takes the package folder or the project folder that holds node_modules', {skip: !found && 'no jsonata package on this machine'}, () => {
  const project = fs.mkdtempSync(path.join(os.tmpdir(), 'jigx-project-'));
  fs.mkdirSync(path.join(project, 'node_modules'), {recursive: true});
  fs.symlinkSync(found.dir, path.join(project, 'node_modules', 'jsonata'), 'dir');
  const empty = fs.mkdtempSync(path.join(os.tmpdir(), 'jigx-empty-'));
  assert.equal(findJsonata(empty, project)?.version, found.version);
  assert.equal(findJsonata(empty, path.join(project, 'node_modules', 'jsonata'))?.version, found.version);
});

// Peter's retest: the skill says to run it "on the changed files", but a file alone failed with ENOTDIR and --help with ENOENT.
test('a changed file alone is checked in the solution above it, index.jigx included, and --help prints the usage', {skip: !found && 'no jsonata package on this machine'}, () => {
  const dir = solution({'index.jigx': 'name: s\nonFocus:\n  when: =@ctx.system.isOnline and\n',
    'jigs/deep/a.jigx': 'title: A\ntype: jig.default\nwhen: =$count(@ctx.datasources.file) > 0\n'});
  const script = fileURLToPath(new URL('./check-syntax.mjs', import.meta.url));
  const run = (...args) => spawnSync(process.execPath, [script, ...args, '--jsonata', found.dir], {encoding: 'utf8'});
  const ok = run(path.join(dir, 'jigs/deep/a.jigx'));
  assert.equal(ok.status, 0, ok.stdout + ok.stderr);
  assert.match(ok.stdout, /1 file\(s\): 0 YAML error\(s\); 1 expressions parsed/);
  const bad = run(path.join(dir, 'index.jigx'));
  assert.equal(bad.status, 1);
  assert.match(bad.stdout, /JSONata index › onFocus\.when/);
  const help = spawnSync(process.execPath, [script, '--help'], {encoding: 'utf8'});
  assert.equal(help.status, 0);
  assert.match(help.stdout, /usage: node check-syntax\.mjs/);
});
