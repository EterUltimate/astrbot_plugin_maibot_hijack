# astrbot_plugin_maibot — MaiBot Adapter

> **AstrBot 作为消息聚合平台，MaiBot 专注 LLM 回复。**

## 架构概览

```
┌─────────────────────────────────────────────────────────────────┐
│                         用户 / 消息平台                           │
│   QQ(aiocqhttp)  Telegram  Discord  微信  飞书  钉钉 … 等        │
└────────────────────────────┬────────────────────────────────────┘
                             │  消息收发
┌────────────────────────────▼────────────────────────────────────┐
│                          AstrBot                                 │
│          多平台统一消息接入层（unified_msg_origin）               │
└────────────────────────────┬────────────────────────────────────┘
                             │  WebSocket (maim_message 协议)
                             │  platform = 真实消息来源平台名
┌────────────────────────────▼────────────────────────────────────┐
│                          MaiBot                                  │
│           LLM 回复引擎（负责生成回复内容）                        │
└─────────────────────────────────────────────────────────────────┘
```

### 核心设计

| 设计点 | 说明 |
|--------|------|
| **平台透传** | 发向 MaiBot 的 `message_info.platform` 字段使用 AstrBot 解析出的真实平台名（如 `aiocqhttp`、`telegram`），而非固定字符串 |
| **动态通道** | 每个不同的消息平台对应一条独立的 MaiBot WS 持久连接，互不干扰 |
| **回路由** | MaiBot 回复时原样返回 `message_dim.platform`，插件据此将回复路由回正确的 AstrBot 会话 |
| **主动消息** | MaiBot 主动推送时，插件通过 `session_map` 查找目标会话，支持群聊与私聊 |
| **工具调用** | MaiBot 可通过 `tool_call` 请求执行 AstrBot 注册的工具函数 |

---

## 安装

1. 在 AstrBot 插件市场搜索 `maibot` 或手动将本目录放入 `data/plugins/`
2. 安装依赖：
   ```bash
   pip install maim_message>=0.3.0
   ```
3. 在 AstrBot 控制台填写插件配置

---

## 配置项

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `maibot_ws_url` | MaiBot API-Server WebSocket 地址 | `ws://127.0.0.1:18040/ws` |
| `maibot_api_key` | 认证 Key，需与 MaiBot 服务端一致 | `astrbot_hijack` |
| `maibot_timeout` | 等待回复超时秒数 | `120` |
| `maibot_bot_id` | AstrBot 在 MaiBot 侧的用户 ID | `astrbot` |
| `maibot_bot_nickname` | AstrBot 在 MaiBot 侧的昵称 | `AstrBot` |

> **注意**：不再需要填写 `platform` 配置项。平台标识会从每条消息的
> `unified_msg_origin` 自动提取。

---

## 消息流程

### 用户消息 → MaiBot

1. 用户在任意平台发送消息
2. AstrBot 接收，生成 `unified_msg_origin`（格式：`platform:MessageType:session_id`）
3. 本插件解析 UMO，提取 `platform`（如 `aiocqhttp`）
4. 如果该平台还没有 WS 通道，**自动创建**并连接
5. 构造 `maim_message` 格式消息，`platform` 字段设为真实平台名
6. 通过对应通道发送给 MaiBot

### MaiBot → 用户

1. MaiBot 处理消息，生成回复
2. 回复 payload 中 `message_dim.platform` 与发送时一致
3. 插件收到回复，按 `platform` 路由回对应 AstrBot 会话
4. 通过 AstrBot 发送回原平台

### MaiBot 主动消息

MaiBot 可以主动向 AstrBot 发送消息（如定时问候）：

1. MaiBot 通过已有通道主动推送 `sys_std` 消息
2. 插件解析 `message_dim.platform`，在 `session_map` 中查找活跃会话
3. 优先精确匹配（群号/用户 ID），降级到平台任意会话
4. 通过 AstrBot 将消息送达目标用户

---

## 版本历史

### v2.0.0 — 多平台动态路由重构

- **新增**：多平台动态通道，每个消息平台独立 WS 连接
- **新增**：`platform` 字段从 UMO 自动提取，无需手动配置
- **新增**：主动消息精确路由（按群号/用户 ID 匹配）
- **移除**：静态 `maibot_platform` 配置项
- **修复**：多平台同时在线时消息不再混串

### v1.2.0 — 初始版本

- AstrBot 劫持模式，固定 `platform = "astrbot"` 转发给 MaiBot

---

## 开发参考

- [MaiBot 适配器开发文档](https://docs.mai-mai.org/develop/adapter_develop/develop_adapter.html)
- [maim_message PyPI](https://pypi.org/project/maim-message/)
- [AstrBot 插件开发文档](https://astrbot.soulter.top/dev/plugin)
