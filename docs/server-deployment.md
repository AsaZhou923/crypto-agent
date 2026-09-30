# Optiplex 服务器部署

2026-09-28 从 macOS 迁移，唯一活动 Paper 会话位于
`optiplex@10.34.158.137:/home/optiplex/services/crypto-agent/runtime/paper-session`。
项目根目录是 `/home/optiplex/services/crypto-agent`，固定上游仓库位于相邻的 `TradingAgents`，提交仍为 `2d17df8da1536c121e4d7395ac5a5dcec9e96d6f`。

## 入口与进程

| 服务 | 地址或 unit |
| --- | --- |
| Paper 面板 | https://optiplex.tail8fb77a.ts.net:8447 |
| Homepage | http://100.66.30.6:3002 |
| Uptime Kuma | http://100.66.30.6:3001 |
| n8n | http://100.66.30.6:5678 |
| 面板服务 | `crypto-agent-dashboard.service` |
| 交易定时器 | `crypto-agent-paper.timer` |
| 单轮交易服务 | `crypto-agent-paper.service` |
| 交易汇总定时器 | `crypto-agent-paper-digest.timer` |
| Codex 两小时策略检查 | `crypto-agent-strategy-review.timer/service` |
| GitHub 自动同步 | `crypto-agent-github-sync.timer/service` |

三个 unit 均位于 `~/.config/systemd/user/`，以 optiplex 身份运行。
已启用 `loginctl enable-linger optiplex`，无需保持 SSH 会话或 Codex 开启。
FastAPI 仍仅监听 `127.0.0.1:8766`，由 Tailscale Serve 提供 HTTPS；未配置公网 Funnel。
`--external-origin` 只放行指定的 HTTPS `.ts.net` Host/Origin，启停仍需同源 Origin 和控制请求头。
访问权继承现有 tailnet ACL；获准访问此入口的成员可以查看账本和启停 Paper 调度。

启动 timer 后首次等待 300 秒；每轮结束后再等待 300 秒。
长任务不重叠、不补跑，既有 CLI 启动最小间隔、文件锁、未知提交阻断和 inflight 保护保留。
面板异常退出由 systemd 重启；交易服务不自动重试故障轮次。

## 常用运维

登录服务器后执行：

```sh
cd /home/optiplex/services/crypto-agent
systemctl --user status crypto-agent-dashboard.service crypto-agent-paper.timer
systemctl --user list-timers crypto-agent-paper.timer
journalctl --user -u crypto-agent-dashboard.service -u crypto-agent-paper.service -n 80
.venv/bin/python scripts/paper_schedule.py --check
```

面板开关是日常启停入口。紧急停止：

```sh
.venv/bin/crypto-agent --config runtime/paper-session --mode paper auto-pause
systemctl --user disable --now crypto-agent-paper.timer
systemctl --user stop crypto-agent-paper.service
```

停止不会撤销平台已受理订单；中断活动轮次时保留 inflight，需要核对再恢复。
重新安装交易 unit：先暂停，确认无活动轮次和未知提交，执行
`.venv/bin/python scripts/paper_schedule.py --prepare`，复制生成的两个 unit 到
`~/.config/systemd/user/` 并 `systemctl --user daemon-reload`。
面板 unit 模板保存在 `deploy/crypto-agent-dashboard.service`。

## 监控和通知

- `/api/health`：面板进程健康；HTTP 200 不代表模型、broker 或交易循环成功。
- `/api/health/scheduler`：固定 Paper 会话已启用、timer 活跃、策略匹配、无未知提交且调度心跳未超过 1,260 秒时返回 200，否则 503。
- 调度心跳保存在 `runtime/local-scheduler/heartbeat.json`，只由正常的已批准调度检查更新；`--check` 不刷新心跳。正常 Retry-After 等待不会误报成失联。
- Kuma 分别监控面板和调度健康，复用已有 `n8n Telegram Alerts` 通知渠道。手动暂停也会让调度监控变为异常；计划维护可同时在 Kuma 暂停对应监控。
- 成交增量、拒单和参数调整记录在调度审计日志中，由 `crypto-agent-paper-digest.timer` 在东京时间每天 09:00、21:00 汇总一次，经专用 n8n 工作流 `cryptoAgentTelegram1` 发往现有 Telegram 渠道。没有成交时仍发送一条汇总。故障停机继续即时告警。
- 汇总脚本在调用 webhook 前记录已发送时段，防止重启或通知失败导致同一时段重复推送；服务失败可查看 `crypto-agent-paper-digest.service` 状态及审计日志。
- 私有配置为 `runtime/local-scheduler/notification.env`（权限 600），包含 `CRYPTO_AGENT_NOTIFY_WEBHOOK` 和 `CRYPTO_AGENT_NOTIFY_TOKEN`；不要提交、输出或覆盖。
- 通知失败只写 `notification_unavailable` 审计事件，不重试交易；通知不是保证送达的队列。

## 数据与迁移边界

完整 runtime 历史、原 `.env`、配置及前端构建已复制；服务器使用 `uv.lock` 安装 Python 3.13 的 dashboard/ai 依赖。
迁移前 SQLite backup 完整性为 `ok`，保留 473 条订单、2,340 次运行、1,321 个自动周期和 1,189 条策略观察。
服务器连接检查已验证配置、Paper 账户及 BTC/XRP 行情；无未知/提交中订单。

策略及风控字段未修改。配置摘要包含解析后的上游绝对路径，路径迁移使摘要从
`5eb1d60bd6cca8d2f6d00a4ac2392f2f6c3b98a4e705cbcfe850ee8e66f608ab`
变为 `19fceb2b74683132d4c393266e512256c2ebb82af19fd89fc0bf8b8983e163d2`。
已核对除此路径外的配置完全一致，并用既有 `auto-enable` / `auto-pause` 登记服务器批准摘要。
历史记录不改写；新摘要的评估样本重新积累，迁移时参数倍数为 1 并保持 1。

