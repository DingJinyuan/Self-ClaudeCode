"""
hooks_permission.py
钩子系统 + 权限校验流水线模块
包含全局钩子注册/触发、权限拦截、日志埋点、执行前后钩子回调
PreToolUse前置权限校验，所有工具执行统一过权限关卡，无需修改各个工具源码

是什么？
Hook 钩子 = 提前预留好的「事件回调点位」
程序走到固定节点时，会主动停下来，执行我们提前挂载好的自定义逻辑；
不用改主流程、不用改工具本身的代码，就能在工具执行前 / 执行后、用户输入时、程序结束时插入额外功能。

怎么触发？
用户输入前后  `UserPromptSubmit`  hooks记录、注入、审计用户输入
工具执行前   `PreToolUse` hooks + permission 拦截危险命令、写越界、破坏性 MCP 工具
工具执行后   `PostToolUse`   hooks大输出告警、日志等后处理
本轮没有 tool_use / 停止时`Stop` hooks 统计、清理、审计
"""

# 导入全局配置
from config import DENY_LIST, DESTRUCTIVE, WORKDIR

# ===================== 全局钩子注册表 =====================
# 钩子事件分类：用户提交、工具执行前、工具执行后、会话结束
HOOKS = {
    "UserPromptSubmit": [],   # 用户输入提交时触发
    "PreToolUse": [],         # 工具调用【执行之前】触发（权限校验核心点位）
    "PostToolUse": [],        # 工具调用【执行完毕】触发
    "Stop": []                # 本轮Agent循环结束、停止推理时触发
}

def register_hook(event: str, callback):
    """
    注册钩子回调函数
    :param event: 钩子事件名称，对应HOOKS的key
    :param callback: 回调函数，事件触发时执行
    """
    HOOKS[event].append(callback)


def trigger_hooks(event: str, *args):
    """
    触发指定事件的所有钩子，按注册顺序依次执行
    只要任意钩子返回非None值，直接终止后续钩子并返回结果（用于拦截）
    :param event: 钩子事件名
    :param args: 要传给回调函数的参数
    :return: 钩子返回值，拦截场景会返回拒绝理由，正常返回None
    """
    for callback in HOOKS[event]:
        result = callback(*args)
        # 前置钩子拿到返回值代表拦截，直接退出
        if result is not None:
            return result
    return None

# ===================== PreToolUse 前置钩子：权限核心钩子 =====================
def permission_hook(block):
    """
    权限校验主钩子，挂载在PreToolUse
    在工具运行前完成安全校验，高危命令、越权路径、危险MCP工具拦截/二次确认
    :param block: LLM输出的tool_use工具块对象
    :return: None=放行，字符串=拒绝理由
    """
    # 1. 针对bash shell命令做高危黑名单校验
    if block.name == "bash":
        command = block.input.get("command", "")
        # 完全禁止的命令，命中直接拒绝
        for pattern in DENY_LIST:
            if pattern in command:
                return f"Permission denied: '{pattern}' is on the deny list"
        # 破坏性命令，弹窗二次确认
        if any(token in command for token in DESTRUCTIVE):
            print(f"\n\033[33m[permission] destructive command\033[0m")
            print(f"  {command}")
            choice = input("  Allow? [y/N] ").strip().lower()
            if choice not in ("y", "yes"):
                return "Permission denied by user"

    # 2. 文件读写工具：校验路径是否逃逸工作目录
    if block.name in ("write_file", "edit_file"):
        path = block.input.get("path", "")
        try:
            from pathlib import Path
            # 拼接成完整绝对路径，resolve() 会自动解析 ../ 上级跳转符号、软链接，拿到真实物理路径
            #防止带 ../ 上级跳转 直接跳出去  绝对路径 /root/.ssh/id_rsa 直接无视workdir
            full_path = (WORKDIR / path).resolve()
            # 判断：目标路径 必须在项目根目录 WORKDIR 的内部
            if not full_path.is_relative_to(WORKDIR.resolve()):
                raise ValueError()
        except Exception:
            return f"Permission denied: path escapes workspace: {path}"

    # 3. MCP外部高危工具二次确认
    # 判断条件：工具名以 mcp__ 开头（代表是第三方MCP外部服务工具），并且工具名包含deploy（部署上线类高危操作
    if block.name.startswith("mcp__") and "deploy" in block.name:
        print(f"\n\033[33m[permission] MCP destructive-looking tool: {block.name}\033[0m")
        choice = input("  Allow? [y/N] ").strip().lower()
        if choice not in ("y", "yes"):
            return "Permission denied by user"

    # 所有校验通过，放行
    return None

# ===================== 通用日志钩子 =====================
def log_hook(block):
    """PreToolUse日志钩子：打印即将执行的工具名称，方便运行日志查看"""
    print(f"\033[90m[HOOK] {block.name}\033[0m")
    return None

# ===================== PostToolUse 后置钩子 =====================
def large_output_hook(block, output):
    """
    工具执行完成后置钩子
    检测超大返回结果，打印告警，提示上下文占用
    """
    output_str = str(output)
    if len(output_str) > 100000:
        print(f"\033[33m[HOOK] large output from {block.name}: {len(output_str)} chars\033[0m")
    return None

# ===================== UserPromptSubmit 用户提交钩子 =====================
def user_prompt_hook(query: str):
    """用户输入提交时触发，打印当前工作目录，可扩展用户输入审计、敏感词过滤"""
    print(f"\033[90m[HOOK] UserPromptSubmit: {WORKDIR}\033[0m")
    return None

# ===================== Stop 循环结束钩子 =====================
def stop_hook(messages: list):
    """本轮循环结束时统计本轮工具调用次数，用于日志统计"""
    tool_count = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            # 统计tool_result工具结果数量
            tool_count += sum(
                1 for item in content
                if isinstance(item, dict) and item.get("type") == "tool_result"
            )
    print(f"\033[90m[HOOK] Stop: {tool_count} tool result(s)\033[0m")
    return None

# ===================== 统一注册全部钩子 =====================
# 程序加载时自动挂载所有钩子
register_hook("UserPromptSubmit", user_prompt_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
register_hook("PostToolUse", large_output_hook)
register_hook("Stop", stop_hook)
