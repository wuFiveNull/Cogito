# P13-15 memU 隔离 PoC

对比 memU 与 Cogito M4~M6 原生实现的内容检索能力（PLAN-13 §16 M9）。

## 数据集

```bash
# 导出合成去敏数据集（固定 seed，覆盖 §15.1 的 15 类语料）
python scripts/poc_memu/run_cogito.py --export scripts/poc_memu/results/dataset.json
```

## Cogito 侧运行

```bash
python scripts/poc_memu/run_cogito.py
# 输出 scripts/poc_memu/results/cogito_results.json
```

## memU 侧运行（Owner 在 Python 3.13 + memU 环境执行）

### 环境准备

```bash
# 1. 新建 Python 3.13 环境
conda create -n memu-poc python=3.13 -y
conda activate memu-poc

# 2. 安装 memU（参考 reference/memU 的安装说明）
pip install memu-py
# 或从源码：
cd reference/memU
pip install -e .
```

### 运行

```bash
# 在 memU 环境运行同一数据集
cd <cogito repo root>
python scripts/poc_memu/run_memu.py \
  --input scripts/poc_memu/results/dataset.json \
  --output scripts/poc_memu/results/memu_results.json
```

`run_memu.py` 读取 dataset.json，对每条 doc 调用 `memu-app` 的 memorize + retrieve，
输出与 Cogito 相同格式的 results.json。

## 生成对比报告

```bash
python scripts/poc_memu/report.py
# 输出 .workspace/reports/plan13-memu-poc-decision.md
```

## 隔离约束

- 使用临时数据库（不污染生产 SQLite）
- 合成数据集，无真实 Secret/PII
- PoC 失败不影响 M0~M8 发布

## 决策门槛（§16.4）

只有同时满足以下条件才进入正式 Adapter 设计：
1. 关键检索指标有显著、稳定提升；
2. 不绕过 Principal/Scope/Policy；
3. 能以派生索引身份重建；
4. 删除和来源版本语义可映射；
5. 依赖与 API 稳定性风险可接受；
6. 本地原生实现维护成本确实更高。

否则记录"不采用"决策，不维持双系统。
