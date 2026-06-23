"""
teammate_runtime.py
常驻队友运行时：空闲轮询 idle_poll、收件消息处理、线程工厂、内部工具封装、主循环

"""
import json,time,re,threading
from dataclasses import asdict
from pathlib import Path

from message_bus import BUS
from typing import List, Optional
from config import (
    WORKTREES_DIR, IDLE_POLL_INTERVAL, IDLE_TIMEOUT,
    MODEL, client,active_teammates
)

from task_worktree import (
    list_tasks, can_start, claim_task, load_task, complete_task
)




# ===================== 空闲轮询 idle_poll =====================
def scan_unclaimed_tasks() -> List[dict]:
    """扫描所有 待认领、依赖全部就绪、无主人的空闲任务"""
    unclaimed = []
    task_list = list_tasks()
    for task in task_list:
        if task.status == "pending" and task.owner is None and can_start(task.id):
            unclaimed.append(asdict(task))
    return unclaimed

def idle_poll(agent_name: str, messages: list, name: str, role: str,
              worktree_context: Optional[dict] = None) -> str:
    """
    队友空闲循环逻辑
    优先级：优先处理收件消息 > 其次自动认领全局空闲任务
    超时则返回timeout，收到关机指令返回shutdown
    """
    for _ in range(IDLE_TIMEOUT // IDLE_POLL_INTERVAL):
        time.sleep(IDLE_POLL_INTERVAL)
        inbox = BUS.read_inbox(agent_name)
        # 优先处理收件消息
        if inbox:
            for msg in inbox:
                # 收到关机请求，直接回复确认，返回shutdown结束线程
                if msg.get("type") == "shutdown_request":
                    req_id = msg.get("metadata", {}).get("request_id", "")
                    BUS.send(name, "lead", "Shutting down.",
                             "shutdown_response", {"request_id": req_id, "approve": True})
                    return "shutdown"
            # 普通业务消息塞入对话上下文，进入模型处理
            messages.append({
                "role": "user",
                "content": f"<inbox>{json.dumps(inbox)}</inbox>"
            })
            return "work"

        # 收件箱为空，尝试自动认领任务
        unclaimed = scan_unclaimed_tasks()
        if unclaimed:
            task_data = unclaimed[0]
            result = claim_task(task_data["id"], agent_name)
            if "Claimed" in result:
                wt_info = ""
                # 如果任务绑定worktree，把工作目录写入上下文，后续文件操作自动锁定该目录
                if task_data.get("worktree"):
                    # 拼接完整物理路径：根worktree目录 / 任务对应的文件夹名
                    wt_path = WORKTREES_DIR / task_data["worktree"]
                    wt_info = f"\nWork directory: {wt_path}"
                    if worktree_context is not None: #全局运行上下文字典，专门存放当前任务的 worktree 环境信息
                        # 把完整路径写入上下文，全局后续代码随时可以读取这个路径
                        worktree_context["path"] = str(wt_path)
                # 把自动认领的任务注入对话
                messages.append({
                    "role": "user",
                    "content": f"<auto-claimed>Task {task_data['id']}: {task_data['subject']}{wt_info}</auto-claimed>"
                })
                return "work"
    # 长时间无消息、无任务，空闲超时
    return "timeout"

# ======================== 4. 常驻队友线程生成 spawn_teammate ========================
def spawn_teammate_thread(name: str, role: str, prompt: str) -> str:
    """
    创建常驻后台队友守护线程，长期存活，自主工作
    :param name: 队友名称（唯一）
    :param role: 角色定位（前端/后端/测试）
    :param prompt: 初始任务提示
    :return: 创建结果文本
    """
    from tool_system import (call_tool_handler, has_tool_use)
    if name in active_teammates:
        return f"Teammate '{name}' already exists"

    # 协议上下文：标记是否正在等待方案审批
    protocol_ctx = {"waiting_plan": None}
    system = (
        f"You are '{name}', a {role}. Use tools to complete tasks. "
        f"If a task has a worktree, work in that isolated directory."
    )

    def handle_inbox_message(name: str,msg: dict, messages: list):
        """处理单条收件消息，区分普通消息、关机、方案审批应答
            队友线程收到消息后，用这个函数解析分类，只处理两类协议消息：
            1.shutdown_request 关机指令（Lead 下发）
                读取消息里的request_id；
                立刻通过BUS.send回复shutdown_response回执，告知主控即将关闭；
                return True，上层循环收到标记，结束线程。
            2.plan_approval_response 方案审批回执（Lead 审批后的回复）
                取出approve审批结果；
                判断回执的req_id是否等于自己正在等待的protocol_ctx["waiting_plan"]，对上说明是本次审批结果；
                清空等待标记，解除阻塞；
                把[Plan approved] 或驳回原因写入对话messages，大模型读取结果，决定继续执行任务还是重新规划方案。
                普通闲聊 / 业务消息，本函数不处理，返回 False，交给上层模型逻辑处理。
            """
        msg_type = msg.get("type", "message")
        meta = msg.get("metadata", {})
        req_id = meta.get("request_id", "")
        # 关机请求
        if msg_type == "shutdown_request":
            BUS.send(name, "lead", "Shutting down.",
                     "shutdown_response", {"request_id": req_id, "approve": True})
            return True
        # 方案审批结果回执 （Lead 审批后的回复）
        if msg_type == "plan_approval_response":
            # 取出审批结果，无字段默认驳回False
            approve = meta.get("approve", False)
            # 校验请求ID一致：当前回执，正好对应正在等待的本次审批请求
            if req_id == protocol_ctx["waiting_plan"]:
                # 清空等待标记，代表本次等待审批结束，不再处于挂起状态
                protocol_ctx["waiting_plan"] = None
            # 审批结果写入上下文
            if approve:
                messages.append({"role": "user", "content": "[Plan approved]"})
            else:
                messages.append({"role": "user", "content": f"[Plan rejected] {msg['content']}"})
        return False

    # 子线程主运行函数
    def run():
        wt_ctx = {"path": None}  # 当前绑定的worktree路径
        # 工具cwd读取函数，自动读取当前任务绑定的隔离工作区
        def _wt_cwd():
            p = wt_ctx["path"]
            return Path(p) if p else None

        # 内部工具封装，队友的bash/读写文件自动限定在自身worktree目录
        # 认领任务成功的瞬间，自动锁定专属工作目录，后续工具全自动隔离运行环境。
        def _run_bash(command: str):
            from tool_system import run_bash
            return run_bash(command, cwd=_wt_cwd())
        def _run_read(path: str, limit=None, offset=0):
            from tool_system import run_read
            return run_read(path, limit, offset, cwd=_wt_cwd())
        def _run_write(path: str, content: str):
            from tool_system import run_write
            return run_write(path, content, cwd=_wt_cwd())

        def _run_list_tasks():
            tasks = list_tasks()
            if not tasks:
                return "No tasks."
            return "\n".join(
                f"  {t.id}: {t.subject} [{t.status}]"
                + (f" (wt:{t.worktree})" if t.worktree else "")
                for t in tasks)

        def _run_claim_task(task_id: str):
            res = claim_task(task_id, owner=name)
            if "Claimed" in res:
                task = load_task(task_id)
                # 读取任务配置里的worktree文件夹名称，拼接完整路径存入wt_ctx
                wt_ctx["path"] = str(WORKTREES_DIR / task.worktree) if task.worktree else None
            return res

        def _run_complete_task(task_id: str):
            result = complete_task(task_id)
            wt_ctx["path"] = None
            return result

        # 队友初始对话上下文
        messages = [{"role": "user", "content": prompt}]
        # 队友可用工具定义
        sub_tools = [
            {"name": "bash", "description": "Run a shell command.",
             "input_schema": {"type": "object",
                              "properties": {"command": {"type": "string"}},
                              "required": ["command"]}},
            {"name": "read_file", "description": "Read file.",
             "input_schema": {"type": "object",
                              "properties": {"path": {"type": "string"},
                                             "limit": {"type": "integer"},
                                             "offset": {"type": "integer"}},
                              "required": ["path"]}},
            {"name": "write_file", "description": "Write file.",
             "input_schema": {"type": "object",
                              "properties": {"path": {"type": "string"},
                                             "content": {"type": "string"}},
                              "required": ["path", "content"]}},
            {"name": "send_message",
             "description": "Send message to another agent.",
             "input_schema": {"type": "object",
                              "properties": {"to": {"type": "string"},
                                             "content": {"type": "string"}},
                              "required": ["to", "content"]}},
            {"name": "submit_plan",
             "description": "Submit a plan for Lead approval.",
             "input_schema": {"type": "object",
                              "properties": {"plan": {"type": "string"}},
                              "required": ["plan"]}},
            {"name": "list_tasks",
             "description": "List all tasks.",
             "input_schema": {"type": "object", "properties": {},
                              "required": []}},
            {"name": "claim_task",
             "description": "Claim a pending task.",
             "input_schema": {"type": "object",
                              "properties": {"task_id": {"type": "string"}},
                              "required": ["task_id"]}},
            {"name": "complete_task",
             "description": "Mark an in-progress task as completed.",
             "input_schema": {"type": "object",
                              "properties": {"task_id": {"type": "string"}},
                              "required": ["task_id"]}},
        ]
        # 工具与执行函数映射表
        sub_handlers = {
            "bash": _run_bash, "read_file": _run_read,
            "write_file": _run_write,
            "send_message": lambda to, content: (BUS.send(name, to, content),
                                                 "Sent")[1],
            "list_tasks": _run_list_tasks,
            "claim_task": _run_claim_task,
            "complete_task": _run_complete_task,
        }
        # 线程主循环，常驻运行
        while True:
            # 上下文过少，插入身份提示
            if len(messages) <= 3:
                messages.insert(0, {"role": "user",
                                    "content": f"<identity>You are '{name}', role: {role}. "
                                               f"Continue your work.</identity>"})
            should_shutdown = False
            # 单次模型回合最多执行10轮工具调用，防止死循环
            for _ in range(10):
                #环节 1：优先读取自身收件箱消息
                inbox = BUS.read_inbox(name)
                for msg in inbox:
                    stopped = handle_inbox_message(name, msg, messages)
                    if stopped:
                        should_shutdown = True
                        break
                if should_shutdown:
                    break
                #环节 2：判断是否处于审批等待状态 只有模型调用了 submit_plan 提交方案
                # 等待方案审批期间，暂停模型推理，只轮询收件箱
                if protocol_ctx["waiting_plan"]:
                    time.sleep(IDLE_POLL_INTERVAL)
                    continue
                #环节 3：调用大模型推理，模型自主选择工具
                # 普通消息写入上下文
                if inbox and not should_shutdown:
                    non_protocol = [m for m in inbox if m.get("type") == "message"]
                    if non_protocol:
                        messages.append({"role": "user", "content": f"<inbox>{json.dumps(non_protocol)}</inbox>"})
                # 调用模型推理
                try:
                    response = client.messages.create(
                        model=MODEL,
                        system=system,
                        messages=messages[-20:],
                        tools=sub_tools,
                        max_tokens=8000
                    )
                except Exception:
                    break
                messages.append({"role": "assistant", "content": response.content})
                # 判断本次是否需要调用工具
                if not has_tool_use(response.content):
                    break
                results = []
                for block in response.content:
                    if block.type == "tool_use":
                        #：队友写完方案，主动提交申请审批（才是真正的审批请求）
                        if block.name == "submit_plan":
                            # 调用队友专属函数，把方案发送给lead，返回结果字符串
                            from protocol_core import _teammate_submit_plan
                            output = _teammate_submit_plan(name, block.input.get("plan", ""))
                            match = re.search(r"\((req_\d+)\)", output)
                            # 提取req_id存入等待标记；提取失败就把完整output放进去兜底
                            protocol_ctx["waiting_plan"] = match.group(1) if match else output
                        else:
                            handler = sub_handlers.get(block.name)
                            output = call_tool_handler(handler, block.input,
                                                       block.name)
                        results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": str(output)
                        })
                        # 提交方案后，直接终止本轮工具循环，等待审批
                        if protocol_ctx["waiting_plan"]:
                            break
                messages.append({"role": "user", "content": results})
                if protocol_ctx["waiting_plan"]:
                    break
            # 收到关机信号，退出主循环
            if should_shutdown:
                break
            # 等待审批，回到循环开头继续轮询收件箱
            if protocol_ctx["waiting_plan"]:
                continue
            # 进入空闲等待逻辑
            idle_result = idle_poll(name, messages, name, role, wt_ctx)
            if idle_result in ("shutdown", "timeout"):
                break
        # 线程收尾，提取最终总结回复发给主控lead
        # 队友任务跑完结束时，遍历完整对话记录，反向找到模型最后一轮的文本回答，当做本次任务的最终总结，上报给主控 Lead
        summary = "Done."
        for msg in reversed(messages):
            if msg["role"] == "assistant" and isinstance(msg["content"], list):
                for item in msg["content"]:
                    if getattr(item, "type", None) == "text":
                        summary = item.text
                        break
                else:
                    continue
                break
        BUS.send(name, "lead", summary, "result")
        # 从活跃队友列表移除
        active_teammates.pop(name, None)

        # 加入活跃列表，启动守护线程，主线程退出子线程自动结束

    active_teammates[name] = True
    threading.Thread(target=run, daemon=True).start()
    return f"Teammate '{name}' spawned as {role}"


def run_spawn_teammate(name: str, role: str, prompt: str) -> str:
    return spawn_teammate_thread(name, role, prompt)