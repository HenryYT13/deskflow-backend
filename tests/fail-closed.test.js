import test from 'node:test';
import assert from 'node:assert/strict';
import { FocusApiClient } from '../lib/api-client.js';
import { evaluateVerdict } from '../lib/rules.js';

test('fail-closed: offline backend returns BLOCK verdict', async () => {
  // Simulate network failure / offline backend
  const offlineFetch = () => {
    throw new Error('fetch failed: connect ECONNREFUSED 127.0.0.1:8787');
  };

  const client = new FocusApiClient({
    deviceToken: 'valid_token',
    fetchFn: offlineFetch
  });

  const res = await client.classifyUrl({
    url: 'https://unknown-site.org',
    title: 'Unknown Site'
  });

  assert.equal(res.action, 'BLOCK');
  assert.equal(res.failClosed, true);
  assert.ok(res.reason.includes('Backend classification failed'));

  // Ensure evaluator also fails closed
  const verdict = evaluateVerdict({
    url: 'https://unknown-site.org',
    focusActive: true,
    classificationResult: res
  });

  assert.equal(verdict.action, 'BLOCK');
  assert.equal(verdict.failClosed, true);
});

test('fail-closed: request timeout returns BLOCK verdict', async () => {
  // Simulate abort timeout
  const timeoutFetch = () => {
    const err = new Error('The operation was aborted');
    err.name = 'AbortError';
    throw err;
  };

  const client = new FocusApiClient({
    deviceToken: 'valid_token',
    fetchFn: timeoutFetch
  });

  const res = await client.classifyUrl({
    url: 'https://slow-responding-site.com',
    title: 'Slow Site'
  });

  assert.equal(res.action, 'BLOCK');
  assert.equal(res.failClosed, true);
  assert.ok(res.reason.includes('timed out') || res.reason.includes('failed'));
});

test('fail-closed: low confidence classification returns BLOCK verdict', async () => {
  // Backend returns uncertain verdict with low confidence
  const lowConfidenceFetch = async () => ({
    ok: true,
    status: 200,
    headers: new Headers({ 'content-type': 'application/json' }),
    json: async () => ({
      success: true,
      action: 'ALLOW',
      confidence: 0.42, // Below 0.6 threshold!
      category: 'uncertain',
      reason: 'Not sure if productive or distracting'
    })
  });

  const client = new FocusApiClient({
    deviceToken: 'valid_token',
    fetchFn: lowConfidenceFetch
  });

  const res = await client.classifyUrl({
    url: 'https://ambiguous-forum.net',
    title: 'Ambiguous Forum'
  });

  assert.equal(res.action, 'BLOCK');
  assert.equal(res.failClosed, true);

  const verdict = evaluateVerdict({
    url: 'https://ambiguous-forum.net',
    focusActive: true,
    classificationResult: res
  });

  assert.equal(verdict.action, 'BLOCK');
});

test('fail-closed: backend HTTP 500 error returns BLOCK verdict', async () => {
  const error500Fetch = async () => ({
    ok: false,
    status: 500,
    headers: new Headers({ 'content-type': 'application/json' }),
    json: async () => ({
      error: 'internal_server_error',
      message: 'Groq AI quota exceeded'
    })
  });

  const client = new FocusApiClient({
    deviceToken: 'valid_token',
    fetchFn: error500Fetch
  });

  const res = await client.classifyUrl({
    url: 'https://some-page.com',
    title: 'Some Page'
  });

  assert.equal(res.action, 'BLOCK');
  assert.equal(res.failClosed, true);
});

test('fail-closed: hard-blocked domain overrides remote allow', () => {
  // Even if a compromised or buggy backend classifier returns ALLOW for youtube or tiktok:
  const verdict = evaluateVerdict({
    url: 'https://youtube.com/feed/explore',
    focusActive: true,
    classificationResult: {
      action: 'ALLOW',
      confidence: 1.0,
      reason: 'Should be blocked anyway'
    }
  });

  assert.equal(verdict.action, 'BLOCK');
  assert.equal(verdict.reason, 'hard_blocked');
});
