from app.infrastructure.usage.context_breakdown_calculator import ContextBreakdownCalculatorImpl


def test_compute_breakdown_basic():
    calc = ContextBreakdownCalculatorImpl()
    sp = "You are a helpful assistant."
    tools = [{"type": "function", "function": {"name": "calc", "parameters": {}}}]
    msgs = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi there"}]
    b = calc.compute(sp, tools, msgs, "")
    assert b.system_prompt > 0
    assert b.tool_definitions > 0
    assert b.conversation > 0
    assert b.memory == 0
    assert b.total == b.system_prompt + b.tool_definitions + b.memory + b.conversation


def test_compute_breakdown_with_memory():
    calc = ContextBreakdownCalculatorImpl()
    b = calc.compute("sys", [], [{"role": "user", "content": "hi"}], "<memory>fact</memory>")
    assert b.memory > 0
    assert b.total > 0
