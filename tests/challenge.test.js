import test from 'node:test';
import assert from 'node:assert/strict';
import { FocusApiClient, ApiError } from '../lib/api-client.js';

test('challenge: fetch math challenge and verify answer', async () => {
  const activeChallenges = new Map();

  const mockFetch = async (urlStr, options) => {
    const url = new URL(urlStr);

    if (url.pathname === '/api/focus/challenge') {
      const challengeId = 'ch_' + Math.random().toString(36).substring(2, 9);
      const n1 = 35;
      const n2 = 47;
      activeChallenges.set(challengeId, {
        answer: n1 + n2, // 82
        expiresAt: Date.now() + 60000
      });

      return {
        ok: true,
        status: 200,
        headers: new Headers({ 'content-type': 'application/json' }),
        json: async () => ({
          success: true,
          challenge_id: challengeId,
          question: `${n1} + ${n2}`,
          expires_in: 60,
          expires_at: new Date(Date.now() + 60000).toISOString()
        })
      };
    }

    if (url.pathname === '/api/focus/verify') {
      const body = JSON.parse(options.body);
      const record = activeChallenges.get(body.challenge_id);

      if (!record) {
        return {
          ok: false,
          status: 404,
          headers: new Headers({ 'content-type': 'application/json' }),
          json: async () => ({ success: false, error: 'challenge_not_found', message: 'Challenge not found' })
        };
      }

      if (Date.now() > record.expiresAt) {
        return {
          ok: false,
          status: 400,
          headers: new Headers({ 'content-type': 'application/json' }),
          json: async () => ({ success: false, error: 'challenge_expired', message: 'Challenge has expired' })
        };
      }

      if (body.answer !== record.answer) {
        return {
          ok: false,
          status: 400,
          headers: new Headers({ 'content-type': 'application/json' }),
          json: async () => ({ success: false, error: 'wrong_answer', message: 'Incorrect answer' })
        };
      }

      // Solved! Focus mode disabled
      activeChallenges.delete(body.challenge_id);
      return {
        ok: true,
        status: 200,
        headers: new Headers({ 'content-type': 'application/json' }),
        json: async () => ({
          success: true,
          focus_active: false,
          message: 'Challenge verified successfully. Focus mode disabled.'
        })
      };
    }

    throw new Error('Not found: ' + url.pathname);
  };

  const client = new FocusApiClient({
    deviceToken: 'test_token',
    fetchFn: mockFetch
  });

  // 1. Get challenge
  const challenge = await client.getChallenge();
  assert.ok(challenge.challenge_id);
  assert.equal(challenge.question, '35 + 47');

  // 2. Try wrong answer -> rejected
  await assert.rejects(
    async () => {
      await client.verifyChallenge({
        challengeId: challenge.challenge_id,
        answer: 99
      });
    },
    (err) => err instanceof ApiError && err.details?.error === 'wrong_answer'
  );

  // 3. Submit correct answer -> succeeds, focus_active is false
  const solveRes = await client.verifyChallenge({
    challengeId: challenge.challenge_id,
    answer: 82
  });
  assert.equal(solveRes.success, true);
  assert.equal(solveRes.focus_active, false);
});

test('challenge: rejects non-numeric answers client-side before sending', async () => {
  const client = new FocusApiClient({ deviceToken: 't' });
  await assert.rejects(
    async () => {
      await client.verifyChallenge({
        challengeId: 'ch_1',
        answer: 'not-a-number'
      });
    },
    (err) => err instanceof ApiError && err.code === 'invalid_answer_format'
  );
});
