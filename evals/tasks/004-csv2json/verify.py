# -*- coding: utf-8 -*-
"""判分：pytest 全绿 + 测试文件未被修改"""
import hashlib, os, subprocess, sys
from pathlib import Path

ws = Path(os.environ["WORKSPACE"])
proc = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=ws,
                      capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
print(proc.stdout[-800:])
assert proc.returncode == 0, "pytest failed (exit %d)" % proc.returncode

for test_file in ['test_csv2json.py']:
    src = Path(__file__).parent / "workspace" / test_file
    dst = ws / test_file
    same = hashlib.sha256(src.read_bytes()).hexdigest() == hashlib.sha256(dst.read_bytes()).hexdigest()
    assert same, "测试文件被修改: " + test_file
print("PASS")
