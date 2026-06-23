"""
main.py
整合全部拆分模块：工具、钩子、压缩、异常恢复、后台任务、Cron定时、MCP、队友消息总线
"""
import time, threading
from datetime import datetime




try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
    READLINE_AVAILABLE = True
except ImportError:
    READLINE_AVAILABLE = False


# ===================== 全局配置初始化(最先导入) =====================
from config import (
    CONTINUATION_PROMPT, PROMPT, MEMORY_INDEX, MEMORY_DIR, mcp_clients, active_teammates, client
, WORKDIR, CONTEXT_LIMIT, DEFAULT_MAX_TOKENS, ESCALATED_MAX_TOKENS, MAX_RECOVERY_RETRIES, MODEL, FALLBACK_MODEL,
    CLI_ACTIVE
)
# ===================== 各功能模块导入 =====================
from error_recovery import RecoveryState, with_retry,  is_prompt_too_long_error
from skill_manager import list_skills
from background_task import collect_background_results, should_run_background, start_background_task
from compact_context import tool_result_budget, snip_compact,micro_compact,estimate_size,compact_history,reactive_compact
from mcp_client import assemble_tool_pool
from cron_scheduler import consume_cron_queue
from tool_system import has_tool_use, call_tool_handler
from hooks_permission import trigger_hooks
from protocol_core import consume_lead_inbox
import config
# ── Prompt Assembly ──

PROMPT_SECTIONS = {
    "identity": "You are a coding agent. Act, don't explain.",
    "tools": "Available tools: bash, read_file, write_file, edit_file, glob, "
             "todo_write, task, load_skill, compact, "
             "create_task, list_tasks, get_task, claim_task, complete_task, "
             "schedule_cron, list_crons, cancel_cron, "
             "spawn_teammate, send_message, check_inbox, "
             "request_shutdown, request_plan, review_plan, "
             "create_worktree, remove_worktree, keep_worktree, "
             "connect_mcp. MCP tools are prefixed mcp__{server}__{tool}.",
    "workspace": f"Working directory: {WORKDIR}",
    "memory": "Relevant memories are injected below when available.",
}
def assemble_system_prompt(context: dict) -> str:
    # 注释：每一轮都会根据实时上下文重建系统提示词。
    # 长期记忆、技能目录、MCP连接状态、活跃队友信息，都会在这里暴露给模型
    sections = [
        PROMPT_SECTIONS["identity"],   # 身份设定：Agent角色、行为准则、基础规则
        PROMPT_SECTIONS["tools"],      # 工具调用通用规则、格式要求、使用规范
        PROMPT_SECTIONS["workspace"]   # 工作目录、文件权限、Git工作区等环境约束
    ]

    # 追加当前精确系统时间
    sections.append(f"Current time: {datetime.now().isoformat(timespec='seconds')}")

    # 追加全部可用技能清单，告知模型可以使用load_skill动态加载技能
    sections.append("Skills catalog:\n" + list_skills() +
                    "\nUse load_skill(name) when a skill is relevant.")

    # 如果上下文携带检索到的长期记忆，把记忆内容注入prompt
    if context.get("memories"):
        sections.append(f"Relevant memories:\n{context['memories']}")

    # 读取当前已连接的所有MCP服务名称，写入提示词，让模型知道外部工具服务可用状态
    mcp_names = list(mcp_clients.keys())
    if mcp_names:
        sections.append(f"Connected MCP servers: {', '.join(mcp_names)}")

    # 所有段落用两个换行分隔，拼接成完整的system字符串返回
    return "\n\n".join(sections)

def terminal_print(text: str):
    """控制台打印适配readline CLI"""
    # 条件判断：主线程 或者 未开启CLI交互模式，直接普通print即可
    if threading.current_thread() is threading.main_thread() or not CLI_ACTIVE:
        print(text)
        return

    line = ""
    # 只有readline可用时，读取用户当前正在输入的未回车内容
    if READLINE_AVAILABLE:
        try:
            # get_line_buffer()：获取用户光标当前行已经输入、还没按下回车的字符串
            line = readline.get_line_buffer()
        except Exception:
            line = ""

    # 1. \r 光标回到本行行首；\033[K ANSI控制码：清空整行内容
    print(f"\r\033[K{text}")
    # 2. 重新打印提示符 + 用户刚才输入的内容，光标停在原有位置，继续输入
    print(PROMPT + line, end="", flush=True)

