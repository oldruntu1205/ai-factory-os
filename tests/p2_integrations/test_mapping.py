"""外部系统接入层门禁（上游 §15 `integrations/`、§16.1「不填编造的生产值」）。

这一组用例守的是**三件会上生产事故的事**：

1. **编造值**：缺字段就补默认值 → 产生"看起来对、实际错"的事实；
2. **时区静默错位**：把工厂本地时间当成 UTC → 事件整体平移数小时，
   而**所有校验依然通过**；
3. **单位静默错位**：把 cm 当 mm、把 kg 当 g → 数量级错误流到排程与账务。

三条的共同点是**不会报错**——所以必须由门禁主动证伪。

另外还守两条 P2 的核心语义：

4. `aggregate_version` 必须来自**外部权威系统**，适配层不得推断或累加
   （否则乱序事件会静默覆盖新事实）；
5. 状态必须如实标 `mock` / `待冻结`，不得声明为已按生产口径。

## 目录命名的由来（踩过的坑）

本套件原名 `tests/integrations/`，结果**遮蔽了仓库根的 `integrations` 包**：
pytest 的 rootdir 是 `tests/`（仓库里没有 pytest 配置文件），
于是 `tests/integrations/` 先被找到，`import integrations` 解析到测试目录自己，
`integrations._base` 自然不存在，收集阶段直接报 `ModuleNotFoundError`。

**通用规则：测试目录名不要与仓库根的被测包同名。**
"""

from __future__ import annotations

import pytest

from integrations._base import (
    UNIT_FACTORS,
    MappingError,
    SystemMapping,
    normalize_event,
    payload_hash,
    to_celsius,
    to_identifier,
    to_int,
    to_unit,
    to_utc,
)
from integrations.erp.adapter import ERP_MAPPING, normalize_erp_demand
from integrations.erp.mock import (
    MOCK_DEMAND_RECORD,
    MOCK_DEMAND_RECORD_LOCAL_TIME,
    REQUIRED_FROM_ERP,
)
from integrations.mes.adapter import MES_MAPPING, normalize_mes_report
from integrations.mes.mock import (
    MOCK_INCONSISTENT_REPORT_RECORD,
    MOCK_REPORT_RECORD,
    REQUIRED_FROM_MES,
)
from integrations.pmc.adapter import PMC_MAPPING, normalize_pmc_schedule
from integrations.pmc.mock import MOCK_SCHEDULE_RECORD, REQUIRED_FROM_PMC
from integrations.wms.adapter import WMS_MAPPING, normalize_wms_stock_txn
from integrations.wms.mock import MOCK_STOCK_TXN_RECORD, REQUIRED_FROM_WMS
from services._kernel import events

RECEIVED_AT = "2026-10-05T00:00:00Z"

ALL_MAPPINGS = (ERP_MAPPING, PMC_MAPPING, MES_MAPPING, WMS_MAPPING)


# ==========================================================================
# 一、契约合规：产出的信封必须能被 kernel 接受
# ==========================================================================

@pytest.mark.parametrize(
    "name,normalize,record",
    [
        ("erp", normalize_erp_demand, MOCK_DEMAND_RECORD),
        ("pmc", normalize_pmc_schedule, MOCK_SCHEDULE_RECORD),
        ("mes", normalize_mes_report, MOCK_REPORT_RECORD),
        ("wms", normalize_wms_stock_txn, MOCK_STOCK_TXN_RECORD),
    ],
)
def test_normalized_envelope_passes_kernel_envelope_validation(name, normalize, record):  # A11
    """翻译结果必须**直接**能被 `events.validate_envelope` 接受。

    用 kernel 的校验器而不是自己写一套——适配层与事件层必须共用同一个契约判据。
    """
    result = normalize(record, received_at=RECEIVED_AT)
    events.validate_envelope(dict(result.envelope))


def test_every_adapter_declares_mock_status():  # A11
    """上游 §16.1：生产口径未冻结时，状态**不得**声明为已定。"""
    for mapping in ALL_MAPPINGS:
        assert mapping.status in ("mock", "待冻结"), (
            f"{mapping.system} 状态为 {mapping.status!r}——"
            "真实字段/单位/时区属外部依赖，未冻结前不得声明为已按生产口径"
        )
        assert mapping.note, f"{mapping.system} 未说明其口径状态"


