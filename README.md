# 数据采集调度中心

面向互联网数据采集、网页爬虫调度、RPA自动化流程与采集结果校验的集中管理平台。支持多源接入、增量采集、抓取规则配置、采集任务编排、失败重试与结果质量稽核。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 测试

```bash
cd backend
pip install -r requirements-dev.txt   # 比运行依赖多一个 httpx（TestClient 用）
python3 -m unittest discover -s tests -v
```

前端只做类型检查与构建校验：`cd frontend && npm run build`。

## 去重预检（入库前）

采集去重只配置粒度与时间窗口时，入库前无法知道哪些记录会被合并。去重预检把判断前置：

1. `POST /api/dedup/prechecks` 按规则的**目标字段 + 时间窗口**对采集记录（`dedup_record`）
   生成候选组，窗口为闭区间 `[锚点-窗口, 锚点]`，每条候选都带命中的窗口起止与距离。
2. 冲突时**保留规则（最早/最晚）优先于来源顺序**，仅采集时间相同才回退来源顺序，每组只留 1 条。
3. 预检可中断（`/interrupt`），续跑（`/resume`）从当前候选组继续；中断态不允许确认。
4. 确认（`/confirm`）带互斥与状态幂等：并发重复确认只生效一次，不生成重复待入库项。
5. 确认后：
   - 剔除结果写入追溯清单 `GET /api/dedup/removals/list`（按候选组编号分段，含窗口边界），
     并在**采集数据存储**侧只读联动：`GET /api/storage/dedup-removals`；
   - 胜出记录立即进入待入库 `GET /api/dedup/pending/list`，
     **任务工作台业务面**通过 `GET /api/task/dedup-workbench` 读取；
   - 按确认时的规则快照留档 `GET /api/dedup/archives/list`；跨月重算只追加留档，
     不删除已入库/已留档记录。
6. 已启用规则（如 DEDU-0001）只参与读取：预检永不改写规则配置；已有结论
   （已确认/已剔除/已入库）的记录在新预检中标记「跳过」，不重复落地。

前端「采集去重」页提供五个页签：去重规则、去重预检、剔除追溯清单、待入库（工作台）、历史留档。

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 采集源管理 | `source` | 采集源 | 源端编号、源端名称、接入协议 |
| 网页抓取规则 | `website` | 抓取规则 | 规则编号、目标站点、列表选择器 |
| 采集任务 | `task` | 采集任务 | 任务编号、任务名称、关联源端 |
| 调度编排 | `schedule` | 调度计划 | 计划编号、关联任务、调度表达式 |
| 增量采集游标 | `increment` | 增量游标 | 游标编号、来源标识、增量字段 |
| RPA流程配置 | `rpa` | RPA流程 | 流程编号、流程名称、输入来源 |
| 采集日志 | `log` | 执行日志 | 日志编号、关联任务、开始时间 |
| 失败重试策略 | `retry` | 重试策略 | 策略编号、适用任务、最大重试 |
| 代理与请求控制 | `proxy` | 代理配置 | 代理编号、代理地址、代理端口 |
| 数据解析模板 | `parser` | 解析模板 | 模板编号、输入格式、字段映射 |
| 采集去重 | `dedup` | 去重规则 | 规则编号、目标字段、去重粒度 |
| 采集版本管理 | `release` | 采集版本 | 版本编号、关联源端、版本号 |
| 采集告警 | `alert` | 告警规则 | 告警编号、监控目标、告警条件 |
| 采集看板 | `dashboard` | 看板指标 | 指标编号、指标名称、统计维度 |
| 采集数据存储 | `storage` | 存储配置 | 存储编号、存储类型、连接串 |
| 采集审计 | `audit` | 审计记录 | 审计编号、操作类型、操作对象 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据采集、爬虫与RPA
