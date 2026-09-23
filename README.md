# 三相变压器遗传优化研究原型

本目录与 `faladi_server` 同级，独立使用 Conda Python 3.10：

```powershell
& C:\ProgramData\anaconda3\envs\python310\python.exe main.py catalog
& C:\ProgramData\anaconda3\envs\python310\python.exe main.py check-core --record-id 28859
& C:\ProgramData\anaconda3\envs\python310\python.exe -m calculation.consistency_check --config-id 445
& C:\ProgramData\anaconda3\envs\python310\python.exe main.py ga-run --config-id 445 --generations 20 --seed 20260907
& C:\ProgramData\anaconda3\envs\python310\python.exe -m streamlit run experiment_ui/app.py
```

建议按以下顺序阅读当前说明文档：

1. [实验架构说明-V2.md](说明文档/实验架构说明-V2.md)：程序分层、数据流与验证边界；
2. [三相GA详细说明-V1-从初始种群到定向变异.md](说明文档/三相GA详细说明-V1-从初始种群到定向变异.md)：候选域、初始种群、双档案、交叉和变异；
3. [页面详细说明.md](说明文档/页面详细说明.md)：Streamlit 五个页面的实际操作、输入来源与结果解释。

## 数据库配置

Python 工程只读取自身 `config/database.local.yml` 的本机 `faladi` 数据库连接，不再读取 Java 的 `application-dev.yml`。首次使用时复制 `config/database.local.yml.example` 为 `config/database.local.yml` 并填写本机账号；真实本地配置已被忽略，禁止提交。

目前数据库访问仅执行 `SELECT`，优化运行日志写入本目录 `outputs/*.sqlite3`，不会写入业务数据库。材料单价优先取方案 `craft_unit_price`；其中字段为空时，才会经同级 `faladi_mcp` 的只读 MCP 工具调用 Java `fuelTankPriceConfig` 获取当前价格，并且只在 Python 当前运行内存中补齐。首次使用该回退能力前，应按 `faladi_mcp/README.md` 配置其 `.venv` 与本机后端账号。

## 目录边界

```text
three_phase_ga_optimizer/
├── 说明文档/                       # 当前 GA、实验架构和页面操作说明
├── calculation/                    # 单设备精算基础层；不包含 GA 算子
│   ├── database.py                 # 业务数据库只读访问与目录/配置读取
│   ├── models.py                   # 候选、结果、约束等数据结构
│   ├── material_price_provider.py  # 方案单价优先、经 MCP 的当前价格回退
│   ├── record_adapter.py           # Java 历史方案 → Python 候选的目录还原
│   ├── similar_history.py          # 相似配置历史方案的筛选与排序
│   ├── single_device_evaluator.py  # 唯一的变压器工程公式链
│   ├── consistency_check.py        # 手工 Java—Python 字段对比入口
│   └── snapshot_regression.py      # 手工快照回归入口，不参与页面或 GA 运行
├── ga/                             # 遗传优化层；只能调用精算器，不能复制工程公式
│   ├── gene_codec.py               # 搜索域、完整记录编码、合法化及 A/B 覆盖
│   ├── optimizer_runtime.py        # 评价缓存、种群对象和运行期工具
│   ├── full_optimizer.py           # 双档案、选择、结构电磁耦合交叉、变异和邻域细化
│   ├── runtime_provenance.py        # 公式版本与核心代码指纹记录
│   ├── trace_store.py              # SQLite 运行轨迹写入
│   └── trace_reader.py             # 本地轨迹回放读取
├── experiment_ui/
│   └── app.py                      # Streamlit 实验页面（单设备、对比、GA、回放）
├── config/
│   ├── database.local.yml          # 本机数据库配置
│   ├── database.local.yml.example  # 脱敏连接模板，复制后填写本机账号
│   ├── optimizer.yml               # 算法默认参数说明
│   └── java_python_regression_samples.yml # 手工回归样本清单
├── tests/                          # 自动化测试目录；当前为空，不是运行依赖
├── 论文/                            # 论文示例稿及其插图，不参与运行
├── outputs/                         # 运行生成的 SQLite 轨迹及本地日志，不写业务数据库
├── main.py                          # 命令行编排入口
├── requirements.txt                 # Python 依赖
└── README.md
```

`calculation/` 只能读取业务数据并进行性能计算；`ga/` 只能调用精算器，不能复制或修改任何变压器公式。

## 运行代码与验证辅助的边界

日常运行只需要 `main.py`、`calculation/`、`ga/`、`experiment_ui/app.py`、`config/` 中的配置模板和依赖文件。若方案单价存在空值，还需要同级 `faladi_mcp` 的只读价格桥接。`tests/`、`calculation/consistency_check.py`、`calculation/snapshot_regression.py`、`config/java_python_regression_samples.yml` 都是测试或手工回归辅助内容：它们不会被 Streamlit 页面或 GA 自动调用，也不应承载新的业务或优化逻辑。

