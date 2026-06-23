"""
cron_scheduler.py
Cron定时任务调度模块
功能：Cron表达式校验、持久化存储、后台调度线程、定时任务触发入队、增删查任务
给你的 Agent 系统做内置定时任务引擎，支持用标准 Linux Cron 表达式，定时自动执行 Prompt 指令，配套增删任务、持久化重启保留、后台秒级扫描调度、任务队列消费完整能力。
依赖：config.py
"""
import json
import threading,random,time
from dataclasses import dataclass, asdict
from datetime import datetime

from typing import List, Optional

# cron_scheduler.py 导入
from config import (
    scheduled_jobs,
    cron_queue,
    cron_lock,
    _last_fired,
    DURABLE_PATH
)

from models import CronJob



def _cron_field_matches(field: str, value: int) -> bool:
    """单段cron字段匹配判断：支持 *、*/步长、逗号枚举、横杠区间"""
    if field == "*":
        return True
    if field.startswith("*/"):
        step = int(field[2:])
        return step > 0 and value % step == 0
    if "," in field:
        return any(_cron_field_matches(part.strip(), value) for part in field.split(","))
    if "-" in field:
        lo, hi = field.split("-", 1)
        return int(lo) <= value <= int(hi)
    return value == int(field)


def cron_matches(cron_expr: str, dt: datetime) -> bool:
    """完整5位cron表达式校验匹配当前时间"""
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return False
    minute, hour, dom, month, dow = fields
    dow_val = (dt.weekday() + 1) % 7
    m_ok = _cron_field_matches(minute, dt.minute)
    h_ok = _cron_field_matches(hour, dt.hour)
    month_ok = _cron_field_matches(month, dt.month)
    dom_ok = _cron_field_matches(dom, dt.day)
    dow_ok = _cron_field_matches(dow, dow_val)

    if not (m_ok and h_ok and month_ok):
        return False
    if dom == "*" and dow == "*":
        return True
    if dom == "*":
        return dow_ok
    if dow == "*":
        return dom_ok
    return dom_ok or dow_ok

def _validate_cron_field(field: str, lo: int, hi: int) -> Optional[str]:
    """校验单个cron字段数值范围与格式合法性"""
    if field == "*":
        return None
    if field.startswith("*/"):
        step = field[2:]
        if not step.isdigit() or int(step) <= 0:
            return f"无效步长: {field}"
        return None
    if "," in field:
        for part in field.split(","):
            err = _validate_cron_field(part.strip(), lo, hi)
            if err:
                return err
        return None
    if "-" in field:
        left, right = field.split("-", 1)
        if not left.isdigit() or not right.isdigit():
            return f"Invalid range: {field}"
        a, b = int(left), int(right)
        if a < lo or a > hi or b < lo or b > hi:
            return f"Range {field} out of bounds [{lo}-{hi}]"
        if a > b:
            return f"Range start > end: {field}"
        return None
    if not field.isdigit():
        return f"Invalid field: {field}"
    value = int(field)
    if value < lo or value > hi:
        return f"Value {value} out of bounds [{lo}-{hi}]"
    return None

def validate_cron(cron_expr: str) -> Optional[str]:
    """完整校验5位cron表达式，返回错误信息，合法返回None"""
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return f"Expected 5 fields, got {len(fields)}"
    bounds = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    names = ["minute", "hour", "day-of-month", "month", "day-of-week"]
    for field, (lo, hi), name in zip(fields, bounds, names):
        err = _validate_cron_field(field, lo, hi)
        if err:
            return f"{name}字段：{err}"
    return None

def save_durable_jobs():
    """将持久化任务写入本地JSON文件"""
    durable_data = [asdict(job) for job in scheduled_jobs.values() if job.durable]
    DURABLE_PATH.write_text(json.dumps(durable_data, indent=2), encoding="utf-8")

def load_durable_jobs():
    """程序启动加载本地持久化定时任务"""
    if not DURABLE_PATH.exists():
        return
    try:
        data_list = json.loads(DURABLE_PATH.read_text(encoding="utf-8"))
        for item in data_list:
            job = CronJob(**item)
            if not validate_cron(job.cron):
                scheduled_jobs[job.id] = job
    except Exception:
        pass

