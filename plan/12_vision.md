---
plan_id: "PLAN-12"
title: "Multimodal Perception Layer Implementation Specification"
version: "1.1"
status: "in_progress"
scope: "图片 MVP：Asset/Payload、VLM、缓存、Task、Context 与 Tool；PDF/扫描件为后续切片"
depends_on: ["PLAN-11 M1-M2"]
---

# Multimodal Perception Layer Implementation Specification

## 当前实施状态（2026-07-10）

- 已落地图片 Asset、消息关联、版本化 VisionAnalysis 与数据库迁移；
- 已修复单 Provider 退化，`main` 与 `vlm` 共享同一 `LLMManager`；
- 已落地 SHA256 精确缓存、`vision.analyze` Durable Task、有限重试；
- 已落地 Context 短描述注入和 `analyze_multimodal_asset` Tool；
- 已落地图片大小/MIME/像素校验、会话级 Asset 访问控制和外部数据边界；
- PDF、扫描文档、OCR、Embedding 与视觉记忆候选尚未实施。

冻结决策：

1. Payload Store 是二进制唯一事实源，Asset 只保存 `payload_ref`；
2. SHA256 才能直接命中缓存，pHash 只用于相似候选；
3. Cache Key 为 `asset + kind + model + prompt + schema + options`；
4. 缓存命中立即注入；首次图片分析有界等待，超时由 Task 接管；
5. Vision 结果是来源事实，不自动升级为已确认长期记忆；
6. 图片内文本始终按不可信外部数据处理。

## 执行计划

### 总体交付顺序

```text
PLAN-11 M1/M2 契约稳定
→ M0 契约冻结
→ M1 Asset/Payload
→ M2 VLM 路由与结构化输出
→ M3 Cache/Coordinator
→ M4 Durable Task/Retry
→ M5 Context/Vision Tool
→ M6 图片 MVP 发布门禁
→ M7 PDF/扫描文档
→ M8 视觉记忆、治理与发布收口
```

### M0：契约冻结与配置

状态：`in_progress`（核心决策已冻结，权威文档同步尚未全部完成）。

实施项：

- 在 `Config` 增加 `[multimodal]`，默认关闭；
- 为模型端点增加 `modalities`，VLM 必须显式声明 `image`；
- 冻结 Asset、VisionAnalysis、Cache Key、状态机和安全边界；
- 更新 `DOMAIN-CONTRACTS`、`RUNTIME-FLOWS`、`ACCESS-DELIVERY`、
  `MESSAGE-PERSISTENCE`、`MODEL-ADAPTER`、`TASK-SCHEDULER`、
  `STORAGE-DATA`、`DATABASE-SCHEMA`、`SECURITY-OBS`；
- 在 `manifest.json` 登记 PLAN-12。

主要文件：

```text
src/cogito/config.py
config.example.toml
manifest.json
markdown/01_architecture/01_核心领域模型与数据契约.md
markdown/02_runtime/00_运行机制与端到端流程.md
markdown/02_runtime/07_模型适配层.md
markdown/04_background/01_Task与Scheduler.md
markdown/05_interaction/00_接入交互身份与投递.md
markdown/05_interaction/03_消息持久化与历史.md
markdown/06_infrastructure/00_存储一致性与数据治理.md
markdown/06_infrastructure/03_数据库Schema与Migration.md
markdown/07_quality/00_安全策略可观察性与资源治理.md
```

验收：配置未知字段仍严格失败；旧配置无行为变化；多模态默认不产生模型调用。

### M1：Asset、Payload 与消息关联

状态：`implemented / validation_pending`。

实施项：

- 新增 `MultimodalAsset`、`VisionAnalysis` 领域对象；
- 新增 Migration `1004_multimodal_perception`；
- 新增 `multimodal_assets`、`message_asset_links`、`asset_derivatives`、
  `vision_analyses`；
- 为 `content_parts` 增加稳定 `ordinal`；
- Data URI 图片经过 MIME、magic bytes、大小和像素限制后写 Payload Store；
- Message 只保留 `payload_ref`，清除原始 Base64；
- SHA256 完全一致时复用 Asset；pHash 不参与自动缓存命中；
- Core 不下载入站消息中的任意 HTTP URL。

主要文件：

