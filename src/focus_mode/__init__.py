"""
DeskFlow Focus Mode - Module Initialization & Lifecycle
Phase 1 & 9 implementation:
- Integrates into existing Discord bot process without replacing or refactoring
- Starts aiohttp HTTP server on 127.0.0.1:8787
- Clean shutdown support
- Strict isolation: NEVER imports main.py
- Fail-safe error handling: port conflict or initialization errors never crash Discord/MQTT
"""

import logging
import asyncio
from typing import Optional, Any
from aiohttp import web

from .storage import FocusStorage, StorageUnavailableError
from .auth import FocusAuth
from .challenge import MathChallengeSystem
from .classifier import WebsiteClassifier
from .service import FocusService
from .api import create_app
from .discord_cmds import setup_discord_commands

logger = logging.getLogger("DeskFlow.FocusMode")

_service_instance: Optional[FocusService] = None
_runner_instance: Optional[web.AppRunner] = None
_site_instance: Optional[web.TCPSite] = None


def get_service() -> Optional[FocusService]:
    return _service_instance


async def init_focus_mode(
    bot: Any,
    groq_client: Optional[Any],
    logger: logging.Logger,
    owner_user_id: int,
    host: str = "127.0.0.1",
    port: int = 8787,
    data_dir: Optional[str] = None
) -> Optional[FocusService]:
    """
    Initializes Focus Mode components, registers Discord commands, and starts aiohttp server.
    Fails safely without raising if ports or storage fail.
    """
    global _service_instance, _runner_instance, _site_instance
    logger.info("[FocusMode] Initializing Focus Mode service...")

    storage = FocusStorage(data_dir=data_dir)
    try:
        storage.initialize()
    except Exception as e:
        logger.error(f"[FocusMode] Storage initialization failed: {e}. Focus Mode marked unavailable.")
        # Proceed with unavailable storage so Discord/MQTT continue unaffected

    auth = FocusAuth(storage=storage)
    challenge = MathChallengeSystem(storage=storage, auth=auth, groq_client=groq_client)
    classifier = WebsiteClassifier(storage=storage, groq_client=groq_client)
    service = FocusService(storage=storage, auth=auth, challenge=challenge, classifier=classifier)
    _service_instance = service

    # Register Discord commands
    try:
        if hasattr(bot, "tree"):
            setup_discord_commands(tree=bot.tree, service=service, owner_user_id=owner_user_id)
            logger.info("[FocusMode] Registered /app Discord command group.")
    except Exception as e:
        logger.error(f"[FocusMode] Failed to register Discord commands: {e}", exc_info=True)

    # Start HTTP server on 127.0.0.1:8787
    try:
        app = create_app(service)
        runner = web.AppRunner(app)
        await runner.setup()
        _runner_instance = runner

        site = web.TCPSite(runner, host=host, port=port)
        await site.start()
        _site_instance = site
        logger.info(f"[FocusMode] REST API listening on http://{host}:{port}/api/focus")
    except OSError as oe:
        logger.error(f"[FocusMode] Failed to bind HTTP server to {host}:{port} ({oe}). Port may be in use.")
        if _runner_instance:
            try:
                await _runner_instance.cleanup()
            except Exception:
                pass
            _runner_instance = None
    except Exception as e:
        logger.error(f"[FocusMode] HTTP server startup failed: {e}", exc_info=True)
        if _runner_instance:
            try:
                await _runner_instance.cleanup()
            except Exception:
                pass
            _runner_instance = None

    return service


async def shutdown_focus_mode() -> None:
    """Cleanly stops HTTP runner during bot shutdown."""
    global _runner_instance, _site_instance, _service_instance
    logger.info("[FocusMode] Shutting down Focus Mode HTTP server...")
    if _runner_instance:
        try:
            await _runner_instance.cleanup()
        except Exception as e:
            logger.warning(f"[FocusMode] Error stopping HTTP runner: {e}")
        _runner_instance = None
        _site_instance = None

    if _service_instance and _service_instance.storage:
        _service_instance.storage.release_process_lock()

    logger.info("[FocusMode] Focus Mode shutdown complete.")
