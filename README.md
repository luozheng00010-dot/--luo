# 自动剪辑软件

面向自有短素材库的文案驱动剪辑软件：素材库 + AI 画面理解与文案匹配 + 使用记录 + FFmpeg 自动合成。

完整开发计划见 [docs/自动剪辑软件完整开发计划.md](docs/自动剪辑软件完整开发计划.md)。

## 环境要求

- macOS（首发平台）+ Apple Silicon
- Python 3.12、[uv](https://docs.astral.sh/uv/)
- Node.js ≥ 20、pnpm ≥ 8
- FFmpeg（含 libass）：`brew install ffmpeg-full`

## 首次启动（新开发环境）

```bash
# 1. 后端：安装依赖并启动本地服务（默认 127.0.0.1:8765）
cd backend
uv sync
uv run python run_server.py

# 2. 前端：另开终端
cd frontend
pnpm install
pnpm dev           # http://localhost:5273

# 3. 独立 Worker（处理导入/分析/渲染等后台任务）
cd backend
uv run python -m app.workers.worker
```

无需修改任何源代码。API 默认只监听 `127.0.0.1`；除 `/healthz` 外的接口都需要
`X-Session-Token` 头，令牌在服务首次启动时生成于 `data/session_token`（权限 0600）。

## 测试

```bash
cd backend && uv run pytest        # 后端 31 个测试（迁移/队列/适配器/安全）
cd frontend && pnpm test           # 前端 Vitest
scripts/p0_media_check.sh          # P0-06 媒体工具链验证（生成 30 秒竖屏样片）
```

## 目录

```
backend/    FastAPI + SQLAlchemy + SQLite 任务队列（计划 3.2 技术栈）
frontend/   React + TypeScript + Vite + Tailwind + Radix
docs/       开发计划与技术决策记录
scripts/    环境检查与启动脚本
data/       运行数据（不入库 Git）
```

## 当前进度

| 阶段 | 状态 |
|---|---|
| P0 关键技术验证 | 部分（本机可验证项已通过；样本集/供应商对比待用户提供素材与 API 配置） |
| P1 工程骨架、数据库与任务框架 | 完成（31 项后端测试 + 前端测试全过） |
| P2 素材库、导入与画面分析 | 进行中 |
