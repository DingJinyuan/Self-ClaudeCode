"""
task_worktree.py
持久任务系统 Task Graph + Git Worktree 环境隔离模块
1. Task：磁盘JSON持久任务、依赖判断、认领/完成，支撑多Agent团队任务台账
2. Worktree：Git独立工作树，为任务分配隔离代码目录，修改互不污染
任务可绑定worktree，一一配套使用
本体 Git 仓库 = 公司总部档案室（唯一的.git 数据库），所有代码版本、提交记录都存在这里。
每一个 Worktree 工作树 = 一间独立办公室，每间办公室绑定专属的 wt/xxx 分支。
Agent 任务 = 办公室里独立干活的员工

"""

import json,random,time,subprocess
from dataclasses import dataclass,asdict
from pathlib import Path

# 导入全局配置
from config import (
    TASKS_DIR, WORKTREES_DIR, WORKDIR, VALID_WT_NAME)




# ======================== 一、Task 持久任务系统 ========================
# 当前会话内存临时待办（轻量内存计划，和磁盘持久Task做区分）
"""
同时保留两层计划
1. **todo_write：内存级临时待办（当前会话专用）**

   数据只放在运行内存里，生命周期仅限本次对话，轻量化、读写极快。

   作用：帮当前这个 Agent 盯住当下的步骤，把当前需求拆成几步待办，防止思考跑偏、跳步骤、忘记当前要做的小事，主打**单人会话内思路稳定**。

2. **task graph 任务图：本地文件持久化任务（`.tasks/task_\*.json`）**

   任务信息落地成磁盘文件，关掉程序再重启、跨会话都不会丢失；支持配置任务依赖关系（A 任务做完才能做 B）、支持多子 Agent 主动认领领取任务。

   作用：面向长期工程、多 Agent 团队协作，是整个项目的全局任务台账，用来统筹长期复杂项目、分配多人 / 多智能体的分工。
   """


@dataclass
class Task:
    """
    任务实体类，每个任务对应 .tasks/task_xxx.json 磁盘文件
    字段全部持久化保存，重启程序数据不丢失
    """
    id: str                  # 全局唯一任务ID
    subject: str             # 任务标题
    description: str         # 任务详细描述
    status: str              # 状态：pending(待认领) / in_progress(进行中) / completed(已完成)
    owner: str | None        # 归属Agent名称，被认领后赋值
    blockedBy: list[str]     # 前置依赖任务ID列表，依赖未完成则当前任务无法认领启动
    worktree: str | None = None  # 绑定的worktree隔离目录名，一个任务对应一套代码环境

def _task_path(task_id: str) -> Path:
    """根据任务ID，拼接任务文件完整路径"""
    return TASKS_DIR / f"{task_id}.json"


def create_task(subject: str, description: str = "",
                blockedBy: list[str] | None = None) -> Task:
    """
    创建新持久任务，自动生成唯一ID，写入磁盘
    :param subject: 任务标题
    :param description: 任务详情
    :param blockedBy: 前置依赖任务ID数组
    :return: 新建的Task对象
    """
    task = Task(
        id=f"task_{int(time.time())}_{random.randint(0, 9999):04d}",
        subject=subject,
        description=description,
        status="pending",    # 新建任务默认：待认领
        owner=None,
        blockedBy=blockedBy or [],
    )
    save_task(task)
    return task

def save_task(task: Task):
    """将Task对象序列化为JSON，写入本地文件持久化"""
    task_path = _task_path(task.id)
    #asdict(task) 把 dataclass 对象 task 一键转换成Python 标准字典 dict，把对象的所有属性变成键值对。 task.id、task.name → {"id": 1, "name": "任务"}。
    #json.dumps 把 Python 对象（字典、列表、数字、字符串等）→ 转换成 JSON 格式的字符串（str）。
    #ensure_ascii=False  关闭 ASCII 强制转义，中文原样输出、原样写入文件
    task_path.write_text(json.dumps(asdict(task), indent=2, ensure_ascii=False))

