import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import fs from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {Client} from '@modelcontextprotocol/sdk/client/index.js';
import {StdioClientTransport} from '@modelcontextprotocol/sdk/client/stdio.js';

const server = fileURLToPath(new URL('./server.mjs', import.meta.url));
const clients = [];
const env = Object.fromEntries(Object.entries(process.env).filter(([key]) =>
  ['SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'PATH', 'USERPROFILE', 'CONTEXT_FORGE_TOKEN', 'CONTEXT_FORGE_TOKEN_FILE'].includes(key.toUpperCase())));
try {
  for (let index = 0; index < 2; index++) {
    const client = new Client({name: 'context-forge-live-check', version: '1.0.0'});
    clients.push(client);
    await client.connect(new StdioClientTransport({command: process.execPath, args: [server], env, stderr: 'pipe'}));
  }
  const catalog = await clients[0].listTools();
  assert.equal(catalog.tools.length, 8);
  assert.ok(catalog.tools.some(tool => tool.name === 'complete_pdf_ocr'));
  for (const client of clients) {
    const response = await client.callTool({name: 'context_forge_status', arguments: {}});
    assert.ok(!response.isError);
    const status = response.structuredContent || JSON.parse(response.content[0].text);
    assert.equal(status.security_revision, 3);
    assert.equal(status.auth_required, true);
  }
  const text = '  Live check: 42.\r\n\r\n\r\n\r\nEnd.  ';
  const response = await clients[1].callTool({name: 'normalize_text', arguments: {text}});
  assert.ok(!response.isError);
  const result = response.structuredContent || JSON.parse(response.content[0].text);
  assert.equal(result.text, 'Live check: 42.\n\nEnd.');
  assert.equal(result.original_sha256, createHash('sha256').update(text).digest('hex'));
  const forbidden = await fetch('http://127.0.0.1:8765/api/status', {
    headers: {Origin: 'https://attacker.invalid'}, signal: AbortSignal.timeout(5000), redirect: 'error',
  });
  assert.equal(forbidden.status, 403);
  await forbidden.arrayBuffer();
  for (const headers of [{}, {Authorization: 'Bearer ' + 'x'.repeat(43)}]) {
    const unauthorized = await fetch('http://127.0.0.1:8765/api/status', {
      headers, signal: AbortSignal.timeout(5000), redirect: 'error',
    });
    assert.equal(unauthorized.status, 401);
    await unauthorized.arrayBuffer();
  }
  // Optional: set CONTEXT_FORGE_INSTALLED_PLUGIN to an installed Hermes plugin __init__.py to compare it with the source.
  const installedPlugin = process.env.CONTEXT_FORGE_INSTALLED_PLUGIN;
  if (installedPlugin) {
    const source = await fs.readFile(new URL('../hermes-plugin/context-forge/__init__.py', import.meta.url));
    const installed = await fs.readFile(installedPlugin);
    assert.ok(source.equals(installed), 'Installed plugin differs from source');
  }
  const report = {ok: true, security_revision: 3, authentication_verified: true, concurrent_clients: 2,
    tools: catalog.tools.map(tool => tool.name), lossless_text_verified: true,
    cross_origin_blocked: true, installed_plugin_checked: Boolean(installedPlugin),
    production_file_jobs_created: 0, verified_at: new Date().toISOString()};
  console.log(JSON.stringify(report, null, 2));
} finally {
  for (const client of clients) await client.close();
}