```text
src/cogito/domain/multimodal.py
src/cogito/contracts/multimodal.py
src/cogito/store/migrations/1004_multimodal_perception.sql
src/cogito/store/multimodal_repo.py
src/cogito/service/asset_service.py
src/cogito/domain/message.py
src/cogito/service/inbound_service.py
src/cogito/inbound/dispatcher.py
src/cogito/infrastructure/payload_store.py
```

验收：首次上传创建一个 Payload/Asset/Link；相同字节再次上传不创建第二个
Payload 或 Asset；非法图片不阻断文本 Turn，且原始二进制不进入 Context。

### M2：VLM 路由与 Vision Service

状态：`implemented / validation_pending`。

实施项：

- 应用启动只构建一个 `LLMManager`；
- `main`、`vlm`、Task 和 Tool 共享同一 Router；
- 修复显式传入 `main Provider` 导致角色路由退化的问题；
- OpenAI/Anthropic 统一接收 `image_url` Data URI 内容块；
- VLM 调用前检查 `capabilities.modalities`；
- 定义版本化 `VisionResult` JSON Schema；
- 结构化输出解析失败时保存安全的结构化 fallback，不把异常对象传给主模型。

主要文件：

```text
src/cogito/model/llm_manager.py
src/cogito/model/openai_compat.py
src/cogito/model/anthropic_provider.py
src/cogito/service/vision_service.py
src/cogito/service/agent_runner.py
src/cogito/application.py
```

验收：配置了独立 `vlm` 时 Vision 请求不落到 text-only main；未声明 image
能力时确定性失败并保持主 Turn 可运行；离线 Provider fixture 校验请求结构。

### M3：版本化 Cache 与并发 Claim

状态：`implemented / validation_pending`。

实施项：

- Cache Key 使用 `asset/kind/model/prompt/schema/options`；
- `INSERT OR IGNORE + UNIQUE` 保证同 Key 只有一个 Analysis；
- `queued → running` 使用条件 UPDATE Claim；
- 自动分析、Task 和 Tool 共享 `VisionAnalysisService`；
- 成功结果永久按版本复用；retryable failure 可重新排队；
- Provider/Prompt/Schema/Options 变化生成新记录。

验收：自动流程与 Tool 并发时只有一次 Provider 调用；相同 Asset 二次读取零调用；
改变任一版本字段后产生新调用；pHash 相似文件不会误复用。

### M4：Durable Task、超时与恢复

状态：`implemented / validation_pending`。

实施项：

- 注册 `vision.analyze` Task Handler；
- Task 幂等键绑定 analysis_id；
- 支持 `max_attempts + backoff_seconds`；
- 修复 scheduled Task 无法再次 claim 和 lease_version 不随 Attempt 更新的问题；
- inline 分析超时后恢复为 queued，由已有 Task 接管；
- retryable Provider 错误创建新 TaskAttempt，非 retryable 错误终止。

主要文件：

```text
src/cogito/service/task_handlers.py
src/cogito/service/task_worker.py
src/cogito/service/task_dispatcher.py
src/cogito/store/task_repo.py
src/cogito/store/multimodal_repo.py
```

验收：Worker 崩溃或 Provider 暂时失败后可恢复；每次重试创建新 Attempt；旧 Lease
不能提交结果；重试达到上限后进入 failed。

### M5：Context 注入与 Vision Tool

状态：`implemented / validation_pending`。

实施项：

- ContextBuilder 仅拼接 text/markdown ContentPart；
- 默认只注入 asset_id、MIME、状态和 short_description；
- 视觉描述放入 `<external_data trust="unverified">`；
- pending/failed 有明确降级文本；
- 新增 `analyze_multimodal_asset(asset_id)`；
- Tool 只允许访问当前 Session 已关联 Asset；
- Tool 返回标准 JSON，并复用同一 Vision Cache。

主要文件：

```text
src/cogito/contracts/context.py
src/cogito/contracts/multimodal.py
src/cogito/tools/analyze_multimodal_asset.py
src/cogito/tools/registry.py
src/cogito/capability/executor.py
```

验收：Context 中没有 Base64、完整 OCR 或存储路径；模型可以用上下文中的 asset_id
读取详情；跨 Session 猜测 asset_id 返回 denied；缓存命中时 Tool 不调用 Provider。

### M6：图片 MVP 发布门禁

