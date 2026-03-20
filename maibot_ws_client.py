import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import websockets

from astrbot.core import logger


class MaiBotWSClient:
    """WebSocket client for communicating with MaiBot's API-Server.

    Maintains a persistent WebSocket connection so MaiBot can route
    responses back via message_dim matching (api_key + platform).
    """

    def __init__(
        self,
        ws_url: str,
        api_key: str,
        platform: str = "astrbot",
        timeout: int = 120,
        keepalive_interval: int = 20,
        bot_user_id: str = "",
        bot_nickname: str = "",
    ):
        self.ws_url = ws_url.rstrip("/")
        self.api_key = api_key
        self.platform = platform
        self.timeout = timeout
        self.keepalive_interval = keepalive_interval
        self.bot_user_id = bot_user_id or "astrbot"
        self.bot_nickname = bot_nickname or "AstrBot"

        self._ws = None
        self._listener_task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._response_queues: dict[str, asyncio.Queue] = {}
        self._global_queue: asyncio.Queue = asyncio.Queue()
        self._connected = False
        self._connect_lock = asyncio.Lock()

        # Tool injection support
        self._tool_call_handler: Callable[..., Awaitable[str]] | None = None
        self._tool_call_futures: dict[str, asyncio.Future] = {}

        # Proactive message support
        self._proactive_message_handler: Callable[..., Awaitable[None]] | None = None
        self._in_request: bool = False

    def _build_connect_url(self) -> str:
        """Build WS URL with api_key and platform query params.

        MaiBot's server_ws_connection.py FastAPI endpoint extracts these
        as query parameters: websocket_endpoint(ws, api_key=None, platform=None)
        """
        sep = "&" if "?" in self.ws_url else "?"
        return f"{self.ws_url}{sep}api_key={self.api_key}&platform={self.platform}"

    def _build_headers(self) -> dict:
        """Build HTTP headers for WS handshake as fallback.

        server_ws_connection.py also checks x-apikey and x-platform headers.
        """
        return {
            "x-apikey": self.api_key,
            "x-platform": self.platform,
        }

    async def ensure_connected(self):
        """Ensure the persistent WebSocket connection is alive."""
        async with self._connect_lock:
            if self._ws is not None and self._connected:
                # Check if connection is still open
                try:
                    # Simple check: see if the connection is still open
                    pong = await asyncio.wait_for(self._ws.ping(), timeout=5.0)
                    await pong
                    return
                except Exception:
                    logger.warning("[MaiBot] Persistent connection lost, reconnecting...")
                    self._connected = False
                    self._ws = None

            url = self._build_connect_url()
            headers = self._build_headers()
            logger.info(f"[MaiBot] Connecting to {url} with platform={self.platform}...")
            try:
                self._ws = await websockets.connect(
                    url,
                    additional_headers=headers,
                )
                self._connected = True
                logger.info("[MaiBot] WebSocket connection established.")

                # Start background listener
                if self._listener_task is None or self._listener_task.done():
                    self._listener_task = asyncio.create_task(self._listen_loop())

                # Start keepalive heartbeat
                if self._keepalive_task is None or self._keepalive_task.done():
                    self._keepalive_task = asyncio.create_task(self._keepalive_loop())
            except Exception as e:
                logger.error(f"[MaiBot] Failed to connect: {e}")
                raise

    async def _listen_loop(self):
        """Background task that receives all messages from MaiBot."""
        try:
            while self._connected and self._ws is not None:
                try:
                    raw = await self._ws.recv()
                except websockets.exceptions.ConnectionClosed:
                    logger.warning("[MaiBot] Connection closed by server.")
                    self._connected = False
                    break
                except Exception as e:
                    logger.warning(f"[MaiBot] Recv error: {e}")
                    self._connected = False
                    break

                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning(f"[MaiBot] Non-JSON message: {str(raw)[:200]}")
                    continue

                msg_type = msg.get("type", "")

                if msg_type == "sys_ack":
                    logger.debug(
                        f"[MaiBot] ACK received for: {msg.get('meta', {}).get('acked_msg_id', '?')}"
                    )
                    continue

                # Handle tool_call requests from MaiBot
                # Can come as:
                #   1. Direct {"type": "tool_call", ...}
                #   2. Legacy custom message: {"is_custom_message": true, "message_type_name": "tool_call", ...}
                #   3. API Server envelope: {"type": "custom_tool_call", "payload": {...}}
                is_tool_call = (
                    msg_type == "tool_call"
                    or msg_type == "custom_tool_call"
                    or (msg.get("is_custom_message") and msg.get("message_type_name") == "tool_call")
                )
                if is_tool_call:
                    # Extract tool call data based on format
                    if msg_type == "custom_tool_call":
                        call_data = msg.get("payload", msg)
                    elif msg.get("is_custom_message"):
                        call_data = msg.get("content", msg)
                    else:
                        call_data = msg
                    asyncio.create_task(self._handle_tool_call(call_data))
                    continue

                if msg_type == "sys_std":
                    if self._in_request:
                        # During active request-response, queue for _collect_response
                        await self._global_queue.put(msg)
                    elif self._proactive_message_handler:
                        # Idle: this is a proactive message from MaiBot
                        logger.info("[MaiBot] 收到主动消息，分派到主动消息处理器")
                        asyncio.create_task(
                            self._safe_proactive_handler(msg)
                        )
                    else:
                        logger.debug(
                            "[MaiBot] 收到主动消息但未注册处理器，丢弃"
                        )
                else:
                    logger.debug(f"[MaiBot] Unknown message type: {msg_type}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[MaiBot] Listener loop error: {e}")
        finally:
            self._connected = False

    def _build_envelope(self, payload: dict) -> dict:
        """Wrap a payload dict into a sys_std envelope."""
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

    def _build_message_payload(
        self,
        text: str,
        user_id: str,
        user_nickname: str = "",
        group_id: str | None = None,
        group_name: str = "",
        images: list[str] | None = None,
    ) -> dict:
        """Construct a maim_message payload from AstrBot request data."""
        message_id = f"astrbot_msg_{uuid.uuid4().hex[:12]}"

        # Build Seg list
        segments: list[dict] = []
        if text:
            segments.append({"type": "text", "data": text})
        if images:
            for img_b64 in images:
                segments.append({"type": "image", "data": img_b64})

        if len(segments) == 0:
            message_segment = {"type": "text", "data": ""}
        elif len(segments) == 1:
            message_segment = segments[0]
        else:
            message_segment = {"type": "seglist", "data": segments}

        sender_info: dict = {
            "user_info": {
                "platform": self.platform,
                "user_id": user_id,
                "user_nickname": user_nickname or user_id,
                "user_cardname": user_nickname or user_id,
            }
        }
        if group_id:
            sender_info["group_info"] = {
                "platform": self.platform,
                "group_id": group_id,
                "group_name": group_name or group_id,
            }

        # format_info tells MaiBot what content types we accept
        format_info = {
            "content_format": ["text", "image", "emoji"],
            "accept_format": ["text", "image", "emoji", "voice", "video"],
        }

        payload = {
            "message_info": {
                "platform": self.platform,
                "message_id": message_id,
                "time": time.time(),
                "user_info": {
                    "platform": self.platform,
                    "user_id": self.bot_user_id,
                    "user_nickname": self.bot_nickname,
                },
                "sender_info": sender_info,
                "format_info": format_info,
            },
            "message_segment": message_segment,
            "message_dim": {
                "api_key": self.api_key,
                "platform": self.platform,
            },
        }
        return payload

    async def send_and_receive(
        self,
        text: str,
        user_id: str,
        user_nickname: str = "",
        group_id: str | None = None,
        group_name: str = "",
        images: list[str] | None = None,
    ) -> list[dict]:
        """Send a message to MaiBot and wait for the response.

        Returns a list of payload dicts (one per MaiBot reply message).
        Each payload contains message_segment, message_info, etc.
        Maintains a persistent connection so MaiBot can route
        the response back to us.
        """
        await self.ensure_connected()

        payload = self._build_message_payload(
            text=text,
            user_id=user_id,
            user_nickname=user_nickname,
            group_id=group_id,
            group_name=group_name,
            images=images,
        )
        envelope = self._build_envelope(payload)

        self._in_request = True
        try:
            await self._ws.send(json.dumps(envelope, ensure_ascii=False))
            logger.debug("[MaiBot] Message sent, waiting for response ...")

            # Drain any stale messages from queue before waiting
            while not self._global_queue.empty():
                try:
                    self._global_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

            # Wait for response(s) from MaiBot
            return await self._collect_response_payloads()

        except websockets.exceptions.ConnectionClosed as e:
            self._connected = False
            raise Exception(f"MaiBot WebSocket connection closed: {e}")
        except asyncio.TimeoutError:
            raise asyncio.TimeoutError(
                f"MaiBot did not respond within {self.timeout}s timeout."
            )
        except Exception as e:
            raise Exception(f"MaiBot communication error: {e}")
        finally:
            self._in_request = False

    async def send_only(
        self,
        text: str,
        user_id: str,
        user_nickname: str = "",
        group_id: str | None = None,
        group_name: str = "",
        images: list[str] | None = None,
    ) -> None:
        """Send a message to MaiBot without waiting for a response.

        Used for transparent message passthrough so MaiBot can see all
        messages in a chat stream for context-building and proactive replies.
        """
        try:
            await self.ensure_connected()
        except Exception as e:
            logger.debug(f"[MaiBot] send_only: 连接失败，跳过透传: {e}")
            return

        payload = self._build_message_payload(
            text=text,
            user_id=user_id,
            user_nickname=user_nickname,
            group_id=group_id,
            group_name=group_name,
            images=images,
        )
        envelope = self._build_envelope(payload)

        try:
            await self._ws.send(json.dumps(envelope, ensure_ascii=False))
            logger.debug(f"[MaiBot] 透传消息已发送: user={user_id}, group={group_id}")
        except Exception as e:
            logger.debug(f"[MaiBot] 透传消息发送失败: {e}")

    async def _collect_response_payloads(self) -> list[dict]:
        """Collect response message payloads from MaiBot via the global queue.

        Returns raw payload dicts so the caller can extract text, images, etc.
        """
        collected_payloads: list[dict] = []
        deadline = time.time() + self.timeout

        while time.time() < deadline:
            remaining = deadline - time.time()
            if remaining <= 0:
                break

            try:
                msg = await asyncio.wait_for(
                    self._global_queue.get(), timeout=min(remaining, self.timeout)
                )
            except asyncio.TimeoutError:
                break

            payload = msg.get("payload", {})
            segment = payload.get("message_segment", {})
            # Check if payload has any meaningful content
            if self._segment_has_content(segment):
                collected_payloads.append(payload)
                # Wait briefly for follow-up messages
                try:
                    while True:
                        msg2 = await asyncio.wait_for(
                            self._global_queue.get(), timeout=5.0
                        )
                        p2 = msg2.get("payload", {})
                        s2 = p2.get("message_segment", {})
                        if self._segment_has_content(s2):
                            collected_payloads.append(p2)
                except asyncio.TimeoutError:
                    pass
                break

        if collected_payloads:
            return collected_payloads

        raise asyncio.TimeoutError(
            f"MaiBot did not send a meaningful response within {self.timeout}s."
        )

    def _segment_has_content(self, segment: dict) -> bool:
        """Check if a Seg has any meaningful content (text, image, etc)."""
        seg_type = segment.get("type", "")
        data = segment.get("data")
        if seg_type == "text" and isinstance(data, str) and data.strip():
            return True
        if seg_type in ("image", "emoji", "imageurl") and data:
            return True
        if seg_type == "seglist" and isinstance(data, list):
            return any(
                self._segment_has_content(sub)
                for sub in data
                if isinstance(sub, dict)
            )
        return False

    def _extract_text_from_payload(self, payload: dict) -> str:
        """Extract text content from a maim_message payload."""
        segment = payload.get("message_segment", {})
        return self._extract_text_from_segment(segment)

    def _extract_text_from_segment(self, segment: dict) -> str:
        """Recursively extract text from a Seg structure."""
        seg_type = segment.get("type", "")
        data = segment.get("data")

        if seg_type == "text" and isinstance(data, str):
            return data
        elif seg_type == "seglist" and isinstance(data, list):
            parts = []
            for sub_seg in data:
                if isinstance(sub_seg, dict):
                    t = self._extract_text_from_segment(sub_seg)
                    if t:
                        parts.append(t)
            return "\n".join(parts) if parts else ""
        elif seg_type in ("image", "emoji") and isinstance(data, str):
            return "[图片]"
        elif seg_type == "voice" or seg_type == "voiceurl":
            return "[语音]"
        elif seg_type == "video" or seg_type == "videourl":
            return "[视频]"

        return ""

    def set_proactive_message_handler(
        self, handler: Callable[..., Awaitable[None]] | None
    ) -> None:
        """Register a callback for handling proactive messages from MaiBot.

        The handler signature: async (msg: dict) -> None
        where msg is the raw sys_std envelope dict.
        """
        self._proactive_message_handler = handler

    async def _safe_proactive_handler(self, msg: dict) -> None:
        """Wrapper to catch errors in the proactive message handler."""
        try:
            if self._proactive_message_handler:
                await self._proactive_message_handler(msg)
        except Exception as e:
            logger.error(f"[MaiBot] 主动消息处理出错: {e}", exc_info=True)

    # ─── Tool Injection Protocol ──────────────────────────────────────

    def set_tool_call_handler(
        self, handler: Callable[..., Awaitable[str]] | None
    ) -> None:
        """Register a callback for handling tool_call requests from MaiBot.

        The handler signature: async (tool_name: str, args: dict) -> str
        """
        self._tool_call_handler = handler

    async def sync_tools(self, tools: list[dict[str, Any]]) -> None:
        """Push AstrBot's available tool definitions to MaiBot via WS.

        Uses maim_message's custom message format so MaiBot's
        BaseMessageHandler.process_message() routes it to a registered
        custom handler for "tool_sync".

        Each tool dict should have: name, description, parameters (JSON Schema).
        """
        if not self._connected or not self._ws:
            logger.warning("[MaiBot] Cannot sync tools: not connected.")
            return

        # Use maim_message API Server envelope format:
        # server_ws_api.py checks type.startswith('custom_') and dispatches
        # to config.custom_handlers[type]
        msg = {
            "type": "custom_tool_sync",
            "platform": self.platform,
            "content": {"tools": tools},
        }
        try:
            await self._ws.send(json.dumps(msg, ensure_ascii=False))
            tool_names = [t.get("name", "?") for t in tools]
            logger.info(f"[MaiBot] Synced {len(tools)} tools to MaiBot: {tool_names}")
        except Exception as e:
            logger.error(f"[MaiBot] Failed to sync tools: {e}")

    async def _handle_tool_call(self, msg: dict) -> None:
        """Handle a tool_call request from MaiBot: execute the tool and send result back."""
        call_id = msg.get("call_id", "")
        tool_name = msg.get("name", "")
        tool_args = msg.get("args", {})

        logger.info(f"[MaiBot] Received tool_call: {tool_name}(call_id={call_id})")

        result_text = ""
        if self._tool_call_handler:
            try:
                result_text = await self._tool_call_handler(tool_name, tool_args)
            except Exception as e:
                result_text = f"Error executing tool {tool_name}: {e}"
                logger.error(f"[MaiBot] Tool execution error: {e}")
        else:
            result_text = f"No tool handler registered for {tool_name}"
            logger.warning("[MaiBot] tool_call received but no handler registered.")

        # Send tool_result back to MaiBot using API Server envelope format
        result_msg = {
            "type": "custom_tool_result",
            "call_id": call_id,
            "name": tool_name,
            "result": {"content": result_text},
        }
        logger.info(f"[MaiBot] Tool result for {tool_name}: {str(result_text)[:200]}")
        if self._ws and self._connected:
            try:
                await self._ws.send(json.dumps(result_msg, ensure_ascii=False))
                logger.info(f"[MaiBot] Sent tool_result for {tool_name}(call_id={call_id})")
            except Exception as e:
                logger.error(f"[MaiBot] Failed to send tool_result: {e}")


    async def _keepalive_loop(self):
        """Periodically ping the server to keep the connection alive."""
        try:
            while self._connected and self._ws is not None:
                await asyncio.sleep(self.keepalive_interval)
                if not self._connected or self._ws is None:
                    break
                try:
                    pong = await asyncio.wait_for(self._ws.ping(), timeout=10.0)
                    await pong
                    logger.debug("[MaiBot] Keepalive ping OK.")
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"[MaiBot] Keepalive ping failed: {e}, reconnecting...")
                    self._connected = False
                    try:
                        await self.ensure_connected()
                    except Exception as re:
                        logger.error(f"[MaiBot] Keepalive reconnect failed: {re}")
                    break
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"[MaiBot] Keepalive loop exited: {e}")

    async def close(self):
        """Close the persistent WebSocket connection."""
        self._connected = False
        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except asyncio.CancelledError:
                pass
            self._keepalive_task = None
        if self._listener_task and not self._listener_task.done():
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
            self._listener_task = None
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
