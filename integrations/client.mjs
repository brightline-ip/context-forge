import {spawn} from 'node:child_process';
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import path from 'node:path';

const root = fileURLToPath(new URL('../', import.meta.url));
const active = new Set(['queued', 'running']);
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const tokenPattern = /^[A-Za-z0-9_-]{43,256}$/;
const sha256Pattern = /^[a-f0-9]{64}$/;
const identityPattern = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const mediaTypePattern = /^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}\/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$/i;
const safeFilenamePattern = /^(?!\.)(?!.*[\\/\x00-\x1f\x7f])[A-Za-z0-9][A-Za-z0-9._ ()+-]{0,179}$/;
const maxAttachmentBytes = 50 * 1024 * 1024;

export class ContextForgeError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'ContextForgeError';
    this.code = code;
  }
}

function attachmentError(code, message) { throw new ContextForgeError(code, message); }

export function loadContextForgeCredential(environment = process.env) {
  const direct = environment.CONTEXT_FORGE_TOKEN;
  const configuredFile = environment.CONTEXT_FORGE_TOKEN_FILE;
  if (direct && configuredFile) throw new Error('Configure CONTEXT_FORGE_TOKEN or CONTEXT_FORGE_TOKEN_FILE, not both');
  let token = direct;
  const tokenFile = configuredFile || path.join(root, 'security/service-token.txt');
  if (!token) {
    try { token = readFileSync(tokenFile, 'utf8'); }
    catch { throw new Error('Context Forge authentication is not initialized'); }
  }
  if (!tokenPattern.test(token)) throw new Error('Context Forge requires a 256-bit URL-safe bearer token');
  return {token, tokenFile: direct ? undefined : tokenFile};
}

export class ContextForgeClient {
  constructor({environment = process.env} = {}) {
    this.environment = environment;
    this.baseUrl = (environment.CONTEXT_FORGE_URL || 'http://127.0.0.1:8765').replace(/\/$/, '');
    const endpoint = new URL(this.baseUrl);
    if (endpoint.protocol !== 'http:' || !['127.0.0.1', 'localhost'].includes(endpoint.hostname) || endpoint.username || endpoint.password || endpoint.search || endpoint.hash || endpoint.pathname !== '/') {
      throw new Error('Context Forge requires a loopback HTTP base URL without credentials, paths, or query parameters');
    }
    const credential = loadContextForgeCredential(environment);
    this.headers = {'Content-Type': 'application/json', Authorization: 'Bearer ' + credential.token};
    this.tokenFile = credential.tokenFile;
  }

  async fetchJson(route, body) {
    const response = await fetch(this.baseUrl + route, {
      method: body === undefined ? 'GET' : 'POST', headers: this.headers,
      ...(body === undefined ? {} : {body: JSON.stringify(body)}), signal: AbortSignal.timeout(10000), redirect: 'error',
    });
    let bytes = 0;
    const parts = [];
    for await (const part of response.body) {
      bytes += part.length;
      if (bytes > 2 * 1024 * 1024) throw new Error('Context Forge response exceeds 2 MiB');
      parts.push(part);
    }
    let value;
    try { value = JSON.parse(Buffer.concat(parts).toString('utf8')); }
    catch { throw new Error('Context Forge returned malformed JSON'); }
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Context Forge returned an invalid response');
    if (!response.ok) throw new Error(`Context Forge HTTP ${response.status}: ${typeof value.error === 'string' ? value.error.slice(0, 300) : 'inspect the job or service configuration'}`);
    return value;
  }

  async fetchMultipart(route, form) {
    const response = await fetch(this.baseUrl + route, {
      method: 'POST', headers: {Authorization: this.headers.Authorization}, body: form,
      signal: AbortSignal.timeout(10000), redirect: 'error',
    });
    let bytes = 0;
    const parts = [];
    for await (const part of response.body) {
      bytes += part.length;
      if (bytes > 2 * 1024 * 1024) throw new ContextForgeError('RESPONSE_TOO_LARGE', 'Context Forge response exceeds 2 MiB');
      parts.push(part);
    }
    let value;
    try { value = JSON.parse(Buffer.concat(parts).toString('utf8')); }
    catch { throw new ContextForgeError('MALFORMED_RESPONSE', 'Context Forge returned malformed JSON'); }
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new ContextForgeError('INVALID_RESPONSE', 'Context Forge returned an invalid response');
    if (!response.ok) throw new ContextForgeError('UPLOAD_REJECTED', `Context Forge rejected the attachment upload (HTTP ${response.status})${typeof value.error === 'string' ? ': ' + value.error.slice(0, 300) : ''}`);
    return value;
  }

