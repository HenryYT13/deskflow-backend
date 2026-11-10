import test from 'node:test';
import assert from 'node:assert/strict';
import {
  extractHostname,
  isExemptUrl,
  matchesDomain,
  isHardBlocked,
  isTrustedEducationalHost,
  isTrustedDomain,
  evaluateVerdict,
  DEFAULT_HARD_BLOCKED_DOMAINS,
  TRUSTED_EDUCATIONAL_HOSTS,
  DEFAULT_TRUSTED_DOMAINS
} from '../lib/rules.js';

test('rules: extractHostname', () => {
  assert.equal(extractHostname('https://youtube.com/watch?v=123'), 'youtube.com');
  assert.equal(extractHostname('http://m.facebook.com/feed'), 'm.facebook.com');
  assert.equal(extractHostname('https://EN.WIKIPEDIA.ORG/wiki/Math'), 'en.wikipedia.org');
  assert.equal(extractHostname('invalid-url'), null);
  assert.equal(extractHostname(''), null);
  assert.equal(extractHostname(null), null);
});

test('rules: isExemptUrl', () => {
  assert.equal(isExemptUrl('chrome://extensions'), true);
  assert.equal(isExemptUrl('chrome-extension://abc/popup.html'), true);
  assert.equal(isExemptUrl('moz-extension://xyz/options.html'), true);
  assert.equal(isExemptUrl('about:blank'), true);
  assert.equal(isExemptUrl('about:newtab'), true);
  assert.equal(isExemptUrl('devtools://devtools/bundled/inspector.html'), true);
  assert.equal(isExemptUrl('http://127.0.0.1:8787/api/focus/status'), true);
  assert.equal(isExemptUrl('http://localhost:8787/api/focus'), true);
  assert.equal(isExemptUrl('https://focus-api.10th012013.duckdns.org/api/focus/status'), true);
  assert.equal(isExemptUrl('https://10th012013.duckdns.org/'), true);
  assert.equal(isExemptUrl('https://google.com'), false);
  assert.equal(isExemptUrl('https://facebook.com'), false);
});

test('rules: matchesDomain', () => {
  assert.equal(matchesDomain('youtube.com', 'youtube.com'), true);
  assert.equal(matchesDomain('m.youtube.com', 'youtube.com'), true);
  assert.equal(matchesDomain('music.youtube.com', 'youtube.com'), true);
  assert.equal(matchesDomain('notyoutube.com', 'youtube.com'), false);
  assert.equal(matchesDomain('facebook.com', 'tiktok.com'), false);
});

test('rules: isHardBlocked with default and custom domains', () => {
  assert.equal(isHardBlocked('https://www.youtube.com/shorts/abc'), true);
  assert.equal(isHardBlocked('https://m.tiktok.com/@user'), true);
  assert.equal(isHardBlocked('https://reddit.com/r/programming'), true);
  assert.equal(isHardBlocked('https://stackoverflow.com/questions'), false);

  // Custom hard blocks
  const custom = ['9gag.com', 'twitch.tv'];
  assert.equal(isHardBlocked('https://9gag.com/trending', custom), true);
  assert.equal(isHardBlocked('https://www.twitch.tv/directory', custom), true);
});

test('rules: evaluateVerdict with fail-closed security', () => {
  // Case 1: Exempt URL always allowed
  const r1 = evaluateVerdict({
    url: 'chrome://settings',
    focusActive: true,
    classificationResult: null
  });
  assert.equal(r1.action, 'ALLOW');
  assert.equal(r1.reason, 'exempt_url');

  // Case 2: Focus mode inactive always allowed
  const r2 = evaluateVerdict({
    url: 'https://youtube.com',
    focusActive: false,
    classificationResult: null
  });
  assert.equal(r2.action, 'ALLOW');
  assert.equal(r2.reason, 'focus_inactive');

  // Case 3: Hard-blocked domain during focus mode is always blocked
  const r3 = evaluateVerdict({
    url: 'https://youtube.com/watch?v=1',
    focusActive: true,
    classificationResult: { action: 'ALLOW' } // Even if classifier erroneously said allow!
  });
  assert.equal(r3.action, 'BLOCK');
  assert.equal(r3.reason, 'hard_blocked');

  // Case 4: Missing classification result -> FAIL CLOSED
  const r4 = evaluateVerdict({
    url: 'https://unknown-blog.org/article',
    focusActive: true,
    classificationResult: null
  });
  assert.equal(r4.action, 'BLOCK');
  assert.equal(r4.failClosed, true);

  // Case 5: Error in classification -> FAIL CLOSED
  const r5 = evaluateVerdict({
    url: 'https://unknown-blog.org/article',
    focusActive: true,
    classificationResult: { error: 'ECONNREFUSED' }
  });
  assert.equal(r5.action, 'BLOCK');
  assert.equal(r5.failClosed, true);

  // Case 6: Low confidence -> FAIL CLOSED
  const r6 = evaluateVerdict({
    url: 'https://unknown-blog.org/article',
    focusActive: true,
    classificationResult: { action: 'ALLOW', confidence: 0.45 }
  });
  assert.equal(r6.action, 'BLOCK');
  assert.equal(r6.failClosed, true);

  // Case 7: High confidence productive site -> ALLOW
  const r7 = evaluateVerdict({
    url: 'https://en.wikipedia.org/wiki/Calculus',
    focusActive: true,
    classificationResult: { action: 'ALLOW', confidence: 0.95, reason: 'Educational' }
  });
  assert.equal(r7.action, 'ALLOW');
  assert.equal(r7.reason, 'Educational');
});

