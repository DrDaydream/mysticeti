# Mysticeti A0–A5：Fabric AWS 实验

此适配器部署包含攻击实现的 `DrDaydream/mysticeti` 源码。策略及其适用范围见 [docs/adversary.md](../docs/adversary.md)。这次变更按授权在临时 Ubuntu 24.04 环境进行 Rust 编译检查；验证范围见 [验证记录](../docs/adversary-validation.md)。没有运行 AWS 实验。以下安装与运行命令供之后在 Ubuntu 24.04 amd64 控制机上执行。

## 控制机准备

控制机和每个工作节点都必须能通过 HTTPS Git 读取 `https://github.com/DrDaydream/mysticeti.git`。私有仓库需要预先配置具备读取权限的 Git 凭据；原来的“节点只读取公开上游”条件已不适用。不要将 token 写入仓库 URL、settings、脚本或结果文件。适配器不会自动复制 Git 凭据到工作节点。

```bash
mkdir -p /home/ubuntu/experiments
cd /home/ubuntu/experiments
git clone --branch main https://github.com/DrDaydream/mysticeti.git
cd mysticeti/benchmark
python3 -m pip install --user --break-system-packages -r requirements.txt
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
cp -n settings.example.json settings.json
chmod 600 /home/ubuntu/.ssh/orca-amazon.pem
fab check
fab info
```

默认使用 AWS 凭据链、五个区域、10 个验证节点、`m5.8xlarge` 和 200 GiB gp3。按自己的控制机环境配置 `key`、`instances` 与 `ssh_cidrs`。settings 和结果不提交到 Git。若没有这套专属标签的实例，`fab create --nodes 2` 会在每个区域新建两台计费实例。

## 固定源码与安装

`repo.url` 指向上述用户仓库。默认在原控制机仓库中构建时，`fab install` 自动选择当前 `HEAD`，`fab remote` 自动选择 `.mysticeti-build.json` 中的已安装提交；无需来回修改 `repo.ref`，此前留下的旧提交或分支引用不会影响这两个操作。只有使用独立 `remote.source_dir` 时，才按 `repo.ref` 选择控制机已有的分支、标签或完整提交。源码身份仍解析为完整 40 位提交，要求 checkout 干净、origin 与 `repo.url` 一致且提交含有 `adversary.rs`。旧 `repo.commit` 设置已移除。

源码改变后先提交并使该提交可从远端获取，再明确安装：

```bash
git status --short
git rev-parse HEAD
fab install
```

只有 `fab install` 安装依赖并编译。控制机和工作节点使用同一解析后提交、Rust `1.72.1`、`--locked --release` 与 `-C target-cpu=native`。`remote.source_dir` 默认 `/home/ubuntu/experiments/mysticeti`，直接使用控制机当前仓库。安装自动构建当前 HEAD，保留当前分支和本地 settings，不另建控制机源码目录。要构建其他源码版本，先将控制机 checkout 切换到该版本；独立源码目录仍可通过 `repo.ref` 选择版本。已有不同 origin 或未保存文件时会拒绝覆盖。

在原来的 `/home/ubuntu/experiments/mysticeti/benchmark` 目录按顺序执行：

```bash
fab create --nodes 2
fab install
fab remote --adversary A0
fab remote --adversary A3 --byzantine 3
```

`create --nodes 2` 在每个配置区域新增两台机器；已有机器时从 `fab install` 继续即可。切换敌手模式只需再次执行 `fab remote`。

编译后运行 genesis 命令检查二进制是否导出完整 adversary schema，再写入 `.mysticeti-build.json`：仓库、完整提交、工具链、编译参数、二进制 SHA-256 与 `adversary_schema=1`。`results/installation.json` 汇总各机器记录。缺失 schema 的旧 manifest、错误提交或校验和均被拒绝。

`fab remote` 在各节点执行纯 Python manifest/校验和校验，不编译、不安装依赖、不更新工作节点 Git。同目录更新 Python 报告代码后，直接执行 `remote` 即可复用已安装版本；要构建当前源码则直接执行 `install`，之后继续 `remote`。终端会打印实际使用的提交，报告中的源码提交表示经过验证的二进制构建提交，可能与更新后的适配器 HEAD 不同。构建记录缺失、损坏、二进制校验和错误或节点版本不一致仍会报错，不能用原上游二进制运行攻击。

## 选择 A0–A5

```bash
fab remote --adversary A0
fab remote --adversary A1 --byzantine 3
fab remote --adversary A2 --byzantine 3
fab remote --adversary A3 --byzantine 3
fab remote --adversary A4 --byzantine-ids 0,1,2
fab remote --adversary A5 --byzantine-ids 0,1,2
```

`--byzantine K` 在启动前固定选择 `[0, ..., K-1]`；`--byzantine-ids` 明确指定身份，二者不能同时给出。A1–A5 未指定身份或配置为空列表时，默认选择前 `f=floor((n-1)/3)` 个身份。A0 没有攻击身份；显式 `--adversary A0` 会清空 settings 中的身份及目标。

`faults` 仍表示不启动委员会末尾的 N 个身份，和 Byzantine 数量独立。两组必须互不相交且总数不超过 `f`。例如 10 个节点中身份 0、1 为 Byzantine，9 崩溃：

```bash
fab remote --nodes 10 --faults 1 --adversary A3 --byzantine-ids 0,1
```

