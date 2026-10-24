"""修复 P1 契约里 `context` 节点的写法缺陷。

问题：`p1-*.json` 里 12 处把 context 写成
    {"$ref": "common-context.json", "properties": {...}, "description": "..."}
JSON Schema 2020-12 规定：与 `$ref` **同级**的关键字一律被忽略。
于是被引用的 common-context 生效（additionalProperties:false + 九键 required），
而同级的 properties 完全不起作用 —— 该节点**没有任何实例可满足**：
- 只给 factory_id/task_id → 缺 7 个 required 键；
- 给全部 9 键 → 被 additionalProperties:false 拒绝。

修法：改成 allOf 覆盖式表达
    {"allOf": [{"$ref": "common-context.json"},
               {"properties": {"factory_id": {"type": "string", "minLength": 1},
                               "task_id": {"type": "string", "minLength": 1}}}]}
语义与 P1 SPEC §2「工厂与 task 在本次工厂只读链路必需，不能以默认值补全」一致，
且不改变 P0 契约（P0 那 12 个文件的哈希仍由 P1 门禁锁定）。

只改 `$ref` + `properties` 同级这一种形态；其它写法（纯 properties 的条件分支）不动。
"""

from __future__ import annotations

import json
import pathlib
import sys

SCHEMAS = pathlib.Path(__file__).resolve().parents[1] / "packages" / "contracts" / "schemas"
TARGETS = ("p1-classification.json", "p1-governance.json", "p1-token-vault.json", "p1-readonly.json")


def fix(node: dict, *, path: str, changed: list[str]) -> dict:
    if not isinstance(node, dict):
        return node
    if "$ref" in node and "properties" in node and len(node) <= 3:
        # 这就是问题形态：$ref + 同级 properties（+ description）
        description = node.get("description")
        wrapper = {
            "allOf": [
                {"$ref": node["$ref"]},
                {"properties": node["properties"]},
            ]
        }
        if description:
            wrapper["description"] = description
        changed.append(path)
        return wrapper
    out = {}
    for key, value in node.items():
        if isinstance(value, dict):
            out[key] = fix(value, path=f"{path}/{key}", changed=changed)
        elif isinstance(value, list):
            out[key] = [
                fix(item, path=f"{path}/{key}/{index}", changed=changed)
                if isinstance(item, dict) else item
                for index, item in enumerate(value)
            ]
        else:
            out[key] = value
    return out


def main() -> int:
    total = 0
    for name in TARGETS:
        target = SCHEMAS / name
        document = json.loads(target.read_text(encoding="utf-8"))
        changed: list[str] = []
        fixed = fix(document, path=name, changed=changed)
        if changed:
            target.write_text(json.dumps(fixed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"{name}: 修复 {len(changed)} 处")
            for item in changed:
                print(f"    {item}")
            total += len(changed)
        else:
            print(f"{name}: 无需修复")
    print(f"合计修复 {total} 处")
    return 0


if __name__ == "__main__":
    sys.exit(main())
