"""
tests/test_llm_gateway.py —— LLM 网关连通性测试

- 验证 DeepSeek 路由配置正确
- 有 API Key 时做真实调用（deepseek-chat）
- 无 Key 时验证 Mock 降级
运行：python tests/test_llm_gateway.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {extra}")


def main():
    print("=" * 60)
    print("LLM 网关测试（DeepSeek）")
    print("=" * 60)

    import config
    check("网关默认 deepseek", config.LLM_GATEWAY == "deepseek", config.LLM_GATEWAY)
    check("模型 deepseek-chat", config.LLM_MODEL == "deepseek-chat", config.LLM_MODEL)

    from ai.llm import GATEWAY_ROUTES, _resolve_route
    base, model = _resolve_route()
    check("路由解析到 DeepSeek", base == "https://api.deepseek.com/v1", base)
    check("模型解析", model == "deepseek-chat", model)

    from ai.llm import get_llm, is_mock

    if config.LLM_API_KEY:
        print("\n[真实调用] 已配置 API Key，测试 deepseek-chat 对话...")
        llm = get_llm()
        try:
            result = llm.invoke("请用一句话介绍你自己")
            content = result.content if hasattr(result, "content") else str(result)
            check("DeepSeek 真实响应", len(content) > 0, content[:100])
            check("非 Mock 模式", is_mock() is False)
            print(f"      响应: {content[:80]}...")
        except Exception as e:
            check(f"DeepSeek 调用失败: {e}", False, str(e)[:200])
    else:
        print("\n[降级模式] 未配置 LLM_API_KEY，验证 Mock 降级...")
        llm = get_llm()
        check("MockLLM 降级", is_mock() is True)
        result = llm.invoke("测试")
        check("Mock 可响应", len(result.content) > 0)
        print("      提示：在 backend/.env 填入 LLM_API_KEY 后运行本测试即真实调用")

    print("\n" + "=" * 60)
    print(f"测试结果: {PASS} 通过, {FAIL} 失败")
    print("=" * 60)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
