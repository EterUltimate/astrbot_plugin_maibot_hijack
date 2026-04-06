# astrbot_plugin_maibot_hijack — MaiBot Adapter for AstrBot

> **AstrBot 作为消息聚合平台，MaiBot 专注 LLM 回复。**

[![AstrBot](https://img.shields.io/badge/AstrBot-4.16+-blue.svg)](https://github.com/Soulter/AstrBot)
[![MaiBot](https://img.shields.io/badge/MaiBot-API--Server-green.svg)](https://github.com/SengokuCola/MaiMBot)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## 📖 简介

本插件将 AstrBot 作为**消息聚合层**，MaiBot 作为**LLM 回复引擎**。AstrBot 负责对接多种消息平台（QQ、Telegram、Discord 等），MaiBot 负责生成智能回复。

### 核心特性

| 特性 | 说明 |
|------|------|
| 🔄 **平台透传** | 自动识别消息来源平台，MaiBot 知道自己在回复哪个平台 |
| 🔌 **动态通道** | 每个平台独立 WebSocket 连接，互不干扰 |
| 🎯 **精确路由** | 主动消息按群号/用户 ID 精准投递 |
| 🛠️ **工具调用** | MaiBot 可调用 AstrBot 注册的工具函数 |
| ⚡ **高并发** | 支持多平台同时在线，消息不串流 |

---

## 🏗️ 架构概览

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

### 消息流程

```
用户消息 → AstrBot → 解析平台 → WS通道 → MaiBot → 生成回复
                                              ↓
用户 ← AstrBot ← 按platform路由 ← WS通道 ←─┘
```

---

## 🚀 安装

### 方式一：AstrBot 插件市场（推荐）

1. 打开 AstrBot 管理面板
2. 进入「插件市场」
3. 搜索 `maibot` 或 `mai` 并安装

### 方式二：手动安装

```bash
# 1. 克隆到 AstrBot 插件目录
cd /path/to/astrbot/data/plugins/
git clone https://github.com/EterUltimate/astrbot_plugin_maibot_hijack.git

# 2. 安装依赖
pip install maim_message>=0.3.0

# 3. 重启 AstrBot
```

---

## ⚙️ 配置

在 AstrBot 控制台 → 插件配置 → MaiBot Adapter 中填写：

| 配置项 | 必填 | 默认值 | 说明 |
|--------|------|--------|------|
| `maibot_ws_url` | ✅ | `ws://127.0.0.1:18040/ws` | MaiBot API-Server WebSocket 地址 |
| `maibot_api_key` | ✅ | `astrbot_hijack` | 认证密钥，需与 MaiBot 服务端一致 |
| `maibot_timeout` | ✅ | `120` | 等待回复超时秒数 |
| `maibot_bot_id` | ✅ | `astrbot` | AstrBot 在 MaiBot 侧的用户 ID |
| `maibot_bot_nickname` | ✅ | `AstrBot` | AstrBot 在 MaiBot 侧的昵称 |

> 💡 **提示**：`platform` 字段不再需要手动配置，会自动从每条消息的 `unified_msg_origin` 提取。

### MaiBot 服务端配置

确保 MaiBot 的 API-Server 已启动并监听正确地址：

```yaml
# MaiBot 配置文件
api_server:
  host: "0.0.0.0"
  port: 18040
  ws_path: "/ws"
  api_key: "astrbot_hijack"  # 与插件配置一致
```

---

## 📡 工作原理

### 1. 用户消息 → MaiBot

1. 用户在任意平台发送消息
2. AstrBot 接收并生成 `unified_msg_origin`（格式：`platform:MessageType:session_id`）
3. 插件解析 UMO，提取真实 `platform`（如 `aiocqhttp`、`telegram`）
4. 自动创建该平台的 WebSocket 通道（如不存在）
5. 构造 `maim_message` 格式消息，`platform` 字段设为真实平台名
6. 通过对应通道发送给 MaiBot

### 2. MaiBot → 用户回复

1. MaiBot 处理消息并生成回复
2. 回复 payload 中 `message_dim.platform` 与发送时一致
3. 插件收到回复，按 `platform` 路由回对应 AstrBot 会话
4. 通过 AstrBot 发送回原平台

### 3. MaiBot 主动消息

MaiBot 可以主动向用户发送消息（如定时任务、系统通知）：

1. MaiBot 通过已有通道主动推送 `sys_std` 消息
2. 插件解析 `message_dim.platform`，在 `session_map` 中查找活跃会话
3. 优先精确匹配（群号/用户 ID），降级到平台任意会话
4. 通过 AstrBot 将消息送达目标用户

---

## 🛠️ 工具调用

MaiBot 可以通过 `tool_call` 请求执行 AstrBot 注册的工具函数：

```json
{
  "type": "tool_call",
  "tool_call": {
    "name": "get_weather",
    "parameters": {"city": "北京"}
  }
}
```

插件会自动调用 AstrBot 的对应工具，并将结果返回给 MaiBot。

---

## 📋 支持的消息平台

| 平台 | 标识符 | 状态 |
|------|--------|------|
| QQ (OneBot) | `aiocqhttp` | ✅ 已测试 |
| Telegram | `telegram` | ✅ 已测试 |
| Discord | `discord` | ✅ 支持 |
| 微信 | `wechat` | ✅ 支持 |
| 飞书 | `feishu` | ✅ 支持 |
| 钉钉 | `dingtalk` | ✅ 支持 |
| 其他 AstrBot 支持的平台 | - | ✅ 理论支持 |

---

## 🐛 故障排查

### 连接失败

```
[ERROR] Failed to connect to MaiBot WS
```

- 检查 MaiBot API-Server 是否已启动
- 确认 `maibot_ws_url` 配置正确
- 检查防火墙是否放行对应端口

### 消息发送成功但无回复

- 检查 MaiBot 是否正常加载并处理消息
- 查看 MaiBot 日志确认是否收到消息
- 确认 `maibot_api_key` 两端一致

### 多平台消息串流

- 确保使用 v2.0.0+ 版本
- 检查 `unified_msg_origin` 是否正确解析

---

## 📦 版本历史

### v2.0.0 — 多平台动态路由重构（当前）

- ✨ **新增**：多平台动态通道，每个平台独立 WS 连接
- ✨ **新增**：`platform` 字段从 UMO 自动提取，无需手动配置
- ✨ **新增**：主动消息精确路由（按群号/用户 ID 匹配）
- ✨ **新增**：并发安全锁、LRU session 缓存、monotonic 时钟
- 🔥 **移除**：静态 `maibot_platform` 配置项
- 🐛 **修复**：多平台同时在线时消息不再混串

### v1.2.0 — 初始版本

- AstrBot 劫持模式，固定 `platform = "astrbot"` 转发给 MaiBot

---

## 🔗 相关链接

- [MaiBot 适配器开发文档](https://docs.mai-mai.org/develop/adapter_develop/develop_adapter.html)
- [maim_message PyPI](https://pypi.org/project/maim-message/)
- [AstrBot 插件开发文档](https://astrbot.soulter.top/dev/plugin)
- [AstrBot GitHub](https://github.com/Soulter/AstrBot)
- [MaiBot GitHub](https://github.com/SengokuCola/MaiMBot)

---

## 📄 License

This project is licensed under the [GNU Affero General Public License v3.0](LICENSE) (AGPL-3.0).

Copyright (C) 2025 EterUltimate

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
