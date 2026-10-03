# -*- coding: utf-8 -*-
import os
from pathlib import Path
ws = Path(os.environ["WORKSPACE"])
f = ws / "answer.md"
assert f.is_file(), "answer.md 未创建"
text = f.read_text(encoding="utf-8")
assert "85" in text, "未回答最大工作温度（应为 85°C）"
assert "Falcon" in text, "未回答通信总线（应为 Falcon）"
print("PASS")
