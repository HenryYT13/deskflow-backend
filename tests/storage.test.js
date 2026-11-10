import test from 'node:test';
import assert from 'node:assert/strict';
import { StorageManager, DEFAULT_API_URL } from '../lib/storage.js';

test('storage: defaults and updateSettings', async () => {
  const sm = new StorageManager();
  await sm.clearAll();

  const settings = await sm.getSettings();
  assert.equal(settings.apiUrl, DEFAULT_API_URL);
  assert.equal(settings.language, 'en');
  assert.equal(settings.pollIntervalSeconds, 15);
  assert.deepEqual(settings.customHardBlocks, []);

  // Update
  await sm.updateSettings({
    language: 'vi',
    pollIntervalSeconds: 30,
    customHardBlocks: ['custom-distraction.com']
  });

  const updated = await sm.getSettings();
  assert.equal(updated.language, 'vi');
  assert.equal(updated.pollIntervalSeconds, 30);
  assert.deepEqual(updated.customHardBlocks, ['custom-distraction.com']);
  assert.equal(updated.apiUrl, DEFAULT_API_URL); // Preserved
});

test('storage: auth management', async () => {
  const sm = new StorageManager();
  await sm.clearAll();

  assert.equal(await sm.getAuth(), null);

  await sm.setAuth({
    deviceToken: 'token_12345',
    deviceId: 'dev_chrome_1',
    deviceName: 'Chrome on Linux'
  });

  const auth = await sm.getAuth();
  assert.ok(auth);
  assert.equal(auth.deviceToken, 'token_12345');
  assert.equal(auth.deviceId, 'dev_chrome_1');
  assert.equal(auth.deviceName, 'Chrome on Linux');

  await sm.clearAuth();
  assert.equal(await sm.getAuth(), null);
});

test('storage: focus state management', async () => {
  const sm = new StorageManager();
  await sm.clearAll();

  const initial = await sm.getFocusState();
  assert.equal(initial.focusActive, false);
  assert.equal(initial.session, null);

  await sm.setFocusState({
    focusActive: true,
    session: {
      session_id: 'sess_1',
      duration_minutes: 25,
      remaining_seconds: 1500,
      reason: 'Calculus'
    }
  });

  const active = await sm.getFocusState();
  assert.equal(active.focusActive, true);
  assert.equal(active.session.reason, 'Calculus');

  // Deactivate
  await sm.setFocusState({ focusActive: false });
  const deactivated = await sm.getFocusState();
  assert.equal(deactivated.focusActive, false);
  assert.equal(deactivated.session, null);
});

test('storage: classification cache with TTL', async () => {
  const sm = new StorageManager();
  await sm.clearAll();

  const url = 'https://study-site.org/notes';
  assert.equal(await sm.getCachedClassification(url), null);

  // Cache ALLOW with positive TTL
  await sm.setCachedClassification(url, 'ALLOW', 'Study resource', 5000);
  const hit = await sm.getCachedClassification(url);
  assert.ok(hit);
  assert.equal(hit.action, 'ALLOW');
  assert.equal(hit.reason, 'Study resource');

  // Expired cache
  await sm.setCachedClassification(url, 'BLOCK', 'Distracting', -100);
  const expired = await sm.getCachedClassification(url);
  assert.equal(expired, null);

  // Clear cache
  await sm.setCachedClassification(url, 'ALLOW', 'Valid', 5000);
  await sm.clearClassificationCache();
  assert.equal(await sm.getCachedClassification(url), null);
});