def update_context(context: dict, messages: list) -> dict:
    """动态组装本轮上下文环境信息"""
    memories = ""
    if MEMORY_INDEX.exists():
        memories = MEMORY_INDEX.read_text(encoding="utf-8")[:2000]
    return {
        "memories": memories,
        "connected_mcp": list(mcp_clients.keys()),
        "active_teammates": list(active_teammates.keys()),
    }

def call_llm(messages: list, context: dict, tools: list,
             state: RecoveryState, max_tokens: int):
    """封装带重试的LLM调用"""
    system_prompt = assemble_system_prompt(context)

    # 调用你之前实现的通用重试函数 with_retry
    return with_retry(
        # 匿名lambda函数：真实的Anthropic Claude接口请求逻辑
        lambda: client.messages.create(
            model=state.current_model,  # 使用state里当前生效的模型（主模型/自动降级后的备用模型）
            system=system_prompt,        # 动态生成的系统角色提示词
            messages=messages,          # 完整对话上下文
            tools=tools,                 # 可用工具列表，开启Function Calling工具调用能力
            max_tokens=max_tokens),      # 最大输出token限制
        state)  # 传入容灾状态对象，供重试逻辑记录连续过载次数、切换备用模型

def build_user_content(results: list[dict]) -> list[dict]:
    """合并工具结果 + 后台任务通知，拼装为用户侧上下文内容"""
    content = list(results)
    for note in collect_background_results():
        content.append({"type": "text", "text": note})
    return content

def prepare_context(messages: list) -> list:
    "tool_result_budget → snip_compact → micro_compact → compact_history"
    # 注释：每一轮大模型调用，都会经过这套统一的上下文额度管控流水线
    # 步骤1：工具结果配额管控 统计本轮所有tool_result总字节，超出阈值时，将体积最大的工具结果转为文件持久化，用文件路径替换原文内容，缩减上下文内存
    messages[:] = tool_result_budget(messages)
    # 步骤2：消息精简裁剪 中段批量裁剪消息，保留头部少量上下文 + 完整尾部对话，中间用占位消息代替
    messages[:] = snip_compact(messages)
    # 步骤3：超细粒度压缩  只保留最近N条完整工具结果，更早的工具结果直接精简占位
    messages[:] = micro_compact(messages)
    #步骤4:估算当前剩余总token，如果依然超出全局上限 全量压缩：存档完整日志 + 生成摘要，上下文替换为摘要
    if estimate_size(messages) > CONTEXT_LIMIT:
        # 执行重量级历史压缩（摘要、截断旧消息）
        messages[:] = compact_history(messages)
    return messages

def inject_background_notifications(messages: list):
    """
    注入已完成后台任务的通知到对话上下文
    作用：拉取本轮已结束的后台异步任务结果，封装为标准消息格式，追加进消息列表，让大模型感知后台任务执行完毕与返回内容
    :param messages: 全局对话消息列表，会原地追加新的user通知消息
    """
    # 轮询获取所有执行完成的后台任务，同时自动清理已消费的任务缓存
    notes = collect_background_results()
    # 判断是否存在已完成的后台任务，无结果则直接跳过，不新增无用消息
    if notes:
        # 按照Anthropic Claude标准消息结构封装：角色为user，content为文本数组
        # 将每条任务通知包装为type=text的标准内容块，统一追加为一条用户消息送入上下文
        messages.append({"role": "user", "content": [
            {"type": "text", "text": note} for note in notes]})
# ── Agent Loop ──
rounds_since_todo = 0
agent_lock = threading.Lock()