状态：`in_progress`。

实施项：

- 图片 Asset/Cache/Context/Tool 专项测试；
- Config、Migration、Provider、Task Retry 和应用启动回归；
- Ruff、完整 Pytest、Web typecheck/build；
- 增加 Vision 指标：requested/cache_hit/started/completed/failed/latency；
- 增加 feature flag、运行手册和失败降级说明；
- 在至少一个真实 Channel 或 Web 上传路径完成 E2E。

测试文件：

```text
tests/multimodal/test_vision_mvp.py
tests/store/test_schema*.py
tests/service/test_inbound_service.py
tests/runtime/test_context.py
tests/architecture/test_model_adapter_contracts.py
tests/service/test_task_dispatcher.py
tests/integration/test_runtime_startup.py
```

完成定义：图片首次上传可在有界等待内获得描述；超时不阻塞文本回复；重复上传和
Tool 读取不重复收费；重启后任务收敛；所有发布门禁通过。

### M7：PDF 与扫描文档

状态：`pending`，必须在 M6 完成后开始。

实施项：

- 增加 PDF MIME/页数/总像素/解压大小限制；
- 文本 PDF 优先提取文本，扫描页渲染为受限图片；
- 每页生成 `asset_derivatives(kind=pdf_page,page_no)`；
- 页级分析可独立缓存，文档级结果按页序稳定聚合；
- 大文档只走 Durable Task，不占用 Context Partition Lane；
- 首期不依赖本地 Tesseract，扫描文本由 VLM 提取；OCR Provider 后续可插拔。

验收：文本 PDF、扫描 PDF、混合 PDF、超页数、损坏 PDF、加密 PDF 和重启恢复。

### M8：视觉记忆、治理与发布收口

状态：`pending`。

实施项：

- 新增 VisionAnalysis → Memory candidate 提取路径；
- `source_type=vision_analysis`，保留 analysis_id、asset_id 和原 Message 来源；
- 视觉推断默认 candidate，不自动成为 Owner 已确认事实；
- 实现删除、保留、Payload GC、备份/恢复与最小 Audit tombstone；
- Trace 只记录 payload_ref/hash/size，不记录二进制和完整 OCR；
- 增加模型成本、缓存命中率、失败率、处理时延和存储增长指标；
- 完成架构依赖图、README、配置示例和发布说明。

验收：视觉记忆可追溯、可确认、可拒绝、可删除；删除后无悬空引用；备份恢复后
Asset、Analysis、Message Link 与 Payload 一致。

## 验收命令

每个里程碑至少运行：

```powershell
python -m pytest -q tests/multimodal
python -m pytest -q tests/service/test_inbound_service.py tests/runtime/test_context.py
python -m pytest -q tests/service/test_task_dispatcher.py tests/service/test_workers.py
python -m pytest -q tests/architecture/test_model_adapter_contracts.py
python -m pytest -q tests/integration/test_runtime_startup.py
python -m ruff check src tests
```

M6 和 M8 还必须运行完整门禁：

```powershell
python -m pytest -q
cd web
npm run typecheck
npm run build
```

## 实施边界

- M6 之前只承诺图片，不宣称 PDF/扫描文档已可用；
- 未安装 `multimodal` 可选依赖时保持文本模式可运行；
- 未配置 `model.roles.vlm` 或 Provider 未声明 image 时不尝试视觉调用；
- 不修改 Agent Loop 的决策协议，扩展点位于入站、Context、Task、Tool 和模型角色；
- 不自动把视觉推断写成已确认长期记忆；
- 不允许 Core 根据外部消息提供的任意 URL 下载附件。

## 目标

为现有 Agent 增加多模态理解能力，使 text-only 主 LLM 能够处理图片、文档等多模态输入。

设计目标：

1. 支持多模态文件自动解析。
2. 支持 Vision LLM 进行视觉理解。
3. 支持多模态结果缓存和复用。
4. 支持主 LLM 主动调用 Vision 能力。
5. 支持视觉信息长期存储。
6. 保持主 LLM 核心架构不变。

---

# 总体架构目标

