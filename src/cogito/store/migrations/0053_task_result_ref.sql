-- 0053: tasks.result_ref —— Task 执行结果回写（PLAN-16 M3 MEM-01）
-- 存放 Task handler 声明的执行结果摘要，例如 memory_dependencies（本 Task
-- 使用并强化的记忆 ID 列表），供 task_worker 在成功后写出 task_succeeded 信号。
-- online_safe: 仅 ADD COLUMN（DEFAULT NULL），不影响已有数据。

ALTER TABLE tasks
    ADD COLUMN result_ref TEXT DEFAULT NULL;