def load_task(task_id: str) -> Task:
    """读取磁盘任务文件，反序列化为Task对象"""
    file_path = _task_path(task_id)
    content = file_path.read_text(encoding="utf-8")
    #json 格式字符串反序列化为python对象
    return Task(**json.loads(content))


def list_tasks() -> list[Task]:
    """读取目录下所有task文件，按文件名排序，返回全部任务列表"""
    task_files = sorted(TASKS_DIR.glob("task_*.json"))
    task_list = []
    for file in task_files:
        data = json.loads(file.read_text(encoding="utf-8"))
        task_list.append(Task(**data))
    return task_list


def get_task_json(task_id: str) -> str:
    """返回任务完整JSON字符串，供给LLM读取任务详情"""
    return json.dumps(asdict(load_task(task_id)), indent=2, ensure_ascii=False)


def can_start(task_id: str) -> bool:
    """
    判断任务是否可以启动认领
    规则：所有前置依赖任务必须【存在】且【状态为completed完成】
    """
    task = load_task(task_id)
    for dep_id in task.blockedBy:
        dep_file = _task_path(dep_id)
        # 依赖任务文件不存在
        if not dep_file.exists():
            return False
        dep_task = load_task(dep_id)
        # 依赖任务未完成
        if dep_task.status != "completed":
            return False
    return True

def claim_task(task_id: str, owner: str = "agent") -> str:
    """
    认领待执行任务
    :param task_id: 目标任务ID
    :param owner: 认领的Agent名称
    :return: 结果文本（成功/失败原因）
    """
    task = load_task(task_id)
    # 状态必须为pending才能认领
    if task.status != "pending":
        return f"Task {task_id} is {task.status}, cannot claim"
    # 已经被别人认领
    if task.owner:
        return f"Task {task_id} already owned by {task.owner}"
    # 校验前置依赖是否就绪
    if not can_start(task_id):
        # 收集未完成的依赖、不存在的依赖
        unready_deps = [d for d in task.blockedBy
                        if _task_path(d).exists() and load_task(d).status != "completed"]
        missing_deps = [d for d in task.blockedBy if not _task_path(d).exists()]
        msg_parts = []
        if unready_deps:
            msg_parts.append(f"blocked by unfinished tasks: {unready_deps}")
        if missing_deps:
            msg_parts.append(f"missing dependency tasks: {missing_deps}")
        return "Cannot start — " + ", ".join(msg_parts)

    # 认领成功，更新状态与归属
    task.owner = owner
    task.status = "in_progress"
    save_task(task)
    print(f"  \033[36m[claim] {task.subject} → in_progress\033[0m")
    return f"Claimed {task.id} ({task.subject})"

def complete_task(task_id: str) -> str:
    """
    标记任务为完成
    完成后自动扫描所有后继任务，解除对应依赖锁定
    """
    task = load_task(task_id)
    if task.status != "in_progress":
        return f"Task {task_id} is {task.status}, cannot complete"

    task.status = "completed"
    save_task(task)

    # 查找所有待办任务中，以当前任务为前置依赖的后继任务
    unblocked_tasks = []
    all_tasks = list_tasks()
    for t in all_tasks:
        if t.status == "pending" and task_id in t.blockedBy and can_start(t.id):
            unblocked_tasks.append(t.subject)

    print(f"  \033[32m[complete] {task.subject} ✓\033[0m")
    res_msg = f"Completed {task.id} ({task.subject})"
    if unblocked_tasks:
        res_msg += f"\nUnblocked subsequent tasks: {', '.join(unblocked_tasks)}"
    return res_msg

# ======================== 二、Git Worktree 隔离工作区系统 ========================
def validate_worktree_name(name: str) -> str | None:
    """
    名校验：限制worktree命名规则，防止非法路径字符
    返回None代表合法，返回字符串为错误提示
    """
    if not name:
        return "Worktree name cannot be empty"
    if name in (".", ".."):
        return f"'{name}' is invalid name"
    if not VALID_WT_NAME.match(name):
        return (f"Invalid worktree name '{name}': " 
                "Only letters, digits, dots, underscores, dashes, length 1~64")
    return None


