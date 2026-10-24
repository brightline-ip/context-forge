import {ContextForgeClient} from './client.mjs';
const client = new ContextForgeClient();
let input = '';
for await (const part of process.stdin) input += part;
try {
  const args = JSON.parse(input.replace(/^\uFEFF/, ''));
  let result;
  switch (args.action) {
    case 'status': result = await client.request('/api/status'); break;
    case 'text': result = await client.request('/api/normalize/text', {text: args.text, mode: 'lossless'}); break;
    case 'files': result = await client.normalizeFiles(args); break;
    case 'jobs': result = await client.getJobs(args.job_ids, args.wait_seconds); break;
    case 'continue-ocr':
      if (!/^[a-f0-9]{32}$/.test(args.document_id)) throw new Error('Invalid document ID');
      result = await client.request('/api/ocr/continue', {document_id: args.document_id}); break;
    case 'complete-ocr': result = await client.completePdfOcr(args); break;
    case 'read': result = await client.request(`/api/documents/${encodeURIComponent(args.document_id)}?offset=${args.offset}&limit=${args.limit}`); break;
    default: throw new Error('Unknown action');
  }
  console.log(JSON.stringify(result, null, 2));
  if (args.action === 'complete-ocr' && (result.status !== 'coverage_complete' || result.quality_status !== 'ready' || result.unresolved_pages?.length)) process.exitCode = result.status === 'failed' ? 1 : 3;
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
}