def test_normalize_refuses_unknown_status():  # A11
    """状态被改成非法值时，归一化必须拒绝——防止有人绕过 status 声明。"""
    tampered = SystemMapping(
        system="tampered",
        source_system="tampered",
        status="production-approved",
        fields=ERP_MAPPING.fields,
        payload_fields=ERP_MAPPING.payload_fields,
    )
    with pytest.raises(MappingError, match="mock 或 待冻结"):
        normalize_event(tampered, MOCK_DEMAND_RECORD, received_at=RECEIVED_AT)


# ==========================================================================
# 二、不编造值：缺字段必须拒绝，不得补默认值
# ==========================================================================

def test_missing_required_field_is_rejected_not_defaulted():  # A11
    record = {
        "header": {
            key: value
            for key, value in MOCK_DEMAND_RECORD["header"].items()
            if key != "change_version"
        },
        "line": MOCK_DEMAND_RECORD["line"],
    }
    with pytest.raises(MappingError) as caught:
        normalize_erp_demand(record, received_at=RECEIVED_AT)
    assert caught.value.code == "MISSING_REQUIRED_FIELD"
    # 报错必须**点名**缺哪个字段——否则现场无法定位
    assert "change_version" in caught.value.message


def test_missing_required_field_error_names_every_gap():  # A11
    """多个字段缺失时要一次列全，不要让人反复试。"""
    with pytest.raises(MappingError) as caught:
        normalize_erp_demand({"header": {}, "line": {}}, received_at=RECEIVED_AT)
    message = caught.value.message
    for field in ("company_code", "doc_no", "change_version"):
        assert field in message, f"报错未点名缺失字段 {field}：{message}"


def test_optional_field_absence_is_recorded_as_unmapped_not_guessed():  # A11
    """非必填字段缺失 ⇒ 进 `unmapped` 清单，**不得**静默补值。"""
    record = {
        **MOCK_DEMAND_RECORD,
        "header": {
            key: value
            for key, value in MOCK_DEMAND_RECORD["header"].items()
            if key != "trace_id"
        },
    }
    result = normalize_erp_demand(record, received_at=RECEIVED_AT)
    assert "trace_id" in result.unmapped
    assert "trace_id" in result.envelope, "trace_id 应从权威标识派生，而不是留空"
    assert result.unmapped["trace_id"]


def test_source_system_and_identity_come_from_the_mapping_not_from_the_record():  # A11
    """`source_system` 必须来自适配器声明——让外部记录自称身份等于不设防。"""
    spoofed = {
        **MOCK_DEMAND_RECORD,
        "header": {**MOCK_DEMAND_RECORD["header"], "source_system": "erp-production"},
    }
    result = normalize_erp_demand(spoofed, received_at=RECEIVED_AT)
    assert result.envelope["source_system"] == "erp-mock"


# ==========================================================================
# 三、时区：本地时间必须显式给工厂时区
# ==========================================================================

def test_explicit_offset_is_normalized_to_utc_z():  # A11
    """带偏移的时间戳 → 归一到 UTC（契约要求 `Z` 结尾）。"""
    result = normalize_erp_demand(MOCK_DEMAND_RECORD, received_at=RECEIVED_AT)
    assert result.envelope["occurred_at"] == "2026-10-05T00:30:00Z"   # 08:30+08:00


def test_factory_local_time_without_timezone_is_rejected():  # A11
    """**没有时区信息的本地时间必须拒绝。**

    假设它是 UTC 会让事件整体平移数小时，而且**所有校验依然通过**——
    这是最危险的一类错误：不报错、查不出。
    """
    with pytest.raises(MappingError) as caught:
        normalize_erp_demand(MOCK_DEMAND_RECORD_LOCAL_TIME, received_at=RECEIVED_AT)
    assert caught.value.code == "TIMEZONE_REQUIRED"
    assert caught.value.external_field == "header.created_at"


def test_factory_local_time_with_timezone_is_converted_correctly():  # A11
    result = normalize_erp_demand(
        MOCK_DEMAND_RECORD_LOCAL_TIME,
        factory_timezone="Asia/Shanghai",
        received_at=RECEIVED_AT,
    )
    assert result.envelope["occurred_at"] == "2026-10-05T00:30:00Z"


def test_wrong_factory_timezone_produces_a_different_instant():  # A11
    """不同工厂时区必须给出**不同**的 UTC——证明时区真的参与了换算。

    若实现忽略时区参数，这两次结果会相同，而本条会失败。
    """
    shanghai = normalize_erp_demand(
        MOCK_DEMAND_RECORD_LOCAL_TIME, factory_timezone="Asia/Shanghai", received_at=RECEIVED_AT
    )
    utc = normalize_erp_demand(
        MOCK_DEMAND_RECORD_LOCAL_TIME, factory_timezone="UTC", received_at=RECEIVED_AT
    )
    assert shanghai.envelope["occurred_at"] != utc.envelope["occurred_at"]


