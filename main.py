import asyncio
import json
import time
import uuid

from astrbot.api import logger, star
from astrbot.core.platform import Platform, PlatformMetadata
from astrbot.core.platform.register import register_platform_adapter
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, Group, MessageMember
from astrbot.core.platform.message_session import MessageSession, MessageType
from astrbot.core.platform.message_type import MessageType as MT
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.message.components import Plain, Image
from astrbot.core.star.register import register_regex
from astrbot.core.provider.register import llm_tools

from .maibot_agent_runner import (
    DEFAULT_PLATFORM,
    DEFAULT_TIMEOUT,
    DEFAULT_WS_URL,
    MaiBotAgentRunner,
)
from .maibot_ws_client import MaiBotWSClient

MAIBOT_CONFIG_METADATA = {
    "maibot_ws_url": {
        "description": "MaiBot WebSocket 地址",
        "type": "string",
        "hint": "MaiBot API-Server 的 WebSocket 端点，如 ws://127.0.0.1:18040/ws",
    },
    "maibot_api_key": {
        "description": "MaiBot API Key",
        "type": "string",
        "hint": "MaiBot 配置中对应的 api_key，用于消息路由",
    },
    "maibot_platform": {
        "description": "MaiBot 平台标识",
        "type": "string",
        "hint": "对应 MaiBot 的 platform 参数，默认 astrbot",
    },
    "maibot_timeout": {
        "description": "请求超时时间（秒）",
        "type": "int",
        "hint": "WebSocket 请求超时时间",
    },
    "maibot_bot_qq": {
        "description": "Bot QQ 号",
        "type": "string",
        "hint": "机器人的 QQ 号，用于群消息发送者标识",
    },
    "maibot_bot_nickname": {
        "description": "Bot 昵称",
        "type": "string",
        "hint": "机器人的昵称",
    },
}

MAIBOT_DEFAULT_CONFIG_TMPL = {
    "type": "maibot",
    "enable": False,
    "id": "maibot_default",
    "maibot_ws_url": DEFAULT_WS_URL,
    "maibot_api_key": "",
    "maibot_platform": DEFAULT_PLATFORM,
    "maibot_timeout": DEFAULT_TIMEOUT,
    "maibot_bot_qq": "",
    "maibot_bot_nickname": "",
}


