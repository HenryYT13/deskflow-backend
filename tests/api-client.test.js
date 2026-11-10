import test from 'node:test';
import assert from 'node:assert/strict';
import {
  FocusApiClient,
  ApiError,
  UnauthorizedError,
  OfflineBackendError,
  RateLimitError
} from '../lib/api-client.js';

function createMockFetch(handler) {
  return async (url, options = {}) => {
    return handler(new URL(url), options);
  };
}

test('api-client: POST /pair', async () => {
  let capturedBody = null;
  const mockFetch = createMockFetch((url, options) => {
    assert.equal(url.pathname, '/api/focus/pair');
    assert.equal(options.method, 'POST');
    capturedBody = JSON.parse(options.body);

    return {
      ok: true,
      status: 200,
      headers: new Headers({ 'content-type': 'application/json' }),
      json: async () => ({
        success: true,
        device_token: 'dftk_test_123',
        device_id: 'dev_browser_99',
        device_name: 'Chrome on Linux',
        paired_at: '2026-11-02T06:00:00Z',
        focus_active: false
      })
    };
  });

  const client = new FocusApiClient({ fetchFn: mockFetch });
  const res = await client.pair({
    pairingCode: '123456',
    deviceName: 'Chrome on Linux',
    browser: 'Chrome'
  });

  assert.equal(res.device_token, 'dftk_test_123');
  assert.equal(client.deviceToken, 'dftk_test_123'); // Token set automatically
  assert.equal(capturedBody.pairing_code, '123456');
  assert.equal(capturedBody.device_name, 'Chrome on Linux');
});

test('api-client: GET /status with Bearer auth', async () => {
  let capturedAuth = null;
  const mockFetch = createMockFetch((url, options) => {
    assert.equal(url.pathname, '/api/focus/status');
    assert.equal(options.method, 'GET');
    capturedAuth = options.headers['Authorization'];

    return {
      ok: true,
      status: 200,
      headers: new Headers({ 'content-type': 'application/json' }),
      json: async () => ({
        success: true,
        focus_active: true,
        session: {
          session_id: 'sess_123',
          duration_minutes: 25,
          remaining_seconds: 1400,
          reason: 'Calculus Homework',
          source: 'discord'
        }
      })
    };
  });

  const client = new FocusApiClient({
    deviceToken: 'my_secret_token',
    fetchFn: mockFetch
  });

  const res = await client.getStatus();
  assert.equal(capturedAuth, 'Bearer my_secret_token');
  assert.equal(res.focus_active, true);
  assert.equal(res.session.source, 'discord');
});

test('api-client: POST /enable', async () => {
  let capturedBody = null;
  const mockFetch = createMockFetch((url, options) => {
    assert.equal(url.pathname, '/api/focus/enable');
    assert.equal(options.method, 'POST');
    capturedBody = JSON.parse(options.body);

    return {
      ok: true,
      status: 200,
      headers: new Headers({ 'content-type': 'application/json' }),
      json: async () => ({
        success: true,
        focus_active: true,
        session: {
          session_id: 'sess_456',
          duration_minutes: 45,
          remaining_seconds: 2700,
          reason: 'Coding'
        }
      })
    };
  });

  const client = new FocusApiClient({
    deviceToken: 'token_123',
    fetchFn: mockFetch
  });

  const res = await client.enable({ durationMinutes: 45, reason: 'Coding' });
  assert.equal(res.focus_active, true);
  assert.equal(capturedBody.duration_minutes, 45);
  assert.equal(capturedBody.reason, 'Coding');
});

test('api-client: POST /classify-url (ALLOW and BLOCK)', async () => {
  const mockFetch = createMockFetch((url, options) => {
    assert.equal(url.pathname, '/api/focus/classify-url');
    const body = JSON.parse(options.body);

    if (body.url.includes('wikipedia')) {
      return {
        ok: true,
        status: 200,
        headers: new Headers({ 'content-type': 'application/json' }),
        json: async () => ({
          success: true,
          action: 'ALLOW',
          category: 'education',
          confidence: 0.98,
          reason: 'Encyclopedia content'
        })
      };
    }

    return {
      ok: true,
      status: 200,
      headers: new Headers({ 'content-type': 'application/json' }),
      json: async () => ({
        success: true,
        action: 'BLOCK',
        category: 'entertainment',
        confidence: 0.99,
        reason: 'Social media video feed'
      })
    };
  });

  const client = new FocusApiClient({
    deviceToken: 'token_123',
    fetchFn: mockFetch
  });

  const rAllow = await client.classifyUrl({ url: 'https://en.wikipedia.org/wiki/Euler', title: 'Leonhard Euler' });
  assert.equal(rAllow.action, 'ALLOW');
  assert.equal(rAllow.category, 'education');
  assert.equal(rAllow.failClosed, false);

  const rBlock = await client.classifyUrl({ url: 'https://tiktok.com/@trend', title: 'Trending' });
  assert.equal(rBlock.action, 'BLOCK');
  assert.equal(rBlock.category, 'entertainment');
});

