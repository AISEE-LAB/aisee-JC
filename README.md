# 中转站分组倍率监测（多站点 Web 版）

> 监测任意数量中转站的**分组倍率 / 模型定价**变化，发生变化时主动推送，提供本地 Web 仪表盘。支持 **New API** 和 **QAPI (sub2api)** 两套系统。

## 核心特性

- 🌐 **多站点**：一个实例同时监测多个中转站，每站独立配置凭证/间隔/通知
- 🎯 **双系统支持**：New API（access_token）+ QAPI（账号密码登录拿 JWT）
- 📊 **可视化仪表盘**：全局概览 + 单站点下钻，倍率趋势图
- 🔔 **8 种通知渠道**：Server 酱 / Telegram / 企业微信 / 钉钉 / 邮件 / Bark / Discord / 飞书
- 🔒 **访问鉴权**：可选 Basic Auth + API Token，公网部署安全
- 🐳 **Docker 一键部署**：内置 Dockerfile + docker-compose，数据持久化
- 🗄️ **历史隔离**：SQLite 按站点隔离数据，独立趋势/变更记录
- ⚡ **自动调度**：后端 APScheduler 定时抓取，按各站点配置的间隔

## 界面

| 页面 | 功能 |
|---|---|
| **仪表盘** | 全局视角：所有站点概览卡片，点击下钻；单站点视角：4 卡片 + 倒计时 + 健康度 |
| **倍率表格** | 模型倍率，搜索/排序，趋势折线图，按站点过滤 |
| **变更历史** | 时间线展示变化，按类型 + 站点过滤 |
| **配置** | 多站点 CRUD：左侧站点列表，右侧编辑表单 |
| **日志** | 运行日志 |

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 启动

双击 `run_web.bat`，或命令行：

```bash
python app.py
```

浏览器访问 `http://127.0.0.1:5000`。

### 3. 添加站点

进入 **配置** 页 → 点「**+ 新增**」→ 输入站点地址，选择系统类型 → 填写凭证 → 保存。

**凭证获取**：
- **New API**：后台 → 设置 → 个人资料 → 生成系统访问令牌 → 填 `access_token`
- **QAPI (sub2api)**：填登录 `email` + `password`（脚本自动登录拿 JWT），可选填 `api_key`（sk-xxx，用于看模型清单）

### 4. 启用通知（可选）

在站点配置的「通知渠道」部分勾选要用的渠道，填 Key，保存。

### 5. 抓取

仪表盘点「立即抓取」→ 首次建立基准 → 之后按间隔自动抓取，**只有变化才告警**。

## Docker 部署（推荐公网用）

### 方式一：拉取 GitHub 官方镜像（最简单，无需构建）

每次推送代码，GitHub Actions 会自动构建多架构镜像并发布到 GHCR：

```bash
docker pull ghcr.io/aisee-lab/aisee-jc:latest
docker run -d --name rate-monitor --restart unless-stopped \
  -p 5000:5000 \
  -v $PWD/data:/app/data \
  -e CONFIG_PATH=/app/data/config.yaml \
  -e DB_PATH=/app/data/data.db \
  ghcr.io/aisee-lab/aisee-jc:latest
```

访问 `http://<服务器IP>:5000`，配置与数据持久化在 `./data/` 目录。

### 方式二：本地构建（自行修改代码后用）

```bash
docker compose up -d            # 构建并后台启动
docker compose logs -f          # 查看日志
docker compose down             # 停止
```

- 访问 `http://<服务器IP>:5000`
- 配置与数据持久化在 `./data/` 目录（含 `config.yaml` 和 `data.db`），容器重建不丢
- 内置健康检查：`GET /api/health`（免鉴权，供 Docker / uptime 监控探针）
- **公网部署务必启用访问鉴权**（见下）

## 访问鉴权（公网部署强烈建议）

配置页底部「访问鉴权（全局）」区域，或直接编辑 `config.yaml`：

```yaml
auth:
  enabled: true              # 启用
  username: "admin"          # Basic Auth 用户名
  password: "your-password"  # Basic Auth 密码
  token: "your-api-token"    # API Token（脚本调用用）
```

启用后：
- **浏览器访问**：弹出 Basic Auth 登录框，输入 username + password
- **API 调用**：两种方式任选其一
  - `X-Api-Token: your-api-token` 请求头
  - `?api_token=your-api-token` 查询参数
- `/api/health` 始终免鉴权（供健康检查）
- password 与 token 可只配其一，二者任一非空即可作为校验手段

## 配置结构（config.yaml）

```yaml
sites:                          # 多站点列表
  - id: "qjxjs_xyz"             # 自动从 base_url 生成
    name: "QAPI 主站"
    system: "qapi"              # newapi / qapi
    base_url: "https://qjxjs.xyz"
    email: "..."                # QAPI 用
    password: "***"
    api_key: "***"              # 可选
    monitor:
      interval_minutes: 30      # 该站点的抓取间隔
      change_threshold_pct: 0
      watch_groups: []
      notify_on_first_run: false
    notify:                     # 该站点的通知渠道
      telegram: {enabled: true, bot_token: "***", chat_id: "123"}
      # ...
  - id: "another_com"
    name: "另一个站"
    system: "newapi"
    base_url: "https://another.com"
    access_token: "***"
    monitor: {interval_minutes: 15}
    notify: {}

log:
  level: INFO
```

