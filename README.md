# Crypto Agent — 最多三币种 AI 模拟交易 MVP

**当前部署（2026-09-28）**：交易和面板已迁至 `optiplex` 服务器，入口为 [Tailscale Paper 监控台](https://optiplex.tail8fb77a.ts.net:8447)。服务器使用 systemd 用户服务并已启用开机常驻；本机 launchd、面板和旧 Codex 自动化保持暂停，服务器另有只读的两小时 Codex 策略检查。运维、通知、备份和恢复步骤见 [服务器部署说明](docs/server-deployment.md)。下文的本机安装方式供开发使用，勿同时恢复同一账户的本机交易。

现货闭环：**行情/账户 → 策略 → 独立风控 → 持久化订单预览 → 显式模拟执行 → 成交核对 → SQLite 记录/评估**。默认手动运行；用户授权后可启用至少间隔 10 分钟的自动 Paper 循环。Paper 交易宇宙固定为最多三个 USD 现货对，当前选择 BTC/USD、XRP/USD；本地离线 broker 仍使用 BTC/USD 合成数据。无实盘、杠杆、做空或模型训练；提供本地 Web 监控台及固定 Paper 调度启停。

规则策略用于验证流程和作为对照，不代表盈利策略。演示参数不是投资建议；离线结果和 Paper 表现均不保证实盘收益。


## 本地交易监控面板

新增 `frontend/`（React + TypeScript + Vite + Lightweight Charts）和 `src/crypto_agent/api/`（FastAPI）。统一深色界面提供账户、BTC/XRP K 线/成交量、独立权益曲线、Agent 评级/依据/风控、持仓、订单、逐笔成交、历史决策及运行日志。行情、账户与交易记录保持只读；面板新增固定本机 Paper 自动交易启停。启动开关复用既有授权命令并加载已批准的 launchd 任务，随后每 5 分钟由原脚本处理 BTC/USD、XRP/USD；停止先写暂停标记再卸载任务，不撤销已经受理的订单。策略、额度和风控仍由原 CLI/配置管理，面板不提供单笔下单或配置修改。

**安装与启动**（Python 3.12–3.14，Node.js 22.12+；本机 Node 25）：

```bash
cd /Users/ze/Projects/crypto-agent
# 保留当前可选 AI 环境；只需监控台时可省略 --extra ai
PATH=/opt/homebrew/bin:$PATH .venv/bin/uv sync --frozen --extra dashboard --extra ai --group dev
npm --prefix frontend ci
npm --prefix frontend run build

# 固定离线演示；不需要密钥，也不读写交易数据库
.venv/bin/python -m crypto_agent.api --demo --port 8765
# 浏览器打开 http://127.0.0.1:8765
```

使用**现有真实 Paper** 配置，在另一个终端运行：

```bash
.venv/bin/python -m crypto_agent.api --config runtime/paper-session --port 8766
# 浏览器打开 http://127.0.0.1:8766
```

`runtime/paper-session` 是本机已有私有配置，不随仓库分发。其他机器传入已有、校验通过的配置目录，例如 `--config config/my-paper`。API 复用 `load_settings`、`AlpacaPaperBroker` 与现有表结构；风控配置不完整时明确报错。后端从本地环境或已有 `.env` 读取凭据，不覆盖 `.env`。不要将密钥放入前端变量或聊天。默认不带 `--demo` 即为 Paper，认证/网络/配置失败不会自动替换成演示数据。即使配置允许原 CLI 交易，行情/账户查询使用的 broker 仍强制关闭提交；新增启停接口仅控制固定的本机 Paper 调度，不直接执行交易轮次。

构建后由 FastAPI 同源提供前端，服务只绑定 `127.0.0.1`。开发时可另运行 `npm --prefix frontend run dev`，打开 `http://127.0.0.1:5173`；Vite 把 `/api` 代理到 `127.0.0.1:8765`，要开发真实 Paper 界面则在 8765 端口启动 Paper 后端。面板不提供数据模式切换按钮。

**自动交易开关**：页面顶部显示真实 launchd 加载状态与交易启用状态，每 5 秒核对。点击“启动自动交易”才会恢复原调度；首次打开页面或重启面板不会恢复交易。只有本机 `runtime/paper-session` 且策略批准摘要、安装任务完全一致时可用；`inflight` 或未知提交会阻断启动。状态异常时仍可尝试“停止调度”，错误会显示并重新核对，不自动重试启停。演示模式禁用控制。完整说明见 [本机 Paper 调度](docs/local-paper-scheduler.md)。

**数据口径**：

| 区域 | 来源、区间和限制 |
| --- | --- |
| 总资产/持仓 | Alpaca Paper 当前权益、持仓估值与未实现盈亏；多次顺序 GET，不是原子快照。可用现金为 `min(cash, non_marginable_buying_power)`，另列现金余额。 |
| 今日盈亏 | Alpaca Paper 权益历史，Asia/Tokyo 午夜至最近已闭合分钟。平台时间戳是桶起点，因此取结束于东京午夜的一桶作为日初基准；扣除净外部入金。 |
| 累计收益 | Alpaca Paper 已闭合 1 分钟历史首尾权益减净外部入金；从匹配账户的本地跟踪起点开始，最多近 29 天，卡片显示实际区间；非账户终身收益。百分比分母为区间期初权益，非时间加权收益率。 |
| 最大回撤 | 同累计收益区间，以资金按分钟桶末调整后的净值峰谷计算；不是分钟内连续最大回撤。演示使用已知资金流的 2 分钟采样。 |
| BTC/XRP K 线 | 顶部行情标的按钮切换 BTC/USD、XRP/USD；Alpaca Crypto US；1 分/5 分/1 小时，各最多 180 根。价格精度 BTC 2 位、XRP 4 位；末根可能未闭合，时间为柱起点。成交箭头与紫色 Agent 圆点不同，只标所选币种在当前图表时间范围内的事件。行情按币种与周期分别缓存，切换时不显示其他币种的旧数据。 |
| 权益曲线 | 匹配当前账户的 SQLite 历史 + 监控台实时读取的 Alpaca 权益，最多 2,000 个点。实时采样约 10 秒更新，只保留在内存，不写交易账本；重启后重新采集。曲线是实际观察余额，不是收益；仅实时观察超过 120 秒未更新时提示延迟。 |
| 订单/成交 | 平台最新最多 500 笔订单、最近 30 天 FILL/CFEE/FEE（首日费用保留日期精度）。订单数量与成交数量分列，不从快照或订单委托量推测成交。 |
| 费用 | 平台独立费用活动可能延迟，无明确订单归属不摊派为逐笔费用，显示未知；只有日期时不编造时刻/时区。演示费用已扣现金，独立记录不重复计费。 |
| Agent | SQLite 最近 500 次运行与决策、500 个自动周期、3,000 个风控阶段。只展示保存字段；无目标仓位不推断，没有信心分数。决策时仓位用该次保存的持仓、行情和权益计算。 |
| 运行状态/日志 | 最近保存的运行/周期状态，并不声称进程持续存活。超过 30 分钟提示过期；周期耗时为真实结束减开始，单独运行或未结束周期的耗时为“未记录”。 |
| 关联 | 只有数据库 Paper/账户绑定和保存的 client/broker order ID 支持时才关联决策→订单→成交；不按时间接近猜测。不可关联或超过当前查询范围时解释原因。 |

收益查询固定使用 Paper `/v2/account/portfolio/history`，`1Min` / `continuous` / `cashflow_types=ALL`，1 分钟权益历史按最多 6 天分段读取并按桶末合并，避免超过平台 7 天限制；并分页核对同区间全部账户活动（最多 5,000 条）。CSD、CSW、ACATC 作为外部资金流；平台已计入历史权益的费用、股息、利息保留在损益中；历史估值可能与实时余额不同，迟记费用尚未反映。每张卡片展开列出计算用的期初/期末权益。未分类 journal、无法定价的资产转移、资金流不一致、缺失分钟或缺少东京午夜基准时，对受影响指标显示具体原因，不补零。最后历史点超过 3 分钟标记延迟；读取失败保留上次成功统计，不影响当前资产展示。

**刷新与异常**：前端集中每 5 秒请求总览，每 10 秒请求当前币种/周期行情；持续定时轮询；浏览器后台节流或电脑休眠后，页面重新可见、获得焦点或网络恢复时立即补刷。后端按数据类型缓存：账户/订单成交/权益/行情 8 秒，数据库 Agent 3 秒，收益统计 60 秒。缓存预留请求耗时，正常情况下账户/订单/成交/权益约 10 秒、Agent 约 5 秒、行情约 10 秒更新；收益以已闭合分钟为准，仍每 60 秒更新。网络慢或请求未完成时跳过本轮，不堆积并发请求。每种缓存 singleflight；手动刷新去重窗口 2 秒。页面请求期间禁用刷新，失败保留同来源最后成功数据并标记过期。Paper 页面按真实当前时间判断决策到期；演示使用固定的 `2026-09-19T12:00:00Z` 时钟；stale 场景将时钟前移 30 分钟，保留原观察时间。API 重启会清空内存缓存。

演示包含买入、卖出、部分成交、风控拒绝、Hold 缺少目标值和历史告警。以下演示 URL 用于验收状态（仅 `--demo` 可用）：

- `http://127.0.0.1:8765/?scenario=stale`：过期数据。
- `http://127.0.0.1:8765/?scenario=partial`：行情与交易流水局部失败，保留成功数据。
- `http://127.0.0.1:8765/?scenario=empty`：无数据。
- `http://127.0.0.1:8765/?scenario=disconnected`：来源断线，保留旧值。

**长期运行排障**：更新 Python 策略代码或配置结构后，请同步重启监控 API，使常驻进程加载新版配置校验器。只重启 `python -m crypto_agent.api` 不会启停 launchd 交易任务。收益卡片的错误仅代表该统计来源，不表示账户、行情或 Agent 都停止刷新。

行情区同时显示最新 K 线时间和成功查询时间。查询时间持续推进但提示“平台 K 线延迟”，表示平台仍返回较旧的柱，自动刷新会继续；不会用本地时间覆盖行情时间或补造缺失柱。

**验证与排障**：

```bash
.venv/bin/pytest -q
.venv/bin/ruff check src tests research
.venv/bin/ruff format --check src tests research
npm --prefix frontend run test
npm --prefix frontend run typecheck
npm --prefix frontend run build
```

若显示前端未构建，执行 `npm ci` 与 `npm run build`；若连接失败，确认后端端口、配置和本地凭据。若数据库未建立/账户绑定不匹配，选择原 Paper 账本，不新建或替换账户数据。历史快照较旧不会再单独触发权益过期；曲线会接入实时账户观察。若权益仍延迟，请看具体来源和最后观察时间，而不是反复点击刷新。`--port` 可避开端口占用；API 拒绝非 loopback Host、外部 Origin 和修改请求。不要将服务通过代理公开。

图表遵循 Lightweight Charts Apache-2.0，完整许可与 TradingView 署名在 `frontend/public/NOTICE.txt`，页面底部提供链接。没有加载远程字体、图标或行情脚本。依赖由 `uv.lock` / `frontend/package-lock.json` 锁定。详细合同见 [API CONTRACT](src/crypto_agent/api/CONTRACT.md)，本次验收见 [监控台验证记录](docs/dashboard-verification.md)。

## 立即运行：不需要任何 API 密钥

本项目已经建立独立 `.venv`。在当前机器上：

```bash
cd /Users/ze/Projects/crypto-agent
.venv/bin/crypto-agent demo
```

首次运行使用明确标注的 **SYNTHETIC TEST DATA**，初始化本地虚拟现金 10,000 USD，以固定 BTC 中间价 50,000、买价 50,010、卖价 49,990 执行一次规则决策、风控、预览、模拟成交、核对和报告。时间戳随本地模拟时钟刷新，价格不是实时行情。不会创建外部订单，也不会调用模型。

离线状态持久化在 `runtime/demo-offline.sqlite` 和 `runtime/demo-offline.broker.sqlite`；再次运行会读取已有持仓，已达到目标或剩余量低于交易下限时不下单。需要全新实验时指定新的数据库文件，避免删除旧记录：

```bash
.venv/bin/crypto-agent --db runtime/my-new-demo.sqlite demo
```

## 安装与真实锁文件

要求 Python 3.12–3.14。当前机器使用 Homebrew Python 3.14；系统 Git/Python 可能遇到 Xcode 许可问题，可用 `/opt/homebrew/bin/git` 和 `/opt/homebrew/bin/python3`，无需更改系统许可或上游源码。

新环境安装（已有 `.venv` 可直接运行 `uv sync`）：

```bash
/opt/homebrew/bin/python3 -m venv .venv
.venv/bin/python -m pip install uv
PATH=/opt/homebrew/bin:$PATH .venv/bin/uv sync --frozen --group dev
```

基础运行仅依赖 `httpx`、`PyYAML`、`python-dotenv`。安装 AI 适配器的可选依赖：

```bash
PATH=/opt/homebrew/bin:$PATH .venv/bin/uv sync --frozen --extra ai --group dev
```

`uv.lock` 由实际依赖解析生成，包含版本、包哈希及 TradingAgents 固定提交。AI 是可选 extra；基础离线环境不需要整套模型依赖。本次验收也实际安装了 AI extra，验证了上游导入和真实结构化 schema。

## 命令入口

全局选项写在子命令之前。命令输出 JSON；错误、风控阻断和不确定提交返回非零状态码。以下命令明确使用离线数据：

```bash
.venv/bin/crypto-agent --config config/demo --mode offline doctor --connect
.venv/bin/crypto-agent --config config/demo --mode offline market
.venv/bin/crypto-agent --config config/demo --mode offline account
.venv/bin/crypto-agent --config config/demo --mode offline positions
.venv/bin/crypto-agent --config config/demo --mode offline analyze
.venv/bin/crypto-agent --config config/demo --mode offline preview
# 将上一步返回的 run_id 代入；这一步才执行已保存的订单。
.venv/bin/crypto-agent --config config/demo --mode offline execute RUN_ID --execute-offline
.venv/bin/crypto-agent --config config/demo --mode offline reconcile
.venv/bin/crypto-agent --config config/demo --mode offline orders
.venv/bin/crypto-agent --config config/demo --mode offline history
.venv/bin/crypto-agent --config config/demo --mode offline report
# 只允许撤销本项目记录的已提交订单；随后查询平台确认状态。
.venv/bin/crypto-agent --config config/demo --mode offline cancel CLIENT_ORDER_ID --execute-offline
```

`status` 是 `doctor` 别名，`run` 是 `preview` 别名。`analyze` 保存分析和风控结果但不创建预览。`doctor --connect` 仅执行 GET 类读取；`history/orders/report` 读取本地记录，`report` 不自动刷新行情。先执行 `reconcile` 获得最新账户和成交记录。

Paper 模式的 `market`、`analyze`、`preview` 和 `run` 可使用 `--symbol BTC/USD` 或 `--symbol XRP/USD`；省略时选择配置列表第一项。账户、核对和报告始终覆盖整个配置组合。

使用 `--db` 时，后续命令始终使用同一路径。一个 Alpaca 账户应固定使用一套数据库；同一数据库有进程锁、模式绑定和账户 ID 绑定，避免并行提交或混入另一个账户。不要用多个数据库同时交易同一个账户。

## Alpaca Paper 配置

默认 `config/risk.yaml` 的必要限制刻意留空，所以直接 `doctor` 会明确指出缺失项，并禁止执行。`config/demo/` 提供完整测试参数，默认仍然 `trading_enabled: false`。可复制到本地目录后修改；不在版本控制中保存凭证。

1. 在本地 `.env` 填写 `ALPACA_API_KEY`、`ALPACA_SECRET_KEY`。只使用 Paper 账户密钥，`ALPACA_PAPER_TRADE=true`。现有 `.env` 不应覆盖；仓库仅提供 `.env.example`。
2. 填写 `risk.yaml` 所有必需限制。`paper.yaml` 固定为 `https://paper-api.alpaca.markets`，`symbols` 与风险白名单必须完全一致、去重且包含 1–3 个受支持 USD 现货对；默认配置为 BTC/USD、`trigger: manual`。
3. 若使用规则策略，`strategy.yaml` 设 `name: baseline`。若使用 AI，见下一节。
4. 检查连接与预览：

```bash
.venv/bin/crypto-agent --mode paper doctor --connect
.venv/bin/crypto-agent --mode paper market
.venv/bin/crypto-agent --mode paper account
.venv/bin/crypto-agent --mode paper analyze
.venv/bin/crypto-agent --mode paper preview
.venv/bin/crypto-agent --config runtime/paper-session --mode paper analyze --symbol XRP/USD
```

10 分钟分钟策略可从 `config/strategy.intraday.example.yaml` 复制；必须与一份完整的 Paper `paper.yaml` 和 `risk.yaml` 一起放在私有配置目录。示例不含密钥，模型密钥仍只从 `.env` 读取。

**外部 Paper 执行需要两项显式条件**：本地 `paper.yaml` 的 `trading_enabled: true`，以及执行命令的 `--execute-paper`。启用后先重新生成预览，因为配置摘要变化会使旧预览失效。

```bash
.venv/bin/crypto-agent --mode paper preview
.venv/bin/crypto-agent --mode paper execute RUN_ID --execute-paper
.venv/bin/crypto-agent --mode paper reconcile
```

只配置密钥或运行分析/预览不会下单；自动模式的 `auto-tick --execute-paper` 则可以在风控通过后提交模拟订单。所有实盘地址、非 HTTPS 地址、附加路径、仿冒域名及重定向均被拒绝。执行定价来自固定的 Alpaca crypto `us` latest orderbook，以最优 bid/ask 及平台时间戳定价；分钟策略另外读取同一 `us` 数据源的历史 `1Min` bars。任一网络请求失败都直接报错，不切换为离线数据。MVP 遇到配置外币种/股票持仓或挂单会拒绝使用该账户。实际外部验证结果见验收记录。

## 已授权的自动 Paper 交易

2026-09-22 本机调度已迁移到 **launchd + 本地脚本**，每 300 秒处理 BTC/USD、XRP/USD，替代 Codex heartbeat。安装时交易保持暂停，不自动恢复。安装、停机、通知及故障恢复见 [本机 Paper 调度](docs/local-paper-scheduler.md)。下方旧 heartbeat 频率及小额实验参数是早期部署说明；本机当前策略与额度以已批准的 `runtime/paper-session` 配置为准。

用户授权后，当前机器使用忽略于 Git 的 `runtime/paper-session/` 配置及同目录账本。`paper.yaml` 设置 BTC/USD、XRP/USD，`trigger: scheduled`、`trading_enabled: true`；当前分钟策略模型调用硬超时为 60 秒。默认仓库配置仍保持手动且禁止提交。

```bash
# 显式启用配置摘要对应的策略；不会安装调度器或立刻下单。
.venv/bin/crypto-agent --config runtime/paper-session --mode paper auto-enable --execute-paper
# 最多运行一个完整自动循环，内部先保存预览，再重新风控并执行。
.venv/bin/crypto-agent --config runtime/paper-session --mode paper auto-tick --execute-paper
.venv/bin/crypto-agent --config runtime/paper-session --mode paper auto-status
# 立即写入持久化暂停开关，分析中的进程会在提交前再次检查。
.venv/bin/crypto-agent --config runtime/paper-session --mode paper auto-pause
```

持续唤醒由本任务的 Codex App heartbeat 调度；CLI 自身不会常驻运行。调度器每 10 分钟尝试一次，数据库记录实际启动时间，并以进程锁阻止重叠。每轮只分析一个币种，按 BTC→XRP 持久化轮换，因此理想情况下每个币种约 20 分钟分析一次；模型耗时不会导致并发补跑。未满 600 秒返回 `cooldown`，运行中返回 `busy`，停机/休眠后不补跑错过的轮次。

每轮首先核对平台订单与账户。有挂单就跳过新分析；本项目已提交订单的决策到期后，请求撤销剩余量并再次核对。未知提交状态立即暂停，不重发 POST。达到已观察日亏损限制立即暂停，并请求撤销本项目仍在挂单的剩余量；撤单超时仍保持暂停并要求核对。连续三轮故障或风控阻断也暂停。配置摘要变化必须重新显式启用。暂停阻止后续自动提交，不撤回已经发到平台的订单；需要 `reconcile`/`cancel` 核对处理。停止后不要运行 `auto-enable`，除非确实要恢复。

当前本地实验参数为：每笔最多 100 USD、每个币种仓位最多权益的 1%、组合总仓位最多 2%、当日已观察权益下降 20 USD 停止。自动档使用 `intraday_ai` / `intraday_10m`：每轮读取最近 60 根 Alpaca `1Min` bars，仅使用已闭合且末端不超过 180 秒的数据，计算 3/10/30 分钟收益、5/20 EMA、20 分钟实现波动/区间及近期成交量变化。基础评级映射仍为 Buy 0.05%、Overweight 0.03%、Underweight 0.01%、Sell 零；当前 `intraday-ai-1min-v3-capped-probe` 的增持目标进一步受 **每币总持仓 10 USD** 和 **往返成本预留预算 0.15 USD** 约束，取更小值。已有持仓计入额度，不是每轮再买 10 USD；两个币种的实验目标合计至多约 20 USD，市场价格上涨可使已持仓市值超过目标。这些是模拟实验限制，不是投资建议。自动化不能放宽风控、增加第四个币种或切换为实盘。

轻量模型的持仓视角为 10–180 分钟，只能返回严格 JSON 评级。代码内的四项动量投票形成 `-4..4` 分：3、10、30 分钟收益和 EMA5/EMA20 各一票；当前阈值基准为 2 bps。空仓增持至少要求总分 2，Buy 至少要求 3；Underweight 减仓要求不高于 -2，Sell 清仓要求不高于 -3，避免弱负面信号触发来回调仓。旧 `cost_cover` 档要求过去 3/10/30 分钟正向价格变化覆盖双边成本，在当前 30 bps 单边费用和 20 bps 单边滑点预留下常阻止所有入场。当前 `capped_probe` 档改为明确预算的小额 Paper 试验：成本预留率为 `点差/中间价 + 2×(费用bps+滑点bps)/10000`，持仓金额上限为 `min(10 USD, 0.15 USD / 成本预留率)`。过去涨幅不再被当成未来收益预测或成本覆盖证据；此模式允许承担试仓成本，并不证明有盈利优势。0.15 USD 是配置的成本预留预算，不是实际手续费或亏损的保证上限。预览和提交前均用最新点差重算，不能通过旧预览绕过预算。模型评级不能绕过这些数值规则。Hold 保持现状，REVIEW/解析失败/模型超时/分钟数据不足、过期或旧 cost_cover 档成本覆盖不足均作为正常 `no_order`，不会累计成自动故障。该规则提高了时间分辨率，不代表传统低延迟高频交易或盈利能力。

### 受限的策略参数评估

每次有效 AI 决策都记录币种、评级、实际可用时间、新鲜 bid/ask、实际循环启动时间、策略版本、模型及配置摘要；包括因成本、信号矛盾或证据不足返回的 REVIEW，因为这些无交易时点同样属于策略表现样本；模型超时、解析失败、无效分钟行情及决策完成时已过期的报价不计为优化样本。每个币种至少收集 **146 条同源新观察** 后才独立评估；当前双币轮换下理想连续运行约需两天。样本不足、数据间断、币种/来源混杂或验证不足时不改参数。双币轮换允许相邻同币观察间隔最多 30 分钟（单币 20 分钟、三币 40 分钟），避免实际约 22 分钟的轮换被当成断档；订单簿 60 秒与分钟 bar 180 秒的实时有效期不变。策略/配置变更重新分组积累，不混用旧版本样本。

只比较原始评级目标的 `1`、`0.75`、`0.5` 三个固定倍数，且只能向下调整；试仓金额和成本预算同步乘此倍数，假设回放也使用相同限制；不重写模型、提示词、交易逻辑或风控。按时间将前约三分之二用于选候选，后段只验证所选候选；训练至少 96 个样本且跨度 16 小时，验证至少 48 个样本且跨度 8 小时。验证段的当前策略和候选策略均要求至少 5 次完整买卖往返；验证净结果须比当前策略改善至少 1 USD 且至少为当前净结果绝对值的 20%，最大回撤不能更差。每个验证窗口只消费一次；策略变更和审计记录在同一事务提交。

评估使用决策可用之后的后续报价，计入配置的费用/滑点预留、余额、订单下限、精度及仓位/日亏损限制。这是**观察数据上的假设成交估计**，不能证明历史限价单确实可成交，也无法观察两次报价之间的风险。它与 broker 的真实 Paper 成交报告分开保存，不保证未来收益。减小目标不会覆盖评级方向规则，例如 Buy 不会因此反向卖出已有持仓。

## TradingAgents 适配器

上游检查版本为 **v0.5.0**，固定 SHA：

```text
2d17df8da1536c121e4d7395ac5a5dcec9e96d6f
```

默认读取独立仓库 `../TradingAgents`（路径相对项目工作目录）。运行前检查固定提交及相关源码无未提交修改；不会修改上游仓库。可选安装使用同一 Git SHA。上游导入、模型缓存及日志在独立子进程临时目录内运行，禁用 dotenv 自动发现和第三方 tracing 环境，子进程不接收 Alpaca 凭证；结束后清理临时分析数据。

配置 `name: tradingagents`，设置 `llm_provider`、`deep_think_llm`、`quick_think_llm` 以及显式 `decision_profile`。支持的决策档只有 `balanced` 和 `short_term_small`；后者仍要求具体市场证据，不会把数据缺失、REVIEW 或不利信号强制改成买入。本 MVP 支持 `openai`、`anthropic`、`google`、`deepseek`；模型 ID 使用你账户实际可用的名称。提供商密钥仅从本地环境/`.env` 获取：`OPENAI_API_KEY`、`ANTHROPIC_API_KEY`、`GOOGLE_API_KEY`、`DEEPSEEK_API_KEY`。不在 YAML 填密钥。以下本地环境变量可以覆盖模型配置，其他风控与执行开关只能由 YAML 设置：

```text
TRADINGAGENTS_LLM_PROVIDER
TRADINGAGENTS_DEEP_THINK_LLM
TRADINGAGENTS_QUICK_THINK_LLM
```

OpenAI 兼容服务可以在本地 `.env` 设置 `OPENAI_BASE_URL=https://your-provider.example/v1`，或在 `strategy.yaml` 设置 `backend_url`。优先级为 `TRADINGAGENTS_LLM_BACKEND_URL` > `OPENAI_BASE_URL`（仅 OpenAI 提供商）> YAML `backend_url`；均未设置时使用提供商默认地址。自定义地址必须为 HTTPS，不得包含用户名、密码、查询参数、片段或空白。地址经过校验后纳入配置摘要并显式传给 AI 子进程；模型密钥仍只从本地环境读取。

实际接口为：

```python
graph = TradingAgentsGraph(selected_analysts=["market"], config=config)
state, signal = graph.propagate(ticker, utc_date, asset_type="crypto", portfolio=PortfolioContext(...))
```

`ticker` 按本轮选择显式使用 `BTC-USD` 或 `XRP-USD`。传入 USD 现金及全部白名单持仓与均价，附带 broker 账户权益、可用购买力、本轮实时报价和 UTC 时间。上游 `PortfolioContext` 没有挂单字段，因此已有挂单时适配器返回 REVIEW，不调用模型。当前 MVP 仅使用 market analyst。

适配器严格要求最终报告包含唯一的 `Rating` 字段、`Executive Summary`、`Investment Thesis` 和非空 `market_report`，并与上游返回评级完全一致。不会从正文中寻找 BUY/SELL 关键词下单。证据字段的存在不代表模型观点真实或有预测能力，硬风控始终独立执行。

`rating_target_pct` 是账户权益比例，`0.10` 表示 10%，不是 10：

| 评级 | 目标规则 |
|---|---|
| Buy / Overweight | `max(当前实际仓位比例, 对应配置目标)`，不会因评级而反向减仓 |
| Hold | 保持已有仓位，不生成订单 |
| Underweight | `min(当前实际仓位比例, 对应配置目标)`，空仓时不会开仓 |
| Sell | 目标为 0 |
| REVIEW、解析失败、依据缺失、超时 | 不生成订单 |

评级映射必须递减有序，Sell 必须为零，配置目标不能超过硬性仓位上限。决策有效期从分析开始时算起，不在超时后延长；worker 具有硬超时，超时会终止子进程。分析后重新读取行情和账户，账户变化或跨 UTC 日期时要求重新分析。

规则策略 `price-threshold-v1`：报价不高于 `baseline_buy_below_usd` 时设定 `baseline_target_pct`，否则目标为零。没有历史训练、参数优化或盈利承诺。

完整 TradingAgents 图仍保留为 `name: tradingagents`，用于手动的较慢深度分析和对照；当前 10 分钟自动档不再等待完整图。`intraday_ai` 只把经过本地校验的数值特征、当前订单簿和账户摘要传给隔离子进程，子进程仅接收模型凭证，不接收 Alpaca 凭证。模型方向之后仍执行同一套评级映射、账户复读、独立风控、预览、幂等提交和核对流程。

## 风控与订单计算口径

所有金额和数量使用 `Decimal`；内部币种使用带斜杠的 USD 交易对，并兼容平台紧凑格式和上游连字符格式。时间统一 UTC。数量和限价按每个币种的 Assets API 返回的 `min_order_size`、`min_trade_increment`、`price_increment` 检查，不硬编码真实交易单位。离线 broker 的测试单位另有明确固定值。

| 必需限制 | 计算及触发行为 |
|---|---|
| `allowed_symbols`、禁用杠杆/做空、预览要求 | 1–3 个受支持且去重的 USD 现货对、false、false、true；Paper 与风险白名单必须完全一致 |
| `max_position_pct` | 单币数量 × 该币价格 / 账户权益；买单用限价估值并扣除预留费用；单币超限时只允许减仓决策或 Hold |
| `max_total_position_pct` | 所有白名单持仓按各自最新报价估值，加未成交买单剩余承诺；买单提交前再次计入限价和费用，超过组合总上限即拒绝 |
| `max_order_notional_usd` / `min_order_notional_usd` | 数量 × 限价，买卖都检查；超最大拒绝，不静默拆单；低于最小或数量不足时无订单 |
| `max_daily_loss_usd` | `max(0, 当天首次有效观察权益 − 当前权益)`，包含已实现/未实现变化及已反映的费用。达到限制后买卖全部阻断，无自动平仓；UTC 换日才建立新基准，重启不重置 |
| `max_data_age_seconds` | broker 最优订单簿报价及账户读取时间都必须在窗口内；未来时间超过 5 秒拒绝。自动 Paper 配置可在不延长该窗口的前提下有限等待真实新订单簿，等待超时仍拒绝 |
| `max_decision_age_seconds` | 同时检查创建时间、最大存活时长和到期时间 |
| `fee_buffer_bps` | 订单金额的费用预留，仅用于风险计算，绝不伪装成实际费用 |
| `slippage_bps` | 买限价按 ask 上浮后向上取 tick；卖限价按 bid 下浮后向下取 tick；价格偏离预览超限拒绝 |
| `max_spread_bps`、`min_price_usd` / `max_price_usd` | 检查非有限数、零/负数、错位报价、过宽价差及异常价格范围 |

日亏损是**该数据库当天首次有效观察后的权益下降**，不是未观察期间的完整 UTC 日损失，也不是经出入金调整的收益。专用 Paper 账户应避免手动充值、提现或并行策略；这类变化会影响计算，报告会注明限制。开始跟踪之前的历史盈亏不归入本项目。

规划对本轮币种计算 `权益 × 目标比例 / 现价 − 该币已有数量 − 该币未成交净买入余量`。部分成交只计算剩余量；组合检查按每个币种最新价格估值，挂卖单不抵消潜在买单敞口。系统一次最多保留一个本项目挂单，要求先核对/撤单后再分析其他币种。买单数量同时受现金、非保证金购买力、费用预留、单币目标和组合总敞口限制；卖单不超过该币可用持仓；数量始终向下按平台单位取整。

所有执行是 `limit` + `gtc`；不会自动追价、改单或清理外部挂单。手动模式的 GTC 可能长期未成交，需要 `reconcile` / `cancel`；自动模式会在后续循环发现本项目订单的决策到期后请求撤销剩余量。

## 持久化、幂等与恢复

SQLite 分别存储 `runs`、`decisions`、`intraday_contexts`、`risk_results`、`orders`、`fills`、`fees`、分币种行情、账户快照和 UTC 日基准。分钟策略逐轮保存实际输入 bars，另保存策略版本、模型标识、无凭证配置摘要及 SHA256、行情/账户/决策时间。报告按币种分别维护移动平均成本和最新报价，再汇总已实现/未实现盈亏。原始 SDK 错误和 HTTP body 不进入日志；输出和 JSON 存储另有本地凭证脱敏。

预览时生成并持久化 `client_order_id=ca-<run_id>`。提交前原子地标记 `submitting` 并提交事务，之后才发送 HTTP。请求超时/网络异常时按同一 ID 查询订单；无结果则留为 `unknown`，**不重发 POST**。即使进程在标记之后、POST 之前崩溃，也保守停留在未知状态。重复 `execute` 仅查询，不重新下单。

恢复时运行 `reconcile`，先验证账户和模式，再查询已提交订单、读取分页成交/费用活动及当前账户。拒单、部分成交、未成交、撤单、到期均按平台响应记录；成交只来自 broker `FILL`，不从请求金额推测。撤单请求不能证明零成交，仍需核对。未知订单未解决前禁止新交易；如果长期查不到，需要依据 Paper 控制台/官方支持确认状态，MVP 不提供跳过不确定性直接重试的开关。

## 报告

`report` 使用最后一次已观察快照，输出其账户和行情时间；可用 `research/evaluate.py --database ... --mode offline|paper` 查询同一账本。

- 已实现毛盈亏：从初始 broker 持仓均价与随后 FILL 按移动平均成本计算；持仓数量与账本不一致时返回 `null`。
- 未实现盈亏：最新实际持仓数量 ×（最新已观察行情中间价 − broker 持仓均价）。
- 费用：单独累计 broker FEE/CFEE；缺少计价信息会标记未知，而不是按零处理。Paper 费用可能延迟入账，始终标记 `fees_may_be_pending`。
- 日期级、无订单归属的费用标记 `fee_attribution_uncertain` 和成本口径暂定，净费用后结果返回 `null`；不把未知归因描述为精确净收益。
- 权益变化是观察区间首尾差额，没有出入金调整。初次读取不是平台原子快照，若期间有外部成交，账本差异会阻止可靠盈亏归因。

离线费用简化为每笔成交以 USD 扣费；Alpaca 买入费用可能以收到的 BTC 扣除，真实模式读取 CFEE 数量和价格，不复用离线假设。可执行历史回测、年化收益和夏普率尚未实现，`research/backtest.py` 会明确提示未实现。自动参数评估单独记录在 `strategy_observations`/`strategy_evaluations`；自动循环记录在 `auto_cycles`。

## 验证

```bash
.venv/bin/pytest -q
.venv/bin/ruff check src tests research
.venv/bin/ruff format --check src tests research
.venv/bin/python -m compileall -q src research
PATH=/opt/homebrew/bin:$PATH .venv/bin/uv lock --check
.venv/bin/crypto-agent --db runtime/acceptance-offline.sqlite demo
```

当前完整测试为 **387 passed**。覆盖配置缺失/错误、禁止实盘、未知字段、凭证保护、订单簿和分钟 bars 的缺失/过期/未来时间/异常 OHLCV、严格 AI 解析及 REVIEW、量化入场门槛、小额总持仓/成本预算、报价刷新后的方向约束、预览/提交时预算复核、对称减仓/清仓门槛、REVIEW 优化观察、决策过期、仓位/金额/亏损拒绝、精度、已有持仓与挂单、超时后查询、重复执行、部分成交、撤单、跨进程恢复、账户切换隔离、命令级闭环及毛盈亏/费用区分。HTTP/模型响应测试均使用明确的 fixture；可选上游 schema 测试在未安装 `ai` extra 时跳过。

本次验收记录见 [docs/verification.md](docs/verification.md)。没有本地 Alpaca/模型凭证时，真实认证、数据权限、实际平台订单生命周期、实际费用入账和模型推理仍属于未验证集成项。mock 测试通过不等于这些集成已成功。

## 上游与官方接口依据

- [已固定的 TradingAgents 源码](https://github.com/TauricResearch/TradingAgents/tree/2d17df8da1536c121e4d7395ac5a5dcec9e96d6f)：`tradingagents/graph/trading_graph.py`、`portfolio.py`、`agents/schemas.py`。
- [Alpaca Crypto Spot Trading](https://docs.alpaca.markets/us/docs/crypto-trading)：资产精度、cash/non-margin 购买力、禁止加密货币做空。
- [Alpaca Crypto Orders](https://docs.alpaca.markets/us/docs/crypto-orders)：限价单及 GTC/IOC 支持。
- [Alpaca Latest Orderbook](https://docs.alpaca.markets/us/reference/cryptolatestorderbooks-1)：最新订单簿及最优 bid/ask 数据端点。
- [Alpaca Historical Crypto Bars](https://docs.alpaca.markets/us/reference/cryptobars-1)：`1Min` 历史 bars、时间范围、排序和分页约定。
- [Alpaca Client Order ID 查询](https://docs.alpaca.markets/us/reference/getorderbyclientorderid)：按持久化客户端订单 ID 核对。
- [Alpaca Crypto Fees](https://docs.alpaca.markets/us/docs/crypto-fees)：FEE/CFEE、收到资产计费与延迟入账。
