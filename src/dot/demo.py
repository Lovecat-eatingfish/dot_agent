import requests

def parse_sse_data(raw_text: str):
    """解析Streamable‑HTTP返回的SSE文本，提取所有data行的json字符串"""
    lines = raw_text.splitlines()
    data_list = []
    for line in lines:
        if line.startswith("data:"):
            json_str = line.removeprefix("data:").strip()
            if json_str:
                data_list.append(json_str)
    return data_list


MCP_URL = "https://mcpmarket.cn/mcp/bbf80d76af234e27d7cb367f"

# ✅ 服务端强制：必须同时带上两个accept
common_headers = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream"
}

init_payload = {
    "jsonrpc": "2.0",
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {"tools": {}},
        "clientInfo": {"name": "py-client", "version": "1.0.0"}
    },
    "id": 1
}

resp_init = requests.post(MCP_URL, json=init_payload, headers=common_headers)
session_id = resp_init.headers.get("Mcp-Session-Id")

print(f"status_code={resp_init.status_code}")
print(f"session_id: {session_id}")
print("raw response text:\n", repr(resp_init.text))

if not session_id:
    raise RuntimeError("未获取 Mcp‑Session‑Id，握手失败")

# 从SSE data行取出rpc消息
sse_data_lines = parse_sse_data(resp_init.text)
import json
for s in sse_data_lines:
    rpc_msg = json.loads(s)
    print(f"initialize rpc message: {rpc_msg}")

session_headers = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "Mcp-Session-Id": session_id
}

# Step2 notifications/initialized 通知
notify_payload = {
    "jsonrpc": "2.0",
    "method": "notifications/initialized",
    "params": {}
}
resp_notify = requests.post(MCP_URL, json=notify_payload, headers=session_headers)
print(f"\ninitialized通知 status={resp_notify.status_code}")

# Step3 tools/list
list_payload = {
    "jsonrpc": "2.0",
    "method": "tools/list",
    "params": {},
    "id": 2
}
resp_tools = requests.post(MCP_URL, json=list_payload, headers=session_headers)
print("\n==== tools/list raw ====")
raw = resp_tools.text
print(repr(raw))
for s in parse_sse_data(raw):
    msg = json.loads(s)
    print("tools/list rpc result:", msg)
