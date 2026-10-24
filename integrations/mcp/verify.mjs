import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {spawn} from 'node:child_process';
import {Client} from '@modelcontextprotocol/sdk/client/index.js';
import {StdioClientTransport} from '@modelcontextprotocol/sdk/client/stdio.js';

const root = fileURLToPath(new URL('../../', import.meta.url));
const server = path.join(root, 'integrations/mcp/server.mjs');
const clients = [];
const runRoot = path.join(root, '.audit-runs');
await fs.mkdir(runRoot, {recursive: true});
const sandbox = await fs.mkdtemp(path.join(runRoot, 'mcp-'));
const testEnv = Object.fromEntries(Object.entries(process.env).filter(([key]) => ['SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'PATH', 'USERPROFILE'].includes(key.toUpperCase())));
testEnv.CONTEXT_FORGE_TOKEN = 'context-forge-isolated-test-token-not-production';
const service = spawn(path.join(root, '.venv/Scripts/python.exe'), ['-m', 'context_forge.server', '--port', '0', '--data-root', sandbox], {cwd: root, env: testEnv, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe']});
service.stderr.resume();
const ready = new Promise((resolve, reject) => {
  const timer = setTimeout(() => reject(new Error('Isolated service startup timed out')), 10000);
  service.once('error', error => { clearTimeout(timer); reject(error); });
  service.once('exit', () => { clearTimeout(timer); reject(new Error('Isolated service exited')); });
  service.stdout.on('data', part => {
    const match = part.toString().match(/http:\/\/127\.0\.0\.1:(\d+)/);
    if (match) { clearTimeout(timer); resolve(match[0]); }
  });
});
const hash = text => createHash('sha256').update(text).digest('hex');
async function connect() {
  const client = new Client({name: 'context-forge-verifier', version: '1.0.0'});
  await client.connect(new StdioClientTransport({command: process.execPath, args: [server], cwd: path.dirname(root), env: testEnv, stderr: 'pipe'}));
  clients.push(client);
  return client;
}
async function call(client, name, args = {}) {
  const result = await client.callTool({name, arguments: args});
  assert.equal(result.isError, undefined, JSON.stringify(result));
  return result.structuredContent || JSON.parse(result.content[0].text);
}
try {
  testEnv.CONTEXT_FORGE_URL = await ready;
  const [first, second] = await Promise.all([connect(), connect()]);
  const catalog = await first.listTools();
  assert.equal(catalog.tools.length, 8);
  assert.ok(catalog.tools.some(tool => tool.name === 'continue_pdf_ocr'));
  assert.ok(catalog.tools.some(tool => tool.name === 'complete_pdf_ocr'));
  assert.ok(catalog.tools.some(tool => tool.name === 'normalize_attachment_bytes'));
  const statuses = await Promise.all([call(first, 'context_forge_status'), call(second, 'context_forge_status')]);
  assert.ok(statuses.every(status => status.service === 'Context Forge'));
  const text = '  Fact: 42.\r\n\r\n\r\n\r\nEnd.  ';
  const inline = await call(first, 'normalize_text', {text});
  assert.equal(inline.text, 'Fact: 42.\n\nEnd.');
  assert.equal(inline.original_sha256, hash(text));
  const fixtureDir = await fs.mkdtemp(path.join(sandbox, 'fixtures-'));
  const source = path.join(fixtureDir, 'context.txt');
  const contents = 'REPEATED FACT\n    indentation\nnon-\nlinear\n\n' + 'source fact 42 '.repeat(1500) + '\nEND';
  await fs.writeFile(source, contents);
  const broken = path.join(fixtureDir, 'broken.pdf');
  await fs.writeFile(broken, 'not a PDF');
  let batch = await call(first, 'normalize_files', {paths: [source, broken, path.join(fixtureDir, 'missing.txt')], wait_seconds: 2});
  assert.ok(batch.items[2].error);
  for (let attempt = 0; batch.pending && attempt < 5; attempt++) {
    batch = {...batch, ...await call(second, 'get_normalization_jobs', {job_ids: batch.jobs.map(job => job.id), wait_seconds: 2})};
  }
  assert.equal(batch.pending, false);
  assert.equal(batch.jobs[0].status, 'completed');
  assert.equal(batch.jobs[1].status, 'completed_with_errors');
  const file = batch.jobs[0].files[0];
  const noOcr = await first.callTool({name: 'continue_pdf_ocr', arguments: {document_id: file.id}});
  assert.equal(noOcr.isError, true);
  assert.equal(file.source_sha256, hash(contents));
  let offset = 0;
  let chunkCount = 0;
  do {
    const page = await call(second, 'read_normalized_document', {document_id: file.id, offset, limit: 1});
    assert.equal(page.manifest.enrichment, 'disabled');
    assert.equal(page.manifest.normalization_mode, 'lossless');
    for (const chunk of page.chunks) {
      assert.ok(chunk.text.length <= 6000);
      assert.equal(chunk.sha256, hash(chunk.text));
      chunkCount++;
    }
    const normalized = await fs.readFile(page.normalized_path, 'utf8');
    assert.ok(normalized.includes(contents));
    offset = page.next_offset;
  } while (offset !== null);
  assert.equal(chunkCount, file.chunks);
  assert.equal(await fs.readFile(source, 'utf8'), contents);
  const invalid = await first.callTool({name: 'read_normalized_document', arguments: {document_id: '0'.repeat(32)}});
  assert.equal(invalid.isError, true);
  const report = {ok: true, tools: catalog.tools.map(tool => tool.name), concurrent_clients: 2, job_ids: batch.jobs.map(job => job.id), chunk_count: chunkCount, verified_at: new Date().toISOString()};
  console.log(JSON.stringify(report, null, 2));
} finally {
  await Promise.all(clients.map(client => client.close()));
  if (service.exitCode === null) {
    await new Promise(resolve => { service.once('exit', resolve); service.kill(); });
  }
}
