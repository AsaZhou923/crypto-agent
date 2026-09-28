# MVP 验收记录

日期：2026-09-19（Asia/Tokyo）。本记录区分本地验证、模拟 HTTP/LLM 契约验证与尚未进行的外部集成。

## 实际已验证

- 独立 `.venv`：Python 3.14.7；初始版本 0.2.0，三币种扩展后为 0.3.0，订单簿与稳定币种轮换修复后为 0.3.1。
- `uv.lock` 实际解析 93 个包；基础依赖和可选 `ai` 依赖均已安装，TradingAgents 为固定 Git 提交的 v0.5.0。
- `pytest -q`：**188 passed**；含配置/风控/精度、HTTP fixture、AI 输出契约、真实子进程终止、数据库幂等、部分成交/撤单/重启和跨进程 CLI 测试。
- `ruff check src tests research`：通过。
- `ruff format --check src tests research`：通过。
- `python -m compileall -q src research`：通过。
- `uv lock --check`：通过；`uv pip check`：安装依赖兼容。
- 实际导入已安装的 `TradingAgentsGraph`、`PortfolioContext`、`PortfolioDecision`，使用上游真实 renderer 验证适配器；此步骤没有创建模型客户端或发起推理。
- `/Users/ze/Projects/TradingAgents` 的 HEAD 为 `2d17df8da1536c121e4d7395ac5a5dcec9e96d6f`，源码工作区检查干净；没有修改上游。
- `.env`、`.venv`、运行数据库以及 `.omx` 已被 Git 忽略；未输出凭证值，未覆盖既有 `.env`。

## 独立离线闭环证据

在新数据库中执行多个真实 CLI 子进程，依次运行 doctor、demo、reconcile、history、report、再次 preview 及重复 execute。

本机证据目录：

```text
/Users/ze/Projects/crypto-agent/runtime/acceptance-20260919T033830Z/
```

原始 JSON 输出和 ledger/broker SQLite 均保留在该目录（不提交 Git）。运行 ID：`07177931cf354ed69bc7931e371e6797`。

| 检查 | 结果 |
|---|---|
| 数据与 broker | 明确标注的离线合成测试数据；没有网络调用 |
| 首次预览 | `preview`，未提交 |
| 显式离线执行 | `filled` |
| 成交数 | 1 |
| 重复执行同一 run ID | `submitted: false`，未产生第二笔成交 |
| 再次规划 | `no_order` |
| 成交账本与实际本地持仓 | 一致 |
| 记录的模拟费用 | 2.4942612525 USD |
| 观察权益变化 | -2.6937622525 USD（包含测试买卖价差与费用） |

以上数字只证明流水线和会计行为，不是历史回测或收益预测。测试另覆盖买入后卖出、已实现毛盈亏减实际记录费用与权益变化相符。

## 初次交付时未验证的外部项

本地检查返回 `alpaca_credentials_present: false`、`model_configured: false`。因此：

- 未验证真实 Alpaca Paper 认证、行情访问权限、账户/资产接口响应。
- 未提交外部 Paper 订单，未验证平台实际成交、部分成交、撤单、超时恢复或实际 FEE/CFEE 入账。
- 未调用真实模型提供商，未验证账户可用模型、模型推理、上游行情工具在线访问或真实分析耗时。
- HTTP MockTransport 与模型 fixture 测试只验证适配和错误处理，不等同于真实服务集成成功。

后续需要在本地配置 Paper 密钥、模型提供商/模型 ID/对应密钥以及默认 `risk.yaml` 必要限制。先执行只读 `doctor --connect`、`analyze`、`preview`；外部模拟下单仍须启用本地配置并显式使用 `execute RUN_ID --execute-paper`。


## 用户配置凭证后的首轮实际连接（2026-09-19）

用户随后配置本地密钥并授权模拟交易。已验证真实 Alpaca Paper 认证、行情、资产规则、账户、持仓、挂单与活动读取；用户指定的兼容模型网关 `gpt-5.6-luna` 调用及完整 TradingAgents 分析成功。首轮结构化评级 Sell、目标仓位 0，账户空仓，结果为 `no_order`，未发送外部订单。核对后模拟权益仍为 100,000 USD。