```
User Message
      |
      v
Multimodal Processor
      |
      +----------------+
      |                |
    Text            Attachments
                       |
                       v
              Asset Management
                       |
              Hash / Cache Check
                       |
              +--------+--------+
              |                 |
            Exists            New
              |                 |
              v                 v
       Load Vision Result   Vision LLM
                                |
                                v
                         Save Analysis
              |
              v
       Multimodal Context
              |
              v
          Main LLM
              |
              v
       Vision Tool (optional)
```

---

# 功能需求

## 1. 多模态输入处理

系统需要支持：

- 图片
- PDF
- 扫描文档
- 其他可扩展文件类型

用户发送包含附件的消息时：

1. 自动提取附件。
2. 创建多模态资源记录。
3. 生成唯一标识。
4. 查询历史视觉分析结果。
5. 根据结果决定是否调用 Vision LLM。

---

# 2. 多模态资源管理

实现 Asset 管理模块。

负责：

- 文件存储
- 文件 metadata 管理
- 文件唯一性判断
- 文件生命周期管理

每个多模态文件需要拥有：

```
asset_id
sha256
perceptual_hash
mime_type
file_name
file_size
storage_location
created_at
```

---

# 3. 文件 Hash 系统

实现双重 hash。

## Content Hash

用途：

判断文件是否完全一致。

算法：

```
SHA256
```

## Perceptual Hash

用途：

判断视觉内容是否相似。

算法：

```
pHash
```

---

# 4. Vision Analysis 缓存系统

所有 Vision LLM 输出必须持久化。

缓存查询优先级：

```
asset_id
    |
    v
vision_analysis
    |
    +---- exists
    |
    +---- reuse
```

不存在时：

```
asset
 |
 v
Vision LLM
 |
 v
save analysis
```

---

# 5. Vision Analysis 数据结构

Vision 输出必须结构化。

```json
{
  "short_description": "",
  "detailed_description": "",
  "extracted_text": "",
  "objects": [],
  "document_type": "",
  "metadata": {}
}
```

---

# 6. 数据库存储

## multimodal_assets

字段：

```
id
sha256
perceptual_hash
mime_type
file_name
file_size
storage_path
created_at
```

## vision_analysis

字段：

```
id
asset_id
model_name
prompt_version
short_description
detailed_description
extracted_text
metadata
created_at
```

---

# 7. Message Context 注入

在消息进入 Main LLM 前：

自动添加视觉上下文。

要求：

- 默认只注入短描述。
- 详细信息通过 Tool 获取。
- 避免占用大量上下文。

---

# 8. Vision Tool

为 Main LLM 增加 Vision Tool。

Tool 名称：

```
analyze_multimodal_asset
```

功能：

允许 Main LLM 主动获取多模态详细信息。

输入：

```json
{
 "asset_id":"xxx"
}
```

处理：

```
Tool Request

      |
      v

Check Database

      |
      +---- Exists
      |
      v

Return Cached Result


      |
      +---- Missing

      |
      v

Call Vision LLM

      |

Save Result

      |

Return
```

---

# 9. Vision Service

实现独立 Vision 服务层。

职责：

- 调用 Vision LLM。
- 管理 Vision Prompt。
- 解析 Vision 输出。
- 返回标准结构。

接口：

```
analyze(asset)

return VisionResult
```

---

# 10. 异步处理

Vision 分析属于耗时任务。

需要支持：

- 异步执行。
- 任务队列。
- 后台 Worker。

---

# 11. 错误处理

Vision 调用失败时：

不能阻塞 Agent。

返回：

```
vision_status:
failed
```

Agent 继续使用文本模式运行。

---

# 12. 可扩展设计

未来支持：

- OCR
- Embedding
- 多模态 RAG
- Video Frame Analysis

---

# 13. 测试要求

## Unit Test

覆盖：

- SHA256
- pHash
- Asset 创建
- Cache 查询
- Vision 调用
- 数据保存

## Integration Test

第一次上传：

```
Vision LLM Called
Database Inserted
```

第二次上传相同文件：

```
Vision LLM Not Called
Database Reused
```

---

# 14. 实现约束

1. 不修改 Main LLM 核心流程。
2. Multimodal Layer 必须独立。
3. Vision LLM 不直接替代 Main LLM。
4. 所有 Vision 结果必须缓存。
5. Tool 和自动处理流程必须共享同一套 Vision Cache。
6. 所有输出必须结构化。
7. 支持未来增加新的多模态模型。