test('rules: isTrustedEducationalHost exact hostname matching and lookalikes', () => {
  // 1. Every explicitly allowed hostname
  assert.equal(isTrustedEducationalHost('https://google.com/'), true);
  assert.equal(isTrustedEducationalHost('https://www.google.com/search?q=math'), true);
  assert.equal(isTrustedEducationalHost('https://scholar.google.com/citations'), true);
  assert.equal(isTrustedEducationalHost('https://olm.vn/'), true);
  assert.equal(isTrustedEducationalHost('https://www.olm.vn/hoc-bai'), true);

  // 2. Unlisted Google subdomains must NOT be trusted
  assert.equal(isTrustedEducationalHost('https://drive.google.com/'), false);
  assert.equal(isTrustedEducationalHost('https://mail.google.com/'), false);
  assert.equal(isTrustedEducationalHost('https://news.google.com/'), false);
  assert.equal(isTrustedEducationalHost('https://play.google.com/'), false);
  assert.equal(isTrustedEducationalHost('https://photos.google.com/'), false);
  assert.equal(isTrustedEducationalHost('https://maps.google.com/'), false);

  // 3. Lookalikes or other domains are NOT trusted
  assert.equal(isTrustedEducationalHost('https://notgoogle.com/'), false);
  assert.equal(isTrustedEducationalHost('https://google.com.attacker.com/'), false);
  assert.equal(isTrustedEducationalHost('https://fakegoogle.com/'), false);
  assert.equal(isTrustedEducationalHost('https://google.co/'), false);
  assert.equal(isTrustedEducationalHost('https://notolm.vn/'), false);
  assert.equal(isTrustedEducationalHost('https://olm.vn.attacker.com/'), false);
  assert.equal(isTrustedEducationalHost('https://facebook.com/'), false);
  assert.equal(isTrustedEducationalHost('https://chatgpt.com/'), false);

  // 4. Custom trusted host (exact matching)
  const custom = ['khanacademy.org', 'coursera.org'];
  assert.equal(isTrustedEducationalHost('https://khanacademy.org/math', custom), true);
  assert.equal(isTrustedEducationalHost('https://coursera.org/learn', custom), true);
  assert.equal(isTrustedEducationalHost('https://sub.khanacademy.org/', custom), false); // exact only
  assert.equal(isTrustedEducationalHost('https://random.org', custom), false);
});

test('rules: all hard-block domains strictly blocked in extension', () => {
  assert.equal(isHardBlocked('https://chatgpt.com/'), true);
  assert.equal(isHardBlocked('https://sub.chatgpt.com/auth'), true);
  assert.equal(isHardBlocked('https://chat.openai.com/'), true);
  assert.equal(isHardBlocked('https://gemini.google.com/'), true);
  assert.equal(isHardBlocked('https://api.gemini.google.com/v1'), true);
  assert.equal(isHardBlocked('https://loigiaihay.com/'), true);
  assert.equal(isHardBlocked('https://loigiaohay.com/'), true);
});

test('rules: evaluateVerdict with trusted educational hosts and hard-block precedence', () => {
  // Explicitly trusted host is allowed immediately without remote classification result
  const r1 = evaluateVerdict({
    url: 'https://www.google.com/search?q=calculus',
    focusActive: true,
    classificationResult: null
  });
  assert.equal(r1.action, 'ALLOW');
  assert.equal(r1.reason, 'trusted_educational');

  const r2 = evaluateVerdict({
    url: 'https://olm.vn/de-thi',
    focusActive: true,
    classificationResult: null
  });
  assert.equal(r2.action, 'ALLOW');
  assert.equal(r2.reason, 'trusted_educational');

  // Hard-block takes absolute precedence: gemini.google.com is BLOCKED
  const r3 = evaluateVerdict({
    url: 'https://gemini.google.com/',
    focusActive: true,
    classificationResult: null
  });
  assert.equal(r3.action, 'BLOCK');
  assert.equal(r3.reason, 'hard_blocked');

  // Unlisted Google subdomain without classification fails closed (BLOCK)
  const r4 = evaluateVerdict({
    url: 'https://drive.google.com/drive/u/0/my-drive',
    focusActive: true,
    classificationResult: null
  });
  assert.equal(r4.action, 'BLOCK');
  assert.equal(r4.failClosed, true);

  // Unknown domain without classification fails closed (BLOCK)
  const r5 = evaluateVerdict({
    url: 'https://example.com/',
    focusActive: true,
    classificationResult: null
  });
  assert.equal(r5.action, 'BLOCK');
  assert.equal(r5.failClosed, true);
});