如果后续需要新增验证脚本，应优先放入 `tests/` 或作为上述两个回归入口的明确扩展；不要把一次性测试代码混入 `ga/`、`calculation/single_device_evaluator.py` 或 `experiment_ui/app.py`。运行过程中生成的 SQLite、日志和临时结果只放入 `outputs/`，可在确认无须回放后清理。

## 已落地

- 原始铁芯、硅钢片、圆线、扁线、箔材、三相配置及历史方案的真实数据库读取；三相硅钢片编码通过 `tb_silicon_steel_brand_mapping` 还原为实际牌号；
- 以“导线类别 + 完整目录记录 ID”为核心的离散基因模型；低压/高压油道由当前 Java 页面允许的数量 × 类型组合构造；
- `calculation/single_device_evaluator.py`：三种铁芯形式、线规解码、绕组/油道几何、P0、PK、UK、波纹/散热器热工、油量、重量、总成本与严格约束诊断。
- `check-core`、`calculation.consistency_check` 和 `snapshot_regression`：用于手工核验 Java—Python 字段；它们是验证工具，不参与 GA 搜索。
- `ga/full_optimizer.py`：完整双档案离散 GA。日常默认采用“结构电磁耦合交叉 + 经验有效域优先注入 + 普通随机变异”；其中经验有效域注入在配置 401 的 v9 配对实验中有统计改善，诊断反馈变异保留为可选实验策略而非默认。交叉仍独立替换完整冷却型号；注入、交叉和变异产生不可计算/不完整结果时仍保留轨迹诊断，但不再占用下一代父代槽位。定向变异先精算交叉基体，再按主导约束做局部探测：阻抗只试最近同向油道档位、没有油道邻居即回退普通随机变异；负载损耗按方向试同类别相邻的加宽/加厚/加粗或反向减小完整线规，普通随机变异仍保留双向探索。第 0 代仍全量评价和留痕；若可计算完整候选不足两条，系统会在同一页面允许域内补抽未见完整候选以寻找交叉起点，达到自动上限仍不足两条才带轨迹正常停止。所有算子与精算结果均写入本地轨迹。
- 每份初始预览、页面 GA、`ga-run` 和 `ga-ablation` 轨迹均写入公式版本、核心源码 SHA-256 指纹、入口来源与耗时；`ga-ablation` 的 `manifest.json` / `summary.csv` 同步记录每次运行耗时，防止 v8/v9 或不同算子代码轨迹混列。
- `ga/full_optimizer.py`：支持消融所需的强制原始域注入、强制纯随机变异和可选真实精算预算。预算不是普通 GA 的必填参数：页面和 `ga-run` 不填预算时仍按最大代数或停滞规则运行；只有 `ga-ablation` 的公平策略对照要求提供预算。严格可行率只统计 `calculable=True`、`complete=True`、`feasible=True` 的候选，近可行候选不计入。
- `experiment_ui/app.py`：本地 Streamlit 实验台，支持按配置读取实验上下文、从历史方案回填离散结构基因、运行单设备精算、显示中文约束诊断、比较 Java—Python 字段，并运行完整 GA；轨迹回放默认展示中文结构、档案处理和诊断摘要，原始 JSON 仅用于排错。

## 实验台使用范围

启动后访问 `http://localhost:8501`。页面分为五个入口：

- **单设备精算**：先集中展示当前配置的完整固定输入（产品需求、标准约束、工艺、绝缘、油箱和价格参数），再定义本次 Python 实验的真实硅钢片牌号与三相铁芯形状范围。手动候选先选择圆形/长圆形/椭圆形，再从该形状的原始铁芯目录中选规格；页面显示性能、成本、温升、重量和约束超限向量。
- **一致性验证**：对单条 Java 方案记录重新精算，逐字段展示 Java 与 Python 的可比较结果。
- **GA 优化实验**：先诊断“多背景 A/B 覆盖 + 同/相似配置历史结构 + 随机完整合法候选”组成的第 0 代；页面默认选中“当前推荐：耦合交叉 + 经验有效域注入”，完整诊断反馈可按需切换为实验策略，随后运行完整双档案 GA 并保存独立轨迹文件。
- **轨迹回放**：无须重新计算，直接查看某份 SQLite 中的中文搜索域/算法摘要、代际档案变化、候选结构、约束上下限诊断与父代关系；原始 SQLite 可下载，原始 JSON 只在排错折叠区显示。
- **研究说明**：说明当前真实数据来源、已验证边界和论文实验用途，不将未验证的分支或单次算法结果包装为工程结论。

页面已经执行选择、结构电磁耦合交叉、探索性交叉、离散变异、可选单主约束局部探测、双档案演化和局部邻域细化。09-22 已完成 v9 功能回归和配置 401 的 10 个配对种子策略对照；该证据不覆盖其它配置、搜索域或预算，不能将其写成全局算法有效性结论。业务数据库保持只读。

