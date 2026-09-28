# Crypto Agent

一个加密货币现货模拟交易实验项目，支持离线演示和 Alpaca Paper 交易。策略分析、独立风控、订单预览、模拟执行与成交核对会记录到 SQLite；另提供 Web 监控面板。可选接入 TradingAgents 进行 AI 分析。

项目仅用于研究和测试，不提供实盘交易，也不保证收益。

## 快速体验

需要 Python 3.12–3.14、[uv](https://docs.astral.sh/uv/) 和 Node.js 22.12+。

```bash
uv sync --frozen --extra dashboard
uv run --frozen --extra dashboard crypto-agent demo

npm --prefix frontend ci
npm --prefix frontend run build
uv run --frozen --extra dashboard python -m crypto_agent.api --demo --port 8765
```

打开 <http://127.0.0.1:8765> 查看演示面板。离线演示使用合成数据，无需 API 密钥。

## Alpaca Paper

Paper 模式需要自己的 Alpaca Paper 密钥和完整风控配置。仓库中的默认配置禁止提交订单；启用前请先阅读 [`config/`](config/) 中的示例，并通过 CLI 检查连接和订单预览。密钥只放在本地环境或 `.env` 中，不要提交到仓库。

## 开发

```bash
uv sync --frozen --extra dashboard --group dev
uv run --frozen --extra dashboard pytest -q
uv run --frozen --extra dashboard ruff check src tests research
```

前端代码在 [`frontend/`](frontend/)，Python 服务在 [`src/crypto_agent/`](src/crypto_agent/)。
