"""
compact_context.py
上下文分级压缩 + 会话持久存档 + 历史恢复 整合模块
当前已完成功能：
1. 五级分级主动压缩策略（由轻到重，全部编码实现）：
   ① 单轮工具结果超大文本落地持久化  ② 老旧历史工具结果精简占位
   ③ 中段消息区段裁剪保留首尾关键对话  ④ 全局LLM大模型摘要浓缩历史会话
   ⑤ 上下文超限报错时的被动应急兜底压缩，层层规避token/字符长度溢出
2. 会话持久化：完整对话自动写入jsonl日志存档，完整落地原始会话文件，持久目录已配置完成。

规划预留恢复能力（底层文件已落地，读取还原函数待后续开发）：
可基于存档的transcript日志回放完整历史上下文，可读取TOOL_RESULTS_DIR持久化超长工具输出文件还原完整原始内容。

全局依赖 config.py 统一路径、阈值、模型客户端等配置参数
"""
import json
from pathlib import Path
from anthropic import Anthropic
from config import (
    client, MODEL, TRANSCRIPT_DIR, TOOL_RESULTS_DIR,
     KEEP_RECENT_TOOL_RESULTS, PERSIST_THRESHOLD,

)
# ---------------------- 基础工具函数 ----------------------
def estimate_size(messages: list) -> int:
    """估算上下文总字符长度"""
    return len(json.dumps(messages, default=str))

def block_type(block):
    """兼容对象/字典，获取区块类型"""
    return block.get("type") if isinstance(block, dict) else getattr(block, "type", None)

def message_has_tool_use(message: dict) -> bool:
    """判断消息是否包含 tool_use 工具调用"""
    if message.get("role") != "assistant":
        return False
    content = message.get("content")
    if not isinstance(content, list):
        return False
    return any(block_type(block) == "tool_use" for block in content)

def is_tool_result_message(message: dict) -> bool:
    """判断消息是否为工具结果返回消息"""
    if message.get("role") != "user":
        return False
    content = message.get("content")
    if not isinstance(content, list):
        return False
    return any(isinstance(block, dict) and block.get("type") == "tool_result"
               for block in content)

def collect_tool_results(messages: list):
    """遍历所有消息，收集全部工具结果区块"""
    found = []
    for mi, msg in enumerate(messages):
        content = msg.get("content")
        if msg.get("role") != "user" or not isinstance(content, list):
            continue
        for bi, block in enumerate(content):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                found.append((mi, bi, block))
    return found

# ---------------------- 超大输出持久化（落地文件） ----------------------
def persist_large_output(tool_use_id: str, output: str) -> str:
    """超长工具结果写入本地文件，上下文只保留预览+路径"""
    if len(output) <= PERSIST_THRESHOLD:
        return output
    TOOL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = TOOL_RESULTS_DIR / f"{tool_use_id}.txt"
    if not path.exists():
        path.write_text(output, encoding="utf-8")
    return (f"<persisted-output>\nFull output file: {path}\n"
            f"Preview content:\n{output[:2000]}\n</persisted-output>")


def tool_result_budget(messages: list, max_bytes: int = 200_000) -> list:
    """
    本轮工具返回结果大小节流控制，防止上下文超长超限
    逻辑：统计本轮所有tool_result总字节，超出阈值时，将体积最大的工具结果转为文件持久化，用文件路径替换原文内容，缩减上下文内存
    :param messages: 当前完整对话上下文列表
    :param max_bytes: 本轮工具结果总字节上限，默认200KB
    :return: 处理后的上下文列表（超限则替换大内容为持久化文件路径）
    """
    #-----1.前置判定，非工具结果直接放行
    # 上下文为空，直接原样返回
    if not messages:
        return messages

    # 取最后一轮消息，工具结果一定是上一轮模型调用后的user返回
    last = messages[-1]
    content = last.get("content")

    # 校验：最后一条必须是user角色，且content为数组格式，否则无需处理，直接返回原消息
    if last.get("role") != "user" or not isinstance(content, list):
        return messages
    #------2. 筛选本轮全部工具结果块
    # 筛选出本轮所有tool_result类型的结果，记录下标与块对象
    blocks = [(i, b) for i, b in enumerate(content)
              if isinstance(b, dict) and b.get("type") == "tool_result"]

    # 计算当前所有工具结果文本内容的总字节长度
    total = sum(len(str(b.get("content", ""))) for _, b in blocks)

    # 总大小未超限，无需压缩持久化，直接返回原始上下文
    if total <= max_bytes:
        return messages

    # 贪心策略：优先压缩最大的内容块（最高效） 按照内容长度从大到小排序，优先压缩体积最大的工具返回内容，效率最高
    for _, block in sorted(blocks, key=lambda pair: len(str(pair[1].get("content", ""))), reverse=True):
        # 总容量已经达标，退出循环
        if total <= max_bytes:
            break
        # 取出当前大块的原始文本
        text = str(block.get("content", ""))
        # 将超长文本落地写入本地文件，content替换为文件路径/索引标识
        block["content"] = persist_large_output(block.get("tool_use_id", "unknown"), text)
        # 重新计算剩余总字节
        total = sum(len(str(b.get("content", ""))) for _, b in blocks)

    return messages

