# 本机 Paper 调度

使用 `scripts/paper_schedule.py` 和用户级 LaunchAgent `com.ze.crypto-agent.paper` 替代 Codex heartbeat。脚本没有恢复交易功能，安装调度器不会解除交易暂停。旧 `btc-paper` heartbeat 保持 PAUSED。

launchd 每 300 秒尝试运行一次脚本；机器需登录、唤醒并联网。关闭 Codex 不影响运行，退出登录、关机或休眠会中断调度。不会补跑错过的轮次，系统调度也不是精确到秒的承诺。

## 执行与边界

固定工作目录为项目根目录，固定使用 `.venv/bin/crypto-agent --config runtime/paper-session --mode paper`，不接受其他币种、数据库或环境参数。沿用项目 `.env` 加载凭证，plist 不包含密钥。模型分析仍由现有 CLI 调用模型服务。

每次先 `auto-status`：已暂停则退出；启用时核对已安装策略摘要、BTC/USD→XRP/USD、300 秒间隔和双币串行配置，再运行一次 `auto-tick --execute-paper`。原有策略、额度、仓位、日亏损限制、行情期限、预览、成交核对和评估器均不改变。

外层文件锁防止脚本重叠，CLI 整轮锁和持久化启动时间继续生效。没有自动重启或 POST 重试。CLI 返回 blocked（即使退出码为 2）不导致脚本额外停机；CLI 自身暂停始终保留。失败、未知提交、无法解析输出等情况写入同一个交易暂停开关，需排查和显式恢复。提交前保存 inflight 标记；进程中断后下一轮会停机，不能盲目重放。900 秒 watchdog 超时先暂停，再终止 CLI 进程组，必须核对既有订单。

收到 SIGTERM/SIGINT 时立即写暂停开关、终止 CLI 进程组并保留 inflight 标记。SIGKILL/断电无法捕获，恢复后通过 inflight 标记阻止再次执行；已经被平台接受的订单不会因本机停止而自动撤回。

账户和行情 GET 查询遇到 HTTP 429 时，本轮返回 `rate_limited`，结束当前双币轮次，不累计故障或额外暂停。等待时间取 300 秒和服务端有效 `Retry-After` 的较大值，并保存到数据库；进程重启或手动暂停/恢复不会提前解除等待。到期后由后续定时轮次重新核对账户并获取新行情，不重放旧预览。POST 提交遇到 429 仍按未知提交处理，必须核对；任何既有 unknown/submitting 订单也不会因为 GET 限流而绕过停机。

常规核对只逐笔查询尚未结束的订单，仍刷新全组合账户、持仓、开放订单、行情以及成交/费用活动。已确认结束的历史订单保留在账本，避免订单越多、每轮请求越多。必要时可手动执行 `reconcile --refresh-terminal` 全量复核历史订单；定时器不运行此选项。

GET 查询的连接中断、超时及 HTTP 408/500/502/503/504 最多尝试 3 次，重试前分别等待 1 秒、2 秒；服务端返回 `Retry-After` 时直接交给后续轮次等待，不在当前进程内长时间重试。耗尽尝试后返回 `read_unavailable`，与只读限流一样结束本轮并持久化等待期限，保持启用、等待后续轮次重新核对。恢复的行情仍须满足原有有效期及提交前检查。POST 和 DELETE 均不自动重试；未知提交、认证失败、无法解析的数据、风控停机及进程异常继续保留停机保护。

确认新增成交量、拒单、正式参数调整和故障停机时通过 macOS 通知中心尝试通知，并记录事件日志。系统通知权限/勿扰模式可能影响显示；事件日志才是可核对记录。通知失败不重试交易。暂停、Hold、pending、普通风控拦截和证据不足不通知。

## 安装与核验

所有命令从 `/Users/ze/Projects/crypto-agent` 执行。先暂停，准备已获批准配置的调度清单：

```sh
.venv/bin/crypto-agent --config runtime/paper-session --mode paper auto-pause
.venv/bin/python scripts/paper_schedule.py --prepare
cp runtime/local-scheduler/com.ze.crypto-agent.paper.plist ~/Library/LaunchAgents/
plutil -lint ~/Library/LaunchAgents/com.ze.crypto-agent.paper.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ze.crypto-agent.paper.plist
launchctl print gui/$(id -u)/com.ze.crypto-agent.paper
.venv/bin/python scripts/paper_schedule.py --check
```

