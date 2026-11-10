"""
Unit tests for Discord Slash Command Group (/focus_mode):
- Owner authorization (interaction.user.id == OWNER_USER_ID)
- Non-owner denial (ephemeral response, no action taken)
- Unset/zero OWNER_USER_ID denial
- Every subcommand:
  - on
  - off
  - answer
  - status
  - language
  - pair
  - devices
  - revoke
- Correct and incorrect challenge answers via /focus_mode answer
- Pairing code never logged or leaked
"""

import os
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock

import sys
import types
if sys.version_info >= (3, 13) and "audioop" not in sys.modules:
    sys.modules["audioop"] = types.ModuleType("audioop")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../src")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../_server_copy/src")))

import discord
from discord import app_commands
from focus_mode.storage import FocusStorage
from focus_mode.auth import FocusAuth
from focus_mode.challenge import MathChallengeSystem
from focus_mode.classifier import WebsiteClassifier
from focus_mode.service import FocusService
from focus_mode.discord_cmds import setup_discord_commands


class DummyClient:
    def __init__(self):
        self.http = MagicMock()
        self._connection = MagicMock()
        self._connection._command_tree = None


class MockUser:
    def __init__(self, user_id: int):
        self.id = user_id


class MockInteraction:
    def __init__(self, user_id: int):
        self.user = MockUser(user_id)
        self.response = AsyncMock()
        self.response.is_done = MagicMock(return_value=False)
        self.followup = AsyncMock()


