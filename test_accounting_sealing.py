# -*- coding: utf-8 -*-
"""核算输入版本与封账机制集成测试

覆盖需求：
1. 企业每次提交形成完整可比较的输入版本
2. 监管确认后冻结当期核算、限值标准和计算结果（封账）
3. 重开需记录理由、权限和受影响企业
4. 基于指定版本重算并给出各积分项差异，不悄悄覆盖旧结论
5. 批量重算中单个企业失败保持其原封账结果
6. 重复执行同一重开决定不再生成新版本（幂等）
7. 下游结转、交易可用额、年度汇总明确采用哪次封账
"""
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app import crud, schemas, models, rules
from app.models import InputVersionStatus, ClosureStatus, ReopenDecisionStatus

TEST_DATABASE_URL = "sqlite:///:memory:"

YEAR = 2025


@pytest.fixture(scope="function")
def db():
    engine = create_engine(
        TEST_DATABASE_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def make_enterprise(db, name="测试车企A", code="TEST-A"):
    return crud.create_enterprise(
        db, schemas.EnterpriseCreate(name=name, short_name=name, credit_code=code)
    )


def make_items(output=1000, pc=12.0, extra=None):
    items = [
        {"model_code": "M1", "model_name": "车型M1", "curb_weight": 1500.0,
         "power_consumption": pc, "range_km": 400.0, "annual_output": output},
        {"model_code": "M2", "model_name": "车型M2", "curb_weight": 1800.0,
         "power_consumption": 16.0, "range_km": 500.0, "annual_output": 500},
    ]
    if extra:
        items.extend(extra)
    return items


def submit(db, ent_id, year=YEAR, items=None, by="企业经办人", note=None):
    return crud.submit_accounting_input_version(
        db, enterprise_id=ent_id, year=year,
        items=items if items is not None else make_items(),
        submitted_by=by, note=note
    )


def seal(db, ent_id, version_id, year=YEAR, by="监管员张三"):
    return crud.seal_accounting(
        db, enterprise_id=ent_id, year=year,
        input_version_id=version_id, sealed_by=by
    )


class TestInputVersions:
    """需求1：企业每次提交形成完整可比较的输入版本"""

    def test_submit_creates_incrementing_versions(self, db):
        ent = make_enterprise(db)
        v1 = submit(db, ent.id, note="年度申报")
        assert v1.version_no == 1
        assert v1.status == InputVersionStatus.SUBMITTED
        assert len(v1.items) == 2

        v2 = submit(db, ent.id, note="补交产量和能耗凭证")
        assert v2.version_no == 2
        # 旧提交被取代但保留可查
        db.refresh(v1)
        assert v1.status == InputVersionStatus.SUPERSEDED

        versions = crud.get_accounting_input_versions(db, enterprise_id=ent.id, year=YEAR)
        assert [v.version_no for v in versions] == [1, 2]

    def test_submit_validation(self, db):
        ent = make_enterprise(db)
        with pytest.raises(ValueError, match="不能为空"):
            submit(db, ent.id, items=[])
        with pytest.raises(ValueError, match="不能重复"):
            submit(db, ent.id, items=[make_items()[0], make_items()[0]])
        with pytest.raises(ValueError, match="企业不存在"):
            submit(db, 9999)

    def test_compare_versions_field_level(self, db):
        ent = make_enterprise(db)
        v1 = submit(db, ent.id)
        new_items = make_items(output=2000, pc=13.0)
        new_items.append({"model_code": "M3", "model_name": "车型M3",
                          "curb_weight": 1200.0, "power_consumption": 10.0,
                          "range_km": 350.0, "annual_output": 300})
        v2 = submit(db, ent.id, items=new_items, note="补交凭证")

        diff = crud.compare_input_versions(db, v1.id, v2.id)
        assert diff["version_a"]["version_no"] == 1
        assert diff["version_b"]["version_no"] == 2
        assert diff["summary"]["added"] == 1
        assert diff["summary"]["changed"] == 1  # 仅 M1 变化，M2 未变
        assert diff["summary"]["unchanged"] == 1
        assert diff["summary"]["removed"] == 0

        changed_by_code = {c["model_code"]: c for c in diff["changed"]}
        assert changed_by_code["M1"]["field_changes"]["annual_output"] == {"old": 1000, "new": 2000}
        assert changed_by_code["M1"]["field_changes"]["power_consumption"] == {"old": 12.0, "new": 13.0}
        assert diff["added"][0]["model_code"] == "M3"

        with pytest.raises(ValueError):
            crud.compare_input_versions(db, v1.id, 9999)


class TestSealing:
    """需求2：监管确认后冻结当期核算、限值标准和计算结果"""

    def test_seal_freezes_inputs_standard_and_results(self, db):
        ent = make_enterprise(db)
        v1 = submit(db, ent.id)
        closure = seal(db, ent.id, v1.id)

        assert closure.status == ClosureStatus.SEALED
        assert closure.sealed_by == "监管员张三"
        assert closure.input_version_id == v1.id
        assert closure.closure_no

        # 冻结的限值标准快照与当前规则一致
        standard = json.loads(closure.limit_standard_snapshot)
        assert standard["credit_multiplier"] == rules.CREDIT_MULTIPLIER
        assert len(standard["tiers"]) == len(rules.POWER_CONSUMPTION_LIMIT_TIERS)

        # 冻结的计算结果与按规则独立计算一致
        result = json.loads(closure.result_snapshot)
        item_m1 = next(i for i in result["items"] if i["model_code"] == "M1")
        expected_limit = rules.calculate_power_consumption_limit(1500.0)
        expected_unit = rules.calculate_unit_credit(12.0, expected_limit)
        assert item_m1["power_consumption_limit"] == expected_limit
        assert item_m1["unit_credit"] == expected_unit
        assert item_m1["total_credit"] == rules.calculate_total_credit(expected_unit, 1000)
        assert result["totals"]["net_credit"] == round(
            sum(i["total_credit"] for i in result["items"]), 2
        )

        # 输入版本被标记为已封账采用
        db.refresh(v1)
        assert v1.status == InputVersionStatus.SEALED

        # 年度汇总明确采用本次封账
        summary = crud.get_or_create_annual_summary(db, ent.id, YEAR)
        assert summary.closure_id == closure.id
        assert summary.net_credit == result["totals"]["net_credit"]

    def test_reseal_rejected_must_use_reopen(self, db):
        ent = make_enterprise(db)
        v1 = submit(db, ent.id)
        seal(db, ent.id, v1.id)

        v2 = submit(db, ent.id, note="补交凭证")
        with pytest.raises(ValueError, match="重开"):
            seal(db, ent.id, v2.id)

    def test_seal_validation(self, db):
        ent = make_enterprise(db)
        v1 = submit(db, ent.id)
        with pytest.raises(ValueError, match="企业不存在"):
            seal(db, 9999, v1.id)
        with pytest.raises(ValueError, match="不存在"):
            seal(db, ent.id, 9999)
        other = make_enterprise(db, name="测试车企B", code="TEST-B")
        with pytest.raises(ValueError, match="不匹配"):
            seal(db, other.id, v1.id)


class TestReopenDecision:
    """需求3：重开需记录理由、权限和受影响企业"""

    def test_decision_records_reason_authority_enterprises(self, db):
        ent = make_enterprise(db)
        v1 = submit(db, ent.id)
        seal(db, ent.id, v1.id)

        decision = crud.create_reopen_decision(
            db, year=YEAR, enterprise_ids=[ent.id],
            reason="企业补交产量和能耗凭证，监管复核确认需重算",
            approved_by="监管局李局长"
        )
        assert decision.status == ReopenDecisionStatus.PENDING
        assert decision.decision_no
        assert json.loads(decision.enterprise_ids) == [ent.id]
        assert decision.reason.startswith("企业补交")
        assert decision.approved_by == "监管局李局长"

    def test_decision_validation(self, db):
        ent = make_enterprise(db)
        with pytest.raises(ValueError, match="理由"):
            crud.create_reopen_decision(db, YEAR, [ent.id], "", "审批人")
        with pytest.raises(ValueError, match="审批人"):
            crud.create_reopen_decision(db, YEAR, [ent.id], "理由", " ")
        with pytest.raises(ValueError, match="受影响企业"):
            crud.create_reopen_decision(db, YEAR, [], "理由", "审批人")
        with pytest.raises(ValueError, match="不存在"):
            crud.create_reopen_decision(db, YEAR, [9999], "理由", "审批人")


class TestReopenExecution:
    """需求4/5/6：基于指定版本重算、差异、失败隔离、幂等"""

    def _sealed_and_supplemented(self, db, ent):
        """首次封账后企业补交凭证形成新版本"""
        v1 = submit(db, ent.id, items=make_items(output=1000, pc=12.0))
        closure1 = seal(db, ent.id, v1.id)
        v2 = submit(db, ent.id, items=make_items(output=2000, pc=13.0), note="补交产量和能耗凭证")
        return v1, closure1, v2

    def test_recalc_with_diff_and_old_conclusion_preserved(self, db):
        ent = make_enterprise(db)
        v1, closure1, v2 = self._sealed_and_supplemented(db, ent)
        old_snapshot = closure1.result_snapshot  # 记录旧结论原文

        decision = crud.create_reopen_decision(
            db, YEAR, [ent.id], "补交凭证重算", "监管局李局长"
        )
        result = crud.execute_reopen_decision(db, decision.id)

        assert result["success_count"] == 1
        assert result["failure_count"] == 0
        created = result["closures_created"][0]
        assert created["previous_closure_id"] == closure1.id

        # 新封账基于补交版本，旧封账完整保留未被覆盖
        closure2 = crud.get_accounting_closure(db, created["new_closure_id"])
        assert closure2.status == ClosureStatus.SEALED
        assert closure2.input_version_id == v2.id
        assert closure2.reopen_decision_id == decision.id
        db.refresh(closure1)
        assert closure1.status == ClosureStatus.SUPERSEDED
        assert closure1.result_snapshot == old_snapshot  # 旧结论一字未动

        # 各积分项差异：M1 产量/电耗变化，M2 未变
        diff = json.loads(closure2.diff_from_previous)
        assert diff["previous_closure_no"] == closure1.closure_no
        item_diffs = {i["model_code"]: i for i in diff["items"]}
        assert set(item_diffs) == {"M1"}
        assert item_diffs["M1"]["change_type"] == "modified"
        assert item_diffs["M1"]["delta"]["annual_output"] == 1000
        assert item_diffs["M1"]["delta"]["power_consumption"] == 1.0
        assert "total_credit" in item_diffs["M1"]["delta"]
        assert diff["unchanged_item_count"] == 1

        old_totals = json.loads(old_snapshot)["totals"]
        new_totals = json.loads(closure2.result_snapshot)["totals"]
        assert diff["totals"]["delta"]["net_credit"] == round(
            new_totals["net_credit"] - old_totals["net_credit"], 4
        )

        # 年度汇总切换到新封账
        summary = crud.get_or_create_annual_summary(db, ent.id, YEAR)
        assert summary.closure_id == closure2.id
        assert summary.net_credit == new_totals["net_credit"]

    def test_execute_is_idempotent(self, db):
        ent = make_enterprise(db)
        _, closure1, _ = self._sealed_and_supplemented(db, ent)
        decision = crud.create_reopen_decision(db, YEAR, [ent.id], "补交重算", "李局长")

        result1 = crud.execute_reopen_decision(db, decision.id)
        closure_count = len(crud.get_accounting_closures(db, enterprise_id=ent.id))
        assert closure_count == 2

        # 重复执行同一决定：不再生成新版本，直接返回首次结果
        result2 = crud.execute_reopen_decision(db, decision.id)
        assert result2 == result1
        assert len(crud.get_accounting_closures(db, enterprise_id=ent.id)) == closure_count

        db.refresh(decision)
        assert decision.status == ReopenDecisionStatus.EXECUTED
        assert decision.executed_at is not None

    def test_batch_failure_keeps_original_closure(self, db):
        ent_ok = make_enterprise(db, "车企-成功", "T-OK")
        ent_bad_version = make_enterprise(db, "车企-版本无效", "T-NV")
        ent_not_sealed = make_enterprise(db, "车企-未封账", "T-NS")

        # 成功路径：已封账 + 补交新版本
        v_ok1 = submit(db, ent_ok.id)
        seal(db, ent_ok.id, v_ok1.id)
        submit(db, ent_ok.id, items=make_items(output=3000), note="补交")

        # 失败路径1：已封账，但决定为其指定的重算版本不存在
        v_nv1 = submit(db, ent_bad_version.id)
        closure_nv = seal(db, ent_bad_version.id, v_nv1.id)

        # 失败路径2：从未封账
        submit(db, ent_not_sealed.id)

        decision = crud.create_reopen_decision(
            db, YEAR, [ent_ok.id, ent_bad_version.id, ent_not_sealed.id],
            "批量重开", "李局长",
            input_version_map={ent_bad_version.id: 999999}
        )

        result = crud.execute_reopen_decision(db, decision.id)
        assert result["success_count"] == 1
        assert result["failure_count"] == 2
        failed = {f["enterprise_id"]: f for f in result["failures"]}
        assert set(failed) == {ent_bad_version.id, ent_not_sealed.id}
        assert "不存在" in failed[ent_bad_version.id]["error"]
        assert "无有效封账" in failed[ent_not_sealed.id]["error"]
        assert all(f["action"] == "保持原封账结果" for f in result["failures"])

        # 失败企业保持原封账结果：仍是唯一有效封账，未生成新封账
        db.refresh(closure_nv)
        assert closure_nv.status == ClosureStatus.SEALED
        closures_nv = crud.get_accounting_closures(db, enterprise_id=ent_bad_version.id)
        assert len(closures_nv) == 1
        assert len(crud.get_accounting_closures(db, enterprise_id=ent_not_sealed.id)) == 0

        # 成功企业生成了新封账
        closures_ok = crud.get_accounting_closures(db, enterprise_id=ent_ok.id)
        assert len(closures_ok) == 2
        active = crud.get_active_closure(db, ent_ok.id, YEAR)
        assert active.previous_closure_id is not None

    def test_execute_uses_designated_version_not_latest(self, db):
        ent = make_enterprise(db)
        v1 = submit(db, ent.id, items=make_items(output=1000))
        seal(db, ent.id, v1.id)
        v2 = submit(db, ent.id, items=make_items(output=2000), note="第一次补交")
        v3 = submit(db, ent.id, items=make_items(output=5000), note="第二次补交")

        # 指定按 v2 重算，而不是最新的 v3
        decision = crud.create_reopen_decision(
            db, YEAR, [ent.id], "按指定版本重算", "李局长",
            input_version_map={ent.id: v2.id}
        )
        result = crud.execute_reopen_decision(db, decision.id)
        created = result["closures_created"][0]
        assert created["input_version_id"] == v2.id
        closure2 = crud.get_accounting_closure(db, created["new_closure_id"])
        snapshot = json.loads(closure2.result_snapshot)
        m1 = next(i for i in snapshot["items"] if i["model_code"] == "M1")
        assert m1["annual_output"] == 2000  # 来自 v2 而非 v3 的 5000

    def test_limit_standard_change_detected(self, db, monkeypatch):
        ent = make_enterprise(db)
        v1 = submit(db, ent.id)
        closure1 = seal(db, ent.id, v1.id)

        # 限值标准发生修订（模拟）：1500kg 档限值从 13.5 调整为 13.0
        new_tiers = [
            rules.PowerConsumptionLimitTier(t.min_weight, t.max_weight, t.limit)
            for t in rules.POWER_CONSUMPTION_LIMIT_TIERS
        ]
        new_tiers[3] = rules.PowerConsumptionLimitTier(1400, 1600, 13.0)
        monkeypatch.setattr(rules, "POWER_CONSUMPTION_LIMIT_TIERS", new_tiers)

        v2 = submit(db, ent.id, note="按新标准重报")
        decision = crud.create_reopen_decision(db, YEAR, [ent.id], "标准修订重算", "李局长")
        result = crud.execute_reopen_decision(db, decision.id)

        closure2 = crud.get_accounting_closure(db, result["closures_created"][0]["new_closure_id"])
        diff = json.loads(closure2.diff_from_previous)
        assert diff["limit_standard_changed"] is True
        new_standard = json.loads(closure2.limit_standard_snapshot)
        assert new_standard["tiers"][3]["limit"] == 13.0
        old_standard = json.loads(closure1.limit_standard_snapshot)
        assert old_standard["tiers"][3]["limit"] == 13.5  # 旧封账标准仍冻结原样


class TestDownstreamClosureBasis:
    """需求7：下游结转、交易可用额和年度汇总明确采用哪次封账"""

    def _sealed_surplus_enterprise(self, db):
        ent = make_enterprise(db)
        # M1: limit 13.5, pc 12.0 → 正积分；M2: limit 15.5, pc 16.0 → 负积分
        v1 = submit(db, ent.id)
        closure = seal(db, ent.id, v1.id)
        return ent, closure

    def test_annual_summary_marks_closure(self, db):
        ent, closure = self._sealed_surplus_enterprise(db)
        summary = crud.update_annual_summary_with_transactions(db, ent.id, YEAR)
        assert summary.closure_id == closure.id

        totals = json.loads(closure.result_snapshot)["totals"]
        assert summary.total_positive_credit == totals["total_positive_credit"]
        assert summary.net_credit == totals["net_credit"]

        v2_summary = crud.calculate_enterprise_credit_summary_v2(db, ent.id, YEAR)
        assert v2_summary.closure_id == closure.id
        assert v2_summary.closure_no == closure.closure_no
        assert v2_summary.total_positive_credit == totals["total_positive_credit"]

        multi = crud.get_multi_year_summary(db, ent.id, YEAR, YEAR)
        assert multi.annual_summaries[0]["closure_id"] == closure.id
        assert multi.annual_summaries[0]["closure_no"] == closure.closure_no

    def test_tradable_amount_based_on_closure(self, db):
        ent, closure = self._sealed_surplus_enterprise(db)
        totals = json.loads(closure.result_snapshot)["totals"]
        surplus = totals["credit_surplus"]
        assert surplus > 0

        # 封账额度内可挂单
        order = crud.create_credit_order(db, schemas.CreditOrderCreate(
            enterprise_id=ent.id, year=YEAR, order_type=models.OrderType.SELL,
            unit_price=3000.0, total_amount=round(surplus, 2)
        ))
        assert order.id

        # 超出封账钟余的挂单被拒绝（交易可用额以封账为准）
        with pytest.raises(ValueError, match="钟余"):
            crud.create_credit_order(db, schemas.CreditOrderCreate(
                enterprise_id=ent.id, year=YEAR, order_type=models.OrderType.SELL,
                unit_price=3000.0, total_amount=round(surplus + 1.0, 2)
            ))

    def test_carryover_marks_source_closure(self, db):
        ent, closure = self._sealed_surplus_enterprise(db)
        carryovers = crud.execute_yearly_carryover(db, from_year=YEAR, to_year=YEAR + 1)
        assert len(carryovers) == 1
        assert carryovers[0].source_closure_id == closure.id

        summary_rows = crud.get_carryover_summary(db, ent.id, YEAR + 1)
        assert summary_rows[0].source_closure_id == closure.id
        assert summary_rows[0].source_closure_no == closure.closure_no

    def test_reopen_updates_downstream_basis(self, db):
        ent, closure1 = self._sealed_surplus_enterprise(db)
        v2 = submit(db, ent.id, items=make_items(output=2000), note="补交凭证")
        decision = crud.create_reopen_decision(db, YEAR, [ent.id], "补交重算", "李局长")
        result = crud.execute_reopen_decision(db, decision.id)
        closure2_id = result["closures_created"][0]["new_closure_id"]

        # 重开后下游一律改用新封账
        summary = crud.update_annual_summary_with_transactions(db, ent.id, YEAR)
        assert summary.closure_id == closure2_id
        active = crud.get_active_closure(db, ent.id, YEAR)
        assert active.id == closure2_id


class TestAccountingApi:
    """接口层冒烟：完整流程走一遍 REST API"""

    @pytest.fixture()
    def client(self, db):
        from fastapi.testclient import TestClient
        from app.main import app
        from app.database import get_db

        def override_get_db():
            try:
                yield db
            finally:
                pass

        app.dependency_overrides[get_db] = override_get_db
        yield TestClient(app)
        app.dependency_overrides.clear()

    def test_full_flow_via_api(self, client, db):
        ent = make_enterprise(db)
        prefix = "/api/v1/accounting"

        # 1. 提交输入版本
        resp = client.post(f"{prefix}/input-versions", json={
            "enterprise_id": ent.id, "year": YEAR, "submitted_by": "经办人",
            "note": "年度申报", "items": make_items()
        })
        assert resp.status_code == 200, resp.text
        v1 = resp.json()
        assert v1["version_no"] == 1

        # 2. 监管封账
        resp = client.post(f"{prefix}/closures/seal", json={
            "enterprise_id": ent.id, "year": YEAR,
            "input_version_id": v1["id"], "sealed_by": "监管员"
        })
        assert resp.status_code == 200, resp.text
        closure1 = resp.json()
        assert closure1["status"] == "sealed"
        assert closure1["result_snapshot"]["totals"]["model_count"] == 2

        # 3. 已封账后禁止直接再次封账
        resp = client.post(f"{prefix}/input-versions", json={
            "enterprise_id": ent.id, "year": YEAR, "submitted_by": "经办人",
            "note": "补交产量和能耗凭证", "items": make_items(output=2000)
        })
        v2 = resp.json()
        resp = client.post(f"{prefix}/closures/seal", json={
            "enterprise_id": ent.id, "year": YEAR,
            "input_version_id": v2["id"], "sealed_by": "监管员"
        })
        assert resp.status_code == 400

        # 4. 版本比较
        resp = client.get(f"{prefix}/input-versions/compare",
                          params={"version_a": v1["id"], "version_b": v2["id"]})
        assert resp.status_code == 200
        assert resp.json()["summary"]["changed"] == 1

        # 5. 登记并执行重开
        resp = client.post(f"{prefix}/reopen-decisions", json={
            "year": YEAR, "enterprise_ids": [ent.id],
            "reason": "企业补交产量和能耗凭证", "approved_by": "李局长"
        })
        assert resp.status_code == 200, resp.text
        decision = resp.json()
        assert decision["status"] == "pending"

        resp = client.post(f"{prefix}/reopen-decisions/{decision['id']}/execute")
        assert resp.status_code == 200, resp.text
        executed = resp.json()
        assert executed["success_count"] == 1

        # 6. 重复执行幂等
        resp = client.post(f"{prefix}/reopen-decisions/{decision['id']}/execute")
        assert resp.status_code == 200
        assert "未生成新版本" in resp.json()["message"]
        resp = client.get(f"{prefix}/closures", params={"enterprise_id": ent.id})
        assert len(resp.json()) == 2

        # 7. 新封账带差异，旧封账保留
        new_closure_id = executed["closures_created"][0]["new_closure_id"]
        resp = client.get(f"{prefix}/closures/{new_closure_id}")
        detail = resp.json()
        assert detail["diff_from_previous"]["totals"]["delta"]["net_credit"] != 0
        resp = client.get(f"{prefix}/closures/{closure1['id']}")
        assert resp.json()["status"] == "superseded"

        # 8. 年度汇总明确所采用的封账
        resp = client.get(f"/api/v1/credit-carryover/annual-summary/{ent.id}/{YEAR}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["closure_id"] == new_closure_id
        assert resp.json()["closure_no"] == detail["closure_no"]
