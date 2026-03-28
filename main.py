
import asyncio
from typing import Any

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Star, Context, register
from astrbot.core.message.components import Image

from .maibot_ws_client import MaiBotWSClient


@register(
    "maibot_hijack",
    "EterUltimate",
    "MaiBot Hijack 模式插件",
    "1.0.0",
    "https://github.com/EterUltimate/astrbot_plugin_maibot_hijack",
)
class MaiBotHijackPlugin(Star):
    """MaiBot Hijack 模式插件，将消息劫持转发到 MaiBot 进行处理。"""

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        
        self.ws_url = self.config.get("maibot_ws_url", "ws://127.0.0.1:18040/ws")
        self.api_key = self.config.get("maibot_api_key", "astrbot_hijack")
        self.platform_name = self.config.get("maibot_platform", "astrbot")
        self.timeout = self.config.get("maibot_timeout", 120)
        
        self.ws_client = MaiBotWSClient(
            ws_url=self.ws_url,
            api_key=self.api_key,
            platform=self.platform_name,
            timeout=self.timeout
        )
        # Register a proactive message handler to yield messages sent asynchronously by MaiBot
        self.ws_client.set_proactive_message_handler(self._handle_proactive_message)

    async def _handle_proactive_message(self, msg: dict):
        '''
        Handle proactive messages from MaiBot by finding a matching session 
        and sending the message there. This is complex in hijack mode without session mapping,
        but typically MaiBot replies contextually.
        '''
        # For simplicity, if we have active sessions we can store them, or just use send_and_receive.
        # Typically the hijack blocks LLM and waits for reply.
        pass

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        logger.info(f"[MaiBot Hijack] Connecting to MaiBot WS at {self.ws_url}...")
        try:
            await self.ws_client.ensure_connected()
            logger.info("[MaiBot Hijack] Connected successfully.")
        except Exception as e:
            logger.error(f"[MaiBot Hijack] Connection failed: {e}")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def hijack_message(self, event: AstrMessageEvent):
        """劫持所有消息并转发到 MaiBot 进行处理。"""
        # We should not hijack our own messages or system messages.
        # Depending on Napcat, self_id messages shouldn't trigger this, but let's be safe.
        
        text = event.message_str.strip()
        if not text and not event.message_obj.message:
            return
            
        # Prevent AstrBot's default LLM processing
        event.stop_event()
        
        # Gather message info
        user_id = event.get_sender_id()
        user_nickname = event.get_sender_name()
        
        group_id = None
        group_name = ""
        if event.message_obj.group_id: # Usually direct prop in newer AstrBot
            group_id = event.message_obj.group_id
        elif hasattr(event.message_obj, 'group') and event.message_obj.group:
            group_id = event.message_obj.group.group_id
            group_name = event.message_obj.group.group_name
            
        # Extract images from components
        images = []
        for comp in event.message_obj.message:
            if isinstance(comp, Image):
                # Try to extract URL or base64 if present, else ignore or convert
                if comp.url:
                    images.append(comp.url)
                elif hasattr(comp, 'base64') and comp.base64:
                    images.append(comp.base64)
                    
        logger.info(f"[MaiBot Hijack] Hijacking message '{text[:20]}...' from {user_nickname}({user_id})")
        try:
            responses = await self.ws_client.send_and_receive(
                text=text,
                user_id=user_id,
                user_nickname=user_nickname,
                group_id=group_id,
                group_name=group_name,
                images=images if images else None
            )
            
            for resp in responses:
                resp_text = self.ws_client._extract_text_from_payload(resp)
                if resp_text:
                    yield event.plain_result(resp_text)
                    
        except asyncio.TimeoutError:
            yield event.plain_result("MaiBot Request Timeout.")
        except Exception as e:
            logger.error(f"[MaiBot Hijack] Error: {e}")
            yield event.plain_result(f"MaiBot Error: {e}")
            
    async def terminate(self):
        if self.ws_client:
            await self.ws_client.close()
