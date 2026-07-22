# 当前环境本地源码 Docker 部署指南

本文记录 QuantDinger **当前开发环境**的完整 Docker 部署方式。它适用于低资源 Linux
服务器上的多仓库联合开发，不等同于面向普通用户的 GHCR 预构建镜像安装方案。

当前模式的核心约束是：

- 后端与 Web 前端从本地源码构建；
- PostgreSQL 使用宿主机已经安装的实例；
- Redis 缓存、Redis 任务队列及应用进程运行在容器中；
- `easy_tdx` 以本地 wheel 安装，不发布到 Python 公共仓库；
- Mobile/H5 源码构建和容器启动暂时禁用；
- 所有构建串行执行，避免低内存主机触发 OOM。

如果只需要部署正式版本而不修改源码，应优先使用
[云服务器部署指南](CLOUD_DEPLOYMENT_CN.md)中的 GHCR 预构建镜像方案。

## 1. 当前运行拓扑

| 组件 | 来源或位置 | 当前运行方式 |
| --- | --- | --- |
| 后端与编排 | `/home/quantadinger/QuantDinger` | 本地构建一个后端镜像，供 API、迁移和各 Worker 共用 |
| Web 前端 | `/home/quantadinger/QuantDinger-Vue` | 本地源码构建 Nginx 镜像 |
| Mobile/H5 | `/home/quantadinger/QuantDinger-Mobile` | 保留源码，不构建、不启动 |
| `easy_tdx` | `/home/quantadinger/easy_tdx` | 构建本地 wheel，随后端镜像安装 |
| PostgreSQL | 宿主机 | 应用容器通过 `host.docker.internal` 访问 |
| Redis 缓存 | `redis` 容器 | 可淘汰缓存，不承担持久任务 |
| Redis 任务队列 | `redis-jobs` 容器 | AOF 持久化、`noeviction`，供 Celery 使用 |

当前需要启动的 Compose 服务如下：

```text
redis
redis-jobs
migration
backend
celery-worker
celery-beat
scheduler-worker
trading-worker
frontend
```

不要启动 `mobile`。`postgres` 已被放入 `container-postgres` profile，只有明确改用
容器 PostgreSQL 时才会启用。

## 2. 资源要求与低内存规则

当前服务器属于低资源构建环境。每次较大构建前先检查：

```bash
free -h
swapon --show
df -h /
```

开始构建前建议至少保留：

- 约 1 GiB 可用内存；
- 512 MiB 可用交换空间；
- 足够容纳 Docker 构建层、Python wheel 和前端产物的磁盘空间。

必须遵守：

1. 后端和前端一次只构建一个，禁止并发构建。
2. `COMPOSE_PARALLEL_LIMIT=1`，Node 堆上限保持在 1536 MiB 左右。
3. 不使用整栈 `docker compose up --build`。
4. 不随意使用 `--no-cache`，保留依赖层缓存。
5. 不在本机做多架构/QEMU 构建。
6. 不构建 Mobile/H5。
7. 不执行 `docker system prune`、删除数据卷或清空共享缓存，除非已获得明确授权。

构建期间如可用内存接近 512 MiB 且交换空间接近耗尽，应停止构建并先释放非必要资源；
不要擅自终止其他用户的进程。

## 3. 准备仓库

当前工作仓库是用户 `luhuiyuan` 名下的 fork：

```bash
cd /home/quantadinger

git clone git@github.com:luhuiyuan/QuantDinger.git
git clone git@github.com:luhuiyuan/QuantDinger-Vue.git
git clone git@github.com:luhuiyuan/QuantDinger-Mobile.git
git clone git@github.com:luhuiyuan/easy_tdx.git
```

默认分支分别为：

- `QuantDinger`：`main`
- `QuantDinger-Vue`：`main`
- `QuantDinger-Mobile`：`master`

这几个仓库保持独立，不复制源码到后端仓库，也不转换为 Git submodule。当前没有配置
官方仓库的 `upstream` remote；不要在部署过程中自动新增或同步 upstream。

已有仓库升级前，分别确认工作区没有未处理的修改，再拉取对应 fork：

```bash
git -C /home/quantadinger/QuantDinger status --short
git -C /home/quantadinger/QuantDinger pull --ff-only origin main

git -C /home/quantadinger/QuantDinger-Vue status --short
git -C /home/quantadinger/QuantDinger-Vue pull --ff-only origin main

git -C /home/quantadinger/easy_tdx status --short
git -C /home/quantadinger/easy_tdx pull --ff-only origin main
```

