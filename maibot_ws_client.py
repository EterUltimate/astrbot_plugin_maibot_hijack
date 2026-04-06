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
MaiBotWSClient — 多平台动态路由客户端

设计原则
--------
* AstrBot 作为消息聚合层，对接多个消息平台（QQ/Telegram/Discord …）。
* 每条消息携带其来源平台的标识（从 AstrBot UMO 解析）。
* 向 MaiBot 发送消息时，message_info.platform 字段使用真实平台标识，
  让 MaiBot 的 LLM 知道正在回复哪个平台。
* MaiBot 回复时，payload.message_info.platform（或 message_dim.platform）
  同样是该真实平台标识，插件据此将回复路由回原来的 AstrBot 会话。
* 每个 (ws_url, api_key, platform) 三元组共享同一条 WS 持久连接。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import websockets
import websockets.exceptions

from astrbot.api import logger

# 等待后续分段回复的窗口时间（秒）
_FOLLOWUP_WINDOW = 5.0
# Base64 data URI 前缀列表
_DATA_URI_PREFIXES = (
    "data:image/png;base64,",
    "data:image/gif;base64,",
    "data:image/jpeg;base64,",
    "data:image/webp;base64,",
)


def _strip_data_uri(data: str) -> str:
    """去除 base64 data URI 前缀，返回纯 base64 字符串。"""
    for prefix in _DATA_URI_PREFIXES:
        if data.startswith(prefix):
            return data[len(prefix) :]
    return data


# ---------------------------------------------------------------------------
# 单平台 WS 通道
# ---------------------------------------------------------------------------


