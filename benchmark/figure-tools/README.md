# 五协议实验工具：Mysticeti 固定窗口适配

本目录由 v3.5 工具包适配而来，可直接在 EC2 控制机运行。五个协议的 checkout 路径由 `--root` 下的目录确定，不依赖本工具所在目录。下面的更新器保留 `/home/ubuntu/experiments/figure-tools` 入口，把旧版五个运行脚本保存到该目录内的 `backup-before-clock-*`。如需续跑旧协议批次，应使用这个旧版备份入口，并保持该协议源码、配置和工具身份一致。

## 更新与运行

先更新 Mysticeti 控制机 checkout 到包含本目录的已推送提交，并保持工作区干净。使用已安装 `benchmark/requirements.txt` 和 `matplotlib` 的 Python/Fabric 环境。控制机和工作节点都必须能读取该提交。

若继续使用原工具包入口，停止旧矩阵运行后执行以下命令。更新器核对原版 v3.5 文件，备份五个运行脚本，再替换 `run_matrix.py` 和 `plot_figures.py`；重复执行同一版本会直接返回。遇到不识别的版本或本地编辑时会在写入前停止。

```bash
python3 /home/ubuntu/experiments/mysticeti/benchmark/scripts/update_figure_tools.py \
  /home/ubuntu/experiments/figure-tools
cd /home/ubuntu/experiments/figure-tools
```

更新后可以在这个原目录使用下面相同的 `fab matrix` 参数。从 Mysticeti A3 开始继续后续模式时，依次选择 `Mysticeti/A3`、`Mysticeti/A5`、`Mahi-Mahi/all`。它们在新目录分别运行 24、24、96 组。新 Mysticeti A3 从该模式的 10 节点第一档 rate 开始，不复用旧时间轴下已完成的 A3 点。

```bash
cd /home/ubuntu/experiments/figure-tools
fab matrix --protocol Mysticeti --mode A3 \
  --root /home/ubuntu/experiments \
  --output /home/ubuntu/experiments/paper-results-v3.5-clock1-from-mysticeti-a3 \
  --dry-run
```

正式执行按阶段串行进行；任何阶段失败就停下。重复同一段命令可跳过本新目录已经完成的项。

```bash
(
set -e
cd /home/ubuntu/experiments/figure-tools
for mode in A3 A5; do
  fab matrix --protocol Mysticeti --mode "$mode" \
    --root /home/ubuntu/experiments \
    --output /home/ubuntu/experiments/paper-results-v3.5-clock1-from-mysticeti-a3 \
    --retry-failed
done
fab matrix --protocol Mahi-Mahi --mode all \
  --root /home/ubuntu/experiments \
  --output /home/ubuntu/experiments/paper-results-v3.5-clock1-from-mysticeti-a3 \
  --retry-failed
)
```

上述流程是 144 组：Mysticeti A3 24 组、A5 24 组、Mahi-Mahi 全部模式 96 组；跳过 Mysticeti A0/A1 和前三个协议。每个模式包含节点数 10/20/35、8 档 rate。默认测量 60 秒、warmup 0；Mysticeti 活动攻击为共同测量开始后的 20–50 秒，Mahi-Mahi 仍使用原节点初始化起点。原有节点池流程自动执行 create、install、逐项 remote、归档和 destroy；无需提前为矩阵手动创建或安装节点。`--dry-run` 只输出计划，不访问 AWS。

## 续跑边界

同一输出目录保存源码提交、settings、run_matrix、lifecycle 和 protocol_tasks 的身份。身份未变时，重复命令会跳过已完成项；`--retry-failed` 重试失败项并保留旧尝试。身份变化时必须使用新目录。`--retry-failed` 和 `--upgrade-pool-policy` 都不会绕过检查。

本次 Mysticeti 修改了输入、攻击和测量的时间轴，因此不能把新版结果接入旧版 Mysticeti 批次。旧数据保留作对照，其他协议无需因此全部重跑；它们可使用备份中的旧版工具、旧结果目录按协议续跑，但该协议源码、配置和工具身份必须一致。不同目录不会自动合并成一批。

## 统计与归档

- Mysticeti schema 2 从 `windows.json` 的原始 `count` 和 `sum_s` 重算，并校验每个节点的身份、run ID、固定时间窗口、完成状态和输入迟到上限；同时保存 `armed.json`。
- 历史 schema 1 的 Mysticeti 和 Mahi-Mahi 保留 `before/after` 解析。Mahi-Mahi schema 1 的攻击起点仍是各节点初始化。
- 每个副本 TPS 为窗口提交数除以测量秒数。窗口内完成的交易 E2E 均值为 `sum_s / count * 1000`；零提交副本为 0 TPS、E2E N/A。矩阵汇总延迟保持诚实副本有效延迟均值的算术平均。
- `measurements.csv` 保存 `timing_schema`、`window_basis`、`attack_window`。其他协议的端点及采样总体仍须按各自口径解释。

## 本地检查

```bash
python3 -m unittest discover -s tests -v
```

测试不访问 AWS。覆盖旧格式、新窗口、零提交、无效/缺失窗口、归档、CSV、节点池生命周期及批次身份约束。`tests/fixtures/mysticeti_metrics.py` 保存本次验证的原生统计器，用于可复现的离线回归；运行时使用每次实验实际归档的统计器。
