"""登记处与阶段门禁的隔离回归；所有文件变更只发生在临时契约目录。"""

import copy
import shutil
from itertools import count, islice

import pytest

from . import contract_inventory as inventory
from . import test_p0_contracts as p0
from . import test_p1_contracts as p1
from . import test_p15_contracts as p15
from . import test_p3_contracts as p3


def _unregistered_phases():
    """合成未登记阶段；不依赖当前阶段进度，也不限定候选名称数量。"""
    for number in count():
        phase = f"p{number}"
        if phase not in inventory.CONTRACT_INVENTORY:
            yield phase


UNKNOWN_PHASES = tuple(islice(_unregistered_phases(), 2))


GATES = (
    pytest.param(p0, "test_stage_inventory_is_complete", p0.Documents, id="p0"),
    pytest.param(p1, "test_p0_original_bytes_are_locked", p1.Documents, id="p1"),
    pytest.param(p15, "test_locked_inventory_is_complete", p15.docs.__wrapped__, id="p15"),
    pytest.param(p3, "test_new_stage_complete_and_no_baseline_overwrite", p3.registry.__wrapped__, id="p3"),
)


@pytest.fixture
def contract_copy(tmp_path, monkeypatch):
    root = tmp_path / "contracts"
    shutil.copytree(inventory.CONTRACT_ROOT, root)
    monkeypatch.setattr(inventory, "CONTRACT_ROOT", root)
    monkeypatch.setattr(inventory, "CONTRACT_INVENTORY", copy.deepcopy(inventory.CONTRACT_INVENTORY))
    for module in (p0, p1, p15, p3):
        monkeypatch.setattr(module, "CONTRACT", root)
    monkeypatch.setattr(p0, "READ_ROOTS", (root / "openapi", root / "schemas"))
    return root


@pytest.mark.parametrize("module,gate,loader", GATES)
@pytest.mark.parametrize("phase", UNKNOWN_PHASES)
def test_registered_future_stage_does_not_affect_earlier_gates(contract_copy, module, gate, loader, phase):
    assert phase not in inventory.CONTRACT_INVENTORY
    inventory.CONTRACT_INVENTORY[phase] = {
        "openapi": (f"{phase}.openapi.json",),
        "schemas": (f"{phase}-future.json",),
    }
    for relative in inventory.files_of(phase):
        # 新增阶段的内容由其自身门禁负责；旧阶段的加载器也不应解析这些文件。
        (contract_copy / relative).write_text("not JSON", encoding="utf-8")
    assert inventory.unregistered() == set()
    assert inventory.missing() == set()
    getattr(module, gate)()
    loader()


@pytest.mark.parametrize("module,gate,loader", GATES)
@pytest.mark.parametrize("relative", (
    f"openapi/{UNKNOWN_PHASES[0]}.openapi.json",
    f"schemas/{UNKNOWN_PHASES[0]}-future.json",
))
def test_unregistered_contract_is_rejected(contract_copy, module, gate, loader, relative):
    assert relative not in inventory.all_registered()
    (contract_copy / relative).write_text("{}", encoding="utf-8")
    assert inventory.unregistered() == {relative}
    with pytest.raises(AssertionError, match="存在未经阶段登记的契约文件"):
        getattr(module, gate)()


@pytest.mark.parametrize("module,gate,loader", GATES)
@pytest.mark.parametrize("relative", ("openapi/p3.openapi.json", "schemas/p3-saga.json"))
def test_missing_registered_contract_is_rejected(contract_copy, module, gate, loader, relative):
    (contract_copy / relative).unlink()
    assert inventory.missing() == {relative}
    with pytest.raises(AssertionError, match="清单登记了但磁盘缺失的契约文件"):
        getattr(module, gate)()


@pytest.mark.parametrize("module,gate,phase", (
    pytest.param(p1, "test_p0_original_bytes_are_locked", "p0", id="p0-baseline"),
    pytest.param(p1, "test_p0_original_bytes_are_locked", "p1", id="p1"),
    pytest.param(p15, "test_locked_inventory_is_complete", "p15", id="p15"),
    pytest.param(p3, "test_new_stage_complete_and_no_baseline_overwrite", "p3", id="p3"),
))
def test_registered_stage_file_without_gate_coverage_is_rejected(contract_copy, module, gate, phase):
    registered = inventory.all_registered()
    name = next(
        f"{phase}-uncovered-{number}.json"
        for number in count()
        if f"schemas/{phase}-uncovered-{number}.json" not in registered
    )
    entry = inventory.CONTRACT_INVENTORY[phase]
    entry["schemas"] += (name,)
    (contract_copy / "schemas" / name).write_text("{}", encoding="utf-8")
    assert inventory.unregistered() == set()
    assert inventory.missing() == set()
    with pytest.raises(AssertionError, match="文件清单与.*集合不一致"):
        getattr(module, gate)()
