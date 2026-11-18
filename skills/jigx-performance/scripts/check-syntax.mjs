#!/usr/bin/env node
// Parse check for a YAML Jigx solution, in any folder (a repo, or a copy in the review folder):
// every .jigx file must parse as YAML, and every expression (a value starting with "=") must
// parse as JSONata. Run it on the files a fix section changed, before publishing:
//
//   node check-syntax.mjs <solution folder> [changed.jigx ...] [--jsonata <dir>] [--yaml-only]
//
// YAML is parsed with the audit's own loader (Ruby). JSONata comes from the solution's own
// node_modules, else the global npm folder (jsonata, or the one @jigx/core-sdk ships), else
// --jsonata: the jsonata package folder, or a project folder that has node_modules/jsonata.
// Jigx's @ctx and @. are read as a variable, which is all the parser needs.
// Exits 0 only when everything was checked and parsed, 1 on any error, and 2 when it could not
// check: no jsonata was found, so the expressions were not parsed (a pass would be false; as a
// CI gate this fails the build). --yaml-only says a YAML-only check is intended. Changes nothing.
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
import {spawnSync} from 'node:child_process';
import {pathToFileURL} from 'node:url';
import {loadSolution, where} from './check-performance.mjs';

function walk(value, visit, p = '$') {
  visit(value, p);
  if (value && typeof value === 'object') for (const [k, v] of Object.entries(value)) walk(v, visit, `${p}.${k}`);
}

// The jsonata package: the solution's own, then the global one (or @jigx/core-sdk's), else null.
export function findJsonata(folder, given) {
  const tries = [];
  // --jsonata: the package itself, or a project folder with node_modules/jsonata.
  if (given) tries.push(path.resolve(given), path.join(path.resolve(given), 'node_modules', 'jsonata'));
  try { tries.push(path.dirname(createRequire(path.join(path.resolve(folder), 'x.js')).resolve('jsonata/package.json'))); } catch { /* none */ }
  const root = spawnSync('npm', ['root', '-g'], {encoding: 'utf8'}).stdout?.trim();
  if (root) tries.push(path.join(root, 'jsonata'), path.join(root, '@jigx', 'core-sdk', 'node_modules', 'jsonata'));
  for (const dir of tries) {
    try {
      const require = createRequire(path.join(dir, 'x.js'));
      const jsonata = require(dir);
      const version = JSON.parse(fs.readFileSync(path.join(dir, 'package.json'), 'utf8')).version;
      if (typeof jsonata === 'function') return {jsonata, version, dir};
    } catch { /* next */ }
  }
  return null;
}

// Jigx's @ctx and @. become a variable so the JSONata parser accepts them, and a name with a
// hyphen or a leading digit in a context path (@ctx.components.chkYes-3, @ctx.inputs.1position)
// is quoted: Jigx reads it as one name, JSONata as a subtraction or a number.
export const forParser = expr => expr.replace(/@ctx\b/g, '$ctx').replace(/(^|[^\w$])@\./g, '$1$ctx.')
  .replace(/\$ctx((?:\.[\w$]+(?:-[\w$]+)*)+)/g, (_, path) =>
    '$ctx' + path.split('.').map(seg => /-|^\d/.test(seg) ? '`' + seg + '`' : seg).join('.'));

export function checkSyntax(folder, {files = [], jsonata = null} = {}) {
  const {app, sources, parseErrors} = loadSolution(folder);
  const only = new Set(files.map(f => path.relative(path.resolve(folder), path.resolve(f)).split(path.sep).join('/')));
  const inScope = file => !only.size || only.has(file);
  const yaml = parseErrors.filter(e => inScope(e.file));
  const expressions = [];
  walk(app, (v, p) => {
    if (typeof v !== 'string' || !/^\s*=/.test(v)) return;
    const loc = where(p, sources);
    if (inScope(loc.split(' › ')[0])) expressions.push({where: loc, expr: v.replace(/^\s*=/, '')});
  });
  const errors = [];
  if (jsonata) for (const e of expressions) {
    try { jsonata(forParser(e.expr)); } catch (x) {
      errors.push({where: e.where, code: x.code, message: x.message, position: x.position, expression: e.expr.trim().slice(0, 160)});
    }
  }
  return {yaml, expressions: expressions.length, errors, checkedJsonata: !!jsonata};
}

function main() {
  const args = process.argv.slice(2);
  const i = args.indexOf('--jsonata');
  const given = i >= 0 ? args.splice(i, 2)[1] : undefined;
  const y = args.indexOf('--yaml-only');
  const yamlOnly = y >= 0;
  if (yamlOnly) args.splice(y, 1);
  const [folder, ...files] = args;
  if (!folder) { console.error('usage: node check-syntax.mjs <solution folder> [changed.jigx ...] [--jsonata <dir>] [--yaml-only]'); process.exitCode = 2; return; }
  try {
    const found = yamlOnly ? null : findJsonata(folder, given);
    if (given && !yamlOnly && !(found && path.resolve(found.dir).startsWith(path.resolve(given))))
      console.error(`--jsonata ${given}: no jsonata package there, nor in its node_modules` + (found ? `; using ${found.dir}.` : '.'));
    const r = checkSyntax(folder, {files, jsonata: found?.jsonata});
    for (const e of r.yaml) console.log(`YAML  ${e.file}: ${e.message ?? e.error ?? 'does not parse'}`);
    for (const e of r.errors) console.log(`JSONata ${e.where}: ${e.code} ${e.message}\n        =${e.expression}`);
    const scope = files.length ? `${files.length} file(s)` : 'the solution';
    console.log(`${scope}: ${r.yaml.length} YAML error(s); ` + (r.checkedJsonata
      ? `${r.expressions} expressions parsed with jsonata ${found.version}, ${r.errors.length} error(s).`
      : yamlOnly ? `${r.expressions} expressions not parsed (--yaml-only).`
      : `${r.expressions} expressions NOT checked: no jsonata found (npm i -g jsonata@1.8.9, or pass --jsonata <dir>).`));
    // Expressions that could not be checked are not a pass: 2, unless YAML alone was asked for.
    const unchecked = !r.checkedJsonata && !yamlOnly && r.expressions > 0;
    if (unchecked) console.error('NOT CHECKED (exit 2): the expressions were not parsed, so this is not a pass. ' +
      'Install jsonata or pass --jsonata <dir>; --yaml-only accepts a YAML-only check.');
    process.exitCode = r.yaml.length || r.errors.length ? 1 : unchecked ? 2 : 0;
  } catch (e) { console.error(`Syntax check failed: ${e.message}`); process.exitCode = 2; }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) main();
