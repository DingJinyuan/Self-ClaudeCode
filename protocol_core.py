"""
protocol_core.py
协议核心：ProtocolState结构体、RequestID、应答匹配、Lead审批/关机接口、队友提交方案
依赖：config + message_bus
"""
import random,time

from typing import List

from message_bus import BUS
from config import pending_requests

from models import ProtocolState

def new_request_id() -> str:
    """生成6位随机请求ID，用来精准匹配请求与回复"""
    return f"req_{random.randint(0, 999999):06d}"

def match_response(response_type: str, request_id: str, approve: bool):
    """
    根据request_id匹配原始请求，修改审批状态
    防止A请求的回复，误判给B请求
    """
    state = pending_requests.get(request_id)
    if not state:
        return
    # 类型强匹配，关机回复只能对应关机请求，方案回复对应方案请求
    if state.type == "shutdown" and response_type != "shutdown_response":
        return
    if state.type == "plan_approval" and response_type != "plan_approval_response":
        return
    # 更新审批结果
    state.status = "approved" if approve else "rejected"

def consume_lead_inbox(route_protocol=True) -> List[dict]:
    """读取主控lead的收件箱，自动路由处理协议应答"""
    msgs = BUS.read_inbox("lead")
    if route_protocol:
        for msg in msgs:
            meta = msg.get("metadata", {})
            req_id = meta.get("request_id", "")
            msg_type = msg.get("type", "")
            # 如果是协议应答消息，自动匹配修改请求状态
            if req_id and msg_type.endswith("_response"):
                match_response(msg_type, req_id, meta.get("approve", False))
    return msgs

# ========================== 提交方案 协议工具 ==========================
def _teammate_submit_plan(from_name: str, plan: str) -> str:
    """队友提交执行方案，向lead发起审批请求，存入全局pending请求池"""
    req_id = new_request_id()
    pending_requests[req_id] = ProtocolState(
        request_id=req_id, type="plan_approval",
        sender=from_name, target="lead",
        status="pending", payload=plan)
    BUS.send(from_name, "lead", plan,
             "plan_approval_request",
             {"request_id": req_id})
    return f"Plan submitted ({req_id})"

# ========================== 主控Lead 对外协议工具 ==========================
def run_request_shutdown(teammate: str) -> str:
    """主控下发关机请求给指定队友"""
    req_id = new_request_id()
    pending_requests[req_id] = ProtocolState(
        request_id=req_id,
        type="shutdown",
        sender="lead",
        target=teammate,
        status="pending",
        payload=""
    )
    BUS.send(
        from_agent="lead",
        to_agent=teammate,
        content="Shut down immediately.",
        msg_type="shutdown_request",
        metadata={"request_id": req_id}
    )
    return f"Shutdown request sent to teammate: {teammate}"

def run_request_plan(teammate: str, task: str) -> str:
    """主控要求队友提交任务执行方案"""
    BUS.send(
        from_agent="lead",
        to_agent=teammate,
        content=f"Please submit execution plan for task: {task}",
        msg_type="message"
    )
    return f"Request sent to {teammate}: submit task plan"

def run_review_plan(request_id: str, approve: bool, feedback: str = "") -> str:
    """主控审批方案：同意/驳回，下发回执"""
    state = pending_requests.get(request_id)
    if not state:
        return f"Error: Request {request_id} does not exist"
    state.status = "approved" if approve else "rejected"
    BUS.send(
        from_agent="lead",
        to_agent=state.sender,
        content=feedback or ("Approved" if approve else "Rejected"),
        msg_type="plan_approval_response",
        metadata={"request_id": request_id, "approve": approve}
    )
    return f"Plan {'approved' if approve else 'rejected'} successfully"

def run_check_inbox() -> str:
    msgs = consume_lead_inbox(route_protocol=True)
    if not msgs:
        return "(inbox empty)"
    lines = []
    for m in msgs:
        meta = m.get("metadata", {})
        req_id = meta.get("request_id", "")
        tag = f" [{m['type']} req:{req_id}]" if req_id else f" [{m['type']}]"
        lines.append(f"  [{m['from']}]{tag} {m['content'][:200]}")
    return "\n".join(lines)