import fs from 'node:fs';
import path from 'node:path';
import {spawnSync} from 'node:child_process';
import vm from 'node:vm';
import {fileURLToPath} from 'node:url';

const root = fileURLToPath(new URL('../', import.meta.url));
const files = ['integrations/client.mjs', 'integrations/cli.mjs', 'integrations/client.test.mjs', 'integrations/mcp/server.mjs', 'integrations/mcp/verify.mjs', 'integrations/mcp/verify-live.mjs', 'scripts/check_syntax.mjs'];
for (const file of files) {
  const result = spawnSync(process.execPath, ['--check', path.join(root, file)], {encoding: 'utf8', windowsHide: true});
  if (result.status !== 0) throw new Error(`Syntax check failed: ${file}`);
}
const html = fs.readFileSync(path.join(root, 'web/index.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(script);
const lock = JSON.parse(fs.readFileSync(path.join(root, 'integrations/mcp/package-lock.json'), 'utf8'));
const manifest = JSON.parse(fs.readFileSync(path.join(root, 'integrations/mcp/package.json'), 'utf8'));
if (JSON.stringify(lock.packages[''].dependencies) !== JSON.stringify(manifest.dependencies)) throw new Error('Manifest and lockfile dependencies differ');
console.log(`PASS: ${files.length} JavaScript modules, inline browser script, and ${Object.keys(lock.packages).length} lockfile package records parse.`);