新增自定义模型地址配置及相关校验测试、预览报价微动回归后，完整测试为 **217 passed**。Ruff、格式、编译及锁文件检查通过。真实订单提交/成交/部分成交/撤单/费用入账仍未验证。详细本机结果保存在 `runtime/paper-session/result.md` 和同目录 JSON、SQLite 中，均忽略于 Git；凭证未写入这些记录。

## 用户授权的自动交易与受限评估（2026-09-19）

用户明确授权全自动模拟交易、频率最高每 10 分钟一次和策略优化。新增 `auto-enable`、`auto-tick`、`auto-status`、`auto-pause`；默认配置仍为手动。当前本地 Paper 配置启用 scheduled，AI 超时 480 秒；原先的 100 USD 单笔、1% 仓位及 20 USD 已观察日亏损限制未放宽。

- 完整 `pytest -q`：**283 passed**；Ruff 检查/格式通过，编译、锁文件和已安装依赖兼容检查通过。
- 新增持久化 600 秒间隔、重启/并发阻止重复提交、未知订单立即停机、分析中/提交前暂停、日亏损立即停机（包括仍有部分成交挂单时撤销剩余量、撤单超时保持停机）、连续失败暂停、挂单过期撤销与部分成交核对测试。
- 新增固定候选倍数的延迟报价估计、时间顺序/数据来源校验、独立验证、样本不足不调整、只降低仓位、评估不能改写共享风控，以及策略变更/审计事务原子性测试。
- 多个实际 CLI 子进程离线闭环：启用 → 首轮 `filled` → 立即再次运行 `cooldown` → 报告仅 1 笔成交 → 暂停 → 状态 disabled。证据保存在 `runtime/auto-acceptance-20260919T043941Z/`。
- Codex App heartbeat `btc-paper` 已创建并通过工具和本地配置确认 `ACTIVE`，附着于当前任务，每 10 分钟尝试一次。CLI 自身没有常驻调度器，延迟/休眠不会补跑；暂停交易需 `auto-pause`，停止唤醒需同时暂停 App 自动化。

以上自动提交/恢复边界测试使用本地 synthetic broker 和 fixture；不代表平台故障场景已真实复现。自动策略提升至少需要 146 条新鲜实际观察、训练及独立验证门槛；尚未积累这些观察，所以不能宣称策略已经改善或产生盈利。

首轮实际自动 Paper 分析已完成：运行 `9d640ed746624b28af1795673ecaeb52`，评级 Sell，空仓目标已满足，结果 `no_order`，没有发送外部 POST。已积累 1 条实际观察，评估状态 `insufficient_evidence`。实际模型调用、读取账户/行情和自动循环成功不等于实际下单集成已验证；外部订单生命周期仍待后续合格信号产生并执行后核对。本机详情见 `runtime/paper-session/automatic-result.md` 和同目录 JSON/SQLite。

## 用户授权扩展至最多三个币种（2026-09-19）

用户授权自行选择其他币种，但同时交易不得超过三个。先考虑 BTC/USD、ETH/USD、SOL/USD；真实只读检查发现 ETH/USD 报价连续超过 60 秒未更新，风控在模型调用前正确阻断。没有放宽时效限制，最终根据当时实际报价新鲜度与价差选择 BTC/USD、SOL/USD、DOGE/USD。扩展期间先写入 CLI kill switch并暂停 heartbeat，避免旧单币策略运行。

- 配置和 broker 同时限制 1–3 个去重白名单；账户出现第四种资产或挂单即拒绝运行。
- 自动循环每次只分析一个币种，按 BTC→SOL→DOGE 持久化轮换；任何时刻仍只允许一个本项目未决订单。
- 单币仓位上限仍为 1%，新增组合总仓位上限 2%；单笔 100 USD、日亏损 20 USD、无杠杆/做空及 Paper-only 边界不变。
- TradingAgents worker 使用本轮动态 ticker，PortfolioContext 包含全部白名单持仓；评级映射和 REVIEW 规则不变。
- SQLite 新增分币种行情快照；成本、成交、CFEE、已实现及未实现盈亏按币种核算后汇总。
- 策略观察和验证按币种隔离；配置摘要变化后旧观察不会进入新策略的评估窗口。
- 测试新增三币配置上限、平台符号、动态 ticker、按币种价格边界、组合敞口、持久化轮换、旧证据隔离和分币种盈亏核算。完整测试为 **301 passed**，Ruff 检查、格式、编译、锁文件和已安装依赖兼容检查通过。

