工作目录有 test_json_utils.py 测试。创建 json_utils.py，实现：
- save_json(data, path)：把对象写为 UTF-8 JSON 文件，ensure_ascii=False（中文原样保存）
- load_json(path)：读取并返回对象
使 pytest 全部通过。运行 pytest 验证。不要修改测试文件。
