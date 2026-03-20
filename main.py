import asyncio

from astrbot.api import logger, star
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.provider.register import register_provider_adapter
from astrbot.core.provider.entities import LLMResponse, ProviderType
from astrbot.core.provider.provider import Provider
from astrbot.core.star.register import register_regex
import astrbot.core.message.components as Comp
from astrbot.core.message.message_event_result import MessageChain

from .maibot_agent_runner import (
    DEFAULT_PLATFORM,
    DEFAULT_TIMEOUT,
    DEFAULT_WS_URL,
    MAIBOT_PROVIDER_TYPE,
    MaiBotAgentRunner,
)
from .maibot_ws_client import MaiBotWSClient

# Module-level reference to plugin config (set by Main.__init__)
_plugin_config: dict | None = None


def _get_plugin_config() -> dict:
    """Get plugin config, falling back to defaults."""
    if _plugin_config:
        return _plugin_config
    return {}


class MaiBotProvider(Provider):
    """AstrBot Provider that bridges to MaiBot via WebSocket."""

    def __init__(self, provider_config: dict, provider_settings: dict) -> None:
        super().__init__(provider_config, provider_settings)
        self.ws_client: MaiBotWSClient | None = None

    def _load_config(self) -> dict:
        """Read settings from plugin config (or defaults)."""
        cfg = _get_plugin_config()
        return {
            "ws_url": cfg.get("maibot_ws_url", DEFAULT_WS_URL),
            "api_key": cfg.get("maibot_api_key", ""),
            "platform": cfg.get("maibot_platform", DEFAULT_PLATFORM),
            "timeout": cfg.get("maibot_timeout", DEFAULT_TIMEOUT),
            "bot_user_id": cfg.get("maibot_bot_qq", ""),
            "bot_nickname": cfg.get("maibot_bot_nickname", ""),
        }

    def _load_cached_config(self) -> dict:
        """Return cached config dict, creating WS client from latest config if needed."""
        cfg = self._load_config()
        timeout = cfg["timeout"]
        if isinstance(timeout, str):
            timeout = int(timeout)

        # Rebuild WS client if config changed
        if self.ws_client and (
            self.ws_client.ws_url != cfg["ws_url"]
            or self.ws_client.api_key != cfg["api_key"]
            or self.ws_client.platform != cfg["platform"]
            or self.ws_client.timeout != timeout
            or self.ws_client.bot_user_id != cfg["bot_user_id"]
            or self.ws_client.bot_nickname != cfg["bot_nickname"]
        ):
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.ensure_future(self.ws_client.close())
                else:
                    loop.run_until_complete(self.ws_client.close())
            except Exception:
                pass
            self.ws_client = None

        return cfg

    async def _ensure_client(self, cfg: dict) -> MaiBotWSClient:
        """Lazily create or reuse the WebSocket client."""
        if self.ws_client is None:
            timeout = cfg["timeout"]
            if isinstance(timeout, str):
                timeout = int(timeout)
            self.ws_client = MaiBotWSClient(
                ws_url=cfg["ws_url"],
                api_key=cfg["api_key"],
                platform=cfg["platform"],
                timeout=timeout,
                bot_user_id=cfg["bot_user_id"],
                bot_nickname=cfg["bot_nickname"],
            )
        await self.ws_client.ensure_connected()
        return self.ws_client

    def get_current_key(self) -> str:
        return self._load_config()["api_key"]

    def set_key(self, key: str) -> None:
        if _plugin_config is not None:
            _plugin_config["maibot_api_key"] = key

    async def get_models(self) -> list[str]:
        """Return a dummy model name since MaiBot handles model selection internally."""
        return ["maibot"]

    async def text_chat(
        self,
        prompt: str | None = None,
        session_id: str | None = None,
        image_urls: list[str] | None = None,
        func_tool=None,
        contexts=None,
        system_prompt: str | None = None,
        tool_calls_result=None,
        model: str | None = None,
        extra_user_content_parts=None,
        **kwargs,
    ) -> LLMResponse:
        """Forward the chat request to MaiBot via WebSocket."""
        cfg = self._load_cached_config()

        if not cfg["api_key"]:
            return LLMResponse(
                role="assistant",
                completion_text="[MaiBot] 未配置 API Key，请在插件配置中填写 MaiBot 的 API Key。",
            )

        try:
            client = await self._ensure_client(cfg)

            # Parse UMO for group/private context
            is_group = False
            ws_user_id = session_id or "unknown"
            ws_group_id = None
            if session_id:
                parts = session_id.split(":", 2)
                if len(parts) >= 2 and "Group" in parts[1]:
                    is_group = True
                    ws_group_id = session_id

            # Send and wait for MaiBot response
            response_payloads = await client.send_and_receive(
                text=prompt or "",
                user_id=ws_user_id if not is_group else (session_id.split(":")[-1] if session_id else ""),
                user_nickname="",
                group_id=ws_group_id,
                group_name="",
                images=image_urls if image_urls else None,
            )

            # Convert MaiBot response to message chain
            chain_components = []
            for payload in response_payloads:
                segment = payload.get("message_segment", {})
                chain_components.extend(
                    MaiBotAgentRunner._parse_segment_to_components(segment)
                )

            if not chain_components:
                for payload in response_payloads:
                    text = client._extract_text_from_payload(payload)
                    if text:
                        chain_components.append(Comp.Plain(text))

            chain = MessageChain(chain=chain_components)
            return LLMResponse(role="assistant", result_chain=chain)

        except asyncio.TimeoutError:
            return LLMResponse(
                role="err",
                completion_text=f"[MaiBot] 请求超时（{cfg['timeout']}s）。",
            )
        except Exception as e:
            logger.error(f"[MaiBot] text_chat error: {e}", exc_info=True)
            return LLMResponse(
                role="err",
                completion_text=f"[MaiBot] 通信错误：{e}",
            )