真实 DOGE/USD TradingAgents 分析成功：运行 `d97ebbdada604b5cae1026045c419f66`，worker 使用 `DOGE-USD`，结构化评级 Sell、目标 0，独立风控通过；该命令为 `analyze`，没有创建预览或外部订单。模型正文中的任意仓位建议不能覆盖固定评级映射、1% 单币及 2% 组合硬上限。

三币配置完成只读连接和核对后已重新启用自动 Paper 循环。新配置首轮运行 `ca982f75af0c417283d89b9c3441688a`，轮到 BTC/USD，结构化评级 Sell、目标仓位 0，结果 `no_order`，未发送订单；下一轮将按持久化游标分析 SOL/USD。当前自动状态 enabled、失败计数 0、停机原因为空。新策略证据从配置切换后独立累计，目前 BTC/USD 为 1 条、SOL/USD 和 DOGE/USD 为 0 条，远低于每个币种 146 条的评估门槛，因此没有调整参数。

## 用户授权的小额短线档（2026-09-19）

用户随后授权适当提高入场积极度并进行小额短线模拟交易。变更前先同时暂停 CLI 自动状态和 Codex App heartbeat，并确认没有平台订单。新增显式 `decision_profile` 白名单；当前 Paper 配置使用 `short_term_small`，把模型决策视角限定为 1–3 天，允许证据偏多但尚未完全确认时以 Overweight 做小额试仓，同时继续禁止强制入场、臆造证据或绕过 REVIEW。适配器版本升级为 `tradingagents-adapter-v2`，上游 TradingAgents 提交未改变。

目标仓位调整为 Buy 0.05%、Overweight 0.03%、Underweight 0.01%、Sell 0；以当前 100,000 USD 权益估算分别约为 50、30、10、0 USD。入场判断更积极，但目标金额比旧配置更小。每币 1%、组合 2%、单笔 100 USD、日亏损 20 USD、数据时效、Paper-only、现货、无杠杆和无做空等硬限制均未放宽。配置摘要变化后，自动状态必须重新显式启用，旧观察不会用于新策略评估。

变更后完整测试为 **304 passed**；Ruff、格式、编译、锁文件和依赖兼容检查通过。实际 Alpaca Paper 只读连接确认三币可交易、行情风控通过、账户可交易且无持仓/挂单；策略摘要为新 SHA256，恢复自动执行前仍保持暂停。

新配置随后以摘要 `ef5763fb18e2a0149b69b6ea9fbae24245538bffbbe5c4e1de7b624d0ace2ae0` 显式启用，并运行首轮实际自动周期 `f1520324393a4f0dabc2b4ad2739fa53`。BTC/USD 的新档输出仍为 Sell：价格接近布林上轨且 MACD 动量未确认，空仓目标已满足，结果 `no_order`，没有发送订单。该结果验证短线档没有强制制造成交。Codex App heartbeat 已更新为新的决策档和目标后恢复 ACTIVE；下一轮轮换至 SOL/USD。

## 陈旧报价诊断与订单簿修复（2026-09-19）

SOL/USD 在模型分析后重新读取时，Alpaca latest quote 已约 71 秒未更新；下一轮 DOGE/USD 的 latest quote 已约 3 分钟未更新。两次均被 60 秒硬限制正确阻断，没有订单。连续阻断并非模型或账户故障，而是 latest quote 的平台事件时间较旧。变更前暂停 CLI 自动状态和 heartbeat，并确认账户无持仓/挂单。

根据 Alpaca 官方 latest orderbook 接口，将 Paper 行情改为 `alpaca_crypto_us_orderbook`：使用真实订单簿第一档 bid/ask 和平台时间戳，不用本地请求时间替代，也不回退到测试数据。`market_data_source` 纳入严格配置与 SHA256 摘要，避免旧 quote 观察和新 orderbook 观察混入同一评估窗口。自动运行对本轮选中币种最多等待 60 秒并轮询真实订单簿；`max_data_age_seconds` 仍为 60，等待结束时没有新数据仍然阻断。

