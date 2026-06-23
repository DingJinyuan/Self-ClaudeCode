"""
config.py
Comprehensive Agent 全局配置文件
统一存放：路径常量、模型参数、阈值配置、环境变量、全局正则、公共初始化
"""
import os
import re
import threading
from pathlib import Path
from typing import Dict, List, Callable

from dotenv import load_dotenv
from anthropic import Anthropic
from models import CronJob,ProtocolState,MCPClient

# ======================== 环境变量加载 ========================
# 加载.env环境文件
load_dotenv(override=True)
# 清理冗余认证参数
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)


# ======================== 工作根目录定义 ========================
WORKDIR = Path.cwd()
# 业务子目录

# 【技能目录】存放各个技能文件夹，每个技能自带SKILL.md定义规则，程序启动自动扫描加载
SKILLS_DIR = WORKDIR / "skills"
# 【会话归档目录】上下文压缩时，完整对话日志会保存至此，用于历史回溯
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
# 【超大工具输出目录】工具返回超长内容时，文本落地写入此处，上下文只存放预览与文件路径
TOOL_RESULTS_DIR = WORKDIR / ".task_outputs" / "tool-results"
# 【持久任务目录】存储task任务json文件，实现跨会话任务保存、任务依赖、多Agent认领
TASKS_DIR = WORKDIR / ".tasks"
# 【Git隔离工作区目录】存放每个任务独立的git worktree代码环境，实现代码修改环境隔离
WORKTREES_DIR = WORKDIR / ".worktrees"
# 【消息总线邮箱目录】以agent名称作为收件箱文件，用于多队友Agent之间收发消息、协议审批
MAILBOX_DIR = WORKDIR / ".mailboxes"
# 【长期记忆目录】存放MEMORY.md记忆文件，系统prompt组装时读取长期记忆内容
MEMORY_DIR = WORKDIR / ".memory"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
# 【定时任务持久化文件】json文件，持久保存cron定时任务，程序重启自动加载定时配置
DURABLE_PATH = WORKDIR / ".scheduled_tasks.json"

# 自动创建所有目录
for dir_path in [
    SKILLS_DIR, TRANSCRIPT_DIR, TOOL_RESULTS_DIR, TASKS_DIR,
    WORKTREES_DIR, MAILBOX_DIR, MEMORY_DIR
]:
    dir_path.mkdir(exist_ok=True, parents=True)

# ======================== LLM 模型配置 ========================
# 模型实例
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]
PRIMARY_MODEL = MODEL
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL_ID")

# Token 限制
DEFAULT_MAX_TOKENS = 8000
ESCALATED_MAX_TOKENS = 16000

# ======================== 重试&错误恢复阈值 ========================
MAX_RETRIES = 3                  # 单次接口最大重试次数
MAX_CONSECUTIVE_529 = 2          # 连续529过载次数上限
MAX_RECOVERY_RETRIES = 2         # 上下文超长兜底重试次数
BASE_DELAY_MS = 500              # 指数退避基础毫秒延迟

# 上下文压缩阈值
CONTEXT_LIMIT = 50000            # 上下文总长度上限（字符）
KEEP_RECENT_TOOL_RESULTS = 3     # 保留最近N条完整工具结果
PERSIST_THRESHOLD = 30000        # 超大输出落地文件阈值

# ======================== 定时&空闲轮询配置 ========================
IDLE_POLL_INTERVAL = 5           # 队友空闲轮询间隔(秒)
IDLE_TIMEOUT = 60                # 空闲超时自动休眠时长(秒)

# ======================== 权限黑名单配置 ========================
# 高危命令拦截名单
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if="]
# 高风险操作（需要二次确认）
DESTRUCTIVE = ["rm ", "> /etc/", "chmod 777"]

# ======================== 正则校验规则 ========================
# Worktree名称正则：字母、数字、._-，长度1~64
VALID_WT_NAME = re.compile(r'^[A-Za-z0-9._-]{1,64}$')
# MCP名称非法字符过滤
_DISALLOWED_CHARS = re.compile(r'[^a-zA-Z0-9_-]')

# ======================== 终端UI常量 ========================
CONTINUATION_PROMPT = "Continue from the previous response. Do not repeat completed work."
PROMPT = "\033[36m yuan >> \033[0m"
CLI_ACTIVE = False

# ======================== 全局空容器（全局状态） ========================
# # 全局技能注册表，程序启动/刷新时缓存
SKILL_REGISTRY: dict[str, dict] = {}

BUILTIN_TOOLS: List[dict] = []
BUILTIN_HANDLERS: Dict[str, Callable] = {}

# MCP客户端连接池
mcp_clients: dict[str, MCPClient] = {}

# Cron定时调度全局变量
# 1. 任务注册表
scheduled_jobs: Dict[str, CronJob] = {}
# 2. 待执行队列
cron_queue: List[CronJob] = []
# 3. 线程锁
cron_lock = threading.Lock()
# 4. 上次触发时间记录
_last_fired: Dict[str, str] = {}

# ---------------- 后台异步工具任务 全局变量 ----------------
_bg_counter = 0
background_tasks: Dict[str, dict] = {}
background_results: Dict[str, str] = {}
background_lock = threading.Lock()

# 活跃常驻队友
# 队友Agent激活状态注册表，key=队友ID，value=是否启用常驻
active_teammates: dict[str, bool] = {}

# 协议待审批请求池
pending_requests: dict[str, ProtocolState] = {}
# 当前会话内存todo
CURRENT_TODOS: list[dict] = []

