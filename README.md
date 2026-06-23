Comprehensive Agent 系统功能说明
一、系统概述
是一个功能完整的 AI 编程助手系统，基于 Anthropic Claude API 构建，具备工具调用、任务管理、多智能体协作、定时调度、上下文压缩等企业级能力。

核心定位：一个可自主执行编程任务、支持团队协作的 AI Agent 框架。

二、系统架构
text
┌─────────────────────────────────────────────────────────────┐
│                     CLI 交互主循环                          │
├─────────────────────────────────────────────────────────────┤
│  提示词组装  →  LLM推理  →  工具执行  →  结果回写          │
├─────────────────────────────────────────────────────────────┤
│  上下文压缩  │  权限钩子  │  后台任务  │  定时调度          │
├─────────────────────────────────────────────────────────────┤
│  任务管理    │  工作树    │  队友协作  │  MCP扩展           │
└─────────────────────────────────────────────────────────────┘
三、核心功能模块
1. 内置工具系统
工具名	功能	说明
bash	执行 Shell 命令	支持后台运行、慢速任务自动异步
read_file	读取文件	支持行偏移、行数限制
write_file	写入/覆盖文件	自动创建父目录
edit_file	精确文本替换	单次替换，原子操作
glob	文件匹配搜索	限定工作目录内
todo_write	待办清单管理	支持 pending/in_progress/completed
task	启动子代理	一次性专项任务执行
load_skill	加载技能	动态加载 SKILL.md 定义
2. 任务管理系统
持久化任务 (Task Graph)

python
# 任务实体
Task {
    id: string          # 唯一标识
    subject: string     # 标题
    description: string # 描述
    status: pending | in_progress | completed
    owner: string       # 归属Agent
    blockedBy: list     # 前置依赖任务ID
    worktree: string    # 绑定的工作树
}
功能清单：

✅ 创建任务（create_task）

✅ 查看所有任务（list_tasks）

✅ 获取任务详情（get_task）

✅ 认领任务（claim_task）

✅ 完成任务（complete_task）

✅ 任务依赖关系（blockedBy）

设计理念：

磁盘持久化（.tasks/task_*.json）

跨会话保留

支持多 Agent 认领分工

3. Git Worktree 工作树隔离
功能：为每个任务创建独立的代码工作区

bash
# 创建隔离工作树
create_worktree(name="feature-auth", task_id="task_xxx")

# 目录结构
.worktrees/
├── feature-auth/          # 独立工作目录
│   └── (代码文件)
└── events.jsonl           # 操作日志
工具清单：

create_worktree — 创建隔离工作区

remove_worktree — 删除工作区（含变更保护）

keep_worktree — 保留供人工审阅

安全特性：

名校验（仅允许 [A-Za-z0-9._-]）

变更保护（存在未提交文件时拒绝删除）

分支隔离（每个工作树对应 wt/xxx 分支）

4. 多智能体协作系统
4.1 队友线程 (Teammate)
python
# 启动一个常驻队友
spawn_teammate(
    name="backend-dev",
    role="后端开发",
    prompt="负责用户认证模块开发"
)
队友特性：

✅ 独立后台线程运行

✅ 自主认领任务

✅ 隔离工作目录

✅ 自主工具调用

✅ 方案审批流程

4.2 消息总线 (Message Bus)
基于 JSONL 文件的异步通信：

text
.mailboxes/
├── lead.jsonl          # 主控收件箱
├── backend-dev.jsonl   # 队友收件箱
└── frontend-dev.jsonl
通信协议：

message — 普通消息

shutdown_request — 关机请求

plan_approval_request — 方案审批请求

plan_approval_response — 审批回执

4.3 审批协议 (Protocol)
text
┌────────┐  提交方案   ┌────────┐
│ 队友   │ ──────────→ │  Lead  │
│        │  等待审批   │        │
│        │ ←────────── │ 审批   │
└────────┘  回执      └────────┘
工具清单：

send_message — 发送消息给队友

check_inbox — 查看收件箱

request_shutdown — 请求队友关机

request_plan — 请求队友提交方案

review_plan — 审批方案

5. 定时任务系统 (Cron)
支持标准 5 位 Cron 表达式：

字段	范围	说明
minute	0-59	分钟
hour	0-23	小时
dom	1-31	日期
month	1-12	月份
dow	0-6	星期
功能特性：

✅ 标准 Cron 表达式（支持 *、*/N、,、-）

✅ 单次执行 (recurring=false)

✅ 持久化存储（重启不丢失）

✅ 后台调度线程（秒级扫描）

✅ 任务队列消费

工具清单：

schedule_cron — 创建定时任务

list_crons — 查看所有任务

cancel_cron — 取消任务

示例：

python
# 每分钟执行一次
schedule_cron(cron="* * * * *", prompt="检查服务状态")

# 明天 9:30 执行一次
schedule_cron(cron="30 9 * * *", prompt="发送日报", recurring=False)
6. 后台异步任务
自动识别慢速命令：

python
# 自动后台运行
install, build, test, deploy, compile,
docker build, pip install, npm install,
cargo build, pytest, make
工作原理：

检测到慢速命令 → 启动后台线程

立即返回任务 ID

主线程继续工作

完成后注入结果通知

python
# 手动后台运行
bash(command="npm install", run_in_background=True)
# 返回: [Background task bg_0001 started]
7. 上下文压缩系统
五级压缩策略：

