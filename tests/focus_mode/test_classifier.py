"""
Unit tests for WebsiteClassifier:
- Hard blocklist: chatgpt.com, chat.openai.com, gemini.google.com, loigiaihay.com and subdomains
- Lookalike domains
- Verifying openai.com and google.com are NOT globally blocked
- Deny policies and Allow policies evaluation
- Initial empty allowlist
- URL normalization (schemes, IDNA, trailing dots, userinfo, drop query string)
- Bounded TTL cache behavior
- Fail-closed security: Groq timeout -> block, Groq exception -> block, malformed -> block, uncertain -> block
- Focus OFF behavior -> allowed with reason focus_off without calling Groq
"""

import os
import shutil
import tempfile
import asyncio
import unittest
from unittest.mock import MagicMock

import sys
import types
if sys.version_info >= (3, 13) and "audioop" not in sys.modules:
    sys.modules["audioop"] = types.ModuleType("audioop")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../src")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../_server_copy/src")))

from focus_mode.storage import FocusStorage
from focus_mode.classifier import (
    WebsiteClassifier,
    HARD_BLOCKED_DOMAINS,
    TRUSTED_EDUCATIONAL_HOSTS,
    DEFAULT_TRUSTED_DOMAINS
)


class TestWebsiteClassifier(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="deskflow_classifier_test_")
        self.storage = FocusStorage(data_dir=self.test_dir)
        self.storage.initialize()
        self.classifier = WebsiteClassifier(storage=self.storage, groq_client=None)

    def tearDown(self):
        self.storage.release_process_lock()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    async def _enable_focus(self):
        def _en(st):
            st["enabled"] = True
            st["version"] = 1
        await self.storage.mutate(_en)

    async def test_focus_off_returns_allowed_without_calling_groq(self):
        mock_groq = MagicMock()
        clf = WebsiteClassifier(storage=self.storage, groq_client=mock_groq)

        # Focus is OFF
        res = await clf.classify("https://youtube.com/watch?v=123")
        self.assertEqual(res["action"], "allow")
        self.assertEqual(res["classification"], "allowed")
        self.assertEqual(res["reason"], "focus_off")
        mock_groq.chat.completions.create.assert_not_called()

    async def test_url_normalization(self):
        # HTTP and HTTPS accepted
        host, url_no_q, _ = self.classifier.normalize_url("https://USER:PASS@en.wikipedia.org:443/wiki/Calculus?ref=google#top")
        self.assertEqual(host, "en.wikipedia.org")
        self.assertEqual(url_no_q, "https://en.wikipedia.org/wiki/Calculus")

        # Trailing dot stripped
        host, url_no_q, _ = self.classifier.normalize_url("http://example.com./page")
        self.assertEqual(host, "example.com")

        # Unsupported scheme rejected
        with self.assertRaises(ValueError):
            self.classifier.normalize_url("file:///etc/passwd")

        with self.assertRaises(ValueError):
            self.classifier.normalize_url("javascript:alert(1)")

    async def test_hard_blocklist_and_subdomains(self):
        await self._enable_focus()

        # Direct hard-blocked domains
        for domain in HARD_BLOCKED_DOMAINS:
            res = await self.classifier.classify(f"https://{domain}/path")
            self.assertEqual(res["action"], "block", f"Failed for {domain}")
            self.assertEqual(res["classification"], "blocked")
            self.assertEqual(res["reason"], "hard_blocked")

        # Subdomains
        res = await self.classifier.classify("https://sub.chatgpt.com/test")
        self.assertEqual(res["action"], "block")
        self.assertEqual(res["reason"], "hard_blocked")

        res = await self.classifier.classify("https://auth.chat.openai.com/")
        self.assertEqual(res["action"], "block")
        self.assertEqual(res["reason"], "hard_blocked")

        res = await self.classifier.classify("https://api.gemini.google.com/v1")
        self.assertEqual(res["action"], "block")
        self.assertEqual(res["reason"], "hard_blocked")

    async def test_openai_and_google_not_globally_blocked(self):
        await self._enable_focus()

        # Check domain matching helper directly
        self.assertFalse(self.classifier.is_hard_blocked("openai.com"))
        self.assertFalse(self.classifier.is_hard_blocked("platform.openai.com"))
        self.assertFalse(self.classifier.is_hard_blocked("google.com"))
        self.assertFalse(self.classifier.is_hard_blocked("docs.google.com"))
        self.assertFalse(self.classifier.is_hard_blocked("scholar.google.com"))

        # Lookalike domains should not trigger hard block
        self.assertFalse(self.classifier.is_hard_blocked("notchatgpt.com"))
        self.assertFalse(self.classifier.is_hard_blocked("chatgpt.com.attacker.com"))

    async def test_explicit_policies(self):
        await self._enable_focus()

        def _add_policies(st):
            st["policies"]["deny"] = ["distraction.com", "*.games.net"]
            st["policies"]["allow"] = ["mycourse.edu", "*.school.org"]
        await self.storage.mutate(_add_policies)

        # Deny policy match
        res = await self.classifier.classify("https://distraction.com/feed")
        self.assertEqual(res["action"], "block")
        self.assertEqual(res["reason"], "policy_deny")

        res = await self.classifier.classify("https://arcade.games.net/play")
        self.assertEqual(res["action"], "block")
        self.assertEqual(res["reason"], "policy_deny")

        # Allow policy match
        res = await self.classifier.classify("https://mycourse.edu/math")
        self.assertEqual(res["action"], "allow")
        self.assertEqual(res["reason"], "policy_allow")

        res = await self.classifier.classify("https://physics.school.org/lecture")
        self.assertEqual(res["action"], "allow")
        self.assertEqual(res["reason"], "policy_allow")

        # Hard block cannot be overridden by allow policy
        def _add_override(st):
            st["policies"]["allow"].append("chatgpt.com")
        await self.storage.mutate(_add_override)

        res = await self.classifier.classify("https://chatgpt.com/c/123")
        self.assertEqual(res["action"], "block")
        self.assertEqual(res["reason"], "hard_blocked")

    async def test_cache_behavior(self):
        await self._enable_focus()
        mock_groq = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = "ALLOWED"
        mock_groq.chat.completions.create.return_value.choices = [mock_choice]

        clf = WebsiteClassifier(storage=self.storage, groq_client=mock_groq)

        # First request hits Groq
        res1 = await clf.classify("https://mit.edu/courses")
        self.assertEqual(res1["action"], "allow")
        self.assertEqual(mock_groq.chat.completions.create.call_count, 1)

        # Second identical request hits cache
        res2 = await clf.classify("https://mit.edu/courses")
        self.assertEqual(res2["action"], "allow")
        self.assertEqual(mock_groq.chat.completions.create.call_count, 1)

    async def test_fail_closed_groq_timeout(self):
        await self._enable_focus()

        def _sleep_call(*args, **kwargs):
            import time
            time.sleep(4.0)
            mock_choice = MagicMock()
            mock_choice.message.content = "ALLOWED"
            return MagicMock(choices=[mock_choice])

        mock_groq = MagicMock()
        mock_groq.chat.completions.create.side_effect = _sleep_call
        clf = WebsiteClassifier(storage=self.storage, groq_client=mock_groq)

        res = await clf.classify("https://unknown-site.org/info")
        self.assertEqual(res["action"], "block")
        self.assertEqual(res["classification"], "blocked")

    async def test_fail_closed_groq_exception(self):
        await self._enable_focus()
        mock_groq = MagicMock()
        mock_groq.chat.completions.create.side_effect = Exception("Rate limit on Groq")
        clf = WebsiteClassifier(storage=self.storage, groq_client=mock_groq)

        res = await clf.classify("https://unknown-site.org/info")
        self.assertEqual(res["action"], "block")
        self.assertEqual(res["classification"], "blocked")

    async def test_fail_closed_uncertain_or_malformed_groq_verdict(self):
        await self._enable_focus()
        mock_groq = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = "Maybe educational, maybe distracting"
        mock_groq.chat.completions.create.return_value.choices = [mock_choice]
        clf = WebsiteClassifier(storage=self.storage, groq_client=mock_groq)

        res = await clf.classify("https://unclear-blog.com/post")
        self.assertEqual(res["action"], "block")
        self.assertEqual(res["classification"], "blocked")

    async def test_strict_groq_verdict_parsing(self):
        """
        Verify strict exact parsing for Groq verdict:
        - Only exact 'ALLOWED' is accepted.
        - Only exact 'BLOCKED' is accepted.
        - 'NOT ALLOWED', 'ALLOWED because...', 'BLOCKED because...', empty, and malformed outputs MUST return 'BLOCKED'.
        """
        test_cases = [
            ("ALLOWED", "ALLOWED", "allow"),
            ("BLOCKED", "BLOCKED", "block"),
            ("NOT ALLOWED", "BLOCKED", "block"),
            ("ALLOWED because it is educational", "BLOCKED", "block"),
            ("BLOCKED because it is social media", "BLOCKED", "block"),
            ("", "BLOCKED", "block"),
            ("   \n\t  ", "BLOCKED", "block"),
            (None, "BLOCKED", "block"),
            ('{"verdict": "ALLOWED"}', "BLOCKED", "block"),
            ("I think this is ALLOWED.", "BLOCKED", "block"),
        ]

        await self._enable_focus()

        for raw_output, expected_verdict, expected_action in test_cases:
            mock_groq = MagicMock()
            mock_choice = MagicMock()
            mock_choice.message.content = raw_output
            mock_groq.chat.completions.create.return_value.choices = [mock_choice]
            clf = WebsiteClassifier(storage=self.storage, groq_client=mock_groq)

            # Test internal parser directly
            sync_verdict = clf._classify_with_groq_sync("https://example-test.org/path", "Test Page")
            self.assertEqual(
                sync_verdict,
                expected_verdict,
                f"Failed internal verdict parse for input {repr(raw_output)}"
            )

            # Test full classify pipeline
            res = await clf.classify(f"https://example-{abs(hash(str(raw_output)))}.org/test")
            self.assertEqual(
                res["action"],
                expected_action,
                f"Failed full classify action for input {repr(raw_output)}"
            )
            self.assertEqual(
                res["classification"],
                "allowed" if expected_action == "allow" else "blocked",
                f"Failed full classify classification for input {repr(raw_output)}"
            )

    async def test_every_explicitly_allowed_hostname(self):
        """
        Verify each of the 5 explicit educational hostnames:
        google.com, www.google.com, scholar.google.com, olm.vn, www.olm.vn
        are immediately allowed with reason 'trusted_educational'.
        """
        await self._enable_focus()
        mock_groq = MagicMock()
        clf = WebsiteClassifier(storage=self.storage, groq_client=mock_groq)

        expected_hosts = [
            "google.com",
            "www.google.com",
            "scholar.google.com",
            "olm.vn",
            "www.olm.vn"
        ]

        for host in expected_hosts:
            self.assertTrue(clf.is_trusted_educational_host(host), f"Host {host} should be trusted")
            res = await clf.classify(f"https://{host}/some/path")
            self.assertEqual(res["action"], "allow", f"Failed allow for {host}")
            self.assertEqual(res["classification"], "allowed", f"Failed classification for {host}")
            self.assertEqual(res["reason"], "trusted_educational", f"Failed reason for {host}")

        # Groq client was never called because trusted hosts are resolved immediately
        mock_groq.chat.completions.create.assert_not_called()

    async def test_unlisted_google_subdomains_not_auto_allowed(self):
        """
        Arbitrary Google subdomains (e.g. drive, mail, news, play) must NOT match
        the narrowed allowlist and must NOT be auto-allowed as trusted_educational.
        """
        unlisted_subdomains = [
            "drive.google.com",
            "mail.google.com",
            "news.google.com",
            "play.google.com",
            "photos.google.com",
            "maps.google.com"
        ]

        await self._enable_focus()
        # With no Groq or failing Groq, unlisted subdomains should fail-closed (blocked)
        clf = WebsiteClassifier(storage=self.storage, groq_client=None)

        for sub in unlisted_subdomains:
            self.assertFalse(clf.is_trusted_educational_host(sub), f"{sub} must NOT be trusted")
            res = await clf.classify(f"https://{sub}/test")
            self.assertNotEqual(res.get("reason"), "trusted_educational", f"{sub} must not have trusted_educational reason")
            self.assertEqual(res["action"], "block")

    async def test_google_lookalike_domains_rejected(self):
        """
        Domains like notgoogle.com, google.com.attacker.com, fakegoogle.com must NOT match.
        """
        lookalikes = [
            "notgoogle.com",
            "google.com.attacker.com",
            "fakegoogle.com",
            "google.co",
            "notolm.vn",
            "olm.vn.evil.org"
        ]

        for lk in lookalikes:
            self.assertFalse(self.classifier.is_trusted_educational_host(lk), f"Lookalike {lk} should not be trusted")

    async def test_all_hard_block_domains_strictly_enforced(self):
        """
        Verify all hard-block domains:
        - chatgpt.com (and subdomains)
        - chat.openai.com
        - gemini.google.com
        - loigiaihay.com
        - loigiaohay.com
        gemini.google.com MUST remain blocked despite google.com being in the allowed list.
        """
        await self._enable_focus()
        mock_groq = MagicMock()
        clf = WebsiteClassifier(storage=self.storage, groq_client=mock_groq)

        hard_blocks = [
            "https://chatgpt.com/",
            "https://sub.chatgpt.com/auth",
            "https://chat.openai.com/",
            "https://gemini.google.com/",
            "https://api.gemini.google.com/v1",
            "https://loigiaihay.com/",
            "https://loigiaohay.com/"
        ]

        for hb_url in hard_blocks:
            res = await clf.classify(hb_url)
            self.assertEqual(res["action"], "block", f"Failed hard-block for {hb_url}")
            self.assertEqual(res["classification"], "blocked")
            self.assertEqual(res["reason"], "hard_blocked")

        mock_groq.chat.completions.create.assert_not_called()

    async def test_groq_successful_classification_and_parameters(self):
        """
        Verify that an unlisted productive site calls Groq with max_completion_tokens=1024,
        reasoning_effort='low', and successfully returns ALLOW.
        """
        await self._enable_focus()
        mock_groq = MagicMock()
        mock_choice = MagicMock()
        mock_choice.finish_reason = "stop"
        mock_choice.message.content = "ALLOWED"
        mock_groq.chat.completions.create.return_value.choices = [mock_choice]

        clf = WebsiteClassifier(storage=self.storage, groq_client=mock_groq)
        res = await clf.classify("https://docs.python.org/3/")
        self.assertEqual(res["action"], "allow")
        self.assertEqual(res["classification"], "allowed")
        self.assertEqual(res["reason"], "educational")

        mock_groq.chat.completions.create.assert_called_once()
        _, kwargs = mock_groq.chat.completions.create.call_args
        self.assertEqual(kwargs.get("model"), "openai/gpt-oss-20b")
        self.assertEqual(kwargs.get("max_completion_tokens"), 1024)
        self.assertEqual(kwargs.get("reasoning_effort"), "low")

    async def test_groq_token_exhaustion_handling(self):
        """
        When Groq hits token exhaustion (finish_reason='length'), the classifier
        must treat the output as truncated and fail-closed to BLOCKED.
        """
        await self._enable_focus()
        mock_groq = MagicMock()
        mock_choice = MagicMock()
        mock_choice.finish_reason = "length"
        mock_choice.message.content = ""  # or partial
        mock_groq.chat.completions.create.return_value.choices = [mock_choice]

        clf = WebsiteClassifier(storage=self.storage, groq_client=mock_groq)
        res = await clf.classify("https://some-unlisted-site.org/page")
        self.assertEqual(res["action"], "block")
        self.assertEqual(res["classification"], "blocked")

    async def test_groq_empty_and_malformed_responses(self):
        """
        Verify fail-closed blocking for empty choices, None message, None content,
        empty content, and malformed content.
        """
        await self._enable_focus()

        # Case 1: Empty choices list
        mock_groq1 = MagicMock()
        mock_groq1.chat.completions.create.return_value.choices = []
        clf1 = WebsiteClassifier(storage=self.storage, groq_client=mock_groq1)
        res1 = await clf1.classify("https://unlisted-site-1.org/")
        self.assertEqual(res1["action"], "block")

        # Case 2: Message is None
        mock_groq2 = MagicMock()
        mock_choice2 = MagicMock()
        mock_choice2.finish_reason = "stop"
        mock_choice2.message = None
        mock_groq2.chat.completions.create.return_value.choices = [mock_choice2]
        clf2 = WebsiteClassifier(storage=self.storage, groq_client=mock_groq2)
        res2 = await clf2.classify("https://unlisted-site-2.org/")
        self.assertEqual(res2["action"], "block")

        # Case 3: Content is None
        mock_groq3 = MagicMock()
        mock_choice3 = MagicMock()
        mock_choice3.finish_reason = "stop"
        mock_choice3.message.content = None
        mock_groq3.chat.completions.create.return_value.choices = [mock_choice3]
        clf3 = WebsiteClassifier(storage=self.storage, groq_client=mock_groq3)
        res3 = await clf3.classify("https://unlisted-site-3.org/")
        self.assertEqual(res3["action"], "block")

        # Case 4: Content is empty string
        mock_groq4 = MagicMock()
        mock_choice4 = MagicMock()
        mock_choice4.finish_reason = "stop"
        mock_choice4.message.content = "   "
        mock_groq4.chat.completions.create.return_value.choices = [mock_choice4]
        clf4 = WebsiteClassifier(storage=self.storage, groq_client=mock_groq4)
        res4 = await clf4.classify("https://unlisted-site-4.org/")
        self.assertEqual(res4["action"], "block")

        # Case 5: Content is malformed or conversational
        mock_groq5 = MagicMock()
        mock_choice5 = MagicMock()
        mock_choice5.finish_reason = "stop"
        mock_choice5.message.content = "ALLOWED because it contains educational articles"
        mock_groq5.chat.completions.create.return_value.choices = [mock_choice5]
        clf5 = WebsiteClassifier(storage=self.storage, groq_client=mock_groq5)
        res5 = await clf5.classify("https://unlisted-site-5.org/")
        self.assertEqual(res5["action"], "block")


if __name__ == "__main__":
    unittest.main()
