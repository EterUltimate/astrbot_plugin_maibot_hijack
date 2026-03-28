"""
AstrBot MaiBot Adapter 插件

架构说明
--------
AstrBot 作为消息聚合层，对接多种消息平台（QQ/Telegram/Discord/微信 等）。
MaiBot 专注于 LLM 回复，不感知底层平台差异。

消息流
------
用户消息 → AstrBot → 本插件 → MaiBot（platform = 真实来源平台）
MaiBot 回复 → 本插件（按 platform 路由） → AstrBot → 原消息平台 → 用户

平台标识
--------
* 发向 MaiBot 的 message_info.platform / message_dim.platform
  均设为真实平台标识（从 AstrBot unified_msg_origin 解析）。
* MaiBot 回复时原样回传 platform，插件据此找到对应 AstrBot 会话并回复。
* 每个不同的平台标识对应一条独立的 WS 持久连接。
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Star, Context, register
from astrbot.core.message.components import Image

from .maibot_ws_client import MaiBotWSClient, parse_segment_to_components

# session_map 最大条目数，防止内存无限增长
_SESSION_MAP_MAX = 500


# ---------------------------------------------------------------------------
# UMO 解析工具
# ---------------------------------------------------------------------------

def parse_umo(umo: str) -> tuple[str, str, str]:
    """解析 AstrBot unified_msg_origin。

    格式：``platform_adapter:MessageType:session_id``
    例如：``aiocqhttp:GroupMessage:123456``

    返回：(adapter_name, message_type, session_id)
    """
    parts = umo.split(":", 2)
    if len(parts) >= 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return parts[0], parts[1], ""
    return umo, "", ""


# ---------------------------------------------------------------------------
# 插件主体
# ---------------------------------------------------------------------------

@register(
    "maibot_hijack",
    "EterUltimate",
    "MaiBot Adapter — AstrBot 作为消息聚合平台，MaiBot 专注 LLM 回复",
    "2.0.0",
    "https://github.com/EterUltimate/astrbot_plugin_maibot_hijack",
)
class MaiBotHijackPlugin(Star):
    """MaiBot Adapter 插件。

    将 AstrBot 作为消息聚合层，把各平台消息转发给 MaiBot 处理。
    每条消息携带真实平台标识，MaiBot 回复时按该标识路由回对应会话。
    """

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}

        self.ws_url: str = self.config.get("maibot_ws_url", "ws://127.0.0.1:18040/ws")
        self.api_key: str = self.config.get("maibot_api_key", "astrbot_hijack")
        self.timeout: int = int(self.config.get("maibot_timeout", 120))
        self.bot_user_id: str = self.config.get("maibot_bot_id", "astrbot")
        self.bot_nickname: str = self.config.get("maibot_bot_nickname", "AstrBot")

        self.ws_client = MaiBotWSClient(
            ws_url=self.ws_url,
            api_key=self.api_key,
            timeout=self.timeout,
            bot_user_id=self.bot_user_id,
            bot_nickname=self.bot_nickname,
        )

        # LRU session 映射：unified_msg_origin → AstrMessageEvent
        # 使用 OrderedDict 模拟 LRU，上限 _SESSION_MAP_MAX，防止内存泄漏。
        # 注意：此字典仅在事件循环线程中访问，无需加锁。
        self._session_map: OrderedDict[str, AstrMessageEvent] = OrderedDict()

        self.ws_client.set_proactive_message_handler(self._handle_proactive_message)

    # ── 生命周期 ────────────────────────────────────────────────────────────

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        """插件加载完成后打印连接信息（通道将在首条消息到来时懒创建）。"""
        logger.info(
            f"[MaiBot] 插件已加载 | WS: {self.ws_url} | API Key: {self.api_key}"
        )

    async def terminate(self):
        """插件卸载时清理资源。"""
        await self.ws_client.close()
        self._session_map.clear()
        logger.info("[MaiBot] 已关闭所有 WS 连接")

    # ── 消息劫持 ────────────────────────────────────────────────────────────

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def hijack_message(self, event: AstrMessageEvent):
        """劫持所有消息，转发给 MaiBot 处理。

        从 unified_msg_origin 解析真实平台标识，发送给 MaiBot 时携带该标识。
        """
        text = event.message_str.strip()
        if not text and not event.message_obj.message:
            return

        # 阻止 AstrBot 默认 LLM 处理
        event.stop_event()

        umo = event.unified_msg_origin
        platform, message_type, raw_session_id = parse_umo(umo)
        is_group = "group" in message_type.lower()

        # 更新 LRU session 映射
        self._update_session_map(umo, event)

        user_id = str(event.get_sender_id() or raw_session_id)
        user_nickname = event.get_sender_name() or user_id

        if is_group:
            ws_user_id = user_id
            ws_user_nickname = user_nickname
            ws_group_id: str | None = raw_session_id
            ws_group_name = raw_session_id
        else:
            ws_user_id = raw_session_id
            ws_user_nickname = user_nickname
            ws_group_id = None
            ws_group_name = ""

        images: list[str] = []
        for comp in event.message_obj.message:
            if isinstance(comp, Image):
                if comp.url:
                    images.append(comp.url)
                elif hasattr(comp, "base64") and comp.base64:
                    images.append(comp.base64)

        logger.info(
            f"[MaiBot/{platform}] 消息来自 {user_nickname}({user_id}), "
            f"group={ws_group_id}, text='{text[:30]}'"
        )

        try:
            responses = await self.ws_client.send_and_receive(
                platform=platform,
                text=text,
                user_id=ws_user_id,
                user_nickname=ws_user_nickname,
                group_id=ws_group_id,
                group_name=ws_group_name,
                images=images or None,
            )
        except asyncio.TimeoutError:
            yield event.plain_result("MaiBot 请求超时，请稍后再试。")
            return
        except Exception as e:
            logger.error(f"[MaiBot/{platform}] 处理消息出错: {e}", exc_info=True)
            yield event.plain_result(f"MaiBot 出错：{e}")
            return

        for resp in responses:
            async for result in self._payload_to_results(event, resp):
                yield result

    # ── session 管理 ─────────────────────────────────────────────────────────

    def _update_session_map(self, umo: str, event: AstrMessageEvent) -> None:
        """以 LRU 策略更新 session 映射，超出上限时淘汰最旧条目。"""
        if umo in self._session_map:
            self._session_map.move_to_end(umo)
        self._session_map[umo] = event
        while len(self._session_map) > _SESSION_MAP_MAX:
            self._session_map.popitem(last=False)

    # ── 主动消息路由 ─────────────────────────────────────────────────────────

    async def _handle_proactive_message(self, msg: dict, platform: str) -> None:
        """处理 MaiBot 主动推送的消息，按 platform 路由回对应 AstrBot 会话。

        路由优先级：精确群聊 UMO → 精确私聊 UMO → 该平台任意会话。
        """
        payload = msg.get("payload", {})
        if not payload:
            return

        msg_info = payload.get("message_info", {})
        sender_info = msg_info.get("sender_info", {})
        group_info = sender_info.get("group_info") or {}
        user_info_inner = sender_info.get("user_info") or {}

        group_id = group_info.get("group_id", "")
        user_id = user_info_inner.get("user_id", "")

        target_event: AstrMessageEvent | None = None
        if group_id:
            target_event = self._session_map.get(f"{platform}:GroupMessage:{group_id}")
        if target_event is None and user_id:
            target_event = self._session_map.get(f"{platform}:FriendMessage:{user_id}")
        if target_event is None:
            target_event = self._find_any_session_for_platform(platform)

        if target_event is None:
            logger.warning(
                f"[MaiBot/{platform}] 收到主动消息，但未找到对应会话，无法路由"
            )
            return

        logger.info(f"[MaiBot/{platform}] 路由主动消息 → {target_event.unified_msg_origin}")

        async for result in self._payload_to_results(target_event, payload):
            await target_event.send(result)

    def _find_any_session_for_platform(
        self, platform: str
    ) -> AstrMessageEvent | None:
        """在 session_map 中查找属于指定平台的最新会话（LRU 末尾即最新）。"""
        prefix = f"{platform}:"
        # 从最新（末尾）开始查找
        for umo in reversed(self._session_map):
            if umo.startswith(prefix):
                return self._session_map[umo]
        return None

    # ── payload → AstrBot 结果 ───────────────────────────────────────────────

    async def _payload_to_results(self, event: AstrMessageEvent, payload: dict):
        """将 MaiBot payload 异步生成 AstrBot 消息结果。"""
        segment = payload.get("message_segment", {})
        async for result in self._seg_to_results(event, segment):
            yield result

    async def _seg_to_results(self, event: AstrMessageEvent, segment: dict):
        """递归将 Seg 转换为 AstrBot 消息结果（异步生成器）。"""
        components = parse_segment_to_components(segment)
        for comp in components:
            import astrbot.core.message.components as Comp
            if isinstance(comp, Comp.Plain):
                yield event.plain_result(comp.text)
            elif isinstance(comp, Comp.Image):
                # Image 组件直接构造 chain_result
                yield event.chain_result([comp])