@register_provider_adapter(
    MAIBOT_PROVIDER_TYPE,
    "MaiBot - 通过 WebSocket 连接 MaiBot API-Server，替代 AstrBot 内置 LLM Agent",
    provider_type=ProviderType.CHAT_COMPLETION,
    default_config_tmpl={
        "type": MAIBOT_PROVIDER_TYPE,
        "enable": False,
        "id": "maibot_default",
    },
    provider_display_name="MaiBot",
)
class MaiBotProviderEntry(MaiBotProvider):
    """Thin wrapper just for registration — real config comes from _conf_schema.json."""
    pass


class Main(star.Star):
    def __init__(self, context: star.Context, config=None) -> None:
        super().__init__(context)
        self.context = context
        self.config = config

        # Share plugin config to module level so MaiBotProvider can access it
        global _plugin_config
        _plugin_config = config

        logger.info(
            f"[MaiBot Plugin] Initialized. Config: ws_url={config.get('maibot_ws_url') if config else 'N/A'}"
        )

    @register_regex(".*")
    async def _passthrough_to_maibot(self, event: AstrMessageEvent):
        """把所有消息透传给麦麦（fire-and-forget，不产生回复）。

        This handler matches every message but returns None, so it doesn't
        produce any response or interfere with normal pipeline processing.
        """
        # Find any available MaiBot WS client from existing providers
        ws_client = self._get_any_maibot_client()
        if not ws_client:
            return  # MaiBot not configured

        if getattr(event, "is_at_or_wake_command", False):
            return

        umo = event.unified_msg_origin
        if not umo:
            return

        platform_name, message_type, session_id = MaiBotAgentRunner._parse_umo(umo)
        is_group = "Group" in message_type

        text = event.message_str or ""

        # Extract images from event message chain
        image_b64_list: list[str] = []
        if hasattr(event, "message_obj") and event.message_obj:
            for comp in event.message_obj:
                if isinstance(comp, Comp.Image):
                    try:
                        b64 = await comp.convert_to_base64()
                        if b64:
                            image_b64_list.append(b64)
                    except Exception:
                        pass

        if not text.strip() and not image_b64_list:
            return

        sender_id = event.get_sender_id() or session_id
        sender_name = event.get_sender_name() or sender_id

        if is_group:
            ws_user_id = sender_id
            ws_user_nickname = sender_name
            ws_group_id = umo
            ws_group_name = session_id
        else:
            ws_user_id = umo
            ws_user_nickname = sender_name or umo
            ws_group_id = None
            ws_group_name = ""

        asyncio.create_task(ws_client.send_only(
            text=text,
            user_id=ws_user_id,
            user_nickname=ws_user_nickname,
            group_id=ws_group_id,
            group_name=ws_group_name,
            images=image_b64_list or None,
        ))

    def _get_any_maibot_client(self) -> MaiBotWSClient | None:
        """Try to get an active MaiBot WS client from provider instances."""
        try:
            provider_manager = self.context.provider_manager
            for inst_id, inst in provider_manager.inst_map.items():
                if isinstance(inst, MaiBotProvider) and inst.ws_client:
                    return inst.ws_client
        except Exception:
            pass
        return None