def agent_loop(messages: list, context: dict):
    """核心Agent主循环：完整一轮思考+工具执行的闭环逻辑，无限循环推理，直到无工具调用则本轮结束
    定时任务注入 → 后台结果注入 → 上下文压缩预处理 → LLM 模型调用 → 解析模型回复 → 同步 / 异步执行工具 → 工具结果回写上下文 → 循环进入下一轮推理。
    """
    # 全局计数器：距离上次更新todo清单经过的轮次，用来定时提醒模型维护待办
    global rounds_since_todo
    # 实时组装【内置工具 + 已连接MCP工具】的完整工具池、工具处理器映射表
    tools, handlers = assemble_tool_pool()
    # 实例化全局容灾恢复状态对象：记录模型降级、重试次数、token升级标记、超限标记等
    recovery_state = RecoveryState()
    # 初始化单次模型输出最大token为默认值
    max_tokens = DEFAULT_MAX_TOKENS

    # 永久主循环，一轮一轮持续推理，直到满足退出条件才return结束本轮会话
    while True:
        # 注释总览：单次循环完整流程
        # 一轮周期：1.注入定时Cron任务  2.注入后台完成任务通知  3.上下文压缩预处理  4.调用LLM模型
        # 5.解析模型返回内容，执行工具调用  6.工具结果写入对话上下文，进入下一轮循环

        # ========== 步骤1：取出Cron定时命中的任务，注入对话上下文 ==========
        fired_jobs = consume_cron_queue()
        for job in fired_jobs:
            # 拼接定时任务消息，模拟用户下发指令
            msg_content = f"[Scheduled Cron Task] {job.prompt}"
            messages.append({"role": "user", "content": msg_content})
            # 控制台紫色打印日志，提示定时任务触发
            terminal_print(f"  \033[35m[cron inject] {job.prompt[:60]}\033[0m")

        # ========== 步骤2：注入已经执行完毕的后台异步任务结果通知 ==========
        inject_background_notifications(messages)

        # ========== 步骤3：每3轮推理，强制提醒模型及时更新Todo待办清单 ==========
        if rounds_since_todo >= 3:
            # 插入系统提醒文本，以user消息送入上下文
            messages.append({"role": "user", "content": "<reminder>Please update your todos timely.</reminder>"})
            # 计数器归零，重新开始计数
            rounds_since_todo = 0

        # ========== 步骤4：上下文预处理流水线 + 刷新环境上下文、工具池 ==========
        # 执行多层上下文压缩、截断、长度管控，严格控制上下文总Token上限
        prepare_context(messages)
        # 根据最新对话消息，刷新全局运行上下文（路径、环境、记忆、状态等）
        context = update_context(context, messages)
        # 每轮重新组装工具池，实时同步最新连接的MCP服务、动态加载的技能
        tools, handlers = assemble_tool_pool()

        # ========== 步骤5：调用LLM大模型，内置指数退避重试、模型降级容灾 ==========
        try:
            response = call_llm(messages, context, tools, recovery_state, max_tokens)
        except Exception as e:
            # 捕获【上下文长度超限】报错，且还没执行过应急重型压缩
            if is_prompt_too_long_error(e) and not recovery_state.has_attempted_reactive_compact:
                # 执行紧急强压缩，精简历史对话
                messages[:] = reactive_compact(messages)
                # 标记已执行应急压缩，避免反复重试压缩
                recovery_state.has_attempted_reactive_compact = True
                # 直接跳过本轮剩余逻辑，回到循环开头重新执行
                continue
            # 非可恢复的致命异常，记录错误消息写入对话
            err_text = f"[Fatal Error] {type(e).__name__}: {str(e)}"
            messages.append({"role": "assistant", "content": [{"type": "text", "text": err_text}]})
            terminal_print(err_text)
            # 直接终止本轮主循环
            return

        # ========== 分支判断：模型因为max_tokens达到上限被截断 ==========
        if response.stop_reason == "max_tokens":
            # 第一次触发截断，升级token上限，重新请求模型续写
            if not recovery_state.has_escalated:
                max_tokens = ESCALATED_MAX_TOKENS
                recovery_state.has_escalated = True
                terminal_print(f"  \033[33m[Warning] Max tokens reached, upgrade limit to {max_tokens}\033[0m")
                # 回到循环开头，重新调用LLM
                continue
            # 已经升级过token上限依旧截断，先把已返回的内容存入对话
            messages.append({"role": "assistant", "content": response.content})
            # 续写重试次数未达到上限，下发续写提示词，让模型继续完成回答
            if recovery_state.recovery_count < MAX_RECOVERY_RETRIES:
                messages.append({"role": "user", "content": CONTINUATION_PROMPT})
                recovery_state.recovery_count += 1
                continue
            # 续写次数耗尽，本轮循环结束
            return

        # token上限升级标记重置，恢复为默认最大token值
        max_tokens = DEFAULT_MAX_TOKENS
        recovery_state.has_escalated = False
        # 正常模型回复，将assistant回答写入对话历史
        messages.append({"role": "assistant", "content": response.content})

        # ========== 分支判断：本次模型回复**没有工具调用**，本轮推理正式结束 ==========
        if not has_tool_use(response.content):
            # 触发Stop生命周期钩子（收尾回调、日志、后置事件）
            trigger_hooks("Stop", messages)
            return

        # ========== 步骤8：存在tool_use工具调用，逐条执行工具逻辑 ==========
        tool_results = []  # 存放本轮所有工具的执行结果
        compact_flag = False # 标记是否触发了手动上下文压缩指令
        for block in response.content:
            # 只处理工具调用区块，跳过普通文本内容
            if block.type != "tool_use":
                continue
            terminal_print(f"\033[36m> Tool Call: {block.name}\033[0m")

            # 特殊指令：compact 手动上下文压缩工具，执行后直接结束本轮工具循环
            if block.name == "compact":
                messages[:] = compact_history(messages)
                # 写入压缩完成提示，告知模型后续使用精简后的上下文
                messages.append({"role": "user", "content": "[Compact finished, continue work with summarized context.]"})
                compact_flag = True
                break

            # 执行PreToolUse前置钩子：权限校验、黑白名单、参数审计、前置拦截
            hook_blocked_msg = trigger_hooks("PreToolUse", block)
            if hook_blocked_msg:
                # 钩子拦截工具，把拦截原因作为工具结果返回
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(hook_blocked_msg)
                })
                continue

            # 判断当前工具是否属于耗时慢速任务，需要丢入后台异步执行
            if should_run_background(block.name, block.input):
                # 启动后台线程执行任务，拿到后台任务ID
                bg_id = start_background_task(block, handlers)
                # 告知模型任务已后台启动，结果完成后会自动推送通知
                res_text = f"[Background Task {bg_id}] Started, result will notify later."
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": res_text})
                continue

            # 同步即时执行工具：从处理器字典拿到对应执行函数
            handler_func = handlers.get(block.name)
            output = call_tool_handler(handler_func, block.input, block.name)
            # 执行PostToolUse后置钩子：执行完成日志、结果后置处理
            trigger_hooks("PostToolUse", block, output)
            # 控制台打印工具输出内容（截断300字符防刷屏）
            terminal_print(str(output)[:300])

            # Todo待办计数器逻辑：调用todo_write则重置计数，其他工具轮次+1
            if block.name == "todo_write":
                rounds_since_todo = 0
            else:
                rounds_since_todo += 1

            # 将同步执行的结果，封装为标准tool_result格式
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": output
            })

        # 如果触发了手动compact压缩，直接回到循环头部，开启新一轮推理
        if compact_flag:
            continue
        # 将本轮全部工具执行结果，统一封装为user消息写入上下文，供下一轮模型读取
        messages.append({"role": "user", "content": build_user_content(tool_results)})
