# 日切放行

> 遵守 `ops/底座生成前必读_效率与技术栈.md`。FastAPI + Vue 3 + SQLite，交付须 docker compose 一键起；库用 SQLite，不挂外置 Postgres。  
> **禁止同构换皮**：本仓主链路、表结构、接口与页面均独立，不得与其它仓共用同一套 `/api/items` 壳。

## 骨架一句话

业务日分区切片登记后裁决完整度，完整才放行下游，并与消费水位对账

## 基本信息

| 项 | 值 |
|----|-----|
| 产品名（首页） | 日切放行 |
| 目录 | `04-slicegate` |
| 领域建议 | 数仓与数据湖 |
| 接口 | http://127.0.0.1:10615 |
| 库 | `data/app.db` |
| 账号 | admin / 123456 |

## 部分完整处置口径（钉死）

行数大于零但未达期望 = **部分完整**，禁止含糊放行。互斥账本（放行流水）落笔前须先按正确性口径裁定并写入**完整度裁决册**（`adjudications`），处置模式甲乙互斥、当日定死禁止再换：

- **甲处置**：部分完整一律硬拒绝，只写拒绝流水与原因，不改放行态、不推水位。
- **乙处置**：部分完整进入降级只读冻结（`冻结`），详情可看，成功放行与水位推进硬拒绝。

铁律：部分完整禁止标成已放行；拒绝流水与已放行态禁止同时成立；偏离裁决册拒绝并记「完整度口径偏了」；两名值班同时裁同一切片须排队，后手 409 记「裁决争用」。部分完整列表（`/api/slices/partial`）、裁决详情（`/api/adjudications`）、放行流水（`/api/releases`）、策略旁注（`/api/policy`）四方可对读，`/api/consistency` 实时校验。

## 为何能出多题

迟到切片、重切、跨分区依赖、部分完整冻结、消费水位回拨

## 避开

别做成作业拓扑编排换皮；别做成纯水位大盘

## 互斥切口候选

- 迟到切片整批拒收 vs 进待补齐冻结区
- 重切失败整笔回滚 vs 保留已完整分区
- 放行与水位同步闸 vs 异步放行后纠偏

## 启动（一键）

`powershell
cd d:\document\cccodex\workspace\slicegate\slicegate-05
docker compose up -d --build
`

浏览器：http://127.0.0.1:10615/  
健康检查：GET http://127.0.0.1:10615/api/health

本机开发（可选）：

`powershell
python -m pip install -r requirements.txt
python -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 10615
python -m pytest -q
`