class _PlatformChannel:
    """维护一条到 MaiBot 的 WebSocket 持久连接，对应特定的 platform 标识。

    并发安全说明
    -----------
    * ``ensure_connected`` 通过 ``_connect_lock`` 防止重复连接。
    * ``send_and_receive`` 通过 ``_request_lock`` 保证同一通道同一时刻
      只有一个请求-响应事务，避免多协程共享 ``_global_queue`` 时的消息混淆。
    * 使用 ``_current_request_id`` 请求 ID 关联机制替代简单的布尔标志，
      ``_dispatch`` 在持锁读取 ``_current_request_id`` 后判断消息归属，
      避免跨协程状态位的时序问题。
    """

    def __init__(
        self,
        ws_url: str,
        api_key: str,
        platform: str,
        timeout: int = 120,
        keepalive_interval: int = 20,
        bot_user_id: str = "astrbot",
        bot_nickname: str = "AstrBot",
        reconnect_interval: int = 5,
        debug_mode: bool = False,
    ) -> None:
        self.ws_url = ws_url.rstrip("/")
        self.api_key = api_key
        self.platform = platform
        self.timeout = timeout
        self.keepalive_interval = keepalive_interval
        self.bot_user_id = bot_user_id
        self.bot_nickname = bot_nickname
        self.reconnect_interval = reconnect_interval
        self.debug_mode = debug_mode

        self._ws: websockets.WebSocketClientProtocol | None = None
        self._connected: bool = False
        # 防止重复建连
        self._connect_lock = asyncio.Lock()

        self._listener_task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None

        # 请求-响应关联机制：使用请求 ID 替代简单的布尔标志
        # 避免并发时消息分类错误（把响应当主动消息，或反之）
        self._global_queue: asyncio.Queue[dict] = asyncio.Queue(
            maxsize=1000
        )  # 限制容量防止内存增长
        self._current_request_id: str | None = None
        self._request_lock = asyncio.Lock()  # 保证 send_and_receive 串行

        # 后台任务追踪，防止任务悬挂
        self._background_tasks: set[asyncio.Task] = set()

        # 外部回调
        self._proactive_handler: Callable[..., Awaitable[None]] | None = None
        self._tool_call_handler: Callable[..., Awaitable[str]] | None = None

    # ── 连接管理 ────────────────────────────────────────────────────────────

    def _build_url(self) -> str:
        """构建 WebSocket URL（不包含 API Key，避免在 URL 中暴露凭证）。"""
        # API Key 通过 header 传递，不在 URL 中暴露
        sep = "&" if "?" in self.ws_url else "?"
        return f"{self.ws_url}{sep}platform={self.platform}"

    def _build_headers(self) -> dict[str, str]:
        return {"x-apikey": self.api_key, "x-platform": self.platform}

    async def ensure_connected(self) -> None:
        """确保 WS 连接处于激活状态，必要时重连。"""
        async with self._connect_lock:
            if self._ws is not None and self._connected:
                try:
                    await asyncio.wait_for(self._ws.ping(), timeout=5.0)
                    return
                except Exception:
                    logger.warning(f"[MaiBot/{self.platform}] 连接丢失，重连中…")
                    self._connected = False
                    self._ws = None

            url = self._build_url()
            # 日志中脱敏显示 URL，不暴露 API Key
            safe_url = url.replace(self.api_key, "****") if self.api_key else url
            logger.info(f"[MaiBot/{self.platform}] 连接到 {safe_url}")
            try:
                self._ws = await websockets.connect(
                    url,
                    additional_headers=self._build_headers(),
                )
                self._connected = True
                logger.info(f"[MaiBot/{self.platform}] WebSocket 已连接")
                self._start_background_tasks()
            except Exception as e:
                logger.error(f"[MaiBot/{self.platform}] 连接失败: {e}")
                raise

    def _start_background_tasks(self) -> None:
        """启动（或重启）后台监听与心跳任务。"""
        if self._listener_task is None or self._listener_task.done():
            self._listener_task = asyncio.create_task(
                self._listen_loop(), name=f"maibot_listen_{self.platform}"
            )
        if self._keepalive_task is None or self._keepalive_task.done():
            self._keepalive_task = asyncio.create_task(
                self._keepalive_loop(), name=f"maibot_ka_{self.platform}"
            )

    def _create_tracked_task(self, coro) -> asyncio.Task:
        """创建后台任务并追踪，防止任务悬挂。"""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    async def _cancel_background_tasks(self) -> None:
        """取消所有后台任务。"""
        tasks = list(self._background_tasks)
        self._background_tasks.clear()
        for task in tasks:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def close(self) -> None:
        """关闭连接并取消后台任务。"""
        self._connected = False
        # 取消所有后台任务（包括监听、心跳和 dispatch 创建的任务）
        await self._cancel_background_tasks()

        for task in (self._keepalive_task, self._listener_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._keepalive_task = None
        self._listener_task = None
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    # ── 监听循环 ─────────────────────────────────────────────────────────────

    async def _listen_loop(self) -> None:
        try:
            while self._connected and self._ws is not None:
                try:
                    raw = await self._ws.recv()
                except websockets.exceptions.ConnectionClosed:
                    logger.warning(f"[MaiBot/{self.platform}] 服务端关闭连接")
                    break
                except Exception as e:
                    logger.warning(f"[MaiBot/{self.platform}] 接收错误: {e}")
                    break

                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning(
                        f"[MaiBot/{self.platform}] 非 JSON 消息: {str(raw)[:200]}"
                    )
                    continue

                await self._dispatch(msg)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[MaiBot/{self.platform}] 监听循环异常: {e}")
        finally:
            self._connected = False

    def _log_debug(self, message: str) -> None:
        """根据 debug_mode 输出调试日志。"""
        if self.debug_mode:
            logger.info(message)
        else:
            logger.debug(message)

    async def _dispatch(self, msg: dict) -> None:
        """根据消息类型分发处理。"""
        msg_type = msg.get("type", "")

        if msg_type == "sys_ack":
            self._log_debug(
                f"[MaiBot/{self.platform}] ACK: "
                f"{msg.get('meta', {}).get('acked_msg_id', '?')}"
            )
            return

        # tool_call 有多种变体格式
        if msg_type == "custom_tool_call":
            self._create_tracked_task(self._handle_tool_call(msg.get("payload", msg)))
            return
        if msg_type == "tool_call":
            self._create_tracked_task(self._handle_tool_call(msg))
            return
        if msg.get("is_custom_message") and msg.get("message_type_name") == "tool_call":
            self._create_tracked_task(self._handle_tool_call(msg.get("content", msg)))
            return

        if msg_type == "sys_std":
            # 使用请求 ID 关联机制，替代简单的 _in_request 标志
            async with self._request_lock:
                is_response = self._current_request_id is not None

            if is_response:
                # 是当前请求的响应，放入队列
                try:
                    self._global_queue.put_nowait(msg)
                except asyncio.QueueFull:
                    logger.warning(f"[MaiBot/{self.platform}] 响应队列已满，丢弃消息")
            elif self._proactive_handler:
                logger.info(f"[MaiBot/{self.platform}] 收到主动消息")
                self._create_tracked_task(self._safe_proactive(msg))
            else:
                logger.debug(f"[MaiBot/{self.platform}] 收到主动消息但无处理器，丢弃")
            return

        logger.debug(f"[MaiBot/{self.platform}] 未知消息类型: {msg_type}")

    # ── 工具调用 ─────────────────────────────────────────────────────────────

    async def _handle_tool_call(self, msg: dict) -> None:
        call_id = msg.get("call_id", "")
        tool_name = msg.get("name", "")
        tool_args = msg.get("args", {})
        logger.info(
            f"[MaiBot/{self.platform}] tool_call: {tool_name}(call_id={call_id})"
        )

        if self._tool_call_handler:
            try:
                result_text = await self._tool_call_handler(tool_name, tool_args)
            except Exception as e:
                result_text = f"Error executing tool {tool_name}: {e}"
                logger.error(f"[MaiBot/{self.platform}] tool 执行错误: {e}")
        else:
            result_text = f"No handler for tool {tool_name}"
            logger.warning(f"[MaiBot/{self.platform}] 未注册工具处理器")

        if self._ws and self._connected:
            result_msg = {
                "type": "custom_tool_result",
                "call_id": call_id,
                "name": tool_name,
                "result": {"content": result_text},
            }
            try:
                await self._ws.send(json.dumps(result_msg, ensure_ascii=False))
            except Exception as e:
                logger.error(f"[MaiBot/{self.platform}] 发送 tool_result 失败: {e}")

    # ── 主动消息 ─────────────────────────────────────────────────────────────

    async def _safe_proactive(self, msg: dict) -> None:
        try:
            if self._proactive_handler:
                await self._proactive_handler(msg, self.platform)
        except Exception as e:
            logger.error(
                f"[MaiBot/{self.platform}] 主动消息处理出错: {e}", exc_info=True
            )

    # ── 心跳 ─────────────────────────────────────────────────────────────────

    async def _keepalive_loop(self) -> None:
        try:
            while self._connected and self._ws is not None:
                await asyncio.sleep(self.keepalive_interval)
                if not self._connected or self._ws is None:
                    break
                try:
                    await asyncio.wait_for(self._ws.ping(), timeout=10.0)
                    self._log_debug(f"[MaiBot/{self.platform}] Keepalive OK")
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(
                        f"[MaiBot/{self.platform}] Keepalive 失败: {e}，{self.reconnect_interval}秒后重连…"
                    )
                    self._connected = False
                    await asyncio.sleep(self.reconnect_interval)
                    try:
                        await self.ensure_connected()
                    except Exception as re_err:
                        logger.error(f"[MaiBot/{self.platform}] 重连失败: {re_err}")
                    break
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"[MaiBot/{self.platform}] Keepalive 循环退出: {e}")

    # ── 消息构建 ──────────────────────────────────────────────────────────────

    def _build_envelope(self, payload: dict) -> dict:
        """将 payload 封装成 sys_std 信封。"""
        return {
            "ver": 1,
            "msg_id": f"astrbot_{uuid.uuid4().hex[:16]}",
            "type": "sys_std",
            "meta": {
                "sender_user": self.api_key,
                "platform": self.platform,
                "timestamp": time.time(),
            },
            "payload": payload,
        }

    def build_message_payload(
        self,
        text: str,
        user_id: str,
        user_nickname: str = "",
        group_id: str | None = None,
        group_name: str = "",
        images: list[str] | None = None,
        message_id: str | None = None,
    ) -> dict:
        """构造符合 maim_message 规范的消息 payload。

        platform 字段绑定到 self.platform（真实消息来源平台）。
        """
        msg_id = message_id or f"astrbot_msg_{uuid.uuid4().hex[:12]}"

        segments: list[dict] = []
        if text:
            segments.append({"type": "text", "data": text})
        if images:
            segments.extend({"type": "image", "data": img} for img in images)

        if not segments:
            message_segment: dict = {"type": "text", "data": ""}
        elif len(segments) == 1:
            message_segment = segments[0]
        else:
            message_segment = {"type": "seglist", "data": segments}

        display_name = user_nickname or user_id
        sender_info: dict = {
            "user_info": {
                "platform": self.platform,
                "user_id": user_id,
                "user_nickname": display_name,
                "user_cardname": display_name,
            }
        }
        if group_id:
            sender_info["group_info"] = {
                "platform": self.platform,
                "group_id": group_id,
                "group_name": group_name or group_id,
            }

        return {
            "message_info": {
                "platform": self.platform,
                "message_id": msg_id,
                "time": time.time(),
                "user_info": {
                    "platform": self.platform,
                    "user_id": self.bot_user_id,
                    "user_nickname": self.bot_nickname,
                },
                "sender_info": sender_info,
                "format_info": {
                    "content_format": ["text", "image", "emoji"],
                    "accept_format": ["text", "image", "emoji", "voice", "video"],
                },
            },
            "message_segment": message_segment,
            # MaiBot 回复时原样返回 message_dim，插件用其路由回正确平台会话
            "message_dim": {
                "api_key": self.api_key,
                "platform": self.platform,
            },
        }

    # ── 发送 / 接收 ───────────────────────────────────────────────────────────

    async def send_and_receive(
        self,
        text: str,
        user_id: str,
        user_nickname: str = "",
        group_id: str | None = None,
        group_name: str = "",
        images: list[str] | None = None,
    ) -> list[dict]:
        """发送消息并等待 MaiBot 回复，返回 payload 列表。

        通过 _request_lock 保证同一通道串行请求，避免响应队列混淆。
        使用请求 ID 关联机制确保响应正确分类。
        """
        await self.ensure_connected()

        async with self._request_lock:
            payload = self.build_message_payload(
                text=text,
                user_id=user_id,
                user_nickname=user_nickname,
                group_id=group_id,
                group_name=group_name,
                images=images,
            )
            envelope = self._build_envelope(payload)
            request_id = envelope.get("msg_id", "")

            # 清空历史残留消息
            while not self._global_queue.empty():
                try:
                    self._global_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

            # 设置当前请求 ID，用于 _dispatch 识别响应
            self._current_request_id = request_id
            try:
                await self._ws.send(json.dumps(envelope, ensure_ascii=False))
                logger.debug(
                    f"[MaiBot/{self.platform}] 消息已发送 (req_id={request_id})，等待回复…"
                )
                return await self._collect_responses()
            except websockets.exceptions.ConnectionClosed as e:
                self._connected = False
                raise ConnectionError(f"MaiBot WS 已关闭: {e}") from e
            except asyncio.TimeoutError:
                raise
            except Exception as e:
                raise RuntimeError(f"MaiBot 通信错误: {e}") from e
            finally:
                self._current_request_id = None

    async def send_only(
        self,
        text: str,
        user_id: str,
        user_nickname: str = "",
        group_id: str | None = None,
        group_name: str = "",
        images: list[str] | None = None,
    ) -> None:
        """透传消息到 MaiBot（仅上下文，不等待回复）。

        使用 _request_lock 与 send_and_receive 串行化，保证消息时序稳定。
        """
        async with self._request_lock:
            try:
                await self.ensure_connected()
            except Exception as e:
                logger.debug(f"[MaiBot/{self.platform}] send_only 连接失败，跳过: {e}")
                return

            payload = self.build_message_payload(
                text=text,
                user_id=user_id,
                user_nickname=user_nickname,
                group_id=group_id,
                group_name=group_name,
                images=images,
            )
            try:
                await self._ws.send(
                    json.dumps(self._build_envelope(payload), ensure_ascii=False)
                )
                logger.debug(
                    f"[MaiBot/{self.platform}] 透传消息: user={user_id}, group={group_id}"
                )
            except Exception as e:
                logger.debug(f"[MaiBot/{self.platform}] 透传失败: {e}")

    async def sync_tools(self, tools: list[dict[str, Any]]) -> None:
        """将 AstrBot 工具列表推送给 MaiBot。"""
        if not self._connected or not self._ws:
            logger.warning(f"[MaiBot/{self.platform}] sync_tools: 未连接")
            return
        msg = {
            "type": "custom_tool_sync",
            "platform": self.platform,
            "content": {"tools": tools},
        }
        try:
            await self._ws.send(json.dumps(msg, ensure_ascii=False))
            logger.info(f"[MaiBot/{self.platform}] 已同步 {len(tools)} 个工具")
        except Exception as e:
            logger.error(f"[MaiBot/{self.platform}] sync_tools 失败: {e}")

    async def _collect_responses(self) -> list[dict]:
        """从全局队列中收集 MaiBot 的回复 payload。"""
        collected: list[dict] = []
        deadline = time.monotonic() + self.timeout

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                msg = await asyncio.wait_for(
                    self._global_queue.get(), timeout=remaining
                )
            except asyncio.TimeoutError:
                break

            payload = msg.get("payload", {})
            if not _segment_has_content(payload.get("message_segment", {})):
                continue

            collected.append(payload)
            # 短暂等待同一话题的后续分段
            try:
                while True:
                    msg2 = await asyncio.wait_for(
                        self._global_queue.get(), timeout=_FOLLOWUP_WINDOW
                    )
                    p2 = msg2.get("payload", {})
                    if _segment_has_content(p2.get("message_segment", {})):
                        collected.append(p2)
            except asyncio.TimeoutError:
                pass
            break

        if not collected:
            raise asyncio.TimeoutError(
                f"MaiBot 未在 {self.timeout}s 内返回有效内容（platform={self.platform}）"
            )
        return collected