如果存在本地修改或分支分叉，先人工处理，不要用 `reset --hard` 覆盖。

## 4. 安装基础软件

要求：

- Linux x86_64；
- Docker Engine；
- Docker Compose v2（命令是 `docker compose`）；
- 宿主机 PostgreSQL 18；
- Git、Python 和用于构建 `easy_tdx` Web 资源的 Node/npm。

检查：

```bash
docker --version
docker compose version
python --version
node --version
npm --version
psql --version
```

Docker 容器必须能够解析宿主机网关。`docker-compose.build.yml` 已为应用容器加入：

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

## 5. 配置宿主机 PostgreSQL

创建独立数据库和最小必要权限用户。以下仅为示例，请替换用户名和强密码：

```bash
sudo -u postgres psql
```

```sql
CREATE ROLE quantdinger_app LOGIN PASSWORD '请替换为强密码';
CREATE DATABASE quantdinger OWNER quantdinger_app;
\q
```

PostgreSQL 必须允许来自 Docker bridge 网关的连接。根据本机网段调整：

- `postgresql.conf` 中的 `listen_addresses`；
- `pg_hba.conf` 中允许的数据库、用户和 Docker bridge CIDR；
- 宿主机防火墙仅允许本机/Docker 网桥访问 5432，不要暴露公网。

重新加载配置后，在宿主机先验证：

```bash
psql 'postgresql://quantdinger_app:密码@127.0.0.1:5432/quantdinger' \
  -c 'select current_database(), current_user;'
```

容器使用的地址应为 `host.docker.internal`，而不是 `127.0.0.1`。

## 6. 构建并校验本地 easy_tdx wheel

当前开发阶段不要求把 `easy_tdx` 上传到 PyPI 或其他 Python 仓库。后端镜像从
`backend_api_python/vendor/` 安装本地 wheel；wheel 文件本身被 Git 忽略，不得提交。

当前锁定基线为：

| 项目 | 值 |
| --- | --- |
| 版本 | `1.20.4` |
| Git commit | `3b7ce97f2a6942cf9f39e25ee29c4e113bcfc69f` |
| wheel | `easy_tdx-1.20.4-py3-none-any.whl` |
| SHA256 | `b45794c11f871607f4661cf17d926f1c5e1896ede84742b42ae874422b74fdef` |
| 许可证 | MIT |

先检查资源，再串行构建前端资源和 wheel：

```bash
free -h
swapon --show
df -h /

cd /home/quantadinger/easy_tdx/web-ui
NODE_OPTIONS=--max-old-space-size=1536 npm ci --ignore-scripts
NODE_OPTIONS=--max-old-space-size=1536 npm run build

cd /home/quantadinger/easy_tdx
PIP_NO_CACHE_DIR=1 python -m pip wheel . --no-deps \
  --wheel-dir /home/quantadinger/QuantDinger/backend_api_python/vendor
```

校验 wheel：

```bash
cd /home/quantadinger/QuantDinger/backend_api_python/vendor
sha256sum -c easy_tdx-wheel.sha256
```

还应对照 `easy_tdx-wheel.env` 检查源码 commit、版本和文件名：

```bash
git -C /home/quantadinger/easy_tdx rev-parse HEAD
cat easy_tdx-wheel.env
```

如果 `easy_tdx` 源码有更新，应重新构建 wheel，并经过测试和权威样本对账后再更新锁定
commit、版本与 SHA256；不要仅替换 wheel 而保留旧元数据。

## 7. 配置项目根目录 `.env`

项目根目录 `.env` 只管理 Compose 编排和构建参数，不存放后端密钥。创建或编辑：

```dotenv
COMPOSE_FILE=docker-compose.yml:docker-compose.build.yml
COMPOSE_PARALLEL_LIMIT=1

FRONTEND_SRC_PATH=/home/quantadinger/QuantDinger-Vue
FRONTEND_IMAGE=quantdinger-frontend

BUILD_NODE_OPTIONS=--max-old-space-size=1536
BUILD_NPM_JOBS=1
INSTALL_LOCAL_EASY_TDX=1

BACKEND_PORT=127.0.0.1:5000
FRONTEND_HOST=127.0.0.1
FRONTEND_PORT=8888
REDIS_PORT=127.0.0.1:6379

REDIS_PASSWORD=独立生成的缓存密码
CELERY_REDIS_PASSWORD=另一个独立生成的任务队列密码
```

