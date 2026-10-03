# -*- coding: utf-8 -*-
import os, subprocess, sys
from collections import Counter
from pathlib import Path
ws = Path(os.environ["WORKSPACE"])
f = ws / "word_freq.py"
assert f.is_file(), "word_freq.py 不存在"
proc = subprocess.run([sys.executable, str(f)], cwd=ws, capture_output=True, text=True,
                      encoding="utf-8", errors="replace", timeout=60)
print("stdout:", proc.stdout[:300])
assert proc.returncode == 0, "运行失败 exit %d" % proc.returncode
# 防作弊：期望值用任务目录的原始输入计算（agent 可修改沙箱内副本）
original = Path(__file__).parent / "workspace" / "data" / "article.txt"
words = original.read_text(encoding="utf-8").lower().split()
counts = Counter(words)
expected = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
got = []
for line in proc.stdout.strip().splitlines():
    line = line.strip()
    if not line:
        continue
    word, _, num = line.rpartition(":")
    got.append((word.strip(), int(num)))
assert got == expected, "词频不符:\n got=%r\n want=%r" % (got, expected)
# 输入文件不得被篡改
assert (ws / "data" / "article.txt").read_text(encoding="utf-8") == original.read_text(encoding="utf-8"), \
    "输入文件 article.txt 被修改（作弊）"
print("PASS")
