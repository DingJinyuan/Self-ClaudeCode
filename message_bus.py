"""

message_bus.py
底层文件JSONL消息总线，线程安全收件箱，只负责消息读写
"""
import json
import time
from typing import List
from config import MAILBOX_DIR
class MessageBus:
    """
    文件式消息总线，每个Agent对应一个 xxx.jsonl 收件箱文件
    线程安全，主线程/子线程都可以读写，用来实现队友间通信、协议指令下发
    """
    def send(self, from_agent: str, to_agent: str, content: str,
             msg_type: str = "message", metadata: dict = None):
        """
        发送消息，写入目标Agent的收件箱
        :param from_agent: 发送方agent名称
        :param to_agent: 接收方agent名称
        :param content: 消息正文
        :param msg_type: 消息类型 message / plan_approval_request / shutdown_request
        :param metadata: 附加数据，一般存放request_id请求编号
        """
        msg = {
            "from": from_agent,
            "to": to_agent,
            "content": content,
            "type": msg_type,
            "ts": time.time(),
            "metadata": metadata or {}
        }
        inbox_file = MAILBOX_DIR / f"{to_agent}.jsonl"
        # 追加写入一行json，append模式
        with open(inbox_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(msg) + "\n")
        # 控制台打印收发日志
        preview = content[:50]
        print(f"  \033[33m[bus] {from_agent} → {to_agent}: ({msg_type}) {preview}\033[0m")

    def read_inbox(self, agent: str) -> List[dict]:
        """
        读取当前Agent全部收件消息，读取后**清空收件箱**，避免重复消费
        :param agent: 当前agent名称
        :return: 消息列表
        """
        inbox_file = MAILBOX_DIR / f"{agent}.jsonl"
        if not inbox_file.exists():
            return []
        # 读取所有行
        lines = inbox_file.read_text(encoding="utf-8").splitlines()
        msgs = []
        for line in lines:
            line = line.strip()
            if line:
                msgs.append(json.loads(line))
        # 读完直接删除文件，清空收件箱
        inbox_file.unlink()
        return msgs

# 全局唯一消息总线实例
BUS = MessageBus()

def run_send_message(to: str, content: str) -> str:
    BUS.send("lead", to, content)
    return f"Sent to {to}"