说明：

- `COMPOSE_FILE` 让普通 `docker compose` 命令自动合并本地构建覆盖层；
- `FRONTEND_SRC_PATH` 指向独立的 Vue 仓库；
- `FRONTEND_IMAGE` 使用本地开发镜像名，不把本地构建误当作 GHCR 发布镜像；
- `INSTALL_LOCAL_EASY_TDX=1` 要求 vendor 目录中存在通过校验的 wheel；
- 端口绑定到 `127.0.0.1`，需要公网访问时应通过 Nginx/HTTPS 反向代理；
- 两套 Redis 使用不同密码，应用容器会通过 Compose 环境自动取得对应密码；
- 不要把 `SECRET_KEY`、数据库密码、交易所密钥等写入此文件。

根目录 `.env` 已被 Git 忽略。确认自动合并是否生效：

```bash
docker compose config --profiles
docker compose config --services
```

正常应看到 `container-postgres` profile；默认服务列表中可以出现 `mobile`，但后续启动命令
不得包含它。

## 8. 配置后端运行时 `.env`

从示例创建：

```bash
cd /home/quantadinger/QuantDinger
cp backend_api_python/env.example backend_api_python/.env
chmod 600 backend_api_python/.env
```

分别生成独立随机值，不要复用同一个密钥：

```bash
python -c "import secrets; print(secrets.token_hex(32))"
python -c "import secrets; print(secrets.token_hex(32))"
```

至少修改：

```dotenv
SECRET_KEY=独立生成的随机值
CREDENTIAL_ENCRYPTION_KEY=另一个独立生成的随机值

ADMIN_USER=非默认管理员用户名
ADMIN_PASSWORD=强且唯一的管理员密码

DATABASE_URL=postgresql://quantdinger_app:URL编码后的密码@host.docker.internal:5432/quantdinger
DB_TYPE=postgresql

FRONTEND_URL=http://127.0.0.1:8888
```

注意：

- 数据库密码含 `@`、`:`、`/`、`#` 等字符时必须进行 URL 编码；
- `docker-compose.yml` 中的 Compose 环境变量优先于后端 env 文件；如改用域名或其他端口，
  还要在根目录 `.env` 设置同名 `FRONTEND_URL`，确保实际 CORS 来源正确；
- 云服务器上建议 `ALLOW_LOCAL_DESKTOP_BROKERS=false`；
- 不要提交 `backend_api_python/.env`；
- 不要把真实密钥复制进镜像、README、日志或 issue。

### A 股历史数据的初始安全配置

首次迁移和部署时先保持读路径与自动同步关闭：

```dotenv
SHOW_CN_STOCK=true
CN_HISTORY_ENABLED=false
CN_HISTORY_SYNC_ENABLED=false
CN_HISTORY_DAILY_SYMBOLS=
CN_HISTORY_DISK_SOFT_FREE_BYTES=5368709120
CN_HISTORY_DISK_HARD_FREE_BYTES=2147483648
```

磁盘软阈值会拒绝新的同步任务，硬阈值会阻断当前写入。若当前磁盘容量不足，不要为了绕过
保护而盲目降低阈值；应先评估并释放可安全回收的空间。

## 9. 部署前校验

先检查敏感文件没有进入 Git：

```bash
git status --short
git check-ignore backend_api_python/.env .env \
  backend_api_python/vendor/easy_tdx-1.20.4-py3-none-any.whl
```

然后执行静态与 Compose 校验：

```bash
python scripts/check_version.py
python scripts/check_mojibake.py
docker compose config -q
docker compose -f docker-compose.yml config -q
docker compose -f docker-compose.ghcr.yml config -q
```

确认本地覆盖层最终使用宿主机 PostgreSQL配置：

```bash
docker compose config | grep -nE 'host\.docker\.internal|FRONTEND_SRC_PATH|easy_tdx'
```

不要把 `docker compose config` 的完整输出贴到公开渠道，因为它可能展开
`backend_api_python/.env` 中的敏感值。

## 10. 首次构建

首次构建也必须逐个服务执行：

