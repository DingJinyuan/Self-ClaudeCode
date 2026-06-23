"""
background_task.py
后台异步任务模块
功能：慢速bash命令自动后台执行、任务ID管理、线程执行、结果缓存、主线程批量取回通知
依赖：config.py + tool_system.py
"""
import threading
from typing import List
from tool_system import call_tool_handler
from hooks_permission import trigger_hooks
import config
# 后台任务文件开头
from config import (
    background_tasks,
    background_results,
    background_lock
)

def is_slow_operation(tool_name: str, tool_input: dict) -> bool:
    """判定是否为慢速命令，符合特征自动后台运行"""
    if tool_name != "bash":
        return False
    command = tool_input.get("command", "").lower()
    slow_keywords = [
        "install", "build", "test", "deploy", "compile",
        "docker build", "pip install", "npm install",
        "cargo build", "pytest", "make"
    ]
    return any(keyword in command for keyword in slow_keywords)


def should_run_background(tool_name: str, tool_input: dict) -> bool:
    """判定是否需要启动后台线程：显式标记run_in_background 或者 自动识别慢速任务"""
    if tool_name != "bash":
        return False
    return bool(tool_input.get("run_in_background")) or is_slow_operation(tool_name, tool_input)


def start_background_task(block, handlers: dict) -> str:
    """
    启动后台线程执行工具
    :param block: tool_use 工具区块
    :param handlers: 全局工具处理器字典
    :return: 后台任务唯一ID bg_xxxx
    """
    config._bg_counter += 1
    # 生成固定格式唯一任务ID，例 bg_0001、bg_0002
    bg_id = f"bg_{config._bg_counter:04d}"
    command = block.input.get("command", block.name)

    def worker():
        # 根据工具名称拿到对应的执行函数
        handler = handlers.get(block.name)
        # 执行工具，拿到返回结果
        result = call_tool_handler(handler, block.input, block.name)
        # 执行后置权限钩子（日志、权限校验、审计）
        trigger_hooks("PostToolUse", block, result)
        # 加线程锁，安全修改全局状态字典
        with background_lock:
            # 任务状态改为已完成
            background_tasks[bg_id]["status"] = "completed"
            # 把执行结果存入全局结果池，供主线程读取
            background_results[bg_id] = str(result)

    # 写入任务状态
    with background_lock:
        background_tasks[bg_id] = {
            "tool_use_id": block.id,
            "command": command,
            "status": "running",
        }
    # 守护线程，主线程退出自动销毁
    threading.Thread(target=worker, daemon=True).start()
    print(f"  \033[33m[background] {bg_id}: {str(command)[:60]}\033[0m")
    return bg_id

def collect_background_results() -> List[str]:
    """主线程统一轮询，取出所有已完成的后台任务，生成通知文本，自动清理缓存
    主线程定期轮询，批量捞取所有已经跑完的后台异步任务，把执行结果包装成标准化通知文本，交给大模型知晓后台任务完成，同时自动清理内存里的过期任务缓存，防止内存堆积。
    """
    ready_ids: List[str] = []
    with background_lock:
        ready_ids = [
            bg_id for bg_id, task in background_tasks.items()
            if task["status"] == "completed"
        ]

    notifications: List[str] = []
    for bg_id in ready_ids:
        with background_lock:
            task_info = background_tasks.pop(bg_id)
            output = background_results.pop(bg_id, "")

        preview = output[:200] if len(output) > 200 else output
        notifications.append(f"""<task_notification>
  <task_id>{bg_id}</task_id>
  <status>completed</status>
  <command>{task_info['command']}</command>
  <summary>{preview}</summary>
</task_notification>""")
    return notifications