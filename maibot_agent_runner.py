import sys
import typing as T

import astrbot.core.message.components as Comp
from astrbot import logger
from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.agent.response import AgentResponse, AgentResponseData
from astrbot.core.agent.run_context import ContextWrapper, TContext
from astrbot.core.agent.runners.base import AgentState, BaseAgentRunner
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.provider.entities import (
    LLMResponse,
    ProviderRequest,
)
from astrbot.core.provider.register import llm_tools

from .maibot_ws_client import MaiBotWSClient

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override


# Constants
MAIBOT_PROVIDER_TYPE = "maibot"

DEFAULT_WS_URL = "ws://127.0.0.1:18040/ws"
DEFAULT_PLATFORM = "astrbot"
DEFAULT_TIMEOUT = 120


class MaiBotAgentRunner(BaseAgentRunner[TContext]):
    """MaiBot Agent Runner.

    Communicates with MaiBot's API-Server via WebSocket,
    using the maim_message envelope protocol.
    """

    # Class-level WS client cache: persists across runner instances
    _ws_clients: T.ClassVar[dict[str, MaiBotWSClient]] = {}

    @classmethod
    def get_ws_client(cls) -> MaiBotWSClient | None:
        """Get the first available MaiBot WS client (public utility)."""
        if cls._ws_clients:
            return next(iter(cls._ws_clients.values()))
        return None

    @override
    async def reset(
        self,
        run_context: ContextWrapper[TContext],
        agent_hooks: BaseAgentRunHooks[TContext],
        **kwargs: T.Any,
    ) -> None:
        self.req = kwargs.get("request")
        self.streaming = kwargs.get("streaming", False)
        self.final_llm_resp = None
        self._state = AgentState.IDLE
        self.agent_hooks = agent_hooks
        self.run_context = run_context

        # Extract MaiBot-specific config from provider_config kwarg
        provider_config: dict = kwargs.get("provider_config", {})
        self.ws_url = provider_config.get("maibot_ws_url", DEFAULT_WS_URL)
        self.api_key = provider_config.get("maibot_api_key", "")
        self.platform = provider_config.get("maibot_platform", DEFAULT_PLATFORM)
        self.timeout = provider_config.get("timeout", DEFAULT_TIMEOUT)
        self.bot_user_id = provider_config.get("maibot_bot_qq", "")
        self.bot_nickname = provider_config.get("maibot_bot_nickname", "")

        if isinstance(self.timeout, str):
            self.timeout = int(self.timeout)

        if not self.api_key:
            raise ValueError(
                "MaiBot API Key 不能为空。请在 AstrBot 配置中填写 MaiBot 的 API Key。"
            )

        if not self.ws_url:
            raise ValueError(
                "MaiBot WebSocket URL 不能为空。请在 AstrBot 配置中填写 MaiBot 的 WS 地址。"
            )

        # Create or reuse WS client (class-level singleton per config)
        client_key = f"{self.ws_url}|{self.api_key}|{self.platform}"
        if client_key in MaiBotAgentRunner._ws_clients:
            self.ws_client = MaiBotAgentRunner._ws_clients[client_key]
            logger.debug("[MaiBot] Reusing existing persistent WS client.")
        else:
            self.ws_client = MaiBotWSClient(
                ws_url=self.ws_url,
                api_key=self.api_key,
                platform=self.platform,
                timeout=self.timeout,
                bot_user_id=self.bot_user_id,
                bot_nickname=self.bot_nickname,
            )
            MaiBotAgentRunner._ws_clients[client_key] = self.ws_client
            logger.info("[MaiBot] Created new persistent WS client.")

        # Set up tool call handler so MaiBot can call AstrBot tools
        self.ws_client.set_tool_call_handler(self._handle_tool_call)

        # Sync tools to MaiBot after connection is established
        await self._sync_tools_to_maibot()

    @override
    async def step(self):
        """Execute one step: send message to MaiBot and get response."""
        if not self.req:
            raise ValueError("Request is not set. Please call reset() first.")

        if self._state == AgentState.IDLE:
            try:
                await self.agent_hooks.on_agent_begin(self.run_context)
            except Exception as e:
                logger.error(f"Error in on_agent_begin hook: {e}", exc_info=True)

        self._transition_state(AgentState.RUNNING)

        try:
            async for response in self._execute_maibot_request():
                yield response
        except Exception as e:
            error_msg = f"MaiBot 请求失败：{str(e)}"
            logger.error(error_msg)
            self._transition_state(AgentState.ERROR)
            self.final_llm_resp = LLMResponse(role="err", completion_text=error_msg)
            yield AgentResponse(
                type="err",
                data=AgentResponseData(chain=MessageChain().message(error_msg)),
            )

    @override
    async def step_until_done(
        self, max_step: int = 30
    ) -> T.AsyncGenerator[AgentResponse, None]:
        while not self.done():
            async for resp in self.step():
                yield resp

    @staticmethod
    def _parse_umo(umo: str) -> tuple[str, str, str]:
        """Parse a Unified Message Origin string.

        Format: "platform:MessageType:session_id"
        e.g. "qq:GroupMessage:123456" or "qq:FriendMessage:user789"

        Returns: (platform_name, message_type, session_id)
        """
        parts = umo.split(":", 2)
        if len(parts) >= 3:
            return parts[0], parts[1], parts[2]
        elif len(parts) == 2:
            return parts[0], parts[1], ""
        return umo, "", ""

    def _extract_sender_info(self) -> tuple[str, str]:
        """Extract sender user_id and nickname from the run context event.

        Returns: (sender_user_id, sender_nickname)
        """
        try:
            ctx = getattr(self.run_context, "context", None)
            if ctx:
                event = getattr(ctx, "event", None)
                if event:
                    # AstrMessageEvent has sender_id / nickname
                    sender_id = getattr(event, "sender_id", "") or ""
                    nickname = getattr(event, "nickname", "") or ""
                    return sender_id, nickname
        except Exception:
            pass
        return "", ""

    async def _execute_maibot_request(self):
        """Core logic: send request to MaiBot and process the response."""
        prompt = self.req.prompt if self.req else ""
        session_id = self.req.session_id if self.req else "unknown"
        image_urls = self.req.image_urls if self.req else []

        if not prompt and not image_urls:
            logger.warning("[MaiBot] Empty prompt and no images, skipping request.")
            self._transition_state(AgentState.DONE)
            chain = MessageChain(chain=[Comp.Plain("")])
            self.final_llm_resp = LLMResponse(role="assistant", result_chain=chain)
            yield AgentResponse(
                type="llm_result",
                data=AgentResponseData(chain=chain),
            )
            return

        # Parse UMO to determine group/private chat
        # session_id = event.unified_msg_origin, e.g. "qq:GroupMessage:123456"
        platform_name, message_type, raw_session_id = self._parse_umo(session_id)
        is_group = "Group" in message_type

        # Extract real sender info from event
        sender_user_id, sender_nickname = self._extract_sender_info()
        if not sender_user_id:
            sender_user_id = raw_session_id or session_id
        if not sender_nickname:
            sender_nickname = sender_user_id

        # For group chat: group_id = UMO (so we can recover it from replies)
        #                  user_id = real sender
        # For private:     group_id = None
        #                  user_id = UMO (so we can recover it from replies)
        if is_group:
            ws_user_id = sender_user_id
            ws_user_nickname = sender_nickname
            ws_group_id = session_id  # full UMO as group_id
            ws_group_name = raw_session_id
        else:
            ws_user_id = session_id  # full UMO as user_id
            ws_user_nickname = sender_nickname or session_id
            ws_group_id = None
            ws_group_name = ""

        logger.info(
            f"[MaiBot] Sending to MaiBot: '{prompt[:100]}...' "
            f"(umo={session_id}, is_group={is_group}, "
            f"user={ws_user_id}, group={ws_group_id}, images={len(image_urls)})"
        )

        # Send message and wait for response segments.
        # Note: the passthrough handler may have already sent this message
        # for context, but send_and_receive uses a unique message_id so
        # MaiBot treats it as a separate interaction and responds to it.
        response_payloads = await self.ws_client.send_and_receive(
            text=prompt,
            user_id=ws_user_id,
            user_nickname=ws_user_nickname,
            group_id=ws_group_id,
            group_name=ws_group_name,
            images=image_urls if image_urls else None,
        )

        logger.info(
            f"[MaiBot] Received {len(response_payloads)} response payload(s)"
        )

        # Convert MaiBot payloads to AstrBot message components
        # Each payload may contain text, images, or seglist
        chain_components = []
        for payload in response_payloads:
            segment = payload.get("message_segment", {})
            components = self._parse_segment_to_components(segment)
            chain_components.extend(components)

        if not chain_components:
            # Fallback: if parsing produced nothing, extract text as before
            for payload in response_payloads:
                text = self.ws_client._extract_text_from_payload(payload)
                if text:
                    chain_components.append(Comp.Plain(text))

        chain = MessageChain(chain=chain_components)

        self.final_llm_resp = LLMResponse(role="assistant", result_chain=chain)
        self._transition_state(AgentState.DONE)

        try:
            await self.agent_hooks.on_agent_done(self.run_context, self.final_llm_resp)
        except Exception as e:
            logger.error(f"Error in on_agent_done hook: {e}", exc_info=True)

        yield AgentResponse(
            type="llm_result",
            data=AgentResponseData(chain=chain),
        )

    # ─── Segment Parsing ─────────────────────────────────────────────

    @staticmethod
    def _parse_segment_to_components(segment: dict) -> list:
        """Convert MaiBot Seg to AstrBot message components.

        Handles: text, image (base64), emoji (base64), seglist (recursive).
        """
        result = []
        seg_type = segment.get("type", "")
        data = segment.get("data")

        if seg_type == "text" and isinstance(data, str) and data.strip():
            result.append(Comp.Plain(data))
        elif seg_type in ("image", "emoji") and isinstance(data, str) and data:
            b64 = data
            for prefix in ("data:image/png;base64,", "data:image/gif;base64,",
                           "data:image/jpeg;base64,", "data:image/webp;base64,"):
                if b64.startswith(prefix):
                    b64 = b64[len(prefix):]
                    break
            result.append(Comp.Image.fromBase64(b64))
        elif seg_type == "imageurl" and isinstance(data, str) and data:
            result.append(Comp.Image.fromURL(data))
        elif seg_type == "seglist" and isinstance(data, list):
            for sub_seg in data:
                if isinstance(sub_seg, dict):
                    result.extend(
                        MaiBotAgentRunner._parse_segment_to_components(sub_seg)
                    )
        return result

    # ─── Tool Injection ──────────────────────────────────────────────

    async def _sync_tools_to_maibot(self) -> None:
        """Push AstrBot's available tools to MaiBot via the WS client."""
        try:
            await self.ws_client.ensure_connected()
        except Exception as e:
            logger.warning(f"[MaiBot] Cannot sync tools, connection failed: {e}")
            return

        tool_defs: list[dict] = []
        for func_tool in llm_tools.func_list:
            if not func_tool.active:
                continue
            tool_defs.append({
                "name": func_tool.name,
                "description": func_tool.description or "",
                "parameters": func_tool.parameters or {},
            })

        if tool_defs:
            await self.ws_client.sync_tools(tool_defs)
        else:
            logger.debug("[MaiBot] No active AstrBot tools to sync.")

    async def _handle_tool_call(self, tool_name: str, tool_args: dict) -> str:
        """Handle a tool_call from MaiBot by executing the AstrBot tool."""
        func_tool = llm_tools.get_func(tool_name)
        if not func_tool:
            return f"Tool '{tool_name}' not found in AstrBot."

        if not func_tool.handler:
            return f"Tool '{tool_name}' has no handler."

        try:
            logger.info(f"[MaiBot] Executing AstrBot tool: {tool_name}({tool_args})")
            import inspect
            sig = inspect.signature(func_tool.handler)
            params = list(sig.parameters.keys())
            if params and params[0] == "event":
                # Get the real AstrBot event from the current run context
                event = None
                if hasattr(self, "run_context") and self.run_context:
                    ctx = getattr(self.run_context, "context", None)
                    if ctx:
                        event = getattr(ctx, "event", None)
                result = await func_tool.handler(event, **tool_args)  # type: ignore
            else:
                result = await func_tool.handler(**tool_args)  # type: ignore
            if result is None:
                return "Tool executed successfully (no output)."
            return str(result)
        except Exception as e:
            logger.error(f"[MaiBot] Tool '{tool_name}' execution error: {e}", exc_info=True)
            return f"Tool execution error: {e}"

    async def close(self):
        """Clean up WebSocket resources."""
        await self.ws_client.close()

    @override
    def done(self) -> bool:
        """Check if the agent has completed."""
        return self._state in (AgentState.DONE, AgentState.ERROR)

    @override
    def get_final_llm_resp(self) -> LLMResponse | None:
        return self.final_llm_resp
