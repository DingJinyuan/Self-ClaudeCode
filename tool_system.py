"""
tool_system.py
全局工具系统核心模块
1. 内置本地工具：bash、read_file、write_file，自带路径安全校验
2. 工具执行入口、参数动态调用封装 call_tool_handler
3. 超大输出落盘持久化逻辑，避免上下文溢出
4. 工具结果解析工具调用判断 has_tool_use
"""
import  ast,glob,subprocess
import json
from pathlib import Path
from typing import Dict, Any, Callable, Tuple, Optional

from task_worktree import run_create_task,run_list_tasks,run_get_task,run_claim_task, run_complete_task,run_create_worktree,run_keep_worktree,run_remove_worktree
from cron_scheduler import run_list_crons,run_cancel_cron,run_schedule_cron
from  teammate_runtime import run_spawn_teammate
from protocol_core import  run_request_shutdown,run_request_plan,run_review_plan, run_check_inbox
from message_bus import run_send_message
from mcp_client import  run_connect_mcp
from skill_manager import load_skill
from config import (
    WORKDIR, TOOL_RESULTS_DIR, PERSIST_THRESHOLD
)

import config


def safe_path(p: str, cwd: Optional[Path] = None) -> Path:
    """
    路径安全校验，防止路径逃逸
    文件工具强制锁定工作区/worktree目录，bash交由权限钩子管控
    """
    base = cwd or WORKDIR
    path = (base / p).resolve()
    if not path.is_relative_to(base):
        raise ValueError(f"Path escapes workspace: {p}")
    return path

def persist_large_output(tool_use_id: str, output: str) -> str:
    """超长工具结果落盘持久化，超限写入文件，上下文返回预览+路径"""
    if len(output) <= PERSIST_THRESHOLD:
        return output
    TOOL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    save_path = TOOL_RESULTS_DIR / f"{tool_use_id}.txt"
    if not save_path.exists():
        save_path.write_text(output, encoding="utf-8")
    preview = output[:2000]
    return (
        f"<persisted-output>\nFull output file: {save_path}\nPreview:\n{preview}\n</persisted-output>"
    )

def call_tool_handler(handler: Optional[Callable], args: Dict[str, Any], name: str) -> str:
    """通用工具执行分发器，自动解包参数，统一捕获调用异常"""
    if not handler:
        return f"Unknown tool: {name}"
    try:
        return handler(** (args or {}))
    except TypeError as e:
        return f"Tool Argument Error[{name}]: {str(e)}"
    except Exception as e:
        return f"Tool Runtime Error[{name}]: {str(e)}"

def has_tool_use(content) -> bool:
    """判断模型返回内容是否存在tool_use工具调用块"""
    return any(getattr(block, "type", None) == "tool_use" for block in content)

# ---------------------- 内置基础工具实现 ----------------------
def run_bash(command: str, cwd: Optional[Path] = None, run_in_background: bool = False) -> str:
    """执行Shell命令，run_in_background仅由上层调度判断后台执行，本函数直接同步运行"""
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=cwd or WORKDIR,
            capture_output=True,
            text=True,
            timeout=120
        )
        full_out = (proc.stdout + proc.stderr).strip()
        return full_out[:50000] if full_out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Bash command timeout (120s)"
    except Exception as e:
        return f"Bash Exception: {str(e)}"


