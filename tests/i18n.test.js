import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { TRANSLATIONS, I18nManager } from '../lib/i18n.js';

test('i18n: complete 1:1 key parity between English and Vietnamese', () => {
  const enKeys = Object.keys(TRANSLATIONS.en).sort();
  const viKeys = Object.keys(TRANSLATIONS.vi).sort();

  const missingInVi = enKeys.filter((k) => !viKeys.includes(k));
  const missingInEn = viKeys.filter((k) => !enKeys.includes(k));

  assert.deepEqual(
    missingInVi,
    [],
    `Keys missing in Vietnamese dictionary: ${missingInVi.join(', ')}`
  );

  assert.deepEqual(
    missingInEn,
    [],
    `Keys missing in English dictionary: ${missingInEn.join(', ')}`
  );
});

test('i18n: translation interpolation', () => {
  const mgr = new I18nManager('en');

  const formatted = mgr.t('challenge_expires_in', { seconds: 45 });
  assert.equal(formatted, 'Expires in: 45s');

  mgr.setLanguage('vi');
  const formattedVi = mgr.t('challenge_expires_in', { seconds: 45 });
  assert.equal(formattedVi, 'Hết hạn sau: 45s');

  const sourceMsg = mgr.t('session_started_by', { source: 'DISCORD' });
  assert.equal(sourceMsg, 'Nguồn: DISCORD');
});

test('i18n: browser extension _locales files validity and parity', () => {
  const enPath = path.resolve('_locales/en/messages.json');
  const viPath = path.resolve('_locales/vi/messages.json');

  assert.ok(fs.existsSync(enPath), '_locales/en/messages.json exists');
  assert.ok(fs.existsSync(viPath), '_locales/vi/messages.json exists');

  const enJson = JSON.parse(fs.readFileSync(enPath, 'utf-8'));
  const viJson = JSON.parse(fs.readFileSync(viPath, 'utf-8'));

  assert.ok(enJson.extensionName?.message);
  assert.ok(viJson.extensionName?.message);
  assert.ok(enJson.extensionDescription?.message);
  assert.ok(viJson.extensionDescription?.message);
});
