#!/usr/bin/env python3
"""
error_recovery.py
LLM调用异常重试与降级恢复模块
功能：指数退避重试、429限流重试、529过载自动切备用模型、上下文超长异常识别、恢复状态管理
依赖：config.py
"""
import random,time
from typing import Callable, Any
from config import (
    MAX_RETRIES, MAX_CONSECUTIVE_529, BASE_DELAY_MS,
    PRIMARY_MODEL, FALLBACK_MODEL
)

class RecoveryState:
    """全局恢复状态实例，全程跟随单次会话LLM调用生命周期"""
    def __init__(self):
        self.has_escalated = False               # 是否已经升级max_tokens上限
        self.recovery_count = 0                  # 超长上下文恢复重试次数
        self.consecutive_529 = 0                 # 连续529过载计数
        self.has_attempted_reactive_compact = False  # 是否执行过应急压缩
        self.current_model = PRIMARY_MODEL       # 当前使用模型

def retry_delay(attempt: int) -> float:
    """指数退避算法 + 随机抖动，防止请求雪崩
    底数：BASE_DELAY_MS，指数2^attempt，上限32秒，附加0~25%随机抖动
    """
    # 1. 指数计算基础延时，强制封顶最大值32000ms = 32秒
    base_ms = min(BASE_DELAY_MS * (2 ** attempt), 32000)
    # 毫秒转为秒，sleep入参单位为秒
    base_sec = base_ms / 1000
    # 2. 随机抖动：生成 0 ~ 当前基础时长25% 的随机小数
    jitter = random.uniform(0, base_sec * 0.25)
    # 3. 最终等待时间 = 基础时长 + 随机抖动值
    return base_sec + jitter

def with_retry(fn: Callable[[], Any], state: RecoveryState) -> Any:
    """通用重试装饰器函数
    区分 429(限流) / 529(服务过载) 做专属重试逻辑，其余异常直接抛出
    """
    for attempt in range(MAX_RETRIES):
        try:
            result = fn()
            # 请求成功，清空连续529计数，重置过载状态
            state.consecutive_529 = 0
            return result
        except Exception as e:
            # 捕获异常，提取异常类名、异常文本，转小写方便判断
            err_type = type(e).__name__.lower()
            err_msg = str(e).lower()

            # 1、429 请求频率超限 / 限流
            is_429 = "ratelimit" in err_type or "429" in err_msg or "too many requests" in err_msg
            if is_429:
                # 计算指数退避等待时长
                delay = retry_delay(attempt)
                print(f"  \033[33m[429 限流] 重试 {attempt + 1}/{MAX_RETRIES}，等待 {delay:.1f}s\033[0m")
                time.sleep(delay)
                continue

            # 2、529 服务过载繁忙
            is_529 = "529" in err_msg or "overloaded" in err_msg or "service busy" in err_msg
            if is_529:
                # 连续过载次数 +1
                state.consecutive_529 += 1
                # 连续过载次数达标，自动切换备用模型
                if state.consecutive_529 >= MAX_CONSECUTIVE_529 and FALLBACK_MODEL:
                    state.current_model = FALLBACK_MODEL
                    state.consecutive_529 = 0
                    print(f"  \033[31m[529 连续过载] 自动切换备用模型: {FALLBACK_MODEL}\033[0m")
                delay = retry_delay(attempt)
                print(f"  \033[33m[529 过载] 重试 {attempt + 1}/{MAX_RETRIES}，等待 {delay:.1f}s\033[0m")
                time.sleep(delay)
                continue

            # 非限流/过载异常，直接向上抛出，不走重试
            raise
    # 全部重试耗尽，抛出最终异常
    raise RuntimeError(f"调用失败，已达到最大重试次数 {MAX_RETRIES}")

def is_prompt_too_long_error(e: Exception) -> bool:
    """精准判断【上下文超长】异常（Anthropic Claude 标准报错关键字）
    精准命中官方三类报错，是触发 reactive_compact 应急压缩的唯一判定条件
    """
    err_msg = str(e).lower()
    keywords = [
        "prompt too long",
        "context_length_exceeded",
        "max_context_window",
        "token limit exceeded",
        "input tokens exceed"
    ]
    return any(k in err_msg for k in keywords)