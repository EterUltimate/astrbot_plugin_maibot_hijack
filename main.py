# Copyright (C) 2025 EterUltimate
#
# This file is part of astrbot_plugin_maibot_hijack.
#
# astrbot_plugin_maibot_hijack is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# astrbot_plugin_maibot_hijack is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""
AstrBot MaiBot Adapter 插件

架构说明
--------
AstrBot 作为消息聚合层，对接多种消息平台（QQ/Telegram/Discord/微信 等）。
MaiBot 专注于 LLM 回复，不感知底层平台差异。

消息流
------
用户消息 → AstrBot → 本插件 → MaiBot（platform = 平台类型名，如 aiocqhttp）
MaiBot 回复 → 本插件（按 platform 路由） → AstrBot → 原消息平台 → 用户

平台标识
--------
* 发向 MaiBot 的 message_info.platform / message_dim.platform
  使用 event.get_platform_name()（平台类型名，如 aiocqhttp、discord）。
* AstrBot 内部 unified_msg_origin 的第一段是 platform_meta.id（用户自定义实例标识），
  与 platform_meta.name（平台类型名）可能不同。
* 主动消息路由通过 SessionInfo.platform_name 匹配，不再依赖 UMO 字符串拼接。
* 每个不同的平台类型对应一条独立的 WS 持久连接。
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Star, Context, register
from astrbot.core.message.components import Image

from .maibot_ws_client import MaiBotWSClient, parse_segment_to_components

# 默认 session_map 最大条目数，可通过配置覆盖
_DEFAULT_SESSION_MAP_MAX = 500