def test_unknown_timezone_is_rejected():  # A11
    with pytest.raises(MappingError) as caught:
        to_utc("2026-10-05 08:30:00", factory_timezone="Mars/Olympus")
    assert caught.value.code == "UNKNOWN_TIMEZONE"


def test_bare_date_is_not_silently_treated_as_midnight_utc():  # A11
    """只给日期也必须要求时区——否则"当天 00:00"是凭空假定的。"""
    with pytest.raises(MappingError) as caught:
        to_utc("2026-10-05", factory_timezone=None)
    assert caught.value.code == "TIMEZONE_REQUIRED"


# ==========================================================================
# 四、单位：未登记的换算必须拒绝
# ==========================================================================

def test_registered_unit_conversion_is_applied():  # A07,A11
    result = normalize_erp_demand(MOCK_DEMAND_RECORD, received_at=RECEIVED_AT)
    assert result.payload["quantity_mm_equivalent"] == 125.0        # 12.5 cm
    assert result.payload["uom"] == "cm"                            # 原始单位保留以便对账


def test_temperature_uses_offset_not_a_factor_table():  # A11
    """华氏→摄氏含偏移，**不能**走倍率表。"""
    result = normalize_erp_demand(MOCK_DEMAND_RECORD, received_at=RECEIVED_AT)
    assert result.payload["process_temperature_c"] == pytest.approx(90.0)  # 194°F


def test_unregistered_conversion_is_rejected():  # A07,A11
    with pytest.raises(MappingError) as caught:
        to_unit(1.0, source_unit="furlong", target_unit="mm")
    assert caught.value.code == "UNKNOWN_SOURCE_UNIT"


def test_pure_factor_table_never_claims_fahrenheit():  # A11
    """倍率表里不得出现需要偏移的换算——那是"取近似"，会产生错误事实。"""
    assert UNIT_FACTORS[("fahrenheit", "c")] is None
    with pytest.raises(MappingError) as caught:
        to_unit(194.0, source_unit="fahrenheit", target_unit="c")
    assert caught.value.code == "UNSUPPORTED_CONVERSION"


def test_kelvin_is_not_silently_treated_as_celsius():  # A11
    with pytest.raises(MappingError) as caught:
        to_celsius(300.0, source_unit="K")
    assert caught.value.code == "UNKNOWN_TEMPERATURE_UNIT"


def test_weight_conversion_keeps_magnitude():  # A07,A11
    result = normalize_wms_stock_txn(MOCK_STOCK_TXN_RECORD, received_at=RECEIVED_AT)
    assert result.payload["quantity_g"] == 250500.0     # 250.5 kg
    assert result.payload["quantity_kg_raw"] == 250.5


# ==========================================================================
# 五、权威版本：适配层不得推断或累加
# ==========================================================================

@pytest.mark.parametrize(
    "name,normalize,record,expected",
    [
        ("erp", normalize_erp_demand, MOCK_DEMAND_RECORD, 3),
        ("pmc", normalize_pmc_schedule, MOCK_SCHEDULE_RECORD, 7),
        ("mes", normalize_mes_report, MOCK_REPORT_RECORD, 12),
        ("wms", normalize_wms_stock_txn, MOCK_STOCK_TXN_RECORD, 41),
    ],
)
def test_aggregate_version_is_taken_verbatim_from_the_source(name, normalize, record, expected):  # A07,A11
    """版本必须**原样**来自外部系统。"""
    result = normalize(record, received_at=RECEIVED_AT)
    assert result.envelope["aggregate_version"] == expected


def test_replaying_the_same_record_yields_the_same_event_id():  # A07,A11
    """同一条外部记录重放 ⇒ 同一个 `event_id`（否则重放会造出重复事实）。

    P2 要求"重复/乱序/中断恢复不破坏事实"：前提是**同一事实有稳定标识**。
    """
    first = normalize_erp_demand(MOCK_DEMAND_RECORD, received_at=RECEIVED_AT)
    second = normalize_erp_demand(MOCK_DEMAND_RECORD, received_at=RECEIVED_AT)
    assert first.envelope["event_id"] == second.envelope["event_id"]
    assert first.envelope["trace_id"] == second.envelope["trace_id"]