```bash
cd /home/quantadinger/QuantDinger
free -h
swapon --show
df -h /

COMPOSE_PARALLEL_LIMIT=1 docker compose build backend

free -h
swapon --show
df -h /

COMPOSE_PARALLEL_LIMIT=1 docker compose build frontend
```

迁移和所有 Worker 复用刚才构建的后端镜像，无需逐个重复构建。

可以验证镜像内已安装锁定版本：

```bash
docker compose run --rm --no-deps backend \
  python -c "import importlib.metadata as m; print(m.version('easy_tdx'))"
```

预期输出为 `1.20.4`。

## 11. 首次启动

明确列出服务，避免 Compose 启动 Mobile/H5 或容器 PostgreSQL：

```bash
docker compose up -d --no-build \
  redis redis-jobs migration backend \
  celery-worker celery-beat scheduler-worker trading-worker frontend
```

`migration` 是一次性容器，成功应用数据库迁移后应以退出码 0 结束。其他应用服务依赖迁移
成功才会启动。

检查状态：

```bash
docker compose ps -a
docker compose logs --tail=100 migration
```

预期：

- `migration` 为 `Exited (0)`；
- 其他已启动服务最终为 `Up`/`healthy`；
- 不存在 `quantdinger-mobile` 和 `quantdinger-db` 容器。

如果迁移失败，先查看日志并修复数据库连接或权限；不要跳过迁移强行启动后端。

## 12. 部署验收

### 12.1 容器与健康检查

```bash
docker compose ps -a
curl -fsS http://127.0.0.1:5000/api/health
curl -fsS http://127.0.0.1:5000/api/health/ready
curl -fsS http://127.0.0.1:5000/api/health/workers
curl -fsS http://127.0.0.1:8888/health
```

`/api/health/ready` 必须确认 PostgreSQL 与 Celery broker 均可用。Worker 接口应出现近期的
`trading`、`scheduler` 和 `celery` 心跳。

检查关键日志：

```bash
docker compose logs --tail=100 backend
docker compose logs --tail=100 celery-worker
docker compose logs --tail=100 celery-beat
docker compose logs --tail=100 scheduler-worker
docker compose logs --tail=100 trading-worker
```

### 12.2 Web 验收

浏览器访问 <http://127.0.0.1:8888>：

1. 使用已配置的管理员账号登录；
2. 确认页面能调用 `/api`，没有跨域或 502；
3. 修改默认初始化密码（如部署流程仍使用了临时密码）；
4. 检查后台任务与系统设置页面；
5. 当前阶段不要验收 Mobile/H5。

### 12.3 A 股行情页面验收

登录后从左侧菜单进入“**A股行情**”，或直接访问：

```text
http://<服务器地址>/quantdinger/#/cn-stocks
```

按以下顺序验收：

1. 页面显示上证指数、深证成指、创业板指；单个指数不可用时，其他指数仍应保留，失败项不得显示伪造的零值；
2. 市场统计显示沪深成交额、上涨、下跌、平盘、涨停和跌停家数，并显示数据时间和来源；
3. 使用股票代码或名称搜索，分别选择沪市、深市以及上涨、下跌、平盘筛选，确认分页总数会随筛选条件变化；
4. 确认 A 股使用红色表示上涨、绿色表示下跌，同时保留正负号或文字，不能只依赖颜色；
5. 点击股票名称进入 `/cn-stocks/<代码.交易所>`，确认报价摘要、日 K 线、成交量以及 MA、MACD、RSI、KDJ、BOLL、ATR 指标可见；
6. 核对个股详情中的报价时间、最后交易日、供应商、数据版本、复权方式和历史数据等级；
7. 在列表或详情加入/移出自选，返回另一页面后状态应一致；
8. 点击“进入回测”，回测中心应显示从详情带入的 `CNStock:<code>.<exchange>` 上下文，但不得自动执行回测；
9. 切换到其他浏览器标签页超过 30 秒后返回，确认页面恢复刷新；离开页面后不应继续产生定时行情请求。

行情列表和市场概览使用服务端共享快照。默认环境变量为：

```dotenv
CN_MARKET_SNAPSHOT_TTL_SEC=30
CN_MARKET_SNAPSHOT_STALE_TTL_SEC=900
CN_MARKET_SNAPSHOT_TIMEOUT_SEC=12
```

