"""
subagent.py
一次性同步子代理执行模块
特性：同步阻塞执行、用完即销毁、无常驻线程、无消息总线、无队友协议
可被主Agent / 常驻Teammate内部调用，执行专项文件、shell任务
"""

# 全局配置统一导入
from config import (
    WORKDIR, client, MODEL, DEFAULT_MAX_TOKENS
)


from hooks_permission import (
    trigger_hooks
)

# ===================== 子代理固定配置 =====================
SUB_SYSTEM_PROMPT = (
    f"You are a lightweight coding subagent, workspace root: {WORKDIR}.\n"
    "Complete the given task strictly with provided tools.\n"
    "Once finished, output only a concise final summary, do not ramble.\n"
    "FORBIDDEN: Do NOT spawn teammates, send cross-agent messages, call team/protocol tools."
)



# 子代理仅开放基础文件、shell工具，完全屏蔽队友、审批、定时、MCP等功能
SUB_TOOLS = [
    {
        "name": "bash",
        "description": "Execute shell command in workspace",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"]
        }
    },
    {
        "name": "read_file",
        "description": "Read file content",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "limit": {"type": "integer"},
                "offset": {"type": "integer"}
            },
            "required": ["path"]
        }
    },
    {
        "name": "write_file",
        "description": "Overwrite content to target file",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"}
            },
            "required": ["path", "content"]
        }
    },
    {
        "name": "edit_file",
        "description": "Replace one occurrence of old_text with new_text",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"}
            },
            "required": ["path", "old_text", "new_text"]
        }
    },
    {
        "name": "glob",
        "description": "Search files by glob pattern",
        "input_schema": {
            "type": "object",
            "properties": {"pattern": {"type": "string"}},
            "required": ["pattern"]
        }
    }
]
from tool_system import (
    run_bash,run_read,run_edit,run_write,run_glob
)
SUB_HANDLERS = {
    "bash": run_bash,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob
}

# ===================== 工具判断辅助函数 =====================
def extract_plain_text(content) -> str:
    """
        提取模型返回内容里的纯文本字符串
        适配模型返回的结构化content数组（text/tool_use对象列表），只拼接text类型内容
        """
    # 如果content不是列表结构，直接转为字符串并去除首尾空格返回
    if not isinstance(content, list):
        return str(content).strip()
    # 存放所有文本片段
    text_blocks = []
    # 遍历模型返回的每一块内容对象
    for block in content:
        # 安全读取type属性，仅收集文本类型的内容
        if getattr(block, "type", None) == "text":
            text_blocks.append(block.text)
    # 所有文本用换行拼接，整体去除首尾空格后返回
    return "\n".join(text_blocks).strip()

def spawn_subagent(task_description: str) -> str:
    """
    同步启动一次性子代理
    :param task_description: 目标任务描述
    :return: 任务最终总结文本
    最大30轮工具循环，执行完成自动销毁上下文，无后台常驻
    """
    from tool_system import (
        has_tool_use, call_tool_handler
    )
    messages = [{"role": "user", "content": task_description}]
    max_loop_count = 30

    for _ in range(max_loop_count):
        response = client.messages.create(
            model=MODEL,
            system=SUB_SYSTEM_PROMPT,
            messages=messages,
            tools=SUB_TOOLS,
            max_tokens=DEFAULT_MAX_TOKENS
        )
        messages.append({"role": "assistant", "content": response.content})

        # 无工具调用，任务结束
        if not has_tool_use(response.content):
            break

        tool_result_list = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            # 前置权限钩子校验
            deny_result = trigger_hooks("PreToolUse", block)
            if deny_result is not None:
                output = str(deny_result)
            else:
                handler = SUB_HANDLERS.get(block.name)
                output = call_tool_handler(handler, block.input, block.name)
                trigger_hooks("PostToolUse", block, output)

            tool_result_list.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": str(output)
            })

        messages.append({"role": "user", "content": tool_result_list})

    # 反向读取最后一轮回答作为结果
    final_summary = "Subagent finished without valid conclusion."
    for msg in reversed(messages):
        if msg["role"] == "assistant":
            text = extract_plain_text(msg["content"])
            if text:
                final_summary = text
                break
    return final_summary