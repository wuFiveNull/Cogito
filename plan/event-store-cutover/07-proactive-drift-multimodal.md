# 07：Proactive、Drift 与 Multimodal

## 目标

将主动推送、Drift 执行和媒体分析从可变状态表切换到回放状态，保持去重、抢占和恢复语义。

## 实施步骤

1. Proactive 候选、准入、决策、频率限制、digest 和外部发送分别追加 `proactive.candidate.created`、`proactive.decision.made`、`proactive.delivery.*` 等 Event；候选和投递的幂等键必须由来源、目标和窗口稳定生成。
2. Drift 使用 `drift.run.admitted|progress.recorded|checkpoint.recorded|paused|completed|failed|needs_review|result.committed`；每个结果 Event 指向来源任务、payload 和 trace。
3. 将 Admission、Preemption、Cadence、Digest 和 DriftRunner 的队列深度、活跃执行和投递统计改为 replay Task/Turn/Delivery 流，而非 SQL 聚合。
4. 多模态分析用 Task/Delivery/Message Event 关联媒体 payload；媒体正文、图像和模型原始响应不得写入 attributes。
5. 重写 Drift/Proactive 恢复：只处理未终结 run/request，先做幂等回执校验，再追加终结或 unknown Event。

## 验证

覆盖频率限制、候选去重、并发准入、抢占/恢复、进度 checkpoint、重复主动投递和媒体任务恢复。阶段退出时删除 proactive/drift/multimodal 状态表后的测试数据库可以完成相同工作流。