当上游临时失败但存在允许使用的缓存时，页面必须明确显示“缓存行情”和最后成功时间；没有任何可用缓存时显示可重试错误，不能把全市场显示为零。

个股日线遵循以下边界：

- 本地 PostgreSQL 历史覆盖完整且质量通过时，页面标记为“本地权威历史”，并可显示 `backtestEligible=true`；
- 本地覆盖不足时可以展示现有行情链路的日线，但必须标记为“展示级历史”和 `backtestEligible=false`；
- 展示级日线不会写入权威历史表，也不会绕过回测中心的覆盖门禁；
- 管理员仍需在“设置 → 市场数据”中执行定向同步，普通用户打开详情不会隐式创建同步任务。

如需直接检查新增 API，可使用已登录用户的临时 JWT（不要把令牌写入文档、日志或 Git）：

```bash
curl -H "Authorization: Bearer <临时JWT>" \
  http://127.0.0.1:5000/api/market/cn/overview
curl -H "Authorization: Bearer <临时JWT>" \
  "http://127.0.0.1:5000/api/market/cn/stocks?page=1&pageSize=20&exchange=SH"
curl -H "Authorization: Bearer <临时JWT>" \
  http://127.0.0.1:5000/api/market/cn/stocks/600519.SH
curl -H "Authorization: Bearer <临时JWT>" \
  "http://127.0.0.1:5000/api/market/cn/stocks/600519.SH/history?limit=260&adjustment=forward"
```

## 13. A 股历史数据初始化与验收

### 13.1 批准口径

首批只允许以下标的：

```text
CNStock:600519.SH
CNStock:000001.SZ
```

已批准的市场规则和费用口径：

- 佣金：成交额 `0.03%`，单笔最低 5 元；
- 印花税：仅卖出收取，2023-08-28 起 `0.05%`，此前 `0.1%`；
- 过户费：买卖双向，2022-04-29 起 `0.001%`，此前 `0.002%`；
- 买入按 100 股整数单位；
- T+1、仅做多。

扩充标的前必须补充按生效日期维护的交易所/巨潮分类证据；无法确认 ST、退市或分类状态
时必须失败关闭，不得自动外推首批结论。

### 13.2 分阶段启用

推荐顺序：

1. 保持 `CN_HISTORY_ENABLED=false`、`CN_HISTORY_SYNC_ENABLED=false` 完成迁移与基础验收；
2. 临时只启用同步能力，提交首批标的的定向串行回填；
3. 检查同步记录、权威样本对账、公司行动证据、缺口和复权覆盖；
4. 数据验收通过后再启用本地历史读取与回测；
5. 最后配置首批日常增量同步。

提交同步任务和查看覆盖需要管理员登录。可通过系统对应的 A 股历史管理界面，或管理员 API：

```text
GET  /api/market-history/capabilities
POST /api/market-history/sync-runs
GET  /api/market-history/sync-runs/{run_id}
GET  /api/market-history/instruments/{instrument}/coverage
GET  /api/market-history/provider-health
GET  /api/market-history/disk-status
```

定向任务请求范围应明确，例如 `2024-01-01` 至最近已确认收盘日，并保持单批标的数很小。
Celery maintenance 队列会执行任务；不要并行提交重叠区间。

### 13.3 最终运行配置

首批回填和质量验收通过后，设置：

```dotenv
SHOW_CN_STOCK=true
CN_HISTORY_ENABLED=true
CN_HISTORY_SYNC_ENABLED=true
CN_HISTORY_DAILY_SYMBOLS=CNStock:600519.SH,CNStock:000001.SZ
CN_HISTORY_INCREMENTAL_LOOKBACK_DAYS=14
CN_HISTORY_DAILY_SYNC_INTERVAL_SEC=86400
```

配置变化后只重建/重启读取该 env 的应用容器，不需要重新构建镜像：

```bash
docker compose up -d --no-build --force-recreate --no-deps backend
docker compose up -d --no-build --force-recreate --no-deps celery-worker
docker compose up -d --no-build --force-recreate --no-deps celery-beat
docker compose up -d --no-build --force-recreate --no-deps scheduler-worker
docker compose up -d --no-build --force-recreate --no-deps trading-worker
```

执行后重新检查 readiness、Worker 心跳和 A 股 capabilities。

### 13.4 当前权威基线

归档记录位于：