def run_git(args: list[str]) -> tuple[bool, str]:
    """
    通用Git命令执行封装
    :param args: git子命令参数列表
    :return: (执行是否成功, 输出信息)
    """
    try:
        proc = subprocess.run(
            ["git"] + args,  # 完整命令列表 拼接完整命令数组，比如 args 是 ["add", "."]，最终命令为 ["git", "add", "."]，列表形式执行命令，规避 shell 注入风险。
            cwd=WORKDIR, #指定工作目录
            capture_output=True, #捕获命令的标准输出stdout + 错误输出stderr
            text=True, #输出内容直接转为字符串 str，而不是原始二进制字节 bytes，方便直接读取打印。
            timeout=30
        )
        output = (proc.stdout + proc.stderr).strip()
        # proc.returncode 是操作系统命令的退出码 ✅ 0 = 执行完全成功，没有报错 ❌ 非 0 数字（1、128、127 等）= 命令执行失败
        return proc.returncode == 0, output[:5000] if output else "(no output)"
    except subprocess.TimeoutExpired:
        return False, "Error: git command timeout"


def log_worktree_event(event_type: str, worktree_name: str, task_id: str = ""):
    """记录worktree操作事件日志，写入events.jsonl"""
    event_info = {
        "type": event_type,
        "worktree": worktree_name,
        "task_id": task_id,
        "timestamp": time.time()
    }
    event_file = WORKTREES_DIR / "events.jsonl" #每行一条独立 JSON，换行分割
    with open(event_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(event_info) + "\n")


def bind_task_to_worktree(task_id: str, worktree_name: str):
    """将任务与worktree绑定，写入task的worktree字段，一一关联"""
    task = load_task(task_id)
    task.worktree = worktree_name
    save_task(task)

def _count_worktree_changes(path: Path) -> tuple[int, int]:
    """
    统计worktree目录内改动：未提交文件数量、本地新增提交数量
    用于删除前判断是否存在未保存代码，防止误删代码
    """
    try:
        # 统计未提交文件
        res1 = subprocess.run(
            ["git", "status", "--porcelain"], #简洁模式查看工作区文件变更
            cwd=path, capture_output=True, text=True, timeout=10
        )
        file_count = len([line for line in res1.stdout.strip().splitlines() if line.strip()])
        # 统计push后新增的本地commit
        res2 = subprocess.run(
            ["git", "log", "@{push}..HEAD", "--oneline"], #查看本地有、远程还没有的提交记录。
            cwd=path, capture_output=True, text=True, timeout=10
        )
        commit_count = len([line for line in res2.stdout.strip().splitlines() if line.strip()])
        return file_count, commit_count
    except Exception:
        # 读取失败返回负数，判定无法校验状态
        return -1, -1


def create_worktree(name: str, task_id: str = "") -> str:
    """
    创建全新Git Worktree隔离目录，配套独立分支 wt/xxx
    可选绑定任务ID，创建后自动关联任务
    """
    # 名校验前置拦截
    err_msg = validate_worktree_name(name)
    if err_msg:
        return f"Error: {err_msg}"
    # 绑定任务时，校验任务必须存在
    if task_id:
        try:
            load_task(task_id)
        except FileNotFoundError:
            return f"Error: task {task_id} not found"
    wt_path = WORKTREES_DIR / name
    if wt_path.exists():
        return f"Worktree '{name}' already exists at {wt_path}"

    # 执行git worktree创建命令，新建独立分支
    #-b = 创建新分支，分支名固定前缀 wt/，用来标识这是工作树专属分支。
    #git branch wt/name HEAD：在本地仓库创建分支 wt/name，起点为当前 HEAD。
    #git worktree add wt_path wt/name： 把已经建好的 wt/name 分支，挂载到 wt_path 文件夹，生成工作树。
    ok, result = run_git(["worktree", "add", str(wt_path), "-b", f"wt/{name}", "HEAD"])
    if not ok:
        return f"Git create failed: {result}"

    # 绑定任务 + 写入操作日志
    if task_id:
        bind_task_to_worktree(task_id, name)
    log_worktree_event("create", name, task_id)
    print(f"  \033[33m[worktree] created: {name} at {wt_path}\033[0m")
    return f"Worktree '{name}' created successfully, path: {wt_path}"