## 消融实验 CLI

`ga-run` 可通过 `--injection-mode`、`--guided-mutation-mode`、`--experiment-strategy`、`--evaluation-budget`（总精算预算）和 `--evolution-evaluation-budget`（第 0 代之后的演化精算预算）控制策略。普通 `ga-run` 默认使用“耦合交叉 + 经验有效域优先注入 + 普通随机变异”；显式传入命名策略或手动开关可覆盖。页面“本次实验策略”、`ga-run --experiment-strategy` 与 `ga-ablation` 共用 `baseline → coupled → empirical_injection → guided_mutation` 映射；它们是当前代码的消融层级，不等同于回退历史旧版 GA。后两项预算对 `ga-run` 都是可选的，不填即按代数/停滞规则正常运行。`ga-ablation` 会按相同配置、搜索域、预算和种子生成独立轨迹及批次汇总；它不自动宣称哪种策略更优。

```powershell
& C:\ProgramData\anaconda3\envs\python310\python.exe main.py ga-ablation --config-id 401 --seeds 20260918,20260919 --evolution-evaluation-budget 5000
```

预算只用于同一配置、同一搜索域、同一批随机种子的公平策略对照，不能据此强行比较不同产品。`ga-ablation` 至少要填写一种预算；正式消融优先固定 `--evolution-evaluation-budget`，将初代覆盖作为单列报告的公共成本。总预算 `--evaluation-budget` 只保留作安全硬上限或小预算回归测试，日常运行不需要填写。09-22 已完成页面、CLI、轨迹与四策略的一轮 v9 功能验证，以及配置 401 的 10 个配对种子效果对照；完整结论见[09-22 测试结论](说明文档/2026-09-22测试/测试结论-2026-09-22.md)。

页面启动后会创建只读长连接池；首次进入会读取并缓存全部三相配置、历史方案和基础目录。后续控件交互和配置切换从内存读取，不会重新连接数据库；点击“刷新数据库读取”只清空数据缓存，并复用池中有效连接。连接被数据库断开时，下一次读取会自动重连。

如果本地 MySQL 未启动，页面会自动进入**离线轨迹回放模式**：单设备精算、一致性验证和新 GA 运行会被明确禁用，但 `outputs/*.sqlite3` 仍可完整浏览。这使已经完成的实验可以脱离业务系统复盘，且不会把离线状态误表示成“计算失败”。Python 不读取 Java 的开发配置文件。

## 当前验证边界

当前候选与精算代码包含三种铁芯和波纹/散热器油箱分支，并依据约束向量给出 `feasible`。当前公式版本为 v9；已对 10 条真实记录完成逐字段回归：7 条含 Java 快照（`calculationSnapshotVersion == 1`）的历史记录 `28927`–`28933`，以及经 `faladi_mcp` 真实发起后端计算后新建的 3 条记录 `28934`–`28936`。按 `calculation/consistency_check.py` 的字段口径比对（历史快照 31 项/条，实时记录额外含记录表顶层 `price`，32 项/条），**320/320 项精确一致**。此前两条低压温升差异已定位为三相 Java 按低压导线类别派生层间绝缘厚度、而 Python 错读配置固定值；Python 已改为同源派生并完成回归。

覆盖率方面：圆形/长圆/椭圆三种铁芯、波纹/散热器两类油箱、全油道与半油道、椭圆×散热器组合、铜价/铝价两条价格来源均已有真实样本，测试覆盖矩阵无空缺。复现性方面，对方案 503 发起实时重算得到的记录 `28935`，与同方案的历史记录 `28930` 的 161 个同名字段逐一相同、`price` 相同，说明后端当前版本对配置 503 的结果与产生历史快照时一致。该结论只覆盖已测样本与字段，不能替代所有产品的工程验证。

09-22 已完成当前 v9 代码的四策略对照：配置 401、当前搜索域、B′=8000、10 个配对种子，四策略均得到严格解；经验有效域注入相对未注入策略显示成本改善（Hodges–Lehmann=196.625，p=0.03389），代价是演化精算调用中位数增加 2843.5。耦合交叉未检出额外效应（p=0.5637），诊断反馈变异在注入之上未检出额外收益（p=0.1434）；401 的油道域各仅一个组合，不能对阻抗油道优先下结论。因此默认只调整为经验有效域注入 + 普通随机变异，保留耦合交叉以维持与已验证注入组合一致；诊断反馈仍为可选实验策略。默认入口切换发生在 09-22 结项后，需按[09-23 最小冒烟清单](说明文档/2026-09-23测试/默认策略调整后冒烟清单-2026-09-23.md)单独核对。完整证据、未覆盖路径与改动说明见[09-22 测试结论](说明文档/2026-09-22测试/测试结论-2026-09-22.md)和[默认策略调整说明](说明文档/2026-09-22测试/基于0922测试的默认策略调整说明.md)。
