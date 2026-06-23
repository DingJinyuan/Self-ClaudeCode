import time
from dataclasses import dataclass, field
from typing import List, Callable, Dict


@dataclass
class CronJob:
    id: str          # 任务唯一标识
    cron: str        # Cron 标准定时表达式
    prompt: str      # 到时要执行的指令/提示词
    recurring: bool  # 是否循环重复执行
    durable: bool    # 是否持久化保存（重启不丢失

@dataclass
class ProtocolState:
    """协议请求状态实体，保存待审批的请求信息"""
    request_id: str       # 请求唯一编号
    type: str             # 请求类型 shutdown / plan_approval
    sender: str           # 请求发起方
    target: str           # 请求接收方
    status: str           # pending / approved / rejected
    payload: str          # 请求附带内容（plan方案文本）
    created_at: float = field(default_factory=time.time)

class MCPClient:
    """MCP客户端封装，负责服务工具注册与远程工具调用（教学版Mock实现）"""
    def __init__(self, name: str):
        self.name = name
        self.tools: List[dict] = []
        self._handlers: Dict[str, Callable] = {}

    def register(self, tool_defs: List[dict], handlers: Dict[str, Callable]):
        """注册当前服务的工具定义与本地模拟执行函数"""
        self.tools = tool_defs
        self._handlers = handlers

    def call_tool(self, tool_name: str, args: dict) -> str:
        """执行MCP工具调用"""
        handler = self._handlers.get(tool_name)
        if not handler:
            return f"MCP error: unknown tool '{tool_name}'"
        try:
            return handler(** args)
        except Exception as e:
            return f"MCP error: {str(e)}"