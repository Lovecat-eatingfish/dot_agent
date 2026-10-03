# -*- coding: utf-8 -*-
"""判分按语义而非拼写：接受 caculator.py / calculator.py 等合理命名"""
import importlib.util, os, sys
from pathlib import Path
ws = Path(os.environ["WORKSPACE"])
candidates = [p for p in ws.glob("*ulator*.py") if p.is_file()]
assert candidates, "未找到计算器模块（*ulator*.py）"
spec = importlib.util.spec_from_file_location("calc_under_test", candidates[0])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
assert mod.add(2, 3) == 5
assert mod.sub(5, 2) == 3
assert mod.mul(3, 4) == 12
assert mod.div(6, 3) == 2
try:
    mod.div(1, 0)
    raise AssertionError("除零未抛异常")
except ValueError:
    pass
print("PASS")
