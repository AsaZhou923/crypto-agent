# 2026-10-04 Paper 行情修复

Alpaca Crypto US 的原始分钟线存在真实缺口，BTC 与 XRP 无法满足连续 60 秒的特征契约。直接读取上游的完整时间窗口也能复现，不能通过补齐空分钟或放宽时效来修复。US orderbook 的 XRP 时间戳也会长时间不更新；latest quotes 同样陈旧，不能作为新鲜执行报价的替代。

当前 Paper 策略显式配置 `intraday_bar_source: coinbase_exchange`，使用 Coinbase Exchange 公开的真实一分钟蜡烛生成分析特征。Coinbase 客户端独立建立，不携带 Alpaca 或模型凭据；请求使用明确的 start/end、最多 242 分钟窗口，筛选真实闭合线并保留来源。无数据、坏数据、重复分钟或缺口不会回退或补造。默认未配置的会话仍使用原 Alpaca 分钟线。

`intraday_max_quote_bar_deviation_bps: 100` 限制分析末根收盘价与 Alpaca 执行中间价的偏差，最大 1%。在模型分析之前，以及买入订单预览和提交前的成本核验中检查来源及偏差。配置字段进入批准摘要，新旧来源的优化样本不混用。模型特征和决策证据注明分钟线来源与执行报价来源。

执行报价、资产规则、账户、成交核对和所有订单仍来自 Alpaca Paper。60 秒报价时效、180 秒分钟线时效、连续分钟、两次信号确认、30 分钟冷却、成本覆盖、仓位及每日亏损限制全部保留。上游执行报价陈旧时，该币继续拒绝下单。

只有没有创建或提交订单、且唯一拒绝原因是 `Market data is stale` 的币种，允许在同一轮安全跳过后检查下一币。账户风险、未知提交、未完成订单、手动暂停和其他异常仍结束整轮。周期汇总保留 blocked 状态，避免掩盖跳过。

部署先停止 Paper timer 并等待活动轮次自然结束，暂停自动交易，保存原源码、三份 YAML、policy 和 SQLite backup。修改策略配置后，通过既有 `auto-enable`、`auto-pause`、`paper_schedule.py --prepare` 绑定新摘要，再恢复 timer 与面板。回滚恢复源码和 YAML 后重新绑定 policy；禁止把备份账本覆盖到已有后续活动的账本。

来源：[Alpaca Historical Crypto Data](https://docs.alpaca.markets/us/docs/historical-crypto-data-1)、[Coinbase Get product candles](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-candles)。Coinbase 也可能存在无成交分钟，运行时仍必须通过完整性检查。
