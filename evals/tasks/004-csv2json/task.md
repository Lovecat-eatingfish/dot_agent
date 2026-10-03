工作目录 data/people.csv 是 CSV 文件（首行表头）。创建 csv2json.py，提供
convert(src, dst)：把 CSV 转成 JSON 数组写入 dst，所有值保持字符串类型，
UTF-8 编码。使 test_csv2json.py 全部通过。运行 pytest 验证。不要修改测试。