"""
整体运行大流程（一轮完整闭环）
事件注入阶段
先把定时 Cron 到期任务、后台刚跑完的异步任务结果主动塞进对话，让模型被动接收系统事件。
上下文管控阶段
定时提醒维护 Todo → 多层上下文压缩瘦身 → 刷新运行环境与最新工具池（MCP / 内置工具实时刷新）。
LLM 调用与容灾
带指数退避重试调用模型，异常兜底处理：
上下文超长：执行应急压缩后重试
token 被截断：先升级 token 上限，多次续写失败则退出本轮
模型回复两大分支
① 无工具调用：普通对话回答，触发 Stop 钩子，本轮循环结束；
② 携带 tool_use 工具调用：进入工具执行流程。
工具执行分支精细化处理
手动compact压缩指令：立刻精简上下文，直接重启循环；
前置钩子拦截：权限不足直接返回拦截结果；
耗时任务 → 后台异步启动，即刻返回「后台已启动」提示；
普通快速工具 → 同步执行，跑完触发后置钩子，记录结果。
工具结果回写上下文
所有工具结果打包写入messages，while True回到循环开头，进入下一轮模型思考。
"""

def print_assistant_output(messages: list, turn_start_idx: int):
    """打印本轮模型输出文本"""
    for msg in messages[turn_start_idx:]:
        if msg.get("role") != "assistant":
            continue
        for block in msg.get("content", []):
            if getattr(block, "type", None) == "text":
                terminal_print(block.text)

def cron_auto_run_loop(history: list, context: dict):
    """Cron后台线程自动执行调度"""
    while True:
        time.sleep(1)
        fired_jobs = consume_cron_queue()
        if not fired_jobs:
            continue
        with agent_lock:
            turn_start = len(history)
            for job in fired_jobs:
                prompt_text = f"[Auto Cron Trigger] {job.prompt}"
                history.append({"role": "user", "content": prompt_text})
                terminal_print(f"  \033[35m[Auto Cron Run] {job.prompt[:60]}\033[0m")
            agent_loop(history, context)
            context.update(update_context(context, history))
            print_assistant_output(history, turn_start)