已加载的任务不能再次 bootstrap；更新前先暂停交易、bootout，再安装。`--check` 仅核对状态，从不执行交易。`--prepare` 要求 CLI 已暂停且配置摘要已经获得批准，不能用来绕过策略授权。

## 暂停、恢复与移除

阻止新增交易：

```sh
.venv/bin/crypto-agent --config runtime/paper-session --mode paper auto-pause
```

停止本机调度（先执行上述暂停）：

```sh
launchctl bootout gui/$(id -u)/com.ze.crypto-agent.paper
```

彻底移除时再删除 `~/Library/LaunchAgents/com.ze.crypto-agent.paper.plist`，否则下次登录会重新加载状态检查。保留账本和审计文件。

只有用户明确要求恢复后，核对故障和既有订单、确认未决/未知提交已解决，再调用既有 `auto-enable --execute-paper`。脚本从不调用此命令。若存在 `runtime/local-scheduler/inflight.json`，必须先核对该次执行对应的账本和平台订单，归档该标记后才能恢复；不得通过删除标记重试提交。恢复后由下一个定时触发处理新行情，无需 kickstart 或补跑。

## 本地记录

- `runtime/paper-session/trading.sqlite`：原有订单、成交、观察和评估审计。
- `runtime/paper-session/trading.auto-paused`：交易暂停标记。
- `runtime/local-scheduler/policy.json`：安装时批准的配置摘要。
- `runtime/local-scheduler/audit.jsonl`：状态、完整轮次结果、重要事件；5 MB 轮换，保留 5 份备份。
- `runtime/local-scheduler/seen-orders.json`：通知去重记录。
- `runtime/local-scheduler/inflight.json`：未正常收尾的调用，需核对。
- `runtime/local-scheduler/launchd.stderr.log`：脚本启动阶段错误。

参考：[Apple launchd 周期任务说明](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html)。

## 面板启停

在使用 `runtime/paper-session` 启动的本机 macOS 面板中，调度卡片显示实际交易暂停开关、launchd 是否加载及当前脚本是否正在执行，每 5 秒刷新。演示模式、其他会话及非 macOS 实例不支持控制。

“启动”恢复已批准的 BTC/USD、XRP/USD 双币串行 Paper 调度，间隔仍为 300 秒。面板先核对安装策略摘要、账本批准摘要、plist 和已加载任务的程序及参数，检查未核对执行标记及未知提交，再在外层调度锁内加载 launchd 并调用原有 `auto-enable --execute-paper`。不会立即运行一轮、补跑或重试；等待下一个定时触发。“启动”不提供修改策略或切换实盘的入口。

“停止”先持久化原有 `trading.auto-paused` 开关，再卸载该 LaunchAgent。活动脚本沿用 SIGTERM 清理机制；平台已经接受的订单不会被撤回。停止不等待交易轮次持有的锁，完成后再读取 launchd 核验结果。命令失败、超时或结果不确定会明确提示，不能当作成功；启动失败时恢复暂停，不自动重试。

状态接口为 `GET /api/scheduler`，只读 SQLite 和 launchd，不构造会迁移账本的 CLI。唯一新增写接口 `POST /api/scheduler` 仅接受 `{"action":"start"}` 或 `{"action":"stop"}`；要求严格同源 `Origin` 和 `X-Crypto-Agent-Control: 1`，拒绝其他字段、任意命令或路径。其他 API 继续只读。控制结果不包含配置、CLI 原始输出或凭证；简要动作结果记录在 `runtime/local-scheduler/control-audit.jsonl`。该记录写入失败不会阻止紧急暂停。

若面板显示“需核对”，请检查本地故障、配置和既有订单；面板不会清除 inflight 标记或跳过原有风控。恢复控制应通过面板明确点击或原有 CLI 显式命令执行，自动刷新状态不会恢复交易。

当前策略更新说明见 [v5 低换手 Paper 实验](paper-v5-low-turnover.md)。新版本只增加入场过滤；部署时重新登记配置摘要并更新调度 policy，原硬风控与正式评估门槛保留。