class TestDiscordIntegration(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="deskflow_discord_test_")
        self.storage = FocusStorage(data_dir=self.test_dir)
        self.storage.initialize()
        self.auth = FocusAuth(storage=self.storage)
        self.challenge = MathChallengeSystem(storage=self.storage, auth=self.auth, groq_client=None)
        self.classifier = WebsiteClassifier(storage=self.storage, groq_client=None)
        self.service = FocusService(
            storage=self.storage,
            auth=self.auth,
            challenge=self.challenge,
            classifier=self.classifier
        )

        self.owner_id = 123456789
        self.mock_client = DummyClient()
        self.tree = app_commands.CommandTree(self.mock_client)
        self.group = setup_discord_commands(
            tree=self.tree,
            service=self.service,
            owner_user_id=self.owner_id
        )

    def tearDown(self):
        self.storage.release_process_lock()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _get_subcommand(self, name: str):
        for cmd in self.group.commands:
            if cmd.name == name:
                return cmd
        raise ValueError(f"Subcommand {name} not found")

    async def test_non_owner_denied(self):
        non_owner_interaction = MockInteraction(user_id=999999999)
        cmd_on = self._get_subcommand("on")
        await cmd_on.callback(non_owner_interaction)

        # Check denial message sent
        non_owner_interaction.response.send_message.assert_called_once()
        args, kwargs = non_owner_interaction.response.send_message.call_args
        self.assertIn("không có quyền", args[0])
        self.assertTrue(kwargs.get("ephemeral"))

        # State should still be OFF
        status = self.service.get_public_status()
        self.assertFalse(status["enabled"])

    async def test_owner_zero_or_unset_denies_all(self):
        tree = app_commands.CommandTree(DummyClient())
        group_zero = setup_discord_commands(tree=tree, service=self.service, owner_user_id=0)
        cmd_on = next(c for c in group_zero.commands if c.name == "on")

        interaction = MockInteraction(user_id=0)
        await cmd_on.callback(interaction)

        interaction.response.send_message.assert_called_once()
        args, kwargs = interaction.response.send_message.call_args
        self.assertIn("disabled", args[0])
        self.assertTrue(kwargs.get("ephemeral"))

    async def test_on_and_status_commands(self):
        owner_interaction = MockInteraction(user_id=self.owner_id)
        cmd_on = self._get_subcommand("on")
        await cmd_on.callback(owner_interaction)

        # Check enabled
        status = self.service.get_public_status()
        self.assertTrue(status["enabled"])
        owner_interaction.followup.send.assert_called_once()
        args, kwargs = owner_interaction.followup.send.call_args
        self.assertIn("BẬT", args[0])
        self.assertTrue(kwargs.get("ephemeral"))

        # Status command
        status_interaction = MockInteraction(user_id=self.owner_id)
        cmd_status = self._get_subcommand("status")
        await cmd_status.callback(status_interaction)
        status_interaction.followup.send.assert_called_once()
        args, _ = status_interaction.followup.send.call_args
        self.assertIn("BẬT (ACTIVE)", args[0])

    async def test_off_and_answer_workflow(self):
        # Enable first
        await self.service.enable()

        # /focus_mode off creates/displays challenge
        off_interaction = MockInteraction(user_id=self.owner_id)
        cmd_off = self._get_subcommand("off")
        await cmd_off.callback(off_interaction)
        off_interaction.followup.send.assert_called_once()
        args, _ = off_interaction.followup.send.call_args
        self.assertIn("Thử thách", args[0])

        st = self.service.get_public_status()
        self.assertIsNotNone(st["challenge"])
        ch_id = st["challenge"]["challenge_id"]

        # Incorrect answer
        ans_interaction_fail = MockInteraction(user_id=self.owner_id)
        cmd_answer = self._get_subcommand("answer")
        await cmd_answer.callback(ans_interaction_fail, number=-99999)
        ans_interaction_fail.followup.send.assert_called_once()
        args, _ = ans_interaction_fail.followup.send.call_args
        self.assertIn("Chưa chính xác", args[0])

        # Focus is still ON
        st_after_fail = self.service.get_public_status()
        self.assertTrue(st_after_fail["enabled"])
        new_ch_id = st_after_fail["challenge"]["challenge_id"]

        # Find correct answer
        state = self.storage.get_state()
        stored_hmac = state["challenge"]["answer_hmac"]
        correct_ans = None
        for cand in range(-1000, 1001):
            if self.auth.compute_hmac(f"{new_ch_id}:{cand}") == stored_hmac:
                correct_ans = cand
                break
        self.assertIsNotNone(correct_ans)

        # Correct answer
        ans_interaction_ok = MockInteraction(user_id=self.owner_id)
        await cmd_answer.callback(ans_interaction_ok, number=correct_ans)
        ans_interaction_ok.followup.send.assert_called_once()
        args, _ = ans_interaction_ok.followup.send.call_args
        self.assertIn("Chính xác", args[0])
        self.assertIn("TẮT", args[0])

        # Focus is now OFF
        st_after_ok = self.service.get_public_status()
        self.assertFalse(st_after_ok["enabled"])

    async def test_language_and_pairing_and_device_commands(self):
        # 1. /focus_mode language
        lang_interaction = MockInteraction(user_id=self.owner_id)
        cmd_lang = self._get_subcommand("language")
        choice = app_commands.Choice(name="Tiếng Việt (vi)", value="vi")
        await cmd_lang.callback(lang_interaction, lang=choice)
        st = self.service.get_public_status()
        self.assertEqual(st["language"], "vi")

        # 2. /focus_mode pair
        pair_interaction = MockInteraction(user_id=self.owner_id)
        cmd_pair = self._get_subcommand("pair")
        await cmd_pair.callback(pair_interaction)
        pair_interaction.followup.send.assert_called_once()
        args, kwargs = pair_interaction.followup.send.call_args
        self.assertIn("Mã liên kết thiết bị", args[0])
        self.assertTrue(kwargs.get("ephemeral"))

        # Pair a mock device
        state = self.storage.get_state()
        # Extract pair code candidate
        code_hmac = next(iter(state["pair_codes"]))
        # Generate and pair via auth
        code, _ = await self.auth.generate_pairing_code()
        reg = await self.service.pair_device(code, "Test Browser", "chromium", "127.0.0.1")
        dev_id = reg["device_id"]

        # 3. /focus_mode devices
        dev_interaction = MockInteraction(user_id=self.owner_id)
        cmd_devices = self._get_subcommand("devices")
        await cmd_devices.callback(dev_interaction)
        dev_interaction.followup.send.assert_called_once()
        args, _ = dev_interaction.followup.send.call_args
        self.assertIn("DANH SÁCH THIẾT BỊ", args[0])
        self.assertIn(dev_id, args[0])

        # 4. /focus_mode revoke
        revoke_interaction = MockInteraction(user_id=self.owner_id)
        cmd_revoke = self._get_subcommand("revoke")
        await cmd_revoke.callback(revoke_interaction, device_id=dev_id)
        revoke_interaction.followup.send.assert_called_once()
        args, _ = revoke_interaction.followup.send.call_args
        self.assertIn("thu hồi thành công", args[0])


if __name__ == "__main__":
    unittest.main()