新增订单簿小数/时间戳、缺失档位、零数量、错位 bid/ask、行情来源白名单和有限等待测试。首次完整测试为 **307 passed**，Ruff、格式、编译、锁文件与依赖检查通过。真实只读验证一度得到 SOL/USD 和 DOGE/USD 小于 9 秒的新鲜时间戳，但随后 SOL/USD 在完整 60 秒等待内仍未更新，证明单次新鲜读取不足以说明该交易对稳定。

随后连续四次、间隔 10 秒采样主要 USD 现货对：BTC/USD、XRP/USD、DOGE/USD 的订单簿年龄维持约 7–38 秒，SOL/USD 持续约 142–174 秒。利用用户既有的币种选择授权，将运行时组合调整为 BTC/USD、XRP/USD、DOGE/USD，轮换顺序同步改为 BTC→XRP→DOGE；SOL 仅从当前运行时白名单移除，历史记录继续保留。XRP 每币 1%、组合 2%、单笔 100 USD、日亏损 20 USD及分币价格边界与其他硬限制相同。

币种替换、XRP 配置边界及锁文件更新后，完整测试为 **308 passed**，其余静态检查继续通过。

随后在独立实时验证中，DOGE/USD 也在完整 60 秒等待结束后仍为约 140 秒旧；BTC/USD 与 XRP/USD 同次读取均约 3 秒。由于用户约束是同时交易币种**不超过**三个，最终运行时组合收敛为 BTC/USD、XRP/USD 两个币种，按 BTC→XRP 轮换，理想情况下每个币种约 20 分钟分析一次。SOL/DOGE 的历史运行和审计记录保留，但不再进入新订单决策。

真实 XRP/USD TradingAgents 完整分析成功：运行 `8bbbacb12584473094ebce9e023f5dd9`，动态 ticker 为 `XRP-USD`；模型返回 Underweight，空仓映射目标为 0。模型结束后的订单簿重新读取通过 60 秒时效、价差和全部独立风控，状态 `analyzed`；该命令只分析、不创建预览或提交订单。

## 10 分钟调度适配的分钟级策略（2026-09-19）

用户指出 10 分钟自动调度与原 1–3 天决策视角不匹配，并授权更短周期的小额模拟交易。迁移前同时暂停 CLI kill switch 和 Codex App heartbeat；账户核对确认权益/现金均为 100,000 USD，无持仓、挂单或本项目待核对订单。

新增 `intraday_ai` / `intraday_10m`：每轮读取 Alpaca crypto `us` 历史接口的最近 61 根 `1Min` bars，丢弃未闭合分钟并对最近 60 根计算 3/10/30 分钟收益、EMA5/EMA20、20 分钟实现波动/区间和近期成交量变化。至少需要 45 根闭合 bars，末端完成时间最多 180 秒，拒绝未来时间、重复、超过 3 分钟断档、混合来源和异常 OHLCV。原 latest orderbook 继续作为执行价格和 60 秒新鲜度依据；任一数据请求失败都不会回退到离线数据。

轻量模型在隔离子进程中只接收数值特征、订单簿和账户摘要，硬超时 60 秒，输出必须是只含 `rating`、`summary`、`evidence` 的严格 JSON。空仓增持由代码二次约束：四项动量投票低于 2 时 Overweight/Buy 都降为 REVIEW，Buy 至少需要 3；评级正文不能覆盖目标比例。决策有效期缩短到 240 秒。固定目标仍为 Buy 0.05%、Overweight 0.03%、Underweight 0.01%、Sell 0；每币 1%、组合 2%、单笔 100 USD、日亏损 20 USD、Paper-only、无杠杆/做空等限制未放宽。完整 TradingAgents v0.5.0 适配器保留作手动深度分析，上游仓库仍为干净的固定提交 `2d17df8da1536c121e4d7395ac5a5dcec9e96d6f`。

验证结果：

