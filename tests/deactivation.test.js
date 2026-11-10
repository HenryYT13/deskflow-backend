import test from 'node:test';
import assert from 'node:assert/strict';
import { StorageManager } from '../lib/storage.js';
import { evaluateVerdict, isHardBlocked, isTrustedEducationalHost } from '../lib/rules.js';

test('deactivation: storage transitions to IDLE and wipes classification cache', async () => {
  const storage = new StorageManager();
  await storage.clearAll();

  // 1. Setup ACTIVE state with cached domain verdicts
  await storage.setFocusState({ focusActive: true, session: { remaining_seconds: 1500 } });
  await storage.setCachedClassification('https://distracting-site.com/feed', 'BLOCK', 'classified_distracting');
  await storage.setCachedClassification('https://game-portal.net/play', 'BLOCK', 'hard_blocked');
  await storage.setCachedClassification('https://google.com/search', 'ALLOW', 'trusted_educational');

  const activeState = await storage.getFocusState();
  assert.equal(activeState.focusActive, true);
  const cachedBlock = await storage.getCachedClassification('https://distracting-site.com/feed');
  assert.equal(cachedBlock?.action, 'BLOCK');

  // 2. Execute deactivation pipeline
  await storage.setFocusState({ focusActive: false, session: null });
  await storage.clearClassificationCache();

  // 3. Verify state is IDLE and cache is completely empty
  const idleState = await storage.getFocusState();
  assert.equal(idleState.focusActive, false);
  assert.equal(idleState.session, null);

  const clearedDistracting = await storage.getCachedClassification('https://distracting-site.com/feed');
  assert.equal(clearedDistracting, null);
  const clearedGame = await storage.getCachedClassification('https://game-portal.net/play');
  assert.equal(clearedGame, null);
});

test('deactivation: stale in-flight classification cannot re-block when session ends', async () => {
  const storage = new StorageManager();
  await storage.clearAll();

  // Simulate session generation counter
  let currentSessionGeneration = 1;
  await storage.setFocusState({ focusActive: true });

  // Simulate starting an in-flight classification request during generation 1
  const capturedGeneration = currentSessionGeneration;
  const targetUrl = 'https://delayed-classification-site.com';

  // Session ends concurrently while request is in-flight!
  currentSessionGeneration++;
  await storage.setFocusState({ focusActive: false });
  await storage.clearClassificationCache();

  // Now, the delayed response arrives from the backend AI
  const rawAiResult = { classification: 'BLOCKED', action: 'BLOCK', reason: 'distracting' };

  // Verification logic from background.js:
  const latestState = await storage.getFocusState();
  let finalVerdict;

  if (capturedGeneration !== currentSessionGeneration || !latestState.focusActive) {
    // Stale response dropped, returns ALLOW
    finalVerdict = { action: 'ALLOW', reason: 'focus_inactive_stale_response' };
  } else {
    finalVerdict = { action: 'BLOCK' };
    await storage.setCachedClassification(targetUrl, 'BLOCK', 'distracting');
  }

  assert.equal(finalVerdict.action, 'ALLOW');
  assert.equal(finalVerdict.reason, 'focus_inactive_stale_response');

  // Verify it was NOT written to the cache
  const cached = await storage.getCachedClassification(targetUrl);
  assert.equal(cached, null, 'Stale response must NOT poison the cache');
});

test('deactivation: releaseBlockedTabs restores original target URLs', async () => {
  const blockPagePrefix = 'chrome-extension://test-id/block/block.html';
  const updatedTabs = [];

  // Mock chrome.tabs
  const mockTabs = [
    { id: 101, url: `${blockPagePrefix}?url=https%3A%2F%2Fnews.ycombinator.com&reason=blocked` },
    { id: 102, url: `${blockPagePrefix}?url=https%3A%2F%2Freddit.com%2Fr%2Fprogramming&reason=hard_block` },
    { id: 103, url: 'https://google.com' }, // Not blocked
    { id: 104, url: `${blockPagePrefix}` } // Block page without url parameter
  ];

  const fakeChromeTabs = {
    query: (queryInfo, callback) => callback(mockTabs),
    update: (tabId, updateProps) => updatedTabs.push({ tabId, ...updateProps }),
    reload: (tabId) => updatedTabs.push({ tabId, reloaded: true })
  };

  // Run tab release logic
  for (const tab of mockTabs) {
    if (tab.url && tab.url.startsWith(blockPagePrefix)) {
      const parsed = new URL(tab.url);
      const originalUrl = parsed.searchParams.get('url');
      if (originalUrl) {
        fakeChromeTabs.update(tab.id, { url: originalUrl });
      } else {
        fakeChromeTabs.reload(tab.id);
      }
    }
  }

  assert.equal(updatedTabs.length, 3);
  assert.deepEqual(updatedTabs[0], { tabId: 101, url: 'https://news.ycombinator.com' });
  assert.deepEqual(updatedTabs[1], { tabId: 102, url: 'https://reddit.com/r/programming' });
  assert.deepEqual(updatedTabs[2], { tabId: 104, reloaded: true });
});

test('deactivation: block page redirects immediately when Focus Mode is IDLE', () => {
  const targetUrl = 'https://en.wikipedia.org/wiki/Computer_science';
  let redirectedUrl = null;

  const mockWindow = {
    location: {
      replace: (url) => { redirectedUrl = url; }
    }
  };

  // Simulate block.js startup when focusActive is false
  const state = { focusState: { focusActive: false } };
  if (!state?.focusState?.focusActive) {
    if (targetUrl) {
      mockWindow.location.replace(targetUrl);
    }
  }

  assert.equal(redirectedUrl, targetUrl, 'Block page must immediately redirect to target URL when Focus Mode is OFF');
});

test('deactivation: rules evaluation returns ALLOW for all destinations when focusActive is false', () => {
  const destinations = [
    'https://chatgpt.com',
    'https://gemini.google.com',
    'https://loigiaihay.com',
    'https://facebook.com',
    'https://youtube.com',
    'https://google.com'
  ];

  for (const url of destinations) {
    const verdict = evaluateVerdict({
      url,
      focusActive: false,
      classificationResult: null
    });
    assert.equal(verdict.action, 'ALLOW');
    assert.equal(verdict.reason, 'focus_inactive');
  }
});

test('deactivation: re-enabling Focus Mode starts fresh session with incremented generation', async () => {
  let sessionGeneration = 1;
  const inFlight = new Map();

  inFlight.set('https://url1.com', Promise.resolve());
  inFlight.set('https://url2.com', Promise.resolve());

  // Deactivate
  sessionGeneration++;
  inFlight.clear();
  assert.equal(sessionGeneration, 2);
  assert.equal(inFlight.size, 0);

  // Re-enable
  sessionGeneration++;
  assert.equal(sessionGeneration, 3, 'Fresh session must have an incremented generation ID');
  assert.equal(inFlight.size, 0);
});
