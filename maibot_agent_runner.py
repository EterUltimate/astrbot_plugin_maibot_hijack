"""
MaiBotAgentRunner — AstrBot Agent Runner 接入 MaiBot

将 MaiBot 注册为 AstrBot 的一个 Agent Runner（LLM Provider），
通过 WS 将请求转发给 MaiBot，并将 MaiBot 的回复转换为 AstrBot 消息链。

平台感知
--------
* 从 ProviderRequest.session_id（即 event.unified_msg_origin）解析真实平台名。
* 向 MaiBot 发送消息时，message_info.platform 使用该真实平台名。
* 每个平台独享一条 WS 持久连接（由 MaiBotWSClient 内部管理）。
"""

from __future__ import annotations

import inspect
import sys
import typing as T

import astrbot.core.message.components as Comp
from astrbot.api import logger
from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.agent.response import AgentResponse, AgentResponseData
from astrbot.core.agent.run_context import ContextWrapper, TContext
from astrbot.core.agent.runners.base import AgentState, BaseAgentRunner
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.provider.entities import LLMResponse, ProviderRequest
from astrbot.core.provider.register import llm_tools

from .maibot_ws_client import MaiBotWSClient, parse_segment_to_components

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

_DEFAULT_WS_URL = "ws://127.0.0.1:18040/ws"
_DEFAULT_TIMEOUT = 120
_FALLBACK_PLATFORM = "astrbot"


