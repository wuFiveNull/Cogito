# Upstream: LangBot Channel Message Gateway

| Field | Value |
|---|---|
| **Source project** | [LangBot](https://github.com/Hoshinonyaruko/LangBot) |
| **Extracted version** | 4.10.5 |
| **Extracted date** | 2026-07-06 |
| **Local path** | `src/cogito/channel/vendor/langbot/` |
| **License** | Apache-2.0 |

## 映射的文件

| 上游路径 | Cogito 路径 |
|---|---|
| `langbot/pkg/platform/sources/telegram.py` | `src/cogito/channel/adapters/telegram.py` |
| `langbot/pkg/utils/httpclient.py` | `src/cogito/channel/utils/httpclient.py` |

## 本地修改

所有 `langbot_plugin.*` 类型的 import 替换为 `cogito.channel.vendor.langbot.compatibility.*`。
所有 `langbot.pkg.*` 实用工具的 import 替换为 `cogito.channel.*`。
所有 `langbot.libs.*` 的 import 替换为 `cogito.channel.clients.*`。

详细的 import 映射见 `PLAN.md` 4.2 节。