# ---------------------- 旧工具结果精简 + 区段消息裁剪 ----------------------
def micro_compact(messages: list) -> list:
    """只保留最近N条完整工具结果，更早的工具结果直接精简占位"""
    tool_results = collect_tool_results(messages)
    if len(tool_results) <= KEEP_RECENT_TOOL_RESULTS:
        return messages
    # 压缩前面旧的结果，保留末尾最新的
    for _, _, block in tool_results[:-KEEP_RECENT_TOOL_RESULTS]:
        if len(str(block.get("content", ""))) > 120:
            block["content"] = "[Earlier tool result compacted. Re-run tool if you need full content.]"
    return messages

def snip_compact(messages: list, max_messages: int = 50) -> list:
    """中段批量裁剪消息，保留头部少量上下文 + 完整尾部对话，中间用占位消息代替
    核心保护规则：严禁拆分 tool_use 调用 和 配套 tool_result 返回结果，保证工具会话完整性
    :param messages: 完整对话上下文列表
    :param max_messages: 允许保留的最大消息条数，默认50条
    :return: 裁剪后的新上下文列表
    """
    if len(messages) <= max_messages:
        return messages
    # 初始分割下标：头部固定留前3条，尾部起始下标 = 总长度 - 尾部预留条数
    head_end, tail_start = 3, len(messages) - (max_messages - 3)
    # 边界保护：不能剪断 tool_use 与对应的 tool_result
    #判断逻辑：如果头部最后一条消息（head_end - 1）是模型的tool_use工具调用；
    # 向后顺延下标，把紧随其后所有配套的tool_result工具返回结果，全部划入头部保留区；
    # 目的：防止把「工具调用指令」留在头部，「工具返回结果」被裁进中间删除区，导致模型上下文逻辑断裂。
    if head_end > 0 and message_has_tool_use(messages[head_end - 1]):
        while head_end < len(messages) and is_tool_result_message(messages[head_end]):
            head_end += 1
    #判断逻辑：尾部起始位置刚好是一条tool_result，且它的上一条是对应的tool_use调用；
    #将尾部起始下标向前移 1 位，把整套工具调用 + 结果完整划入尾部保留区；
    #杜绝工具调用被拆分在裁剪分界线两侧。
    if (tail_start > 0 and tail_start < len(messages)
            and is_tool_result_message(messages[tail_start])
            and message_has_tool_use(messages[tail_start - 1])):
        tail_start -= 1
    # 容错：如果修正下标后，头部区间已经覆盖尾部区间，不执行裁剪
    if head_end >= tail_start:
        return messages
    snipped_count = tail_start - head_end   #统计被裁剪的消息数量
    return (messages[:head_end]
            + [{"role": "user", "content": f"[snipped {snipped_count} historical conversation messages]"}]
            + messages[tail_start:])
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

# ---------------------- 完整对话存档 + LLM全局摘要压缩 ----------------------
def write_transcript(messages: list) -> Path:
    """压缩前完整对话写入日志文件存档"""
    import time
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str, ensure_ascii=False) + "\n")
    return path

def summarize_history(messages: list) -> str:
    """调用模型，生成完整对话总结摘要"""
    conversation = json.dumps(messages, default=str, ensure_ascii=False)[:80000]
    prompt = ("Summarize this coding-agent conversation concisely. "
              "Keep core goals, key findings, modified files, remaining tasks and user constraints.\n\n" + conversation)
    response = client.messages.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2000)
    return extract_plain_text(response.content) or "(Empty conversation summary)"

def compact_history(messages: list) -> list:
    """全量压缩：存档完整日志 + 生成摘要，上下文替换为摘要"""
    transcript_path = write_transcript(messages)
    print(f"  \033[36m[compact] Full transcript saved to: {transcript_path}\033[0m")
    summary = summarize_history(messages)
    return [{"role": "user", "content": f"[Full conversation compacted]\n\n{summary}"}]

# ---------------------- 超长上下文报错 被动兜底压缩 ----------------------
def reactive_compact(messages: list) -> list:
    """触发上下文超长报错时的应急压缩，保留末尾最新几条对话 + 摘要"""
    transcript_path = write_transcript(messages)
    print(f"  \033[31m[reactive compact] Oversized context, transcript saved: {transcript_path}\033[0m")
    try:
        summary = summarize_history(messages)
    except Exception:
        summary = "Previous conversation was trimmed due to context length limit exceeded."
    tail_start = max(0, len(messages) - 5)
    # 边界保护，不切断工具调用链路
    if (tail_start > 0 and tail_start < len(messages)
            and is_tool_result_message(messages[tail_start])
            and message_has_tool_use(messages[tail_start - 1])):
        tail_start -= 1
    return [{"role": "user", "content": f"[Reactive emergency compact]\n\n{summary}"},
            *messages[tail_start:]]