def run_read(path: str, limit: Optional[int] = None, offset: int = 0, cwd: Optional[Path] = None) -> str:
    """读取文件，支持行偏移、行数限制"""
    try:
        full_path = safe_path(path, cwd)
        lines = full_path.read_text(encoding="utf-8").splitlines()
        offset = max(int(offset or 0), 0)
        lines = lines[offset:]
        if limit is not None and limit < len(lines):
            remain_count = len(lines) - limit
            lines = lines[:limit] + [f"... ({remain_count} more lines omitted)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Read File Error: {str(e)}"


def run_write(path: str, content: str, cwd: Optional[Path] = None) -> str:
    """写入/覆盖文件，自动创建父目录"""
    try:
        full_path = safe_path(path, cwd)
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")
        return f"Write Success: Saved {len(content)} bytes to {path}"
    except Exception as e:
        return f"Write File Error: {str(e)}"


def run_edit(path: str, old_text: str, new_text: str, cwd: Optional[Path] = None) -> str:
    """精准单次文本替换编辑文件"""
    try:
        full_path = safe_path(path, cwd)
        text = full_path.read_text(encoding="utf-8")
        if old_text not in text:
            return f"Edit Error: Target text not found in {path}"
        new_content = text.replace(old_text, new_text, 1)
        full_path.write_text(new_content, encoding="utf-8")
        return f"Edit Success: Modified {path}"
    except Exception as e:
        return f"Edit File Error: {str(e)}"


def run_glob(pattern: str, cwd: Optional[Path] = None) -> str:
    """Glob 文件匹配，严格限制在工作目录内，禁止跨路径匹配"""
    try:
        base = cwd or WORKDIR
        match_list = []
        for match_name in glob.glob(pattern, root_dir=base):
            resolve_p = (base / match_name).resolve()
            if resolve_p.is_relative_to(base):
                match_list.append(match_name)
        return "\n".join(match_list) if match_list else "(no files matched)"
    except Exception as e:
        return f"Glob Error: {str(e)}"


# ----------------------Todo 任务清单工具 ----------------------
def _normalize_todos(todos) -> Tuple[Optional[list], Optional[str]]:
    """
    Todo入参格式化校验，支持字符串json/字面量列表转对象
    入参兼容两种格式：1. JSON字符串数组  2. Python字面量列表字符串  3. 原生list对象
    返回值：(格式化后的合法todo列表, 错误信息)
    成功：(todo列表, None)
    失败：(None, 具体错误描述)
    """
    # 第一步：如果传入的是字符串，尝试解析转为列表对象
    if isinstance(todos, str):
        # 优先标准json解析
        try:
            todos = json.loads(todos)
        except json.JSONDecodeError:
            # JSON解析失败，尝试解析Python字面量（兼容单引号、元组等Python写法）
            try:
                todos = ast.literal_eval(todos)
            except (SyntaxError, ValueError):
                # 两种解析全部失败，直接返回错误
                return None, "Error: todos must be JSON array string or list object"

    # 第二步：顶层类型校验，必须是list数组
    if not isinstance(todos, list):
        return None, "Error: todos must be a list array"

    # 定义允许的任务状态枚举
    valid_status = ("pending", "in_progress", "completed")
    # 遍历每一项todo，逐字段严格校验
    for idx, todo_item in enumerate(todos):
        # 单条任务必须为字典格式
        if not isinstance(todo_item, dict):
            return None, f"Error: todos[{idx}] must be dictionary object"
        # 校验必填字段：必须包含content(任务内容)、status(任务状态)
        if "content" not in todo_item or "status" not in todo_item:
            return None, f"Error: todos[{idx}] missing required key 'content' or 'status'"
        # 校验状态值，只能是指定的三种状态
        if todo_item["status"] not in valid_status:
            return None, f"Error: todos[{idx}] invalid status '{todo_item['status']}', allowed: {valid_status}"

    # 全部校验通过，返回格式化后的列表，无错误信息
    return todos, None

def run_todo_write(todos: list) -> str:
    """更新全局Todo列表"""

    data, err_msg = _normalize_todos(todos)
    if err_msg:
        return err_msg
    config.CURRENT_TODOS = data
    todo_list = config.CURRENT_TODOS
    print(f"  \033[33m[todo] Updated total {len(todo_list)} todo items\033[0m")
    return f"Todo Update Complete, total items: {len(todo_list)}"



# ── Tool Definitions ──

# The model sees tool schemas; Python executes handlers.  yuan keeps both tables
# explicit so every added capability is visible in one place.
# tool_system.py
config.BUILTIN_TOOLS.clear()
config.BUILTIN_HANDLERS.clear()

config.BUILTIN_TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object",
                      "properties": {"command": {"type": "string"},
                                     "run_in_background": {"type": "boolean"}},
                      "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "limit": {"type": "integer"},
                                     "offset": {"type": "integer"}},
                      "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "content": {"type": "string"}},
                      "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file once.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "old_text": {"type": "string"},
                                     "new_text": {"type": "string"}},
                      "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": "Find files matching a glob pattern.",
     "input_schema": {"type": "object",
                      "properties": {"pattern": {"type": "string"}},
                      "required": ["pattern"]}},
    {"name": "todo_write",
     "description": "Create and manage a task list for the current session.",
     "input_schema": {"type": "object",
                      "properties": {"todos": {"type": "array",
                          "items": {"type": "object",
                                    "properties": {
                                        "content": {"type": "string"},
                                        "status": {"type": "string",
                                                   "enum": ["pending", "in_progress", "completed"]}},
                                    "required": ["content", "status"]}}},
                      "required": ["todos"]}},
    {"name": "task",
     "description": "Launch a focused subagent. Returns only its final summary.",
     "input_schema": {"type": "object",
                      "properties": {"description": {"type": "string"}},
                      "required": ["description"]}},
    {"name": "load_skill",
     "description": "Load the full content of a skill by name.",
     "input_schema": {"type": "object",
                      "properties": {"name": {"type": "string"}},
                      "required": ["name"]}},
    {"name": "compact",
     "description": "Summarize earlier conversation and continue with compacted context.",
     "input_schema": {"type": "object",
                      "properties": {"focus": {"type": "string"}},
                      "required": []}},
    {"name": "create_task", "description": "Create a task.",
     "input_schema": {"type": "object",
                      "properties": {"subject": {"type": "string"},
                                     "description": {"type": "string"},
                                     "blockedBy": {"type": "array",
                                                   "items": {"type": "string"}}},
                      "required": ["subject"]}},
    {"name": "list_tasks", "description": "List all tasks.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "get_task", "description": "Get full task details.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
    {"name": "claim_task", "description": "Claim a pending task.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
    {"name": "complete_task", "description": "Complete an in-progress task.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
    {"name": "schedule_cron",
     "description": ("Schedule a cron job. cron is 5-field: min hour dom "
                     "month dow. For one-shot reminders, compute the target "
                     "minute and set recurring=false."),
     "input_schema": {"type": "object",
                      "properties": {"cron": {"type": "string"},
                                     "prompt": {"type": "string"},
                                     "recurring": {"type": "boolean"},
                                     "durable": {"type": "boolean"}},
                      "required": ["cron", "prompt"]}},
    {"name": "list_crons", "description": "List registered cron jobs.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "cancel_cron", "description": "Cancel a cron job by ID.",
     "input_schema": {"type": "object",
                      "properties": {"job_id": {"type": "string"}},
                      "required": ["job_id"]}},
    {"name": "spawn_teammate", "description": "Spawn an autonomous teammate.",
     "input_schema": {"type": "object",
                      "properties": {"name": {"type": "string"},
                                     "role": {"type": "string"},
                                     "prompt": {"type": "string"}},
                      "required": ["name", "role", "prompt"]}},
    {"name": "send_message", "description": "Send message to a teammate.",
     "input_schema": {"type": "object",
                      "properties": {"to": {"type": "string"},
                                     "content": {"type": "string"}},
                      "required": ["to", "content"]}},
    {"name": "check_inbox",
     "description": "Check inbox for messages and protocol responses.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "request_shutdown",
     "description": "Request a teammate to shut down.",
     "input_schema": {"type": "object",
                      "properties": {"teammate": {"type": "string"}},
                      "required": ["teammate"]}},
    {"name": "request_plan",
     "description": "Ask a teammate to submit a plan.",
     "input_schema": {"type": "object",
                      "properties": {"teammate": {"type": "string"},
                                     "task": {"type": "string"}},
                      "required": ["teammate", "task"]}},
    {"name": "review_plan",
     "description": "Approve or reject a submitted plan.",
     "input_schema": {"type": "object",
                      "properties": {"request_id": {"type": "string"},
                                     "approve": {"type": "boolean"},
                                     "feedback": {"type": "string"}},
                      "required": ["request_id", "approve"]}},
    {"name": "create_worktree",
     "description": "Create an isolated git worktree.",
     "input_schema": {"type": "object",
                      "properties": {"name": {"type": "string"},
                                     "task_id": {"type": "string"}},
                      "required": ["name"]}},
    {"name": "remove_worktree",
     "description": "Remove a worktree. Refuses if changes exist.",
     "input_schema": {"type": "object",
                      "properties": {"name": {"type": "string"},
                                     "discard_changes": {"type": "boolean"}},
                      "required": ["name"]}},
    {"name": "keep_worktree",
     "description": "Keep a worktree for manual review.",
     "input_schema": {"type": "object",
                      "properties": {"name": {"type": "string"}},
                      "required": ["name"]}},
    {"name": "connect_mcp",
     "description": "Connect to an MCP server (docs, deploy) and discover tools.",
     "input_schema": {"type": "object",
                      "properties": {"name": {"type": "string"}},
                      "required": ["name"]}},
]
# 定义懒加载函数，只有工具真正被调用时，才导入sub_agent
def lazy_spawn_subagent(**kwargs):
    from sub_agent import spawn_subagent
    return spawn_subagent(**kwargs)
config.BUILTIN_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob,
    "todo_write": run_todo_write,
    "task": lazy_spawn_subagent,
    "load_skill": load_skill,
    "create_task": run_create_task, "list_tasks": run_list_tasks,
    "get_task": run_get_task,
    "claim_task": run_claim_task, "complete_task": run_complete_task,
    "schedule_cron": run_schedule_cron,
    "list_crons": run_list_crons,
    "cancel_cron": run_cancel_cron,
    "spawn_teammate": run_spawn_teammate,
    "send_message": run_send_message, "check_inbox": run_check_inbox,
    "request_shutdown": run_request_shutdown,
    "request_plan": run_request_plan, "review_plan": run_review_plan,
    "create_worktree": run_create_worktree,
    "remove_worktree": run_remove_worktree,
    "keep_worktree": run_keep_worktree,
    "connect_mcp": run_connect_mcp,
}
