# 企业双积分核算与交易服务

本项目是使用 Python、FastAPI 与 SQLite 实现的服务端应用，覆盖企业、车型、年度核算、订单撮合、交易结转和统计。它可在单个 Linux 应用容器内完成安装、测试、编译和接口验收，不依赖浏览器、外部数据库、缓存、消息队列或额外运行服务。

## 核算输入版本与封账

年度申报截止后企业补交凭证时，系统不再直接改写已公布结果，而是通过"输入版本 + 封账 + 重开决定"机制保证可追溯：

- **核算输入版本**：企业每次提交产量/能耗凭证都形成一份完整、不可变、可逐字段比较的输入版本（同一企业同一年度版本号递增）。
- **封账**：监管确认后冻结当期核算输入版本、限值标准快照和计算结果快照；已封账年度禁止直接再次封账。
- **重开决定**：确需重开时必须登记理由、审批人（权限）和受影响企业；执行时基于指定输入版本重算，生成新封账并落盘与上一封账的各积分项差异，旧封账完整保留（置为 superseded），绝不悄悄覆盖。
- **失败隔离与幂等**：批量重算中单个企业失败仅回滚该企业、保持其原封账结果；重复执行同一重开决定直接返回首次执行结果，不再生成新版本。
- **下游追溯**：年度汇总、跨年度结转、交易可用额（卖单校验）均记录并采用当前有效封账。

核心接口（前缀 `/api/v1/accounting`）：

```
POST /input-versions                        提交核算输入版本
GET  /input-versions                        版本列表
GET  /input-versions/compare?version_a=&version_b=   比较两个版本
POST /closures/seal                         监管确认封账
GET  /closures                              封账列表（含历史 superseded）
GET  /closures/active/{enterprise_id}/{year}         查询当前有效封账
GET  /closures/{closure_id}                 封账详情（标准/结果快照、积分项差异）
POST /reopen-decisions                      登记重开决定（理由/权限/受影响企业）
POST /reopen-decisions/{id}/execute         执行重开（幂等，单企业失败隔离）
GET  /reopen-decisions/{id}                 决定详情与执行结果
```

## 安装

```bash
python3 -m pip install -r requirements.txt -r requirements-dev.txt
```

## 测试

```bash
python3 -m pytest -q test_dual_credit_integration.py test_accounting_sealing.py
```

## 编译

```bash
python3 -m compileall -q .
```

## 接口验收

```bash
python3 -c "from app.main import app; assert len(app.routes) > 5; print(len(app.routes))"
```

## 启动

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```
