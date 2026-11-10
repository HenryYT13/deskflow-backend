"""
DeskFlow Focus Mode - Discord Slash Command Group
Phase 8 implementation:
- Group: /app
- Subcommands: on, off, answer, status, language, pair, devices, revoke
- Strict owner authorization check (interaction.user.id == OWNER_USER_ID)
- If OWNER_USER_ID is 0 or unset -> deny all commands, do not expose configuration
- Ephemeral responses for all interactions
- /app off requests/displays math challenge (NEVER directly disables Focus Mode)
- /app answer verifies challenge
- /app pair generates one-time 8-char code (ephemeral only, NEVER logged)
- Existing /solve command is completely untouched
"""

import discord
from discord import app_commands
import logging
from typing import Optional, Any

from .service import FocusService
from .storage import StorageUnavailableError
from .challenge import ChallengeError, CooldownActiveError, ChallengeNotActiveError

logger = logging.getLogger("DeskFlow.FocusMode.Discord")


def setup_discord_commands(
    tree: app_commands.CommandTree,
    service: FocusService,
    owner_user_id: int
) -> app_commands.Group:
    """
    Registers the /app command group on the provided Discord CommandTree.
    """
    focus_group = app_commands.Group(
        name="app",
        description="Quản lý và điều khiển chế độ tập trung Focus Mode"
    )

    async def _owner_check(interaction: discord.Interaction) -> bool:
        if not owner_user_id or owner_user_id <= 0:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ Focus Mode is disabled because OWNER_USER_ID is not configured.",
                    ephemeral=True
                )
            return False

        if interaction.user.id != owner_user_id:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "⛔ Bạn không có quyền sử dụng lệnh Focus Mode (Chỉ dành cho Owner).",
                    ephemeral=True
                )
            return False
        return True

    @focus_group.command(name="on", description="Bật chế độ tập trung Focus Mode")
    @app_commands.describe(
        duration="Thời gian tập trung (phút, mặc định 25)",
        reason="Mục tiêu bài học/công việc"
    )
    async def cmd_on(
        interaction: discord.Interaction,
        duration: Optional[int] = 25,
        reason: Optional[str] = "Study Session"
    ):
        if not await _owner_check(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            status = await service.start_session(
                session_type="focus",
                duration_minutes=duration or 25,
                break_minutes=0,
                reason=reason or "Study Session"
            )
            rem_str = f"{status.get('remaining_seconds', 0) // 60}m" if status.get('remaining_seconds') else "Vô thời hạn"
            await interaction.followup.send(
                f"🔒 **Focus Mode đã BẬT!**\n"
                f"• Thời gian: `{rem_str}`\n"
                f"• Mục tiêu: *{reason}*\n"
                f"Tất cả các trang web gây xao nhãng sẽ bị chặn trên trình duyệt.",
                ephemeral=True
            )
        except StorageUnavailableError:
            await interaction.followup.send("⚠️ Lỗi lưu trữ: Bộ nhớ Focus Mode hiện không khả dụng.", ephemeral=True)
        except Exception as e:
            logger.error(f"[Discord] Error in /app on: {e}", exc_info=True)
            await interaction.followup.send("❌ Có lỗi xảy ra khi bật Focus Mode.", ephemeral=True)

    @focus_group.command(name="off", description="Yêu cầu thử thách toán học để tắt Focus Mode")
    async def cmd_off(interaction: discord.Interaction):
        if not await _owner_check(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            ch = await service.request_challenge()
            await interaction.followup.send(
                f"🧮 **Thử thách tắt Focus Mode:**\n"
                f"> **{ch['question']}**\n\n"
                f"⏱️ Hết hạn trong: **{ch.get('expires_in', 120)} giây**\n"
                f"Dùng lệnh: `/app answer <đáp án>` để gửi câu trả lời.",
                ephemeral=True
            )
        except CooldownActiveError as cde:
            await interaction.followup.send(
                f"⏳ **Hệ thống đang trong thời gian chờ (Cooldown)!**\n"
                f"Bạn đã trả lời sai quá 5 lần. Vui lòng thử lại sau **{cde.retry_after} giây**.",
                ephemeral=True
            )
        except ChallengeError as ce:
            if ce.code == "focus_not_active":
                await interaction.followup.send("ℹ️ Focus Mode hiện đang **TẮT**.", ephemeral=True)
            else:
                await interaction.followup.send(f"❌ Không thể tạo thử thách: `{ce.code}`", ephemeral=True)
        except StorageUnavailableError:
            await interaction.followup.send("⚠️ Lỗi lưu trữ: Bộ nhớ Focus Mode hiện không khả dụng.", ephemeral=True)
        except Exception as e:
            logger.error(f"[Discord] Error in /app off: {e}", exc_info=True)
            await interaction.followup.send("❌ Có lỗi xảy ra khi yêu cầu thử thách toán học.", ephemeral=True)

    @focus_group.command(name="answer", description="Nhập đáp án thử thách toán để tắt Focus Mode")
    @app_commands.describe(number="Đáp án số của câu hỏi toán")
    async def cmd_answer(interaction: discord.Interaction, number: int):
        if not await _owner_check(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            status = service.get_public_status()
            ch_info = status.get("challenge")
            if not ch_info or not ch_info.get("challenge_id"):
                await interaction.followup.send("❌ Không có thử thách toán học nào đang hoạt động. Hãy dùng `/app off` để tạo câu hỏi mới.", ephemeral=True)
                return

            ch_id = ch_info["challenge_id"]
            res = await service.verify_challenge(challenge_id=ch_id, answer=number)
            if res.get("correct"):
                await interaction.followup.send(
                    f"🎉 **Chính xác!** Đáp án `{number}` đúng.\n"
                    f"🔓 **Focus Mode đã TẮT thành công trên tất cả thiết bị.** (Version: {res['version']})",
                    ephemeral=True
                )
            else:
                new_ch = res.get("new_challenge", {})
                await interaction.followup.send(
                    f"❌ **Chưa chính xác!** Đáp án `{number}` không đúng.\n"
                    f"Focus Mode vẫn đang BẬT.\n\n"
                    f"🧮 **Thử thách thay thế mới:**\n"
                    f"> **{new_ch.get('question', '')}**\n"
                    f"Gõ `/app answer <đáp án>` để giải lại.",
                    ephemeral=True
                )
        except CooldownActiveError as cde:
            await interaction.followup.send(
                f"⏳ **Hệ thống đang cooldown ({cde.retry_after}s)!** Trả lời sai quá nhiều lần.",
                ephemeral=True
            )
        except ChallengeNotActiveError:
            await interaction.followup.send(
                "⚠️ Thử thách đã hết hạn hoặc đã được giải. Hãy gõ `/app off` để nhận câu hỏi mới.",
                ephemeral=True
            )
        except StorageUnavailableError:
            await interaction.followup.send("⚠️ Lỗi lưu trữ: Bộ nhớ Focus Mode hiện không khả dụng.", ephemeral=True)
        except Exception as e:
            logger.error(f"[Discord] Error in /app answer: {e}", exc_info=True)
            await interaction.followup.send("❌ Có lỗi xảy ra khi xác thực đáp án.", ephemeral=True)

    @focus_group.command(name="status", description="Xem trạng thái hiện tại của Focus Mode")
    async def cmd_status(interaction: discord.Interaction):
        if not await _owner_check(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            status = service.get_public_status()
            is_enabled = status.get("enabled", False)
            state_str = "🔒 **BẬT (ACTIVE)**" if is_enabled else "🔓 **TẮT (INACTIVE)**"
            ch = status.get("challenge")
            ch_str = f"Đang hoạt động (ID: `{ch.get('challenge_id')}`)" if ch else "Không có"

            phase_str = status.get("phase", "IDLE")
            sess_type = status.get("session_type", "none")
            rem_sec = status.get("remaining_seconds")
            rem_str = f"{rem_sec // 60}m {rem_sec % 60}s" if rem_sec is not None else "Vô thời hạn"
            cycle_cnt = status.get("cycle_count", 0)

            devices = service.get_paired_devices()
            dev_count = len(devices)

            msg = (
                f"📊 **TRẠNG THÁI FOCUS MODE**\n"
                f"• Trạng thái: {state_str}\n"
                f"• Chế độ: `{sess_type.upper()}` | Phase: `{phase_str}`\n"
                f"• Thời gian còn lại: `{rem_str}`" + (f" | Cycle: `{cycle_cnt}`" if sess_type == "pomodoro" else "") + "\n"
                f"• Version: `{status.get('version', 0)}`\n"
                f"• Thử thách toán: {ch_str}\n"
                f"• Thiết bị đã kết nối: `{dev_count}` thiết bị"
            )
            await interaction.followup.send(msg, ephemeral=True)
        except StorageUnavailableError:
            await interaction.followup.send("⚠️ Lỗi lưu trữ: Bộ nhớ Focus Mode hiện không khả dụng.", ephemeral=True)
        except Exception as e:
            logger.error(f"[Discord] Error in /app status: {e}", exc_info=True)
            await interaction.followup.send("❌ Có lỗi xảy ra khi kiểm tra trạng thái.", ephemeral=True)

    @focus_group.command(name="language", description="Cài đặt ngôn ngữ hiển thị (en hoặc vi)")
    @app_commands.describe(lang="Ngôn ngữ lựa chọn")
    @app_commands.choices(lang=[
        app_commands.Choice(name="English (en)", value="en"),
        app_commands.Choice(name="Tiếng Việt (vi)", value="vi")
    ])
    async def cmd_language(interaction: discord.Interaction, lang: app_commands.Choice[str]):
        if not await _owner_check(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            status = await service.set_language(lang.value)
            await interaction.followup.send(f"🌐 Đã đổi ngôn ngữ Focus Mode sang: **{lang.name}**", ephemeral=True)
        except StorageUnavailableError:
            await interaction.followup.send("⚠️ Lỗi lưu trữ: Bộ nhớ Focus Mode hiện không khả dụng.", ephemeral=True)
        except Exception as e:
            logger.error(f"[Discord] Error in /app language: {e}", exc_info=True)
            await interaction.followup.send("❌ Có lỗi khi đổi ngôn ngữ.", ephemeral=True)

    @focus_group.command(name="pair", description="Tạo mã liên kết thiết bị DeskFlow (Một lần dùng, 10 phút)")
    @app_commands.describe(full_access="Cấp toàn quyền điều khiển Control & Spotify cho Android App (Mặc định: False)")
    async def cmd_pair(interaction: discord.Interaction, full_access: Optional[bool] = False):
        if not await _owner_check(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            scopes = ["focus", "control", "spotify"] if full_access else ["focus"]
            raw_code, expires_at = await service.auth.generate_pairing_code(scopes=scopes)
            scope_desc = "Toàn quyền (Focus, Control, Spotify)" if full_access else "Cơ bản (Focus Mode)"
            # Send raw code strictly to owner in ephemeral message. Never logged!
            await interaction.followup.send(
                f"🔑 **Mã liên kết thiết bị:** `{raw_code}`\n"
                f"🛡️ **Quyền cấp phát:** {scope_desc}\n\n"
                f"⚠️ *Mã chỉ có hiệu lực trong 10 phút và sử dụng đúng 1 lần.* Không chia sẻ mã này cho người khác!",
                ephemeral=True
            )
        except StorageUnavailableError:
            await interaction.followup.send("⚠️ Lỗi lưu trữ: Bộ nhớ Focus Mode hiện không khả dụng.", ephemeral=True)
        except Exception as e:
            logger.error(f"[Discord] Error in /app pair: {e}", exc_info=True)
            await interaction.followup.send("❌ Có lỗi khi tạo mã liên kết.", ephemeral=True)

    @focus_group.command(name="devices", description="Xem danh sách các thiết bị đã liên kết")
    async def cmd_devices(interaction: discord.Interaction):
        if not await _owner_check(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            devices = service.get_paired_devices()
            if not devices:
                await interaction.followup.send("📱 Chưa có thiết bị nào được liên kết.", ephemeral=True)
                return

            lines = ["📱 **DANH SÁCH THIẾT BỊ ĐÃ KẾT NỐI:**"]
            for d in devices:
                dev_id = d.get("device_id")
                name = d.get("device_name", "Unknown")
                plat = d.get("platform", "unknown")
                paired = d.get("paired_at", "N/A")
                lines.append(f"• **{name}** (`{dev_id}`) - {plat} (Liên kết: {paired})")

            await interaction.followup.send("\n".join(lines), ephemeral=True)
        except StorageUnavailableError:
            await interaction.followup.send("⚠️ Lỗi lưu trữ: Bộ nhớ Focus Mode hiện không khả dụng.", ephemeral=True)
        except Exception as e:
            logger.error(f"[Discord] Error in /app devices: {e}", exc_info=True)
            await interaction.followup.send("❌ Có lỗi khi lấy danh sách thiết bị.", ephemeral=True)

    @focus_group.command(name="revoke", description="Thu hồi quyền truy cập của một thiết bị")
    @app_commands.describe(device_id="ID của thiết bị cần thu hồi (dev_...)")
    async def cmd_revoke(interaction: discord.Interaction, device_id: str):
        if not await _owner_check(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            success = await service.unpair_device(device_id.strip())
            if success:
                await interaction.followup.send(f"🗑️ Đã thu hồi thành công thiết bị: `{device_id}`", ephemeral=True)
            else:
                await interaction.followup.send(f"⚠️ Không tìm thấy thiết bị với ID: `{device_id}`", ephemeral=True)
        except StorageUnavailableError:
            await interaction.followup.send("⚠️ Lỗi lưu trữ: Bộ nhớ Focus Mode hiện không khả dụng.", ephemeral=True)
        except Exception as e:
            logger.error(f"[Discord] Error in /app revoke: {e}", exc_info=True)
            await interaction.followup.send("❌ Có lỗi khi thu hồi thiết bị.", ephemeral=True)

    tree.add_command(focus_group)

    @tree.command(name="focus_mode", description="[Đã chuyển sang /app] Vui lòng sử dụng /app")
    async def legacy_focus_mode_cmd(interaction: discord.Interaction):
        await interaction.response.send_message(
            "ℹ️ Lệnh `/focus_mode` đã được đổi thành `/app`!\n"
            "Vui lòng sử dụng các lệnh mới:\n"
            "• `/app on` - Bật chế độ tập trung\n"
            "• `/app off` - Tắt chế độ tập trung (giải toán)\n"
            "• `/app answer` - Nộp đáp án thử thách\n"
            "• `/app pair` - Tạo mã liên kết thiết bị\n"
            "• `/app status` - Xem trạng thái\n"
            "• `/app devices` - Danh sách thiết bị",
            ephemeral=True
        )

    return focus_group