@dataclass
class SessionInfo:
    """会话信息的最小表示，用于主动消息路由。

    避免缓存完整的 AstrMessageEvent 对象，防止：
    - 对象生命周期过长导致资源占用
    - 上下文（如连接）已失效但对象仍存在
    """

    unified_msg_origin: str
    platform: str
    platform_name: str = ""  # MaiBot 识别的平台类型名（如 aiocqhttp）
    message_type: str
    session_id: str
    # 弱引用到原始事件，仅在需要时用于发送消息
    # 使用 object 类型避免循环导入问题
    _event_ref: object = field(default=None, repr=False)

    @classmethod
    def from_event(
        cls, event: AstrMessageEvent, platform_name: str = ""
    ) -> SessionInfo:
        """从 AstrMessageEvent 创建 SessionInfo。"""
        umo = event.unified_msg_origin
        platform, msg_type, session_id = parse_umo(umo)
        return cls(
            unified_msg_origin=umo,
            platform=platform,
            platform_name=platform_name or event.get_platform_name(),
            message_type=msg_type,
            session_id=session_id,
            _event_ref=event,
        )

    def get_event(self) -> AstrMessageEvent | None:
        """获取原始事件对象（如果仍可用）。"""
        return self._event_ref  # type: ignore

    async def send(self, result) -> bool:
        """尝试发送消息到该会话。

        Returns:
            bool: 是否发送成功
        """
        event = self.get_event()
        if event is None:
            return False
        try:
            await event.send(result)
            return True
        except Exception as e:
            logger.warning(f"[MaiBot] 发送消息到 {self.unified_msg_origin} 失败: {e}")
            return False


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

        # 支持新旧两种配置结构
        # 新结构：config = {"connection": {...}, "identity": {...}, "advanced": {...}}
        # 旧结构：config = {"maibot_ws_url": ..., ...}
        conn_cfg = self.config.get("connection", {})
        id_cfg = self.config.get("identity", {})
        adv_cfg = self.config.get("advanced", {})

        def _get_cfg(new_key: str, old_key: str, default):
            """优先从新结构获取，不存在则从旧结构获取，最后使用默认值。"""
            if new_key in conn_cfg or new_key in id_cfg or new_key in adv_cfg:
                # 确定新配置属于哪个分组
                if new_key in ("maibot_ws_url", "maibot_api_key", "maibot_timeout"):
                    return conn_cfg.get(new_key, default)
                elif new_key in ("maibot_bot_id", "maibot_bot_nickname"):
                    return id_cfg.get(new_key, default)
                else:
                    return adv_cfg.get(new_key, default)
            return self.config.get(old_key, default)

        self.ws_url: str = _get_cfg(
            "maibot_ws_url", "maibot_ws_url", "ws://127.0.0.1:18040/ws"
        )
        self.api_key: str = _get_cfg(
            "maibot_api_key", "maibot_api_key", "astrbot_hijack"
        )
        self.timeout: int = int(_get_cfg("maibot_timeout", "maibot_timeout", 120))
        self.bot_user_id: str = _get_cfg("maibot_bot_id", "maibot_bot_id", "astrbot")
        self.bot_nickname: str = _get_cfg(
            "maibot_bot_nickname", "maibot_bot_nickname", "AstrBot"
        )

        # 高级配置
        self.reconnect_interval: int = int(adv_cfg.get("reconnect_interval", 5))
        self.max_session_cache: int = int(adv_cfg.get("max_session_cache", 500))
        self.debug_mode: bool = bool(adv_cfg.get("debug_mode", False))

        self.ws_client = MaiBotWSClient(
            ws_url=self.ws_url,
            api_key=self.api_key,
            timeout=self.timeout,
            bot_user_id=self.bot_user_id,
            bot_nickname=self.bot_nickname,
            reconnect_interval=self.reconnect_interval,
            debug_mode=self.debug_mode,
        )

        # LRU session 映射：unified_msg_origin → SessionInfo
        # 使用 OrderedDict 模拟 LRU，上限 max_session_cache，防止内存泄漏。
        # 只保存发送消息所需的最小信息，避免缓存完整事件对象导致陈旧引用。
        self._session_map: OrderedDict[str, SessionInfo] = OrderedDict()

        self.ws_client.set_proactive_message_handler(self._handle_proactive_message)

    # ── 生命周期 ────────────────────────────────────────────────────────────

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        """插件加载完成后打印连接信息（通道将在首条消息到来时懒创建）。"""
        # API Key 脱敏显示：只显示前4位和后4位，中间用****代替
        masked_key = self.api_key
        if len(self.api_key) > 8:
            masked_key = f"{self.api_key[:4]}****{self.api_key[-4:]}"
        elif len(self.api_key) > 4:
            masked_key = f"{self.api_key[:2]}****{self.api_key[-2:]}"
        else:
            masked_key = "****"
        logger.info(f"[MaiBot] 插件已加载 | WS: {self.ws_url} | API Key: {masked_key}")

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

        # 阻止 AstrBot 默认 LLM 处理（不能用 stop_event，因为 yield 后会被 set_result 覆盖）
        event.should_call_llm(False)
        event.continue_event()

        umo = event.unified_msg_origin
        platform_id, message_type, raw_session_id = parse_umo(umo)
        # platform_name 是 MaiBot 能识别的平台类型名（如 aiocqhttp, discord）
        # platform_id 是 AstrBot 内部的适配器实例唯一标识（用户自定义）
        platform_name = event.get_platform_name()
        is_group = "group" in message_type.lower()

        # 更新 LRU session 映射（同时记录 platform_name 用于主动消息路由）
        self._update_session_map(umo, event, platform_name)

        user_id = str(event.get_sender_id() or raw_session_id)
        user_nickname = event.get_sender_name() or user_id

        if is_group:
            ws_user_id = user_id
            ws_user_nickname = user_nickname
            ws_group_id: str | None = raw_session_id
            ws_group_name = raw_session_id
        else:
            # 私聊场景：确保 user_id 不为空，防止路由异常
            ws_user_id = raw_session_id or user_id or f"user_{umo.replace(':', '_')}"
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
            f"[MaiBot/{platform_name}] 消息来自 {user_nickname}({user_id}), "
            f"group={ws_group_id}, text='{text[:30]}'"
        )

        try:
            responses = await self.ws_client.send_and_receive(
                platform=platform_name,
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
            logger.error(f"[MaiBot/{platform_name}] 处理消息出错: {e}", exc_info=True)
            yield event.plain_result(f"MaiBot 出错：{e}")
            return

        for resp in responses:
            async for result in self._payload_to_results(event, resp):
                yield result

    # ── session 管理 ─────────────────────────────────────────────────────────

    def _update_session_map(
        self, umo: str, event: AstrMessageEvent, platform_name: str = ""
    ) -> None:
        """以 LRU 策略更新 session 映射，超出上限时淘汰最旧条目。"""
        if umo in self._session_map:
            self._session_map.move_to_end(umo)
        # 保存 SessionInfo 而非完整事件对象
        self._session_map[umo] = SessionInfo.from_event(event, platform_name)
        max_size = getattr(self, "max_session_cache", _DEFAULT_SESSION_MAP_MAX)
        while len(self._session_map) > max_size:
            self._session_map.popitem(last=False)

    # ── 主动消息路由 ─────────────────────────────────────────────────────────

    async def _handle_proactive_message(self, msg: dict, platform: str) -> None:
        """处理 MaiBot 主动推送的消息，按 platform 路由回对应 AstrBot 会话。

        platform 参数是 MaiBot 识别的平台类型名（如 aiocqhttp）。
        路由策略：按 platform_name 精确匹配 session_map 中的会话。
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

        target_session: SessionInfo | None = None
        # 支持多种群聊消息类型命名
        group_msg_types = ["GroupMessage", "group_message", "Group"]
        private_msg_types = [
            "FriendMessage",
            "PrivateMessage",
            "private_message",
            "Friend",
            "Private",
        ]

        if group_id:
            target_session = self._find_session_by_platform_and_id(
                platform, group_id, group_msg_types
            )
        if target_session is None and user_id:
            target_session = self._find_session_by_platform_and_id(
                platform, user_id, private_msg_types
            )
        if target_session is None:
            target_session = self._find_any_session_for_platform_name(platform)

        if target_session is None:
            logger.warning(
                f"[MaiBot/{platform}] 收到主动消息，但未找到对应会话，无法路由"
            )
            return

        logger.info(
            f"[MaiBot/{platform}] 路由主动消息 → {target_session.unified_msg_origin}"
        )

        # 尝试获取原始事件用于生成结果
        event = target_session.get_event()
        if event is None:
            logger.warning(
                f"[MaiBot/{platform}] 会话 {target_session.unified_msg_origin} 的事件已失效"
            )
            return

        async for result in self._payload_to_results(event, payload):
            success = await target_session.send(result)
            if not success:
                break

    def _find_session_by_platform_and_id(
        self, platform_name: str, target_id: str, msg_types: list[str]
    ) -> SessionInfo | None:
        """按 platform_name 和群号/用户 ID 在 session_map 中查找会话。"""
        for umo, session in self._session_map.items():
            if (
                session.platform_name == platform_name
                and session.session_id == target_id
            ):
                return session
        return None

    def _find_any_session_for_platform_name(
        self, platform_name: str
    ) -> SessionInfo | None:
        """查找属于指定 platform_name 的最新会话（LRU 末尾即最新）。"""
        # 从最新（末尾）开始查找
        for session in reversed(self._session_map.values()):
            if session.platform_name == platform_name:
                return session
        return None

    # ── payload → AstrBot 结果 ───────────────────────────────────────────────

    async def _payload_to_results(self, event: AstrMessageEvent, payload: dict):
        """将 MaiBot payload 异步生成 AstrBot 消息结果。"""
        segment = payload.get("message_segment", {})
        async for result in self._seg_to_results(event, segment):
            yield result

    async def _seg_to_results(self, event: AstrMessageEvent, segment: dict):
        """递归将 Seg 转换为 AstrBot 消息结果（异步生成器）。"""
        import astrbot.core.message.components as Comp

        components = parse_segment_to_components(segment)
        for comp in components:
            if isinstance(comp, Comp.Plain):
                yield event.plain_result(comp.text)
            elif isinstance(comp, Comp.Image):
                # Image 组件直接构造 chain_result
                yield event.chain_result([comp])