def remove_worktree(name: str, discard_changes: bool = False) -> str:
    """
    删除worktree工作区
    默认保护：存在未提交文件/提交时禁止删除；discard_changes=True强制销毁
    discard_changes：布尔开关
    False = 非强制模式（默认安全模式），不允许直接删掉有改动的工作区
    True = 强制模式，直接丢弃本地所有修改 + 本地提交，强行删除
    """
    err_msg = validate_worktree_name(name)
    if err_msg:
        return err_msg
    wt_path = WORKTREES_DIR / name
    if not wt_path.exists():
        return f"Worktree '{name}' not found"

    # 非强制模式，检查代码改动
    if not discard_changes:
        file_num, commit_num = _count_worktree_changes(wt_path)
        if file_num < 0:
            return "Cannot verify workspace status. Set discard_changes=true to force remove."
        if file_num > 0 or commit_num > 0:
            return (f"Worktree '{name}' has {file_num} modified files, {commit_num} local commits. "
                    "Use discard_changes=true to force delete or keep_worktree for reserve.")
    #分支是逻辑指针，工作目录 (Worktree) 是看得见的代码文件夹
    # 强制移除worktree目录，同步删除对应分支
    # 一条分支，同一时间只能被 1 个工作目录占用，防止多进程同时修改同一条分支冲突。
    #工作目录（wt_path 文件夹）= 独立办公室 作用：提供场地，让 Agent 进来干活、读写文件、运行程序。办公室随时可以拆掉。
    #分支 wt/name = 这间办公室专属的代码卷宗（版本线） 作用：记录所有修改痕迹、所有提交版本，归属这条任务，可保存、可推送、可复用。
    ok1, _ = run_git(["worktree", "remove", str(wt_path), "--force"])
    if not ok1:
        return f"Failed to remove worktree '{name}'"
    run_git(["branch", "-D", f"wt/{name}"])

    log_worktree_event("remove", name)
    print(f"  \033[33m[worktree] removed: {name}\033[0m")
    return f"Worktree '{name}' removed completed"


def keep_worktree(name: str) -> str:
    """保留worktree目录，标记为人工审阅，不自动删除"""
    err_msg = validate_worktree_name(name)
    if err_msg:
        return err_msg
    log_worktree_event("keep", name)
    return f"Worktree '{name}' reserved for manual review, branch name: wt/{name}"


# ── Lead Worktree Tools ──

def run_create_worktree(name: str, task_id: str = "") -> str:
    return create_worktree(name, task_id)

def run_remove_worktree(name: str, discard_changes: bool = False) -> str:
    return remove_worktree(name, discard_changes)

def run_keep_worktree(name: str) -> str:
    return keep_worktree(name)

# ── Basic tool handlers ──

def run_create_task(subject: str, description: str = "",
                    blockedBy: list[str] | None = None) -> str:
    task = create_task(subject, description, blockedBy)
    deps = f" (blockedBy: {', '.join(blockedBy)})" if blockedBy else ""
    print(f"  \033[34m[create] {task.subject}{deps}\033[0m")
    return f"Created {task.id}: {task.subject}{deps}"


def run_list_tasks() -> str:
    tasks = list_tasks()
    if not tasks:
        return "No tasks."
    return "\n".join(
        f"  {t.id}: {t.subject} [{t.status}]"
        + (f" (wt:{t.worktree})" if t.worktree else "")
        for t in tasks)


def run_get_task(task_id: str) -> str:
    try:
        return get_task_json(task_id)
    except FileNotFoundError:
        return f"Error: task {task_id} not found"

def run_claim_task(task_id: str) -> str:
    try:
        return claim_task(task_id, owner="agent")
    except FileNotFoundError:
        return f"Error: task {task_id} not found"

def run_complete_task(task_id: str) -> str:
    try:
        return complete_task(task_id)
    except FileNotFoundError:
        return f"Error: task {task_id} not found"


