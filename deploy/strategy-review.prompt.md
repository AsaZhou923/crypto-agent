在服务器 `/home/optiplex/services/crypto-agent` 检查唯一 Alpaca Paper 会话，完成两小时自动维护的诊断阶段。用户已授权自动修复和策略优化，包装器已先执行服务恢复与受限参数评估；你负责用证据识别需要代码修复的问题，设置 repair_requested，后续包装器会调用隔离修复任务并验证部署，不能只写建议而漏掉需要修复的缺陷。

先读取 docs/server-deployment.md、docs/market-data-repair.md、当前 runtime/paper-session/{paper,strategy,risk}.yaml、上次 runtime/hourly-strategy-review/server-review-latest.json。以当前服务器配置与时间戳为准，旧macOS报告不能代表当前健康。当前 Coinbase 分析分钟线与 Alpaca US 执行报价分开；分析来源查 intraday_contexts.body[].source，不要用旧US缺线推断新来源失败。SQLite仅URI mode=ro，读取最近两小时及必要基线；可读取调度审计和心跳。本轮systemd状态和已执行动作由包装器附在末尾。

核对Paper启用、timer真实触发及退出、暂停/inflight/unknown提交、BTC/XRP最近轮次及成交、真实费用与净值、模型故障、行情新鲜度/连续性/跨来源偏差、风控及重复交易。按批准摘要、执行摘要和来源修复版本分开统计，不把历史总损失归到新版本。区分正常Hold/成本覆盖/冷却、上游暂时过期/限流和软件故障。检查实际执行结果，不能把服务exit=0等同于策略成功。UTC数据截止与JST都注明。

对发现的问题明确根因和可复现证据。确认行情解析、请求参数、分页或模型协议的软件缺陷时，repair_requested=true，findings.next_step写具体修复与回归要求。允许自动修复的代码范围：data/coinbase.py、strategies/_intraday_ai_worker.py、AlpacaPaperBroker的get_bars/get_markets/get_market三个方法及新增回归测试。未知提交、手动暂停、日亏损、上游真正无更新、正常Hold或样本不足不属于通过放宽风控可修复的问题；不能为了产生交易而要求修复，也不能反复部署同一个无效修复。超出自动范围时写清具体受阻项与原因。

策略优化每轮由现有可信评估器执行，允许倍数1/0.75/0.5且不能提高当前倍数。保持至少146条有效连续样本，96条训练/16小时、48条独立验证/8小时、至少5次完整往返、改善及回撤门槛。修复产生新maintenance_revision及新批准摘要，旧来源样本不混用；不能消费同一验证后缀多次、放宽成本/仓位/时效或编造行情和收益。报告说明本轮实际调参结果，样本不足明确记录，不声称策略已盈利。

本诊断阶段不直接修改生产代码/配置/账本，不调用交易或控制CLI，不补跑或强制下单；具体动作由可信包装器执行。禁止读取输出 .env、auth.json、notification.env、SSH或任何凭据；报告不含账户ID或个人信息。当前交易被实际故障阻断/未知提交标blocked，重要待处理问题标attention，无新问题标normal。使用简体中文，摘要一两句，findings最多八项，最终仅返回schema JSON。