def main():
    """程序主入口函数，CLI交互式主线程，负责初始化、后台线程拉起、用户输入循环、Agent主逻辑调度、队友消息处理"""
    # 全局开关开启交互式CLI命令行模式，启用readline优化打印、提示符交互
    config.CLI_ACTIVE = True

    # 控制台打印初始化欢迎分割线
    terminal_print("=" * 50)
    # 打印当前主模型、降级备用模型配置信息
    terminal_print(f"Primary Model: {MODEL}, Fallback: {FALLBACK_MODEL}")
    terminal_print("Enter your task prompt, input q / exit to quit.")
    terminal_print("=" * 50)

    # 全局对话历史容器，完整存放本轮会话所有user/assistant消息
    history: list[dict] = []
    # 初始化全局运行上下文，读取环境、路径、初始配置
    context = update_context({}, [])

    # ===================== 拉起Cron定时调度守护后台线程 =====================
    # daemon=True 守护线程：主线程退出时，该子线程会自动跟随进程销毁，不会后台残留
    # 传入history对话列表、context上下文，供定时任务触发时读取全局会话环境
    threading.Thread(target=cron_auto_run_loop, args=(history, context), daemon=True).start()

    # ===================== 主线CLI永久交互循环，持续接收用户输入 =====================
    while True:
        try:
            # 读取控制台用户输入，PROMPT为自定义命令行提示符
            user_input = input(PROMPT)
        # 捕获异常：Ctrl+D(EOF结束)、Ctrl+C(键盘中断退出)，执行正常退出流程
        except (EOFError, KeyboardInterrupt):
            terminal_print("\nProgram exit normally.")
            break

        # 首尾空格清理
        user_input = user_input.strip()
        # 退出指令判断：输入q/exit/空行，结束CLI循环
        if user_input.lower() in ("q", "exit", ""):
            terminal_print("Exit agent. Goodbye!")
            break

        # 触发【用户提交输入】前置生命周期钩子，可做输入过滤、日志记录、内容审计
        trigger_hooks("UserPromptSubmit", user_input)
        # 记录本轮回答起始下标，用于后续切片打印本轮模型新输出内容
        turn_start_pos = len(history)
        # 将用户输入追加进全局对话历史
        history.append({"role": "user", "content": user_input})

        # ===================== 加互斥锁执行Agent主推理逻辑 =====================
        # agent_lock 全局线程锁：防止后台Cron定时任务线程 和 主线用户任务 并发抢占推理
        # 同一时刻只能有一套推理逻辑执行，避免history对话列表被多线程同时读写，防止列表错乱、数据异常
        with agent_lock:
            # 执行完整Agent推理主循环（思考+工具调用+上下文压缩+LLM调用全套逻辑）
            agent_loop(history, context)
            # 推理完成后，刷新最新上下文环境（同步最新对话、工具状态、内存信息）
            context = update_context(context, history)
            # 只打印本轮新增的模型回复内容，不重复输出完整历史对话
            print_assistant_output(history, turn_start_pos)

        # ===================== 读取多队友Agent的通信回执消息 =====================
        # 消费队友通信收件箱，route_protocol=True 启用队友协议路由解析
        inbox_msgs = consume_lead_inbox(route_protocol=True)
        if inbox_msgs:
            inbox_lines = []
            for m in inbox_msgs:
                # 提取请求ID、消息来源、消息类型
                req_id = m.get("metadata", {}).get("request_id", "")
                tag = f"[{m['type']} req:{req_id}]" if req_id else f"[{m['type']}]"
                # 消息内容截断200字符，避免内容过长
                inbox_lines.append(f"From {m['from']} {tag}: {m['content'][:200]}")
            # 封装为统一的队友收件箱通知文本
            inbox_full_text = "[Teammate Inbox Message]\n" + "\n".join(inbox_lines)
            # 把队友消息作为user消息写入对话上下文，让模型读取队友回执内容
            history.append({"role": "user", "content": inbox_full_text})
        # 空行分割，优化CLI界面排版
        terminal_print("")
"""
主线程（当前 main 循环）：前台 CLI 交互线程
负责接收键盘输入、串行执行用户指令的agent_loop推理、处理队友消息、控制台界面展示，是前台交互入口。
子线程（Cron 守护线程 cron_auto_run_loop）：后台定时调度线程
独立常驻后台轮询时间，定时任务触发时，同样会争抢agent_lock锁执行推理，把定时任务自动注入对话上下文，实现无人值守定时自动化执行。
"""
if __name__ == "__main__":
    main()