class MaiBotAgentRunner(BaseAgentRunner[TContext]):
    """MaiBot Agent Runner。

    类级别缓存 WS 客户端，确保同一配置下跨请求复用连接。
    每个平台（从 UMO 解析）对应一条独立的 WS 通道。
    """

    # key = "{ws_url}|{api_key}"
    _ws_clients: T.ClassVar[dict[str, MaiBotWSClient]] = {}
    _client_last_used: T.ClassVar[dict[str, float]] = {}  # 记录最后使用时间
    _CLIENT_MAX_IDLE_SECONDS: T.ClassVar[int] = 3600  # 1小时无使用则回收

    @classmethod
    def get_ws_client(cls, ws_url: str = "", api_key: str = "") -> MaiBotWSClient | None:
        """获取缓存的 WS 客户端（供外部调试使用）。"""
        if ws_url and api_key:
            return cls._ws_clients.get(f"{ws_url}|{api_key}")
        return next(iter(cls._ws_clients.values()), None)

    @classmethod
    async def cleanup_idle_clients(cls) -> None:
        """清理长时间未使用的 WS 客户端，防止内存泄漏。"""
        now = asyncio.get_event_loop().time()
        to_remove = []
        for key, last_used in list(cls._client_last_used.items()):
            if now - last_used > cls._CLIENT_MAX_IDLE_SECONDS:
                client = cls._ws_clients.get(key)
                if client:
                    try:
                        await client.close()
                        logger.info(f"[MaiBot] 回收空闲 WS 客户端: {key[:50]}...")
                    except Exception as e:
                        logger.warning(f"[MaiBot] 回收 WS 客户端失败: {e}")
                to_remove.append(key)
        for key in to_remove:
            cls._ws_clients.pop(key, None)
            cls._client_last_used.pop(key, None)

    @classmethod
    def _update_client_usage(cls, key: str) -> None:
        """更新客户端最后使用时间。"""
        cls._client_last_used[key] = asyncio.get_event_loop().time()

    @override
    async def reset(
        self,
        run_context: ContextWrapper[TContext],
        agent_hooks: BaseAgentRunHooks[TContext],
        **kwargs: T.Any,
    ) -> None:
        self.req: ProviderRequest | None = kwargs.get("request")
        self.streaming: bool = kwargs.get("streaming", False)
        self.final_llm_resp: LLMResponse | None = None
        self._state = AgentState.IDLE
        self.agent_hooks = agent_hooks
        self.run_context = run_context

        cfg: dict = kwargs.get("provider_config", {})
        self.ws_url: str = cfg.get("maibot_ws_url", _DEFAULT_WS_URL)
        self.api_key: str = cfg.get("maibot_api_key", "")
        timeout_raw = cfg.get("timeout", _DEFAULT_TIMEOUT)
        self.timeout: int = int(timeout_raw)

        if not self.api_key:
            raise ValueError("MaiBot API Key 不能为空，请在 AstrBot 配置中填写。")
        if not self.ws_url:
            raise ValueError("MaiBot WebSocket URL 不能为空，请在 AstrBot 配置中填写。")

        client_key = f"{self.ws_url}|{self.api_key}"
        
        # 清理空闲客户端
        await cls.cleanup_idle_clients()
        
        if client_key in MaiBotAgentRunner._ws_clients:
            self.ws_client = MaiBotAgentRunner._ws_clients[client_key]
            cls._update_client_usage(client_key)
            logger.debug("[MaiBot] 复用已有 WS 客户端")
        else:
            # 配置键名统一：优先使用 maibot_bot_id，兼容 maibot_bot_qq
            bot_user_id = cfg.get("maibot_bot_id") or cfg.get("maibot_bot_qq") or "astrbot"
            bot_nickname = cfg.get("maibot_bot_nickname") or "AstrBot"
            self.ws_client = MaiBotWSClient(
                ws_url=self.ws_url,
                api_key=self.api_key,
                timeout=self.timeout,
                bot_user_id=bot_user_id,
                bot_nickname=bot_nickname,
            )
            MaiBotAgentRunner._ws_clients[client_key] = self.ws_client
            cls._update_client_usage(client_key)
            logger.info("[MaiBot] 创建新 WS 客户端")

        self.ws_client.set_tool_call_handler(self._handle_tool_call)
        await self._sync_tools()

    # ── 执行步骤 ────────────────────────────────────────────────────────────

    @override
    async def step(self):
        if not self.req:
            raise ValueError("Request 未设置，请先调用 reset()。")

        if self._state == AgentState.IDLE:
            try:
                await self.agent_hooks.on_agent_begin(self.run_context)
            except Exception as e:
                logger.error(f"on_agent_begin 出错: {e}", exc_info=True)

        self._transition_state(AgentState.RUNNING)

        try:
            async for response in self._execute():
                yield response
        except Exception as e:
            err = f"MaiBot 请求失败：{e}"
            logger.error(err, exc_info=True)
            self._transition_state(AgentState.ERROR)
            self.final_llm_resp = LLMResponse(role="err", completion_text=err)
            yield AgentResponse(
                type="err",
                data=AgentResponseData(chain=MessageChain().message(err)),
            )

    @override
    async def step_until_done(
        self, max_step: int = 30
    ) -> T.AsyncGenerator[AgentResponse, None]:
        """执行直到完成或达到最大步数限制。
        
        Args:
            max_step: 最大执行步数，防止无限循环
        """
        step_count = 0
        while not self.done() and step_count < max_step:
            step_count += 1
            async for resp in self.step():
                yield resp
        
        if step_count >= max_step and not self.done():
            err_msg = f"MaiBot 执行超过最大步数限制 ({max_step})，强制终止"
            logger.warning(f"[MaiBot] {err_msg}")
            self._transition_state(AgentState.ERROR)
            self.final_llm_resp = LLMResponse(role="err", completion_text=err_msg)
            yield AgentResponse(
                type="err",
                data=AgentResponseData(chain=MessageChain().message(err_msg)),
            )

    # ── 核心执行逻辑 ─────────────────────────────────────────────────────────

    async def _execute(self):
        """将请求转发给 MaiBot，处理响应并生成 AgentResponse。"""
        assert self.req is not None
        prompt = self.req.prompt or ""
        session_id = self.req.session_id or "unknown"
        image_urls = self.req.image_urls or []

        if not prompt and not image_urls:
            logger.warning("[MaiBot] 空 prompt 且无图片，跳过")
            self._transition_state(AgentState.DONE)
            chain = MessageChain(chain=[Comp.Plain("")])
            self.final_llm_resp = LLMResponse(role="assistant", result_chain=chain)
            yield AgentResponse(type="llm_result", data=AgentResponseData(chain=chain))
            return

        platform, message_type, raw_session_id = _parse_umo(session_id)
        is_group = "group" in message_type.lower()

        sender_user_id, sender_nickname = self._extract_sender_info()
        sender_user_id = sender_user_id or raw_session_id or session_id
        sender_nickname = sender_nickname or sender_user_id

        if is_group:
            ws_user_id = sender_user_id
            ws_user_nickname = sender_nickname
            ws_group_id: str | None = raw_session_id
            ws_group_name = raw_session_id
        else:
            ws_user_id = raw_session_id or session_id
            ws_user_nickname = sender_nickname
            ws_group_id = None
            ws_group_name = ""

        logger.info(
            f"[MaiBot] → MaiBot: platform={platform}, user={ws_user_id}, "
            f"group={ws_group_id}, prompt='{prompt[:50]}', images={len(image_urls)}"
        )

        response_payloads = await self.ws_client.send_and_receive(
            platform=platform,
            text=prompt,
            user_id=ws_user_id,
            user_nickname=ws_user_nickname,
            group_id=ws_group_id,
            group_name=ws_group_name,
            images=image_urls or None,
        )

        logger.info(f"[MaiBot] ← MaiBot: {len(response_payloads)} 条 payload")

        # 解析消息组件，fallback 到纯文本提取
        chain_components: list = []
        for payload in response_payloads:
            chain_components.extend(
                parse_segment_to_components(payload.get("message_segment", {}))
            )

        if not chain_components:
            for payload in response_payloads:
                text = extract_text_from_segment(payload.get("message_segment", {}))
                if text:
                    chain_components.append(Comp.Plain(text))

        chain = MessageChain(chain=chain_components)
        self.final_llm_resp = LLMResponse(role="assistant", result_chain=chain)
        self._transition_state(AgentState.DONE)

        try:
            await self.agent_hooks.on_agent_done(self.run_context, self.final_llm_resp)
        except Exception as e:
            logger.error(f"on_agent_done 出错: {e}", exc_info=True)

        yield AgentResponse(type="llm_result", data=AgentResponseData(chain=chain))

    # ── 工具同步 ─────────────────────────────────────────────────────────────

    async def _sync_tools(self) -> None:
        """将 AstrBot 可用工具同步到 MaiBot（当前请求平台通道）。"""
        tool_defs: list[dict] = [
            {
                "name": ft.name,
                "description": ft.description or "",
                "parameters": ft.parameters or {},
            }
            for ft in llm_tools.func_list
            if ft.active
        ]
        if not tool_defs:
            return

        platform = _FALLBACK_PLATFORM
        if self.req and self.req.session_id:
            platform, _, _ = _parse_umo(self.req.session_id)

        try:
            ch = await self.ws_client.get_channel(platform)
            await ch.ensure_connected()
            await ch.sync_tools(tool_defs)
        except Exception as e:
            logger.warning(f"[MaiBot] 工具同步失败: {e}")

    async def _handle_tool_call(self, tool_name: str, tool_args: dict) -> str:
        """执行 MaiBot 请求的 AstrBot 工具。
        
        支持同步和异步 handler，自动检测并正确调用。
        """
        func_tool = llm_tools.get_func(tool_name)
        if not func_tool:
            return f"Tool '{tool_name}' not found."
        if not func_tool.handler:
            return f"Tool '{tool_name}' has no handler."

        try:
            sig = inspect.signature(func_tool.handler)
            first_param = next(iter(sig.parameters), None)
            
            # 准备参数
            if first_param == "event":
                event = None
                ctx = getattr(getattr(self, "run_context", None), "context", None)
                if ctx:
                    event = getattr(ctx, "event", None)
                call_args = (event,)
            else:
                call_args = ()
            
            # 检测 handler 是同步还是异步
            handler = func_tool.handler
            if inspect.iscoroutinefunction(handler):
                # 异步 handler
                if call_args:
                    result = await handler(call_args[0], **tool_args)
                else:
                    result = await handler(**tool_args)
            else:
                # 同步 handler，在线程池中执行避免阻塞
                import asyncio
                if call_args:
                    result = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: handler(call_args[0], **tool_args)
                    )
                else:
                    result = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: handler(**tool_args)
                    )
            
            return str(result) if result is not None else "Tool executed (no output)."
        except Exception as e:
            logger.error(f"[MaiBot] 工具 '{tool_name}' 执行出错: {e}", exc_info=True)
            return f"Tool execution error: {e}"

    # ── 工具函数 ─────────────────────────────────────────────────────────────

    def _extract_sender_info(self) -> tuple[str, str]:
        """从当前 run_context 中提取发送者 ID 和昵称。"""
        try:
            ctx = getattr(getattr(self, "run_context", None), "context", None)
            if ctx:
                event = getattr(ctx, "event", None)
                if event:
                    return (
                        str(getattr(event, "sender_id", "") or ""),
                        str(getattr(event, "nickname", "") or ""),
                    )
        except Exception:
            pass
        return "", ""

    async def close(self) -> None:
        """关闭资源。"""
        ws_client = getattr(self, "ws_client", None)
        if ws_client:
            await ws_client.close()

    @override
    def done(self) -> bool:
        return self._state in (AgentState.DONE, AgentState.ERROR)

    @override
    def get_final_llm_resp(self) -> LLMResponse | None:
        return self.final_llm_resp


# ---------------------------------------------------------------------------
# 模块级工具函数
# ---------------------------------------------------------------------------

def _parse_umo(umo: str) -> tuple[str, str, str]:
    """解析 unified_msg_origin，返回 (platform, message_type, session_id)。"""
    parts = umo.split(":", 2)
    if len(parts) >= 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return parts[0], parts[1], ""
    return umo, "", ""