def test_different_versions_yield_different_event_ids():  # A07,A11
    """版本不同 ⇒ 事实不同 ⇒ 标识必须不同（否则新版本会被当成重复而丢弃）。"""
    base = normalize_erp_demand(MOCK_DEMAND_RECORD, received_at=RECEIVED_AT)
    bumped = normalize_erp_demand(
        {
            **MOCK_DEMAND_RECORD,
            "header": {**MOCK_DEMAND_RECORD["header"], "change_version": 4},
        },
        received_at=RECEIVED_AT,
    )
    assert base.envelope["event_id"] != bumped.envelope["event_id"]


def test_payload_hash_is_stable_and_content_sensitive():  # A11
    a = payload_hash({"b": 2, "a": 1})
    b = payload_hash({"a": 1, "b": 2})       # 键序不同，内容相同
    c = payload_hash({"a": 1, "b": 3})
    assert a == b, "同一内容必须得到同一个 hash"
    assert a != c, "内容变化必须改变 hash"


def test_result_payload_hash_matches_recomputation():  # A11
    result = normalize_wms_stock_txn(MOCK_STOCK_TXN_RECORD, received_at=RECEIVED_AT)
    recomputed = payload_hash(
        {key: value for key, value in result.payload.items() if key != "payload_hash"}
    )
    assert result.payload["payload_hash"] == recomputed


# ==========================================================================
# 六、领域语义：数量不自洽必须拒绝（§17 A11 的接入侧落点）
# ==========================================================================

def test_inconsistent_report_quantities_are_rejected():  # A11
    """合格 + 报废 ≠ 报工总数 ⇒ 拒绝，**不取其一**。

    静默取一个值会让账实差异一路流到库存与成本，而且再也查不出来。
    """
    with pytest.raises(MappingError) as caught:
        normalize_mes_report(MOCK_INCONSISTENT_REPORT_RECORD, received_at=RECEIVED_AT)
    assert caught.value.code == "REPORT_QUANTITY_MISMATCH"


def test_missing_report_total_is_rejected():  # A11
    record = {
        "workorder": {
            key: value
            for key, value in MOCK_REPORT_RECORD["workorder"].items()
            if key != "report_qty"
        }
    }
    with pytest.raises(MappingError) as caught:
        normalize_mes_report(record, received_at=RECEIVED_AT)
    assert caught.value.code == "MISSING_REPORT_TOTAL", (
        "没有总数就无法核验自洽——不能跳过校验直接接受"
    )


def test_negative_quantity_is_rejected():  # A11
    record = {
        "workorder": {
            **MOCK_REPORT_RECORD["workorder"],
            "good_qty": -1,
            "report_qty": 4,
            "scrap_qty": 5,
        }
    }
    with pytest.raises(MappingError) as caught:
        normalize_mes_report(record, received_at=RECEIVED_AT)
    assert caught.value.code == "NEGATIVE_QUANTITY"


def test_boolean_is_not_an_integer():  # A11
    """`True` 在 Python 里是 int 的子类——必须显式排除，否则会被当成 1。"""
    with pytest.raises(MappingError) as caught:
        to_int(True)
    assert caught.value.code == "NOT_AN_INTEGER"


# ==========================================================================
# 七、标识与对接清单
# ==========================================================================

def test_identifier_rejects_free_text():  # A11
    with pytest.raises(MappingError) as caught:
        to_identifier("含空格 的编号")
    assert caught.value.code == "NOT_AN_IDENTIFIER"


def test_adapter_refuses_unregistered_event_type():  # A11
    with pytest.raises(MappingError) as caught:
        normalize_erp_demand(
            MOCK_DEMAND_RECORD, event_type="erp.invented.type", received_at=RECEIVED_AT
        )
    assert caught.value.code == "UNKNOWN_EVENT_TYPE"


def test_each_integration_publishes_its_open_questions():  # A11
    """每个接入点必须**显式列出**要向对方索取什么。

    上游 §16.1 要求「在 handoff 清单记录依赖 owner、需要时间及阻塞阶段」——
    清单为空等于宣称「我们没有未决问题」，而真实字段/单位/时区尚未提供。
    """
    for name, checklist in (
        ("erp", REQUIRED_FROM_ERP),
        ("pmc", REQUIRED_FROM_PMC),
        ("mes", REQUIRED_FROM_MES),
        ("wms", REQUIRED_FROM_WMS),
    ):
        assert checklist, f"{name} 未列出对接清单"
        for topic, question in checklist.items():
            assert question.strip(), f"{name}.{topic} 的说明为空"
        # 清单必须覆盖时区与单位这两个最容易静默出错的维度
        assert any("时区" in topic for topic in checklist), f"{name} 清单未涉及时区"