- [权威样本对账](../../openspec/changes/archive/2026-07-21-add-cn-stock-history-backtesting/verification/authoritative-sample-reconciliation.md)
- [首批回填与基准回测](../../openspec/changes/archive/2026-07-21-add-cn-stock-history-backtesting/verification/initial-backfill-and-baseline.md)

锁定数据与 `easy_tdx 1.20.4` 的已记录基线为：2024-01-03 至 2026-07-20、前复权、
614/614 请求交易日完整、组合总收益率约 `14.6599%`、账本审计通过。

该数值是特定数据版本、因子版本、费用口径和策略的回归证据，**不是任何新部署都应获得的
通用收益率**。验收时必须同时核对数据版本、日期、复权模式、费用和账本审计结果。

## 14. 日常升级

升级前先备份数据库，记录三个源码仓库和 `easy_tdx` 的 commit：

```bash
git -C /home/quantadinger/QuantDinger rev-parse HEAD
git -C /home/quantadinger/QuantDinger-Vue rev-parse HEAD
git -C /home/quantadinger/easy_tdx rev-parse HEAD
```

### 14.1 只更新后端

```bash
cd /home/quantadinger/QuantDinger
free -h && swapon --show && df -h /
COMPOSE_PARALLEL_LIMIT=1 docker compose build backend

docker compose up -d --no-build --force-recreate migration
docker compose logs --tail=100 migration

docker compose up -d --no-build --force-recreate --no-deps backend
docker compose up -d --no-build --force-recreate --no-deps celery-worker
docker compose up -d --no-build --force-recreate --no-deps celery-beat
docker compose up -d --no-build --force-recreate --no-deps scheduler-worker
docker compose up -d --no-build --force-recreate --no-deps trading-worker
```

后端镜像由多个服务共用，因此涉及公共代码、任务或数据库模型时，应逐个重建容器，不能只
更新 API 容器。每条命令串行执行。

### 14.2 只更新前端

```bash
cd /home/quantadinger/QuantDinger
free -h && swapon --show && df -h /
COMPOSE_PARALLEL_LIMIT=1 docker compose build frontend
docker compose up -d --no-build --force-recreate --no-deps frontend
```

### 14.3 easy_tdx 更新

1. 拉取并审查 `easy_tdx` 变更；
2. 重新构建 Web 资源与 wheel；
3. 更新并核验 `easy_tdx-wheel.env`、`easy_tdx-wheel.sha256`；
4. 运行 provider、公司行动、质量和 Strategy V2 聚焦测试；
5. 重新执行权威样本对账；
6. 重新构建后端镜像并逐个更新应用容器。

开发阶段仍使用本地 wheel，不需要发布 Python 包。

## 15. 日常运维

常用命令：

```bash
docker compose ps -a
docker compose logs -f --tail=100 backend
docker compose logs -f --tail=100 celery-worker
docker compose restart backend
docker compose stop frontend
docker compose start frontend
```

停止当前应用栈但保留数据：

```bash
docker compose stop \
  frontend backend celery-worker celery-beat scheduler-worker trading-worker \
  redis redis-jobs
```

不要使用 `docker compose down -v`，`-v` 会删除命名数据卷。

## 16. 备份

### 16.1 PostgreSQL

使用宿主机 `pg_dump` 创建可恢复的逻辑备份：

```bash
mkdir -p /home/quantadinger/backups
umask 077
pg_dump \
  --format=custom \
  --file=/home/quantadinger/backups/quantdinger-$(date +%F-%H%M%S).dump \
  'postgresql://quantdinger_app:密码@127.0.0.1:5432/quantdinger'
```

检查备份可读：

```bash
pg_restore --list /home/quantadinger/backups/quantdinger-日期时间.dump | head
```

备份文件包含账户、配置、行情和审计数据，应限制权限并异机保存。定期在独立测试数据库做
恢复演练，不能只验证文件存在。

### 16.2 Redis Jobs 与应用数据卷

`redis-jobs` 使用 AOF，但它不能替代 PostgreSQL 备份。备份命名卷前应先停止会持续写入的
Celery 服务，确认 Redis 已落盘，再使用经过审查的卷备份工具复制：

```bash
docker compose stop celery-beat celery-worker
docker compose exec redis-jobs redis-cli SAVE
docker volume ls | grep -E 'celery_redis_data|backend_data|backend_logs'
```

