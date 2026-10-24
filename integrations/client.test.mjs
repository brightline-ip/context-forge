import assert from 'node:assert/strict';
import {mkdtempSync, rmSync, writeFileSync} from 'node:fs';
import {createServer} from 'node:http';
import {once} from 'node:events';
import {createHash} from 'node:crypto';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import {ContextForgeClient} from './client.mjs';

const TEST_TOKEN = 'context-forge-node-test-token-0000000000000000';
const WRONG_TOKEN = 'context-forge-node-wrong-token-000000000000000';

function testEnvironment(overrides = {}) {
  return {...process.env, CONTEXT_FORGE_TOKEN: TEST_TOKEN, CONTEXT_FORGE_TOKEN_FILE: '', ...overrides};
}

async function mock(handler, action, overrides = {}) {
  const server = createServer(handler);
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const environment = testEnvironment({CONTEXT_FORGE_URL: `http://127.0.0.1:${server.address().port}`, ...overrides});
  try { await action(new ContextForgeClient({environment})); }
  finally {
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
}

test('external or credential-bearing base URLs are rejected before any network access', () => {
  for (const url of ['http://hostile.invalid', 'http://user:mock@127.0.0.1:8765', 'file:///C:/private', 'http://127.0.0.1:8765?token=mock']) {
    assert.throws(() => new ContextForgeClient({environment: testEnvironment({CONTEXT_FORGE_URL: url})}));
  }
});

test('credential loading fails closed and supports a protected token file', async () => {
  const directory = mkdtempSync(path.join(os.tmpdir(), 'forge-auth-'));
  try {
    const missing = path.join(directory, 'missing-token.txt');
    assert.throws(() => new ContextForgeClient({environment: {CONTEXT_FORGE_TOKEN_FILE: missing}}), /not initialized/);
    const tokenFile = path.join(directory, 'service-token.txt');
    writeFileSync(tokenFile, TEST_TOKEN, {encoding: 'utf8', mode: 0o600});
    const client = new ContextForgeClient({environment: {CONTEXT_FORGE_TOKEN_FILE: tokenFile}});
    assert.equal(client.headers.Authorization, 'Bearer ' + TEST_TOKEN);
    assert.throws(() => new ContextForgeClient({environment: {CONTEXT_FORGE_TOKEN: TEST_TOKEN, CONTEXT_FORGE_TOKEN_FILE: tokenFile}}), /not both/);
  } finally { rmSync(directory, {recursive: true, force: true}); }
});

test('missing and wrong credentials fail while the correct token succeeds', async () => {
  const handler = (req, res) => {
    if (req.headers.authorization !== 'Bearer ' + TEST_TOKEN) {
      res.writeHead(401, {'Content-Type': 'application/json'}); res.end('{"error":"Unauthorized"}'); return;
    }
    res.writeHead(200, {'Content-Type': 'application/json'});
    res.end('{"service":"Context Forge","agent_api_version":1,"security_revision":3,"auth_required":true}');
  };
  await mock(handler, async client => {
    const value = await client.fetchJson('/api/status');
    assert.equal(value.security_revision, 3);
  });
  await mock(handler, async client => {
    await assert.rejects(client.fetchJson('/api/status'), /HTTP 401/);
  }, {CONTEXT_FORGE_TOKEN: WRONG_TOKEN});
});

test('redirects are rejected without following the target', async () => {
  let requests = 0;
  await mock((req, res) => { requests++; res.writeHead(302, {Location: '/target'}); res.end(); }, async client => {
    await assert.rejects(client.fetchJson('/redirect'));
    assert.equal(requests, 1);
  });
});

test('malformed responses do not echo sensitive fragments', async () => {
  await mock((req, res) => res.end('mock-sensitive-fragment not JSON'), async client => {
    await assert.rejects(client.fetchJson('/status'), error => !error.message.includes('mock-sensitive-fragment') && error.message.includes('malformed JSON'));
  });
});

test('response size is bounded', async () => {
  await mock((req, res) => res.end('x'.repeat(2 * 1024 * 1024 + 1)), async client => {
    await assert.rejects(client.fetchJson('/large'), /exceeds 2 MiB/);
  });
});

test('job arguments are validated before network calls', async () => {
  const client = new ContextForgeClient({environment: testEnvironment()});
  await assert.rejects(client.getJobs(['../settings']));
  await assert.rejects(client.getJobs([], Infinity));
  await assert.rejects(client.normalizeFiles({paths: []}));
});

test('failed mutating submissions are not automatically retried', async () => {
  const client = new ContextForgeClient({environment: testEnvironment()});
  client.ensureService = async () => {};
  let attempts = 0;
  client.fetchJson = async () => { attempts++; throw new Error('mock connection failure'); };
  const result = await client.normalizeFiles({paths: ['C:/mock.txt']});
  assert.equal(attempts, 1);
  assert.ok(result.items[0].error);
  assert.equal(result.pending, false);
});

test('attachment bytes are verified before one authenticated multipart upload', async () => {
  const content = Buffer.from('exact attachment bytes\r\n', 'utf8');
  let requests = 0;
  await mock(async (req, res) => {
    requests++;
    assert.equal(req.url, '/api/jobs/upload');
    assert.equal(req.headers.authorization, 'Bearer ' + TEST_TOKEN);
    assert.match(req.headers['content-type'], /^multipart\/form-data; boundary=/);
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const body = Buffer.concat(chunks);
    assert.ok(body.includes(content));
    assert.match(body.toString('latin1'), /name="enrich"[\s\S]*false/);
    assert.match(body.toString('latin1'), /filename="evidence.txt"/);
    res.writeHead(202, {'Content-Type': 'application/json'});
    res.end('{"job_id":"abcdef123456","files":1}');
  }, async client => {
    client.ensureService = async () => {};
    const result = await client.normalizeAttachmentBytes({
      content_base64: content.toString('base64'), filename: 'evidence.txt', media_type: 'text/plain',
      expected_sha256: createHash('sha256').update(content).digest('hex'), byte_length: content.length,
      profile_id: 'profile-1', session_id: 'session-1', attachment_id: 'attachment-1',
    });
    assert.equal(result.job_id, 'abcdef123456');
    assert.match(result.receipt, /^[a-f0-9]{64}$/);
  });
  assert.equal(requests, 1);
});

test('attachment mismatch and unsafe metadata fail before network access', async () => {
  const client = new ContextForgeClient({environment: testEnvironment()});
  client.ensureService = async () => { throw new Error('network must not be reached'); };
  const valid = {
    content_base64: Buffer.from('abc').toString('base64'), filename: 'evidence.txt', media_type: 'text/plain',
    expected_sha256: createHash('sha256').update('abc').digest('hex'), byte_length: 3,
    profile_id: 'profile-1', session_id: 'session-1', attachment_id: 'attachment-1',
  };
  await assert.rejects(client.normalizeAttachmentBytes({...valid, byte_length: 4}), error => error.code === 'BYTE_LENGTH_MISMATCH');
  await assert.rejects(client.normalizeAttachmentBytes({...valid, expected_sha256: '0'.repeat(64)}), error => error.code === 'SHA256_MISMATCH');
  await assert.rejects(client.normalizeAttachmentBytes({...valid, filename: '../secret.txt'}), error => error.code === 'INVALID_FILENAME');
  await assert.rejects(client.normalizeAttachmentBytes({...valid, content_base64: 'YWJj==='}), error => error.code === 'INVALID_BASE64');
});

test('complete PDF OCR chains bounded continuations until coverage is complete', async () => {
  const client = new ContextForgeClient({environment: testEnvironment()});
  client.ensureService = async () => {};
  const documents = {
    ['a'.repeat(32)]: {format: '.pdf', quality_status: 'needs_ocr', metadata: {ocr: {attempted_pages: [1], recognized_pages: [1], pending_pages: [2, 3], unresolved_pages: [2, 3]}}},
    ['b'.repeat(32)]: {format: '.pdf', quality_status: 'needs_ocr', metadata: {ocr: {attempted_pages: [1, 2], recognized_pages: [1, 2], pending_pages: [3], unresolved_pages: [3]}}},
    ['c'.repeat(32)]: {format: '.pdf', quality_status: 'needs_review', metadata: {ocr: {attempted_pages: [1, 2, 3], recognized_pages: [1, 2, 3], pending_pages: [], unresolved_pages: []}}},
  };
  let submissions = 0;
  client.request = async (route, body) => {
    if (route.startsWith('/api/documents/')) return {manifest: documents[route.split('/')[3].split('?')[0]]};
    assert.equal(route, '/api/ocr/continue');
    submissions++;
    return {job_id: 'job' + submissions};
  };
  client.fetchJson = async route => ({status: 'completed', files: [{status: 'completed', id: submissions === 1 ? 'b'.repeat(32) : 'c'.repeat(32)}]});
  const result = await client.completePdfOcr({document_id: 'a'.repeat(32), max_batches: 4, timeout_seconds: 10});
  assert.equal(result.status, 'coverage_complete');
  assert.equal(result.batches_run, 2);
  assert.equal(result.quality_status, 'needs_review');
  assert.equal(result.recognition_verified, false);
});

test('complete PDF OCR stops when a new page fails', async () => {
  const client = new ContextForgeClient({environment: testEnvironment()});
  client.ensureService = async () => {};
  let document = {format: '.pdf', quality_status: 'needs_ocr', metadata: {ocr: {attempted_pages: [1], recognized_pages: [1], failed_pages: [], pending_pages: [2, 3], unresolved_pages: [2, 3]}}};
  client.request = async route => {
    if (route.startsWith('/api/documents/')) return {manifest: document};
    return {job_id: 'failed-page-job'};
  };
  client.fetchJson = async () => {
    document = {format: '.pdf', quality_status: 'needs_ocr', metadata: {ocr: {attempted_pages: [1, 2], recognized_pages: [1], failed_pages: [2], pending_pages: [3], unresolved_pages: [2, 3]}}};
    return {status: 'completed', files: [{status: 'completed', id: 'b'.repeat(32)}]};
  };
  const result = await client.completePdfOcr({document_id: 'a'.repeat(32), max_batches: 4, timeout_seconds: 10});
  assert.equal(result.status, 'ocr_page_failures');
  assert.deepEqual(result.failed_pages, [2]);
  assert.equal(result.batches_run, 1);
});