  async ensureService() {
    const check = async () => {
      const status = await this.fetchJson('/api/status');
      if (status.service !== 'Context Forge' || status.agent_api_version !== 1 || status.security_revision !== 3 || status.auth_required !== true) {
        throw new Error('This port is not the current Context Forge agent API. Restart Context Forge.');
      }
    };
    try { await check(); return; }
    catch (error) {
      if (error.cause?.code !== 'ECONNREFUSED' || !/^http:\/\/127\.0\.0\.1:\d+$/.test(this.baseUrl)) throw error;
    }
    const python = this.environment.CONTEXT_FORGE_PYTHON || path.join(root, '.venv/Scripts/python.exe');
    const env = Object.fromEntries(Object.entries(this.environment).filter(([key]) => ['SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'PATH', 'USERPROFILE', 'CONTEXT_FORGE_TOKEN', 'CONTEXT_FORGE_TOKEN_FILE'].includes(key.toUpperCase())));
    if (!env.CONTEXT_FORGE_TOKEN && !env.CONTEXT_FORGE_TOKEN_FILE && this.tokenFile) env.CONTEXT_FORGE_TOKEN_FILE = this.tokenFile;
    const child = spawn(python, ['-m', 'context_forge.server', '--port', new URL(this.baseUrl).port], {cwd: root, env, detached: true, windowsHide: true, stdio: 'ignore'});
    let startError;
    child.on('error', error => { startError = error; });
    child.unref();
    for (let attempt = 0; attempt < 40; attempt++) {
      if (startError) throw startError;
      await sleep(250);
      try { await check(); return; }
      catch (error) { if (error.cause?.code !== 'ECONNREFUSED') throw error; }
    }
    throw new Error('Context Forge failed to start. Run start.ps1 to inspect the error.');
  }

  async request(route, body) {
    await this.ensureService();
    return this.fetchJson(route, body);
  }

  async normalizeFiles({paths, recursive = false, wait_seconds = 2}) {
    if (!Array.isArray(paths) || paths.length < 1 || paths.length > 20 || paths.some(p => typeof p !== 'string' || !p.trim()) || typeof recursive !== 'boolean') throw new Error('Supply 1-20 paths and a boolean recursive option');
    await this.ensureService();
    const items = [];
    for (const source of paths) {
      try {
        const job = await this.fetchJson('/api/jobs/path', {path: source, recursive, enrich: false, normalization_mode: 'lossless'});
        items.push({path: source, ...job});
      } catch (error) { items.push({path: source, error: error.message}); }
    }
    return {items, ...await this.getJobs(items.filter(item => item.job_id).map(item => item.job_id), wait_seconds)};
  }

  async normalizeAttachmentBytes({content_base64, filename, media_type, expected_sha256, byte_length, profile_id, session_id, attachment_id}) {
    if (typeof content_base64 !== 'string' || !content_base64.length || content_base64.length > Math.ceil(maxAttachmentBytes / 3) * 4 ||
        !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(content_base64)) {
      attachmentError('INVALID_BASE64', 'Attachment content must be canonical base64 within the 50 MiB limit');
    }
    if (!safeFilenamePattern.test(filename ?? '') || ['security', 'service-token.txt'].includes(filename.toLowerCase())) {
      attachmentError('INVALID_FILENAME', 'Attachment filename is not safe');
    }
    if (!mediaTypePattern.test(media_type ?? '')) attachmentError('INVALID_MEDIA_TYPE', 'Attachment media type is not valid');
    if (!sha256Pattern.test(expected_sha256 ?? '')) attachmentError('INVALID_SHA256', 'Attachment SHA-256 must be lowercase hexadecimal');
    if (!Number.isSafeInteger(byte_length) || byte_length < 1 || byte_length > maxAttachmentBytes) attachmentError('INVALID_BYTE_LENGTH', 'Attachment byte length is not valid');
    for (const [name, value] of Object.entries({profile_id, session_id, attachment_id})) {
      if (!identityPattern.test(value ?? '')) attachmentError('INVALID_IDENTITY', `${name} is not valid`);
    }
    const content = Buffer.from(content_base64, 'base64');
    if (content.toString('base64') !== content_base64) attachmentError('INVALID_BASE64', 'Attachment content must be canonical base64');
    if (content.length !== byte_length) attachmentError('BYTE_LENGTH_MISMATCH', 'Attachment byte length does not match');
    const actualSha256 = createHash('sha256').update(content).digest('hex');
    if (actualSha256 !== expected_sha256) attachmentError('SHA256_MISMATCH', 'Attachment SHA-256 does not match');
    const receipt = createHash('sha256').update(JSON.stringify({profile_id, session_id, attachment_id, filename, media_type, expected_sha256, byte_length})).digest('hex');
    const form = new FormData();
    form.append('enrich', 'false');
    form.append('files', new Blob([content], {type: media_type}), filename);
    await this.ensureService();
    let accepted;
    try { accepted = await this.fetchMultipart('/api/jobs/upload', form); }
    catch (error) {
      if (error instanceof ContextForgeError) throw error;
      throw new ContextForgeError('UPLOAD_UNAVAILABLE', 'Context Forge attachment upload is unavailable');
    }
    if (!/^[a-f0-9]{12}$/.test(accepted.job_id ?? '') || accepted.files !== 1) {
      throw new ContextForgeError('UNCONFIRMED_ACCEPTANCE', 'Context Forge did not confirm attachment acceptance');
    }
    return {job_id: accepted.job_id, files: 1, receipt, expected_sha256, byte_length, profile_id, session_id, attachment_id};
  }

  async normalizeBoundFile({path: source, expected_sha256, operation_key}) {
    if(typeof source!=='string'||!source.trim()||!/^[a-f0-9]{64}$/.test(expected_sha256??'')||!/^[a-f0-9]{64}$/.test(operation_key??''))throw Error('Supply a file, original hash and exact operation key');
    const job=await this.request('/api/jobs/bound-path',{path:source,expected_sha256,operation_key});
    if(!/^[a-f0-9]{12}$/.test(job.job_id??'')||job.files!==1)throw Error('Unconfirmed Context Forge acceptance');
    return {items:[{path:source,...job}]};
  }

  async boundReceipt(operationKey) {
    if(!/^[a-f0-9]{64}$/.test(operationKey??''))throw Error('Supply an exact operation key');
    // Read only: do not launch/restart the service or invoke submission.
    return this.fetchJson('/api/bound-receipt?operation_key='+operationKey);
  }

  async getJobs(ids, waitSeconds = 0) {
    if (!Array.isArray(ids) || ids.length > 20 || ids.some(id => !/^[a-f0-9]{12}$/.test(id)) || !Number.isFinite(waitSeconds) || waitSeconds < 0 || waitSeconds > 20) throw new Error('Invalid job IDs or wait duration');
    await this.ensureService();
    const deadline = Date.now() + Math.min(20, Math.max(0, waitSeconds)) * 1000;
    let jobs;
    do {
      jobs = await Promise.all(ids.map(async id => {
        try { return await this.fetchJson('/api/jobs/' + id); }
        catch (error) { return {id, status: 'error', error: error.message}; }
      }));
      if (!jobs.some(job => active.has(job.status)) || Date.now() >= deadline) break;
      await sleep(250);
    } while (true);
    return {jobs, pending: jobs.some(job => active.has(job.status))};
  }

  async completePdfOcr({document_id, max_batches = 8, timeout_seconds = 300}) {
    if (!/^[a-f0-9]{32}$/.test(document_id) || !Number.isInteger(max_batches) || max_batches < 1 || max_batches > 16 ||
        !Number.isFinite(timeout_seconds) || timeout_seconds < 10 || timeout_seconds > 600) {
      throw new Error('Supply a document ID, 1-16 batches, and a 10-600 second timeout');
    }
    const deadline = Date.now() + timeout_seconds * 1000;
    const chain = [];
    let currentDocumentId = document_id;
    let previousAttempted = -1;
    let previousFailed = null;
    let previousBlank = null;
    let details;
    while (true) {
      details = await this.request(`/api/documents/${currentDocumentId}?offset=0&limit=1`);
      if (details.manifest?.format !== '.pdf' || !details.manifest?.metadata?.ocr) {
        throw new Error('OCR completion requires a normalized PDF document');
      }
      const ocr = details.manifest?.metadata?.ocr || {};
      const pendingPages = Array.isArray(ocr.pending_pages) ? ocr.pending_pages : [];
      const attempted = Array.isArray(ocr.attempted_pages) ? ocr.attempted_pages.length : 0;
      const attemptedPages = new Set(ocr.attempted_pages || []);
      const failedPages = new Set(ocr.failed_pages || []);
      const recognizedPages = new Set(ocr.recognized_pages || []);
      const blankPages = new Set([...attemptedPages].filter(page => !failedPages.has(page) && !recognizedPages.has(page)));
      if (chain.length && (failedPages.size > previousFailed || blankPages.size > previousBlank)) {
        return {status: 'ocr_page_failures', document_id: currentDocumentId, batches_run: chain.length,
          continuation_jobs: chain, quality_status: details.manifest?.quality_status || 'needs_review',
          pending_pages: pendingPages, failed_pages: [...failedPages], blank_pages: [...blankPages],
          unresolved_pages: ocr.unresolved_pages || [], recognition_verified: false};
      }
      if (!pendingPages.length) {
        return {status: 'coverage_complete', document_id: currentDocumentId, batches_run: chain.length,
          continuation_jobs: chain, quality_status: details.manifest?.quality_status || 'needs_review',
          pending_pages: [], unresolved_pages: ocr.unresolved_pages || [], recognition_verified: false};
      }
      if (chain.length >= max_batches) {
        return {status: 'batch_cap_reached', document_id: currentDocumentId, batches_run: chain.length,
          continuation_jobs: chain, quality_status: details.manifest?.quality_status || 'needs_ocr',
          pending_pages: pendingPages, unresolved_pages: ocr.unresolved_pages || [], recognition_verified: false};
      }
      if (Date.now() >= deadline) {
        return {status: 'timeout', document_id: currentDocumentId, batches_run: chain.length,
          continuation_jobs: chain, pending_pages: pendingPages, recognition_verified: false};
      }
      if (previousAttempted >= 0 && attempted <= previousAttempted) {
        return {status: 'no_progress', document_id: currentDocumentId, batches_run: chain.length,
          continuation_jobs: chain, pending_pages: pendingPages, recognition_verified: false};
      }
      previousAttempted = attempted;
      previousFailed = failedPages.size;
      previousBlank = blankPages.size;
      const submitted = await this.request('/api/ocr/continue', {document_id: currentDocumentId});
      let job = await this.fetchJson('/api/jobs/' + submitted.job_id);
      while (active.has(job.status) && Date.now() < deadline) {
        await sleep(400);
        job = await this.fetchJson('/api/jobs/' + submitted.job_id);
      }
      chain.push(submitted.job_id);
      if (active.has(job.status)) {
        return {status: 'timeout', document_id: currentDocumentId, batches_run: chain.length,
          continuation_jobs: chain, pending_pages: pendingPages, recognition_verified: false};
      }
      const file = job.files?.find(item => item.status === 'completed');
      if (!file || !['completed', 'completed_with_errors'].includes(job.status)) {
        return {status: 'failed', document_id: currentDocumentId, batches_run: chain.length,
          continuation_jobs: chain, failed_job: job, pending_pages: pendingPages, recognition_verified: false};
      }
      currentDocumentId = file.id;
    }
  }
}