如配置了 Redis 密码，`redis-cli` 必须使用对应认证参数。完成卷备份后再启动 Celery。
不要为了备份测试而删除或覆盖现有卷。

## 17. 回滚

### 17.1 应用版本回滚

1. 记录当前故障版本、日志和数据库迁移状态；
2. 将后端/前端源码切回已经验收的 commit 或发布分支；
3. 按低内存规则重新构建受影响的单个镜像；
4. 先运行该版本的迁移命令，再逐个重建应用容器；
5. 验证 readiness、Worker 心跳和关键业务。

不要用 `git reset --hard` 覆盖未知本地修改。数据库 migration 默认应保持向前兼容；任何
破坏性 schema 回滚都必须单独评审并先备份。

### 17.2 A 股历史功能回滚

非破坏性关闭：

```dotenv
CN_HISTORY_ENABLED=false
CN_HISTORY_SYNC_ENABLED=false
```

随后停止或撤销待处理的历史同步任务，逐个重建后端、Celery 和相关 Worker 容器。保留
`qd_cn_*` 表及其行情、质量、同步和来源证据，不在普通回滚中删表。

完整说明见 [A 股历史数据回滚](CN_MARKET_HISTORY_ROLLBACK.md)。

## 18. 常见问题

### Compose 仍尝试启动 PostgreSQL 容器

检查根目录 `.env` 是否包含：

```dotenv
COMPOSE_FILE=docker-compose.yml:docker-compose.build.yml
```

不要添加 `--profile container-postgres`。使用 `docker compose config --profiles` 检查。

### 后端连接数据库失败

依次检查：

1. `DATABASE_URL` 的 host 是否为 `host.docker.internal`；
2. 密码是否正确 URL 编码；
3. 宿主 PostgreSQL 是否监听 Docker bridge；
4. `pg_hba.conf` 和防火墙是否允许实际 bridge CIDR；
5. 容器内解析：

   ```bash
   docker compose run --rm --no-deps backend getent hosts host.docker.internal
   ```

### 后端镜像找不到 easy_tdx wheel

重新执行第 6 节的构建和 SHA256 校验，确认 wheel 位于
`backend_api_python/vendor/`，并保留：

```dotenv
INSTALL_LOCAL_EASY_TDX=1
```

### 前端仍拉取 GHCR 镜像

检查是否自动合并 `docker-compose.build.yml`，并确认：

```dotenv
FRONTEND_SRC_PATH=/home/quantadinger/QuantDinger-Vue
FRONTEND_IMAGE=quantdinger-frontend
```

然后只执行 `docker compose build frontend`，不要执行 `docker compose pull frontend`。

### 构建被 OOM 杀死

停止继续重试，检查 `free -h`、`swapon --show`、`dmesg` 和当前容器资源。确保没有同时构建
前后端，保留 `COMPOSE_PARALLEL_LIMIT=1` 与 Node 1536 MiB 上限。未经许可不要停止无关服务。

### migration 一直失败

查看：

```bash
docker compose ps -a migration
docker compose logs --tail=200 migration
```

先修复数据库连接、权限或 SQL 问题。不要设置跳过迁移来掩盖错误。

### A 股同步被磁盘保护阻断

通过管理员接口 `/api/market-history/disk-status` 查看软/硬阈值和实际可用空间。优先扩容或
定向清理明确可删除的文件；不要执行 Docker 全局清理，也不要删除数据库卷。

更多 Docker、网络和镜像问题见[安装故障排查](INSTALL_TROUBLESHOOTING.md)。

## 19. 未来生产发布边界

当前本地源码模式用于开发和验证，不是最终发布链路。正式发布时应：

- 由 GitHub Actions 在托管 runner 上构建、测试并发布版本化 GHCR 镜像；
- 生产服务器只拉取固定版本镜像，不本地编译 Python/Node；
- 多架构构建由 CI 完成；
- 验证 fork 的 `ghcr.io/luhuiyuan/...` 镜像已实际发布且访问权限正确后，才能切换 namespace；
- 在 easy_tdx 的交付、许可证、版本和可复现性方案确定前，继续使用受控本地 wheel；
- 生产部署启用 TLS、最小权限、防火墙、备份、监控和
  [生产加固](PRODUCTION_HARDENING.md)。

在 fork 镜像尚未发布或不可匿名访问时，不要把当前可用部署改指向不存在的 GHCR tag。