**兼容**：老的 `site:` 单 dict 格式会自动转成单元素 `sites:` 列表，零成本升级。

## 工作原理

```
APScheduler（取所有站点 interval 最小值为全局节奏）
        ↓ 遍历 sites
每个站点独立：
  build_snapshot_auto(site_cfg)
    ├─ New API → /api/pricing + /api/user/self + /api/option
    └─ QAPI    → /api/v1/auth/login 拿 JWT
                → /api/v1/admin/groups(分组倍率)
                → /v1/models(模型清单)
                → /api/v1/usage/dashboard/models(花费)
        ↓
  diff_snapshots（与该站点上次快照比对）
        ↓
  SQLite（按 site_id 隔离写入）
        ↓
  有变化 → 该站点独立的通知渠道推送
```

## 文件结构

```
yy/
├── app.py                 # Flask + APScheduler + 多站点编排 + 鉴权
├── config_helper.py       # 配置规范化 + 兼容转换 + 环境变量路径
├── monitor.py             # 抓取/比对核心（CLI 多站点 + Web 复用）
├── qapi_client.py         # QAPI(sub2api) 客户端
├── notifiers.py           # 8 渠道通知
├── db.py                  # SQLite 数据层（按 site_id 隔离）
├── config.example.yaml    # 多站点配置模板
├── config.yaml            # 运行时配置（gitignore）
├── data.db                # SQLite（gitignore）
├── templates/             # 6 个页面
├── static/                # CSS + JS
├── Dockerfile             # 容器镜像构建
├── docker-compose.yml     # 一键部署（数据持久化）
├── .dockerignore          # 构建排除
├── run_web.bat            # 双击启动（本地）
└── backup_single_site/    # 改造前的单站点版本备份
```

## API

所有读 API 支持 `?site_id=xxx` 过滤（不传或 `all` = 全站点）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查（免鉴权，供 Docker/监控探针） |
| GET | `/api/auth` | 当前鉴权状态（是否启用/已登录） |
| GET | `/api/dashboard[?site_id=]` | 仪表盘（无参=全局概览，带参=单站点） |
| GET | `/api/models?site_id=&q=&sort=` | 模型列表 |
| GET | `/api/models/<name>/history?site_id=` | 单模型趋势 |
| GET | `/api/changes?site_id=&limit=&kind=` | 变更列表 |
| GET | `/api/notifications?site_id=` | 通知记录 |
| GET | `/api/config` | 配置（凭证脱敏） |
| POST | `/api/config` | 保存配置（body: {sites:[...]}） |
| GET | `/api/sites` | 站点列表 |
| POST | `/api/sites` | 新增站点（body: {base_url, system}） |
| DELETE | `/api/sites/<id>` | 删除站点 |
| POST | `/api/fetch_now` | 立即抓取（body: {site_id?}，不传=全抓） |
| POST | `/api/test_notify` | 测试通知（body: {site_id} 必填） |
| POST | `/api/reset` | 重置基准（body: {site_id?}） |
| GET | `/api/logs?limit=&level=` | 日志 |

## CLI 模式

```bash
python monitor.py            # 遍历所有站点抓取一次
python monitor.py --loop     # 常驻循环
python monitor.py --test     # 强制发通知
python monitor.py --show     # 打印所有站点快照
python monitor.py --reset    # 清空所有站点的 state
```

CLI 模式数据存 `state_<site_id>.json`（按站点分文件），与 Web 模式的 SQLite 独立。

## 关于两种系统的监测能力

| 监测项 | New API | QAPI (sub2api) |
|---|---|---|
| 分组倍率 | ✅ `/api/option/`（管理员）| ✅ `/api/v1/admin/groups` 的 `rate_multiplier` |
| 模型单价 | ✅ `/api/pricing` | ❌ 系统不暴露（藏后端静态 JSON） |
| 模型增减 | ✅ | ✅ `/v1/models`（需 api_key） |
| 花费趋势 | ❌ | ✅ `/api/v1/usage/dashboard/models`（间接反映） |

## 常见问题

**Q：怎么加第二个站点？**
A：配置页 → 「+ 新增」→ 填地址和系统类型 → 填凭证 → 保存。

**Q：不同站点能用不同通知渠道吗？**
A：可以，每个站点的 `notify` 完全独立配置。

**Q：单站点视角怎么切？**
A：顶部「全部站点」标签旁会列出所有站点，点击切换；或仪表盘点站点卡片下钻。

**Q：通知收不到？**
A：仪表盘点「测试通知」，日志页看每个渠道的 ✓/✗。

## 安全提示

- `config.yaml` / `data.db` 含敏感信息，已在 `.gitignore`
- **公网部署务必启用访问鉴权**（配置页 → 访问鉴权，或编辑 `config.yaml` 的 `auth` 段）
- 启用鉴权后，API 调用需带 `X-Api-Token` 头或 `?api_token=` 参数
- 凭证建议定期轮换
- Docker 部署时 `./data/` 目录含配置与数据库，注意目录权限

## 兼容性

- Python 3.8+
- Windows / Linux / macOS
- 从单站点版升级：DB 自动迁移加 `site_id` 列，历史数据保留