- 完整 `pytest -q`：**334 passed**；Ruff 检查/格式、Python 编译、`uv lock --check` 和 `pip check` 通过。
- 新增真实 bars 解析/精度、缺失/过期/未来/断档/异常数据、严格模型 schema、弱动量禁止开仓、超时、挂单前置阻断、模型/券商凭证隔离，以及运行器先持久化 `intraday_contexts` 再决策的测试。
- 离线闭环 `53da3b6622af4715b116ace8fc6a8fcf` 使用明确标注的 synthetic broker，完成预览、模拟成交、核对和收益/费用报告；这不是 Alpaca 成交。
- 真实 Alpaca 只读采样中 BTC/USD、XRP/USD 各返回 61 根 bars，最近闭合分钟完成后约 4 秒，均成功计算 60 根窗口特征。
- 真实新策略 `analyze` 运行 `c0a7b108b6094aeeb36b7cea1d04d168` 约 9 秒完成；BTC 动量分 -3，模型返回 Hold，独立风控允许但未创建预览或订单。
- 新配置摘要 `a2e8627e077fa51edb8c951e4a9cda51c33175d272a71316545d7056eb41013b` 显式恢复自动 Paper 后，首轮 `4d2f3949c9984586a75b17d9c9c6be7c` 的 BTC 动量分 -2、评级 Hold、结果 `no_order`；没有发送订单 POST。自动状态 enabled、失败计数 0，下一轮轮换 XRP/USD。Codex App heartbeat 已按同一策略和 600 秒频率恢复 ACTIVE。

这轮验证证明真实分钟数据、兼容模型调用和自动闭环的无单分支可运行；尚未出现满足门槛的入场信号，因此没有把测试或 `no_order` 描述成真实平台下单/成交验证，也没有盈利结论。

## BTC 换手问题修复（2026-09-19）

实盘模拟审计显示旧版 `intraday-ai-1min-v1` 用动量分 2 和 4 分两次建立约 30 USD、20 USD 的 BTC 仓位，随后允许 -1 分的 Underweight 把约 40 USD 降到 10 USD，再由 -4 分 Sell 清掉余仓。旧规则的入场门槛与减仓门槛不对称，而且没有要求短周期价格变化覆盖点差和双边费用/滑点预留；这会产生与小额超短线成本不相称的换手。

修复版 `intraday-ai-1min-v2-cost-aware` 保留原仓位和硬风控，但增加三项确定性约束：Underweight 必须为 -2 分或更低，Sell 必须为 -3 分或更低；新增入场成本门槛，要求 3/10/30 分钟正向变化最大值至少覆盖 `当前点差 + 2 × (fee_buffer_bps + slippage_bps)`；REVIEW 和成本门槛阻断记为正常 `no_order`，不再计入连续自动故障。`intraday_require_cost_cover: true` 是必填安全配置，配置摘要因此更新，旧策略观察不会进入新策略评估窗口。

验证结果：

- 修复期间自动 CLI kill switch 与 Codex App heartbeat 均暂停；核对确认账户无持仓、无未成交订单。
- 完整 `pytest -q`：**342 passed**；Ruff 检查和格式、Python 编译、`pip check` 均通过。本轮没有依赖变更；当前 shell 没有可调用的全局 `uv`，所以没有重复执行 `uv lock --check`。
- 真实 Paper 只读运行 `83a0817a60e545aab61a535f3482d2d7` 使用修复版策略：BTC 价格变化代理为 10.8447 bps，配置往返成本门槛为 101.6174 bps，结果为 REVIEW / `no_order`，没有创建预览或提交订单。
- 当前 Paper 权益/现金为 99,999.43 USD，账户空仓；费用活动仍可能延迟，报告将已实现净盈亏保持为未知，没有把权益变化当作精确策略收益。
- 测试和真实只读验证通过后，沿用用户已有 Paper 自动交易授权恢复 600 秒调度；新配置摘要为 `2d63319bf635b4a6142900b0c5fbc94c6047ea2d16a4fd3e9832cdc148ed71a4`，评估游标从新策略版本开始，不复用旧版观察。
- 自动观察记录随后补充 REVIEW 分支：成本覆盖不足或证据不足造成的正常无交易决策也进入同版本时间序列，供后续独立验证；REVIEW 始终不可执行，回放时不会被当作成交信号。