本机已经 `auto-pause`、bootout 并 disable `com.ze.crypto-agent.paper`，原面板进程停止。
Codex `btc-paper` 和每两小时策略优化自动化 `paper` 均为 PAUSED。
服务器使用已登录的 Codex CLI 运行独立的两小时只读检查；原 `paper` 自动化保持暂停，避免重复执行。交易服务内置的受限参数评估仍运行。
本机保留迁移备份，不能再作为当前账本；不要向服务器覆盖旧 runtime 数据。

## Codex 两小时策略检查

2026-09-30 的成本覆盖、入场冷却、费用元数据、分页和评估时效改进见 [v5.2 Paper 优化](paper-v5.2-economic-optimization.md)。新策略效果需要后续独立数据验证。

`crypto-agent-strategy-review.timer` 在 JST 双数小时的 20 分触发 `crypto-agent-strategy-review.service`，不会补跑错过的时间。服务以 optiplex 用户启动本机已经登录的 Codex CLI，用官方 `codex exec --sandbox read-only` 模式检查服务器的 Paper 账本、策略、风控和调度证据。[Codex 非交互模式](https://learn.chatgpt.com/docs/non-interactive-mode)支持在脚本和计划任务中运行，并默认只读。

提示词和 JSON 报告格式分别保存在 `deploy/strategy-review.prompt.md` 与 `deploy/strategy-review.schema.json`。每次完整报告存入 `runtime/hourly-strategy-review/<UTC时间>/report.json`，最近报告写入 `server-review-latest.json`；`attention/blocked` 结果仅保存在服务器，不发送 Telegram 通知。Codex 调用有 25 分钟超时，systemd 总超时 30 分钟；失败写入该次目录的 `failure.json` 并返回失败状态，同样不发送 Telegram 通知。定时器不执行 `auto-tick` 或修改生产文件。

2026-09-28 20:15–20:20 JST 已完成首份服务器 Codex 检查，服务退出码 0，JSON 报告为 `attention`：XRP/USD 分钟线持续缺口、信号被安全过滤；没有发现未知提交或当前风控停机。下一次计划触发为 22:20 JST。

这一迁移恢复了定期策略和执行检查。原本地任务中“研究后自主改码并上线”的能力没有放到无人值守的交易服务器；报告给出具体问题和证据，再在单独的代码任务中验证、上线。这样正在运行的交易版本不会在后台检查时被更改。查看状态：

```sh
systemctl --user status crypto-agent-strategy-review.timer crypto-agent-strategy-review.service
systemctl --user list-timers crypto-agent-strategy-review.timer
cat runtime/hourly-strategy-review/server-review-latest.json
```

## GitHub 自动同步

服务器 `origin` 使用已认证的 GitHub SSH 远端 `git@github.com:AsaZhou923/crypto-agent.git`。`crypto-agent-github-sync.timer` 每 10 分钟检查代码和策略变更；只在 `main` 上创建普通提交，向 `main` 正常推送，不强推。如果远端已有本机未包含的新提交，停止并通过 n8n 告警，等待人工合并。

交易实际读取的 `runtime/paper-session/` 始终被 Git 忽略。同步脚本先用现有配置加载器校验三份 Paper YAML，再复制到 `config/deployment/paper-session/` 作为可追溯的配置快照。只暂存仓库的代码、测试、文档、部署模板和这三份快照；不会暂存 `.env`、交易 SQLite、通知令牌或其他 runtime 数据。推送前检查暂存内容是否含当前密钥或常见密钥格式，拒绝二进制及超大文件。同步失败不影响交易；相同故障只通知一次。成功提交后发送简短提交 ID 通知。

```sh
systemctl --user status crypto-agent-github-sync.timer crypto-agent-github-sync.service
cat runtime/github-sync/latest.json
git log -1 --oneline
```

远端自动推送会使本机 Git 分支落后；本机后续开发先 `git fetch` 并核对差异，再基于 GitHub 的最新提交继续。GitHub 上的配置快照用于审计，不能直接替换服务器当前 runtime 会话。

迁移证据及原始备份保存在两端的 `runtime/server-migration-20260928/`。
回迁前必须先暂停服务器并结束活动轮次，核对订单，再用 SQLite backup 将服务器最新账本带回本机；核对配置摘要和 policy 后才能恢复本机调度，不能同时启用两端。

## 验证

- 本机和服务器 Python 全部 814 项测试通过。
- 前端 8 项测试、TypeScript、生产构建、Ruff lint/format、diff 检查通过。
- Tailscale HTTPS 的账户、订单、决策和权益 API 成功，面板启用→停止→恢复均通过严格同源请求验证。
- n8n 已实发迁移测试到原 Telegram；实际 systemd 环境调用 `Scheduler.notify()` 的执行 #40 也确认送达。无效 Bearer 请求不会执行 Telegram 节点。
- 服务器于 2026-09-28 20:09:22–20:09:56 JST 自然完成首个 BTC/XRP 周期，两币均 `no_order`，无未知提交、无残留 inflight、交易保持启用，下一轮排定为 20:14:56 JST。
- Kuma #17（面板）与 #18（调度）均为 UP，Homepage 实时服务列表已返回正确入口。

参考：[Tailscale Serve](https://tailscale.com/docs/features/tailscale-serve)、[systemd timer](https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html)、[Codex 自动化](https://learn.chatgpt.com/docs/automations?surface=app)。