级别	策略	触发条件
1	超大工具结果落地文件	>30KB
2	老旧工具结果精简占位	>3条结果
3	中段消息裁剪	>50条消息
4	LLM 摘要压缩	>50KB
5	应急被动压缩	API 报错超长
特性：

✅ 自动保护工具调用链完整性

✅ 完整对话存档（.transcripts/）

✅ 超大输出持久化（.task_outputs/tool-results/）

8. 权限与钩子系统
8.1 权限管控
高危命令黑名单：

python
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if="]
DESTRUCTIVE = ["rm ", "> /etc/", "chmod 777"]
拦截策略：

完全禁止 → 直接拒绝

破坏性操作 → 二次确认

路径逃逸 → 拒绝

MCP 部署工具 → 二次确认

8.2 钩子事件
事件	触发时机	用途
UserPromptSubmit	用户提交输入	日志、审计
PreToolUse	工具执行前	权限校验、拦截
PostToolUse	工具执行后	日志、大输出告警
Stop	本轮循环结束	统计、清理
9. 技能系统 (Skills)
目录结构：

text
skills/
├── code-review/
│   └── SKILL.md         # 包含 YAML Frontmatter
├── pdf-processing/
│   └── SKILL.md
└── agent-builder/
    └── SKILL.md
SKILL.md 格式：

markdown
---
name: code-review
description: 代码审查与审计
---

# Code Review Skill

## 使用场景
...
工具：

list_skills — 查看所有可用技能

load_skill(name) — 加载完整技能内容

10. MCP 扩展系统
内置 Mock 服务：

服务名	工具	说明
docs	search, get_version	文档查询
deploy	trigger, status	部署管理
工具命名规范：

text
mcp__{服务名}__{工具名}
例: mcp__docs__search
使用方式：

python
# 连接 MCP 服务
connect_mcp(name="docs")

# 调用工具
mcp__docs__search(query="API 参考")
11. 子代理系统 (Subagent)
特性：

✅ 一次性同步执行

✅ 用完即销毁

✅ 只开放基础工具（bash/read/write/edit/glob）

✅ 隔离上下文（不污染主对话）

✅ 返回最终摘要

使用场景：

专项文件处理

独立脚本执行

大型代码重构

四、错误恢复机制
1. 指数退避重试
尝试次数	等待时间
1	0.5s + 抖动
2	1.0s + 抖动
3	2.0s + 抖动
4	4.0s + 抖动
...	上限 32s
2. 自动降级
text
连续 2 次 529 过载 → 自动切换到备用模型
3. 容灾状态
python
RecoveryState {
    has_escalated: bool              # 是否已升级 Token 上限
    recovery_count: int              # 恢复重试次数
    consecutive_529: int             # 连续 529 计数
    has_attempted_reactive_compact: bool  # 是否已应急压缩
    current_model: string            # 当前使用的模型
}
五、快速开始
安装依赖
bash
pip install anthropic python-dotenv pyyaml
配置环境变量
bash
# .env
ANTHROPIC_API_KEY=sk-xxx
MODEL_ID=claude-3-5-sonnet-20241022
FALLBACK_MODEL_ID=claude-3-opus-20240229
ANTHROPIC_BASE_URL=https://api.anthropic.com
运行
bash
python code.py
交互示例
text
 yuan >> 帮我创建一个任务：实现用户登录功能
 yuan >> 创建一个工作树用于开发
 yuan >> schedule_cron cron="30 9 * * *" prompt="发送日报" recurring=false
 yuan >> spawn_teammate name="前端开发" role="前端" prompt="开发登录页面"
六、文件结构
text
 yuan_comprehensive/
├── code.py                    # 主程序入口（所有功能整合）
├── config.py                  # 全局配置
├── tool_system.py             # 工具系统核心
├── hooks_permission.py        # 权限与钩子系统
├── background_task.py         # 后台任务管理
├── compact_context.py         # 上下文压缩
├── cron_scheduler.py          # 定时调度
├── error_recovery.py          # 错误恢复
├── mcp_client.py              # MCP 客户端
├── message_bus.py             # 消息总线
├── protocol_core.py           # 协作协议
├── skill_manager.py           # 技能管理
├── sub_agent.py               # 子代理
├── task_worktree.py           # 任务与工作树
├── teammate_runtime.py        # 队友运行时
├── models.py                  # 数据模型
├── .env                       # 环境变量
├── skills/                    # 技能目录
├── .tasks/                    # 任务持久化
├── .worktrees/                # 工作树目录
├── .mailboxes/                # 消息邮箱
├── .memory/                   # 长期记忆
└── .transcripts/              # 对话存档
七、系统优势
特性	说明
模块化	各功能独立模块，易于扩展
持久化	任务、记忆、对话全部落地磁盘
协作	支持多 Agent 团队协作
安全	权限钩子、路径校验、命令黑名单
可靠	指数退避、自动降级、异常恢复
高效	上下文压缩、后台任务、定时调度
八、注意事项
运行环境：设计为 Linux/macOS，Windows 需使用 WSL2

API Key：需要有效的 Anthropic API Key

Git 依赖：Worktree 功能需要 Git 已安装

权限：部分操作需要用户交互确认

文档版本: 1.0 | 最后更新: 2026-06-23