@register_platform_adapter(
    "maibot",
    "MaiBot 消息平台适配器 - 通过 WebSocket 桥接 MaiBot",
    default_config_tmpl=MAIBOT_DEFAULT_CONFIG_TMPL,
    adapter_display_name="MaiBot",
    support_streaming_message=False,
    config_metadata=MAIBOT_CONFIG_METADATA,
)
class MaiBotPlatformAdapter(Platform):
    """AstrBot Platform adapter that bridges to MaiBot via WebSocket.

    This adapter connects to MaiBot's API-Server WebSocket endpoint.
    When MaiBot sends proactive messages (bot replies), this adapter
    wraps them into AstrMessageEvent and submits them to AstrBot's
    event pipeline so plugins and LLM can process them.
    """

    def __init__(
        self,
        platform_config: dict,
        platform_settings: dict,
        event_queue: asyncio.Queue,
    ) -> None:
        super().__init__(platform_config, event_queue)
        self.settings = platform_settings

        self.ws_url = platform_config.get("maibot_ws_url", DEFAULT_WS_URL)
        self.api_key = platform_config.get("maibot_api_key", "")
        self.platform_name = platform_config.get("maibot_platform", DEFAULT_PLATFORM)
        self.timeout = platform_config.get("maibot_timeout", DEFAULT_TIMEOUT)
        self.bot_user_id = platform_config.get("maibot_bot_qq", "")
        self.bot_nickname = platform_config.get("maibot_bot_nickname", "")

        if isinstance(self.timeout, str):
            self.timeout = int(self.timeout)

        self.ws_client: MaiBotWSClient | None = None
        self._run_task: asyncio.Task | None = None
        self._shutdown_event = asyncio.Event()

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(
            name="maibot",
            description="MaiBot 消息平台适配器",
            id=self.config.get("id", "maibot_default"),
            adapter_display_name="MaiBot",
            support_streaming_message=False,
            support_proactive_message=True,
        )

    async def run(self) -> None:
        """Main run loop: connect to MaiBot and listen for messages."""
        self.ws_client = MaiBotWSClient(
            ws_url=self.ws_url,
            api_key=self.api_key,
            platform=self.platform_name,
            timeout=self.timeout,
            bot_user_id=self.bot_user_id,
            bot_nickname=self.bot_nickname,
        )

        # Register proactive message handler
        self.ws_client.set_proactive_message_handler(self._handle_maibot_message)

        # Sync AstrBot's LLM tools to MaiBot
        self.ws_client.set_tool_call_handler(self._handle_tool_call)
        await self._sync_tools()

        # Connect and stay connected
        await self.ws_client.ensure_connected()
        logger.info("[MaiBot] Platform adapter running.")

        # Keep running until shutdown
        await self._shutdown_event.wait()

    async def _sync_tools(self) -> None:
        """Push AstrBot's registered LLM tools to MaiBot."""
        if not self.ws_client:
            return
        try:
            tools = []
            if hasattr(llm_tools, "func_list"):
                for func in llm_tools.func_list:
                    tools.append({
                        "name": getattr(func, "name", getattr(func, "__name__", str(func))),
                        "description": getattr(func, "description", ""),
                        "parameters": getattr(func, "parameters", {}),
                    })
            if tools:
                await self.ws_client.sync_tools(tools)
        except Exception as e:
            logger.warning(f"[MaiBot] Tool sync failed: {e}")

    async def _handle_tool_call(self, tool_name: str, args: dict) -> str:
        """Handle tool_call requests from MaiBot by delegating to AstrBot's llm_tools."""
        try:
            if hasattr(llm_tools, "func_list"):
                for func in llm_tools.func_list:
                    fname = getattr(func, "name", getattr(func, "__name__", ""))
                    if fname == tool_name:
                        result = await func(**args) if asyncio.iscoroutinefunction(func) else func(**args)
                        return str(result) if result is not None else ""
            return f"Tool '{tool_name}' not found in AstrBot."
        except Exception as e:
            logger.error(f"[MaiBot] Tool execution error for {tool_name}: {e}")
            return f"Error: {e}"

    async def _handle_maibot_message(self, msg: dict) -> None:
        """Handle a proactive message from MaiBot: wrap into AstrMessageEvent and commit."""
        payload = msg.get("payload", {})
        if not payload:
            return

        message_info = payload.get("message_info", {})
        sender_info = payload.get("sender_info", {})
        user_info = sender_info.get("user_info", {})
        group_info = sender_info.get("group_info", {})
        message_segment = payload.get("message_segment", {})

        # Extract text
        text = self._extract_text_from_segment(message_segment)

        # Build AstrBotMessage
        ab_msg = AstrBotMessage()
        ab_msg.type = MT.GROUP_MESSAGE if group_info else MT.FRIEND_MESSAGE
        ab_msg.self_id = self.bot_user_id or "maibot"
        ab_msg.message_id = message_info.get("message_id", uuid.uuid4().hex)

        if group_info:
            ab_msg.group = Group(
                group_id=group_info.get("group_id", ""),
                group_name=group_info.get("group_name", ""),
            )
            session_id = f"{group_info.get('group_id', '')}:{user_info.get('user_id', '')}"
        else:
            ab_msg.group = None
            session_id = user_info.get("user_id", "unknown")

        ab_msg.session_id = session_id
        ab_msg.sender = MessageMember(
            user_id=user_info.get("user_id", "unknown"),
            nickname=user_info.get("user_nickname", ""),
        )

        # Build message components
        ab_msg.message = self._segment_to_components(message_segment)
        ab_msg.message_str = text
        ab_msg.raw_message = msg

        # Build session string: platform_id:message_type:session_id
        platform_id = self.config.get("id", "maibot_default")
        msg_type_str = ab_msg.type.value  # "GroupMessage" or "FriendMessage"
        session_str = f"{platform_id}:{msg_type_str}:{session_id}"

        # Build AstrMessageEvent
        event = AstrMessageEvent(
            message_str=text,
            message_obj=ab_msg,
            platform_meta=self.meta(),
            session_id=session_id,
        )

        # Override session to include proper UMO
        event.session = MessageSession(
            platform_name=platform_id,
            message_type=MT(msg_type_str),
            session_id=session_id,
        )

        logger.debug(f"[MaiBot] Committing event: umo={event.unified_msg_origin}, text={text[:100]}")
        self.commit_event(event)

        # Also forward non-group messages to MaiBot as passthrough context
        # (so MaiBot maintains conversation history)
        if text.strip():
            try:
                asyncio.create_task(
                    self.ws_client.send_only(
                        text=text,
                        user_id=user_info.get("user_id", ""),
                        user_nickname=user_info.get("user_nickname", ""),
                        group_id=group_info.get("group_id") if group_info else None,
                        group_name=group_info.get("group_name", "") if group_info else "",
                    )
                )
            except Exception:
                pass

    def _segment_to_components(self, segment: dict) -> list:
        """Convert MaiBot message segment to AstrBot message components."""
        seg_type = segment.get("type", "")
        data = segment.get("data")

        if seg_type == "text" and isinstance(data, str):
            return [Plain(data)]
        elif seg_type == "image" and isinstance(data, str):
            return [Image.fromURL(data) if data.startswith("http") else Image.fromBase64(data)]
        elif seg_type == "seglist" and isinstance(data, list):
            result = []
            for sub in data:
                if isinstance(sub, dict):
                    result.extend(self._segment_to_components(sub))
            return result
        elif seg_type == "emoji":
            return [Plain(f"[emoji:{data}]")]
        elif seg_type in ("voice", "voiceurl"):
            return [Plain("[语音]")]
        elif seg_type in ("video", "videourl"):
            return [Plain("[视频]")]

        return [Plain(str(data) if data else "")]

    def _extract_text_from_segment(self, segment: dict) -> str:
        """Recursively extract text from a MaiBot segment structure."""
        seg_type = segment.get("type", "")
        data = segment.get("data")

        if seg_type == "text" and isinstance(data, str):
            return data
        elif seg_type == "seglist" and isinstance(data, list):
            parts = []
            for sub in data:
                if isinstance(sub, dict):
                    t = self._extract_text_from_segment(sub)
                    if t:
                        parts.append(t)
            return "\n".join(parts) if parts else ""
        elif seg_type in ("image", "emoji") and isinstance(data, str):
            return "[图片]"
        elif seg_type in ("voice", "voiceurl"):
            return "[语音]"
        elif seg_type in ("video", "videourl"):
            return "[视频]"
        return ""

    async def send_by_session(
        self,
        session: "MessageSesion",
        message_chain: MessageChain,
    ) -> None:
        """Send a message back through MaiBot.

        This is called when AstrBot wants to reply to a message.
        """
        if not self.ws_client or not self.api_key:
            logger.warning("[MaiBot] Cannot send: not configured or not connected.")
            return

        session_id = session.session_id
        is_group = ":" in session_id
        parts = session_id.split(":", 1)
        if is_group and len(parts) == 2:
            group_id, user_id = parts
        else:
            user_id = session_id
            group_id = None

        # Extract text from message chain
        text = message_chain.get_plain_text()

        try:
            await self.ws_client.send_only(
                text=text,
                user_id=user_id or "",
                user_nickname="",
                group_id=group_id,
                group_name="",
            )
        except Exception as e:
            logger.error(f"[MaiBot] send_by_session error: {e}")

    async def terminate(self) -> None:
        """Shut down the platform adapter."""
        self._shutdown_event.set()
        if self.ws_client:
            await self.ws_client.close()
            self.ws_client = None


class Main(star.Star):
    def __init__(self, context: star.Context, config=None) -> None:
        super().__init__(context)
        self.context = context
        logger.info("[MaiBot Plugin] MaiBot platform adapter loaded.")
