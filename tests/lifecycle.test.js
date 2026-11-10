import test from 'node:test';
import assert from 'node:assert/strict';
import { isHardBlocked, isExemptUrl } from '../lib/rules.js';

test('lifecycle: narrow exemptions strictly permit /api/focus and block root duckdns', () => {
  // Loopback focus API paths are exempt
  assert.equal(isExemptUrl('http://127.0.0.1:8787/api/focus/status'), true);
  assert.equal(isExemptUrl('http://localhost:8787/api/focus/classify-url'), true);
  assert.equal(isExemptUrl('https://focus-api.10th012013.duckdns.org/api/focus/status'), true);

  // Production web dashboard is exempt
  assert.equal(isExemptUrl('https://10th012013.duckdns.org/'), true);

  // Non-focus paths on focus-api are NOT exempt
  assert.equal(isExemptUrl('https://focus-api.10th012013.duckdns.org/admin/secret'), false);

  // Arbitrary duckdns subdomains are strictly NOT exempt
  assert.equal(isExemptUrl('https://duckdns.org/'), false);
  assert.equal(isExemptUrl('https://attacker.duckdns.org/'), false);
  assert.equal(isExemptUrl('https://sub.10th012013.duckdns.org/'), false);
});

test('lifecycle: canonical 22 domains are hard blocked', () => {
  const canonical22 = [
    'https://facebook.com',
    'https://instagram.com',
    'https://tiktok.com',
    'https://twitter.com',
    'https://x.com',
    'https://reddit.com',
    'https://youtube.com',
    'https://netflix.com',
    'https://twitch.tv',
    'https://discord.com',
    'https://pinterest.com',
    'https://linkedin.com',
    'https://threads.net',
    'https://bilibili.com',
    'https://weibo.com',
    'https://douyin.com',
    'https://zhihu.com',
    'https://kuaishou.com',
    'https://xiaohongshu.com',
    'https://snapchat.com',
    'https://tumblr.com',
    'https://vk.com'
  ];

  for (const url of canonical22) {
    assert.equal(isHardBlocked(url), true, `Expected ${url} to be hard blocked`);
  }
});