A1 有限时间扣留 Byzantine 提议；A2 与 DAG 无关地分发两个版本；A3 根据本地 DAG 支持构造分裂；A4 跟踪未决目标并延迟相关证据；A5 再调度诚实节点之间的区块路径。最多两个版本，没有 `fork_count` 设置。实际策略及回退条件以策略说明和节点事件为准。

## 参数、时间窗口与复现

`settings.example.json` 列出全部参数。有效 `benchmark.adversary` 写入 `parameters.yaml` 根级，并在所有运行副本上使用相同公共配置，包含诚实节点。默认对象为：

```json
{
  "mode": "A0",
  "byzantine_authorities": [],
  "seed": 1,
  "start_after_ms": null,
  "duration_ms": 60000,
  "delay_ms": 200,
  "late_delay_ms": 1000,
  "max_pending_messages": 4096,
  "max_pending_bytes": 67108864,
  "max_extra_bytes_per_round": 8388608,
  "target_authority": null,
  "target_round": null
}
```

省略 `start_after_ms` 或设置 null，表示 `warmup * 1000`，会考虑 CLI 的 `--warmup`；最终 YAML 使用整数。`duration_ms` 是攻击时间，默认 60 秒。`benchmark.duration` 是 warmup 后的测量时长，默认 120 秒。要求 `start_after_ms + duration_ms + late_delay_ms <= (warmup + duration) * 1000`，并且 `0 < delay_ms <= late_delay_ms`，留出恢复时间。短 A0 基线不要求容纳活动攻击窗口。

`target_authority` 必须在固定 Byzantine 集合里。`target_round` 如提供必须为正，并确实选出该身份（或未指定身份时的某个固定 Byzantine 身份）。开启 pipeline 后第 r 轮的 leaders 为 `(r + offset) % n`，其中 `offset=0..leaders-1`。只给身份时由策略选择符合条件的轮次；两者均为空时使用策略找到的最早合适目标。

此实现使用已有 `round_robin` leader schedule 和三轮 wave；默认两个 leaders。schedule 写入 run metadata，不增加 Rust 协议字段。适配器开启 pipeline，并记录设置。活动模式必须设置 `enable_synchronizer=true`；启动脚本通过 `unset USE_SYNCER` 启用缺失区块抓取。这份原型的 Rust 代码检测变量是否存在，并在存在时跳过抓取，因此 `USE_SYNCER=0` 也会禁用抓取。A0 显式禁用同步器时才设置 `USE_SYNCER=1`。

此前适配器将该开关映射反了：历史结果中的 `enable_synchronizer=true` 不能证明补块实际启用。修复只改变 Python 生成的节点启动环境；在原控制目录直接执行 `fab remote` 即可复用现有二进制，无需重新编译。修复后需重新运行实验，旧采样不会因此变化；是否恢复 A2–A5 的提交，需要实际运行验证。

攻击时间相对各节点 Core 初始化的本地单调时钟。Fabric 并行启动存在偏差，`run.json` 记录控制机观察的 `launch_spread_seconds`。测量 warmup 在所有运行节点就绪后开始，因此与节点攻击时间轴存在启动和就绪偏差；分析时对齐各节点日志，不能把相同毫秒设置当作全局同时启动。

## 结果与离线检查

测量结束立即在终端打印 Bullshark 风格的 `SUMMARY`，随后停止节点并下载日志；最终状态写入 `summary.txt` 和 `run.json`。零提交节点的 TPS 为 0、延迟为 `N/A`（JSON 中为 null），平均延迟仅使用有提交的副本均值；全部零提交时平均延迟为 `N/A`。零提交仍计入平均 TPS，缺失指标或计数器重置仍报告错误。输出分别标明所有运行副本和诚实副本的统计，列出每个副本的提交数、TPS、延迟和采样时长。`complete` 仅表示实验流程完成，不代表所有节点都有进展。

在 `benchmark` 目录补打印最新一次结果：

```bash
/usr/bin/python3 -m mysticeti_aws.report --latest
```

也可以指定 `results/run-...` 目录。报告器优先读取 `samples.jsonl` 的完整 before/after 窗口重新计算，能恢复旧版本因零提交而未生成的汇总。输出保存在该轮 `summary.txt`，不改写历史 `run.json` 状态；没有完整窗口时显示 `N/A` 和诊断，不把部分采样冒充完整实验。

`results/run-*` 保留 `run.json`、`summary.json`、`summary.csv`、生成的配置、原始 Prometheus 快照及节点日志。run manifest 包含全部有效攻击参数、固定 Byzantine/崩溃身份、wave/leader 设置、同步器状态、源码来源及各二进制记录；summary 同时保存攻击参数。CSV 继续记录逐副本吞吐与延迟。攻击效果、额外版本、commit/skip/undecided 和恢复需要结合节点事件分析。

离线 Python 检查不访问 AWS，也不编译 Rust。构建失败回归使用退出失败的假编译命令：

```bash
python3 -m pip install --user --break-system-packages -r requirements-test.txt
python3 -m pytest tests -q
```

停止实验进程用 `fab kill`；停止专属 EC2 实例用 `fab stop`；恢复已有实例用 `fab start --max 2`。`fab destroy` 会终止这套专属标签的实例并移除相应安全组。