test('api-client: POST /challenge and POST /verify', async () => {
  const mockFetch = createMockFetch((url, options) => {
    if (url.pathname === '/api/focus/challenge') {
      return {
        ok: true,
        status: 200,
        headers: new Headers({ 'content-type': 'application/json' }),
        json: async () => ({
          success: true,
          challenge_id: 'ch_abc999',
          question: '43 + 68',
          expires_in: 60,
          expires_at: '2026-11-02T06:30:00Z'
        })
      };
    }

    if (url.pathname === '/api/focus/verify') {
      const body = JSON.parse(options.body);
      if (body.answer === 111) {
        return {
          ok: true,
          status: 200,
          headers: new Headers({ 'content-type': 'application/json' }),
          json: async () => ({
            success: true,
            focus_active: false,
            message: 'Challenge verified successfully.'
          })
        };
      }
      return {
        ok: false,
        status: 400,
        headers: new Headers({ 'content-type': 'application/json' }),
        json: async () => ({
          success: false,
          error: 'wrong_answer',
          message: 'Incorrect answer.'
        })
      };
    }

    throw new Error('Unexpected route');
  });

  const client = new FocusApiClient({
    deviceToken: 'token_123',
    fetchFn: mockFetch
  });

  // Get challenge
  const challenge = await client.getChallenge();
  assert.equal(challenge.challenge_id, 'ch_abc999');
  assert.equal(challenge.question, '43 + 68');

  // Verify correct answer
  const correctRes = await client.verifyChallenge({
    challengeId: challenge.challenge_id,
    answer: 111
  });
  assert.equal(correctRes.success, true);
  assert.equal(correctRes.focus_active, false);

  // Verify wrong answer
  await assert.rejects(
    async () => {
      await client.verifyChallenge({
        challengeId: challenge.challenge_id,
        answer: 999
      });
    },
    (err) => err instanceof ApiError && err.status === 400
  );
});

test('api-client: GET /devices and POST /unpair', async () => {
  const mockFetch = createMockFetch((url) => {
    if (url.pathname === '/api/focus/devices') {
      return {
        ok: true,
        status: 200,
        headers: new Headers({ 'content-type': 'application/json' }),
        json: async () => ({
          success: true,
          devices: [
            { device_id: 'dev_1', device_name: 'Chrome', device_type: 'browser_extension' },
            { device_id: 'dev_2', device_name: 'ESP32 Station', device_type: 'iot_station' }
          ]
        })
      };
    }

    if (url.pathname === '/api/focus/unpair') {
      return {
        ok: true,
        status: 200,
        headers: new Headers({ 'content-type': 'application/json' }),
        json: async () => ({
          success: true,
          message: 'Device unpaired'
        })
      };
    }
  });

  const client = new FocusApiClient({
    deviceToken: 'token_123',
    fetchFn: mockFetch
  });

  const devicesRes = await client.getDevices();
  assert.equal(devicesRes.devices.length, 2);

  const unpairRes = await client.unpair();
  assert.equal(unpairRes.success, true);
  assert.equal(client.deviceToken, null); // Cleared locally
});

test('api-client: 401 Unauthorized handling', async () => {
  const mockFetch = createMockFetch(() => ({
    ok: false,
    status: 401,
    headers: new Headers(),
    json: async () => ({ error: 'unauthorized' })
  }));

  const client = new FocusApiClient({
    deviceToken: 'expired_token',
    fetchFn: mockFetch
  });

  await assert.rejects(
    async () => client.getStatus(),
    (err) => err instanceof UnauthorizedError && err.status === 401
  );
});

test('api-client: 429 RateLimit handling', async () => {
  const mockFetch = createMockFetch(() => ({
    ok: false,
    status: 429,
    headers: new Headers({ 'Retry-After': '30' }),
    json: async () => ({ error: 'rate_limited' })
  }));

  const client = new FocusApiClient({
    deviceToken: 'token_123',
    fetchFn: mockFetch
  });

  await assert.rejects(
    async () => client.getStatus(),
    (err) => err instanceof RateLimitError && err.status === 429 && err.retryAfter === '30'
  );
});

test('api-client: Offline backend network error handling', async () => {
  const mockFetch = () => {
    throw new Error('fetch failed: ECONNREFUSED');
  };

  const client = new FocusApiClient({
    deviceToken: 'token_123',
    fetchFn: mockFetch
  });

  await assert.rejects(
    async () => client.getStatus(),
    (err) => err instanceof OfflineBackendError
  );
});

test('api-client: testConnection', async () => {
  const liveFetch = createMockFetch(() => ({
    ok: true,
    status: 200,
    headers: new Headers(),
    json: async () => ({ success: true })
  }));

  const liveClient = new FocusApiClient({ fetchFn: liveFetch });
  const liveRes = await liveClient.testConnection('http://127.0.0.1:8787/api/focus');
  assert.equal(liveRes.reachable, true);

  const deadFetch = () => {
    throw new Error('Connection refused');
  };
  const deadClient = new FocusApiClient({ fetchFn: deadFetch });
  const deadRes = await deadClient.testConnection('http://127.0.0.1:8787/api/focus');
  assert.equal(deadRes.reachable, false);
});
