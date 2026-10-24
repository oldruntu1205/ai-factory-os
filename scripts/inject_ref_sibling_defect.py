"""把 p1-classification 的 context 节点改回"$ref + 同级 properties"的缺陷写法。

仅用于验证 tests/contracts/test_contract_meta_validation.py 能否捕获该类缺陷；
用法：python inject_ref_sibling_defect.py <restore|inject>
"""

from __future__ import annotations

import json
import pathlib
import sys

TARGET = pathlib.Path(__file__).resolve().parents[1] / "packages" / "contracts" / "schemas" / "p1-classification.json"
BACKUP = pathlib.Path(__file__).resolve().parents[1] / "packages" / "contracts" / ".p1-classification.bak"


def inject() -> None:
    BACKUP.write_bytes(TARGET.read_bytes())
    document = json.loads(TARGET.read_text(encoding="utf-8"))
    document["properties"]["context"] = {
        "$ref": "common-context.json",
        "properties": {"factory_id": {"type": "string", "minLength": 1}, "task_id": {"type": "string", "minLength": 1}},
    }
    TARGET.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("已注入缺陷")


def restore() -> None:
    assert BACKUP.is_file(), "没有备份可还原"
    TARGET.write_bytes(BACKUP.read_bytes())
    BACKUP.unlink()
    print("已还原")


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "inject"
    {"inject": inject, "restore": restore}[action]()