# ---------------------------------------------------------------------------
# 独立工具函数（模块级，可被多处复用）
# ---------------------------------------------------------------------------


def _segment_has_content(segment: dict) -> bool:
    """检查 Seg 是否含有可展示的内容。"""
    seg_type = segment.get("type", "")
    data = segment.get("data")
    if seg_type == "text":
        return isinstance(data, str) and bool(data.strip())
    if seg_type in ("image", "emoji", "imageurl"):
        return bool(data)
    if seg_type == "seglist" and isinstance(data, list):
        return any(_segment_has_content(s) for s in data if isinstance(s, dict))
    return False


def parse_segment_to_components(segment: dict) -> list:
    """将 MaiBot Seg 递归转换为 AstrBot 消息组件列表。

    此函数为模块级函数，供 main.py 和 maibot_agent_runner.py 共同使用，
    消除两处重复的 _parse_segment 实现。
    """
    import astrbot.core.message.components as Comp  # 延迟导入，避免循环依赖

    result = []
    seg_type = segment.get("type", "")
    data = segment.get("data")

    if seg_type == "text" and isinstance(data, str) and data.strip():
        result.append(Comp.Plain(data))
    elif seg_type == "image" and isinstance(data, str) and data:
        # 图片类型：按 base64 处理
        result.append(Comp.Image.fromBase64(_strip_data_uri(data)))
    elif seg_type == "emoji" and isinstance(data, str) and data:
        # emoji 类型：如果是 base64 图片则渲染为图片，否则作为纯文本
        if data.startswith("data:image") or len(data) > 100:  # 假设长数据是 base64
            result.append(Comp.Image.fromBase64(_strip_data_uri(data)))
        else:
            # 短文本或 unicode emoji，作为纯文本
            result.append(Comp.Plain(data))
    elif seg_type == "imageurl" and isinstance(data, str) and data:
        result.append(Comp.Image.fromURL(data))
    elif seg_type == "seglist" and isinstance(data, list):
        for sub in data:
            if isinstance(sub, dict):
                result.extend(parse_segment_to_components(sub))
    return result


