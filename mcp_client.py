"""
mcp_client.py
MCP客户端管理模块
功能：MCP服务连接管理、工具发现、工具调用、命名规范化、内置Mock服务（docs/deploy）
依赖：config.py
"""
from typing import List, Dict, Callable

from config import _DISALLOWED_CHARS, mcp_clients, BUILTIN_TOOLS, BUILTIN_HANDLERS


from models import MCPClient



def normalize_mcp_name(name: str) -> str:
    """名称标准化，非法字符统一替换为下划线，用于生成 mcp__server__tool 工具名"""
    return _DISALLOWED_CHARS.sub('_', name)

# ===================== Mock 内置MCP服务（原版自带docs、deploy模拟服务） =====================
def _mock_server_docs() -> MCPClient:
    """文档查询Mock服务"""
    client = MCPClient("docs")
    client.register(
        tool_defs=[
            {
                "name": "search",
                "description": "Search documentation. (readOnly)",
                "inputSchema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"]
                }
            },
            {
                "name": "get_version",
                "description": "Get API version. (readOnly)",
                "inputSchema": {"type": "object", "properties": {}, "required": []}
            },
        ],
        handlers={
            "search": lambda query: f"[docs] Found 3 results for '{query}'",
            "get_version": lambda: "[docs] API v2.1.0",
        }
    )
    return client

def _mock_server_deploy() -> MCPClient:
    """部署发布Mock服务（高危操作，配合权限钩子）"""
    client = MCPClient("deploy")
    client.register(
        tool_defs=[
            {
                "name": "trigger",
                "description": "Trigger a deployment. (destructive — requires approval)",
                "inputSchema": {
                    "type": "object",
                    "properties": {"service": {"type": "string"}},
                    "required": ["service"]
                }
            },
            {
                "name": "status",
                "description": "Check deployment status. (readOnly)",
                "inputSchema": {
                    "type": "object",
                    "properties": {"service": {"type": "string"}},
                    "required": ["service"]
                }
            },
        ],
        handlers={
            "trigger": lambda service: f"[deploy] Triggered deployment: {service}",
            "status": lambda service: f"[deploy] {service}: running (v1.4.2)",
        }
    )
    return client

# 内置可用Mock服务注册表
MOCK_SERVERS = {
    "docs": _mock_server_docs,
    "deploy": _mock_server_deploy,
}


def connect_mcp(name: str) -> str:
    """
    连接指定MCP服务，生成客户端实例存入全局mcp_clients
    :param name: 服务名称
    :return: 连接结果文本，成功/失败的中文英文提示信息
    """
    # 1. 防重复判断：如果该服务已经连接，直接返回提示，不会重复创建客户端
    if name in mcp_clients:
        return f"MCP server '{name}' already connected"

    # 2. 从工厂注册表，取出对应服务的构造函数
    factory = MOCK_SERVERS.get(name)
    # 校验：输入的服务名不存在
    if not factory:
        # 取出所有可用的服务名称，拼接提示返回
        available = ", ".join(MOCK_SERVERS.keys())
        return f"Unknown server '{name}'. Available servers: {available}"

    # 3. 执行工厂函数，实例化MCP客户端对象
    mcp_client = factory()
    # 存入全局客户端池，后续随时通过name读取使用这个客户端
    mcp_clients[name] = mcp_client

    # 4. 自动提取当前服务暴露的全部工具名称
    tool_names = [t["name"] for t in mcp_client.tools]
    # 控制台红色日志打印，展示连接成功 + 扫描到的工具列表
    print(f"  \033[31m[mcp] connected: {name} → discovered tools: {tool_names}\033[0m")

    # 5. 拼接返回结果字符串，告知调用方连接成功、工具数量、工具名
    return (
        f"Connected to MCP server '{name}'. "
        f"Total discovered tools: {len(mcp_client.tools)}, tool list: {', '.join(tool_names)}"
    )
def assemble_tool_pool() -> tuple[List[dict], Dict]:
    """
    合并【内置基础工具 + 全部已连接MCP工具】
    MCP工具自动前缀命名规则：mcp__服务名__工具名
    返回完整工具定义列表 + 对应的执行handler映射字典
    """
    # 第一步：初始化，先完整复制内置工具与处理器，作为工具池基底
    tools = list(BUILTIN_TOOLS)
    handlers = dict(BUILTIN_HANDLERS)

    # 遍历所有已经成功连接的MCP服务
    for server_name, mcp_client in mcp_clients.items():
        # 清洗服务名称，规避非法字符，保证命名安全
        safe_server = normalize_mcp_name(server_name)
        # 遍历当前MCP服务暴露的每一个原生工具
        for tool_def in mcp_client.tools:
            safe_tool = normalize_mcp_name(tool_def["name"])
            # 全局唯一命名格式：mcp__服务名__工具名
            full_tool_name = f"mcp__{safe_server}__{safe_tool}"

            # 把MCP工具包装成模型可识别的标准tool schema，加入总工具列表
            tools.append({
                "name": full_tool_name,
                "description": tool_def.get("description", ""),
                "input_schema": tool_def.get("inputSchema", {}),
            })

            # 绑定闭包handler：执行时转发调用对应MCP客户端的call_tool方法
            handlers[full_tool_name] = (
                lambda *, c=mcp_client, t=tool_def["name"], **kw: c.call_tool(t, kw)
            )
    # 返回成品工具池：工具描述列表 + 执行处理器字典
    return tools, handlers

def run_connect_mcp(name: str) -> str:
    return connect_mcp(name)