def schedule_job(cron: str, prompt: str, recurring: bool = True, durable: bool = True) -> CronJob | str:
    """创建定时任务，校验表达式，生成唯一ID"""
    err = validate_cron(cron)
    if err:
        return f"cron表达式错误：{err}"
    job_id = f"cron_{random.randint(0, 999999):06d}"
    job = CronJob(
        id=job_id,
        cron=cron,
        prompt=prompt,
        recurring=recurring,
        durable=durable
    )
    with cron_lock:
        scheduled_jobs[job.id] = job
    if durable:
        save_durable_jobs()
    return job

def cancel_job(job_id: str) -> str:
    """取消指定定时任务，持久化任务同步删除文件记录"""
    with cron_lock:
        job = scheduled_jobs.pop(job_id, None)
    if not job:
        return f"Job {job_id} not found"
    if job.durable:
        save_durable_jobs()
    return f"Cancelled {job_id}"

def cron_scheduler_loop():
    """后台常驻调度线程，每秒扫描时间，命中则推入执行队列"""
    # 无限死循环，永久持续运行
    while True:
        # 每轮循环休眠1秒，1秒扫描一次时间
        time.sleep(1)
        # 获取当前实时时间
        now = datetime.now()
        # 生成时间标记：年-月-日 时:分  例 2026-06-22 15:30
        time_marker = now.strftime("%Y-%m-%d %H:%M")

        # 加线程锁，安全读取全局任务字典，防止主线程增删任务造成并发冲突
        with cron_lock:
            # 把任务字典转为列表快照遍历（遍历过程中任务新增/删除不会影响本次循环）
            job_list = list(scheduled_jobs.values())

            # 循环遍历每一条定时任务
            for job in job_list:
                try:
                    # 两个判断条件同时满足才执行：
                    # 1. 当前时间匹配这条任务的cron表达式
                    # 2. 当前【分钟标记】本次还没触发过（防重复执行）
                    if cron_matches(job.cron, now) and _last_fired.get(job.id) != time_marker:
                        # 将任务加入全局待执行队列 cron_queue，主线程会消费队列执行prompt
                        cron_queue.append(job)
                        # 记录本次触发的分钟标记，代表这一分钟已经执行过了
                        _last_fired[job.id] = time_marker

                        # 逻辑：非循环的【单次定时任务】，执行一次直接删掉
                        if not job.recurring:
                            scheduled_jobs.pop(job.id, None)
                            # 如果是持久化任务，同步删掉本地json文件里的记录
                            if job.durable:
                                save_durable_jobs()

                # 单个任务报错不会崩掉整个调度线程，只打印错误日志
                except Exception as e:
                    print(f"  \033[31m[cron error] {job.id}: {e}\033[0m")

def consume_cron_queue() -> List[CronJob]:
    """主线程取出所有待执行的定时任务，清空队列"""
    with cron_lock:
        ready_jobs = list(cron_queue)
        cron_queue.clear()
    return ready_jobs


# 对外工具函数，供主工具池调用
def run_schedule_cron(cron: str, prompt: str, recurring: bool = True, durable: bool = True) -> str:
    res = schedule_job(cron, prompt, recurring, durable)
    if isinstance(res, str):
        return f"Error:{res}"
    return f"Scheduled {res.id}: '{cron}' -> {prompt}"


def run_list_crons() -> str:
    #  查看全部定时任务
    #  加全局线程锁，安全读取任务字典，防止后台调度线程正在遍历修改字典
    with cron_lock:
        # 拷贝任务列表快照，避免遍历过程中集合变化
        jobs = list(scheduled_jobs.values())
    # 空任务判断
    if not jobs:
        return "暂无定时任务"

    lines = []
    for j in jobs:
        # 布尔值转中文标签
        recur_tag = "recurring" if j.recurring else "one-shot"
        dur_tag = "durable" if j.durable else "session"
        # 字段格式化：任务ID + cron表达式 + prompt前40字符截断 + 执行类型 + 持久化类型
        lines.append(f"  {j.id} | {j.cron} | 提示：{j.prompt[:40]}... | {recur_tag} | {dur_tag}")
    # 拼接多行字符串返回给模型展示
    return "\n".join(lines)

def run_cancel_cron(job_id: str) -> str:
    return cancel_job(job_id)


# 初始化加载本地任务 + 启动后台调度守护线程
load_durable_jobs()
threading.Thread(target=cron_scheduler_loop, daemon=True).start()