def extract_text_from_segment(segment: dict) -> str:
    """从 Seg 结构中递归提取纯文本（fallback 用途）。"""
    seg_type = segment.get("type", "")
    data = segment.get("data")
    if seg_type == "text" and isinstance(data, str):
        return data
    if seg_type == "seglist" and isinstance(data, list):
        parts = [extract_text_from_segment(s) for s in data if isinstance(s, dict)]
        return "\n".join(p for p in parts if p)
    if seg_type in ("image", "emoji"):
        return "[图片]"
    if seg_type in ("voice", "voiceurl"):
        return "[语音]"
    if seg_type in ("video", "videourl"):
        return "[视频]"
    return ""


# ---------------------------------------------------------------------------
# 多平台路由客户端（对外暴露）
# ---------------------------------------------------------------------------


class MaiBotWSClient:
    """多平台 MaiBot WS 路由客户端。

    每个不同的 platform 对应一条独立的 _PlatformChannel（WS 持久连接）。
    插件从 AstrBot UMO 中解析真实平台名传入，MaiBot 回复时原样回传，
    插件据此路由回原始 AstrBot 会话。
    """

    def __init__(
        self,
        ws_url: str,
        api_key: str,
        timeout: int = 120,
        keepalive_interval: int = 20,
        bot_user_id: str = "astrbot",
        bot_nickname: str = "AstrBot",
        reconnect_interval: int = 5,
        debug_mode: bool = False,
    ) -> None:
        self.ws_url = ws_url
        self.api_key = api_key
        self.timeout = timeout
        self.keepalive_interval = keepalive_interval
        self.bot_user_id = bot_user_id
        self.bot_nickname = bot_nickname
        self.reconnect_interval = reconnect_interval
        self.debug_mode = debug_mode

        self._channels: dict[str, _PlatformChannel] = {}
        self._channel_lock = asyncio.Lock()
        self._proactive_handler: Callable[..., Awaitable[None]] | None = None
        self._tool_call_handler: Callable[..., Awaitable[str]] | None = None

    # ── 通道管理 ──────────────────────────────────────────────────────────────

    async def get_channel(self, platform: str) -> _PlatformChannel:
        """获取（或懒创建）指定平台的 WS 通道。"""
        async with self._channel_lock:
            if platform not in self._channels:
                ch = _PlatformChannel(
                    ws_url=self.ws_url,
                    api_key=self.api_key,
                    platform=platform,
                    timeout=self.timeout,
                    keepalive_interval=self.keepalive_interval,
                    bot_user_id=self.bot_user_id,
                    bot_nickname=self.bot_nickname,
                    reconnect_interval=self.reconnect_interval,
                    debug_mode=self.debug_mode,
                )
                ch._proactive_handler = self._proactive_handler
                ch._tool_call_handler = self._tool_call_handler
                self._channels[platform] = ch
                logger.info(f"[MaiBotWSClient] 创建平台通道: {platform}")
            return self._channels[platform]

    # ── 回调注册 ──────────────────────────────────────────────────────────────

    def set_proactive_message_handler(
        self,
        handler: Callable[..., Awaitable[None]] | None,
    ) -> None:
        """注册主动消息处理器。签名：``async (msg: dict, platform: str) -> None``"""
        self._proactive_handler = handler
        for ch in self._channels.values():
            ch._proactive_handler = handler

    def set_tool_call_handler(
        self,
        handler: Callable[..., Awaitable[str]] | None,
    ) -> None:
        """注册工具调用处理器。签名：``async (tool_name: str, args: dict) -> str``"""
        self._tool_call_handler = handler
        for ch in self._channels.values():
            ch._tool_call_handler = handler

    # ── 发送接口 ──────────────────────────────────────────────────────────────

    async def send_and_receive(
        self,
        platform: str,
        text: str,
        user_id: str,
        user_nickname: str = "",
        group_id: str | None = None,
        group_name: str = "",
        images: list[str] | None = None,
    ) -> list[dict]:
        """向指定平台的 MaiBot 通道发送消息并等待回复。"""
        ch = await self.get_channel(platform)
        return await ch.send_and_receive(
            text=text,
            user_id=user_id,
            user_nickname=user_nickname,
            group_id=group_id,
            group_name=group_name,
            images=images,
        )

    async def send_only(
        self,
        platform: str,
        text: str,
        user_id: str,
        user_nickname: str = "",
        group_id: str | None = None,
        group_name: str = "",
        images: list[str] | None = None,
    ) -> None:
        """透传消息到 MaiBot（不等待回复）。"""
        ch = await self.get_channel(platform)
        await ch.send_only(
            text=text,
            user_id=user_id,
            user_nickname=user_nickname,
            group_id=group_id,
            group_name=group_name,
            images=images,
        )

    async def sync_tools(
        self, tools: list[dict[str, Any]], platform: str | None = None
    ) -> None:
        """向指定平台（或所有已连接平台）同步工具列表。"""
        if platform:
            ch = await self.get_channel(platform)
            await ch.sync_tools(tools)
        else:
            for ch in list(self._channels.values()):
                await ch.sync_tools(tools)

    # ── 文本提取（兼容旧调用方） ──────────────────────────────────────────────

    def _extract_text_from_payload(self, payload: dict) -> str:
        """从 maim_message payload 中提取纯文本（fallback 用途）。"""
        return extract_text_from_segment(payload.get("message_segment", {}))

    # ── 关闭 ─────────────────────────────────────────────────────────────────

    async def close(self) -> None:
        """关闭所有平台通道。"""
        for ch in list(self._channels.values()):
            try:
                await ch.close()
            except Exception as e:
                logger.warning(f"[MaiBotWSClient] 关闭通道 {ch.platform} 出错: {e}")
        self._channels.clear()
