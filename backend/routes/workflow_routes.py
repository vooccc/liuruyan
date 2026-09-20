# -*- coding: utf-8 -*-
"""工作流 API：/workflow/list + /workflow/run（SSE）+ /workflow/resume（human 续跑）。

设计见 _docs/workflow_engine_spec_2026_06_16.md。
运行时子进程调用 ai-quant 的工作流引擎（各自 venv，解耦、不污染柳如烟），
逐行 JSON 事件转 SSE 推前端。human 节点暂停时把完整 state 暂存在后端(run_id)，
前端只拿 run_id + 审核结果回传 /workflow/resume 续跑。
"""
import os
import sys
import json
import uuid
import asyncio
import tempfile

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

router = APIRouter()

# ai-quant 项目位置（可用环境变量 AIQUANT_DIR 覆盖）
AIQUANT_DIR = os.environ.get("AIQUANT_DIR", r".")

# 暂停中的运行：run_id -> 完整 state（内存暂存，等前端审核后续跑）
PAUSED_RUNS: dict = {}


def _aiquant_python() -> str:
    cand = os.path.join(AIQUANT_DIR, "venv", "Scripts", "python.exe")
    return cand if os.path.exists(cand) else sys.executable


# 工作流目录（MVP 硬编码；与 ai-quant/workflow/definitions.py 的 REGISTRY 对齐）
CATALOG = [
    {
        "id": "quant_council", "name": "AI 投研团队",
        "desc": "多分析师(技术/基本面/新闻/情绪) + 多空辩论 + 裁决 → 买卖持决策",
        "inputs": [
            {"key": "symbol", "label": "股票代码", "placeholder": "600519"},
            {"key": "date", "label": "日期", "placeholder": "2026-06-15"},
        ],
    },
    # 内容流水线已迁到「自媒体 · 创作工作台」（分步可调），此处不再暴露，避免双入口重复。
]


@router.get("/workflow/list")
async def list_workflows():
    """返回可用工作流目录。"""
    return {"workflows": CATALOG, "aiquant_dir": AIQUANT_DIR,
            "available": os.path.isdir(AIQUANT_DIR)}


@router.get("/workflow/models")
def workflow_models():
    """列可选 LLM 模型（openai 兼容、enabled），供工作流选模型下拉（前端按 provider 分组）。"""
    from backend.llm_catalog import list_text_models, current_model
    return {"models": list_text_models(), "current": current_model()}


async def _stream_proc(cmd: list, extra_env: dict = None):
    """跑子进程，逐行 JSON 事件转 SSE；拦截 paused 事件把 state 暂存后端、只回传 run_id。

    extra_env：额外注入的环境变量（如选中模型的 LLM_BASE_URL/KEY/MODEL，让 ai-quant 直连
    厂商、复用柳如烟模型配置，不再走 ai-quant 自己的 local_settings）。
    """
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if extra_env:
        env.update(extra_env)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=AIQUANT_DIR,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
            limit=64 * 1024 * 1024,   # 提高单行上限：paused 事件含完整 state，远超默认 64KB，否则 readline 抛 LimitOverrunError 致 SSE 断流
        )
    except Exception as e:
        yield f"data: {json.dumps({'type': 'error', 'msg': str(e)}, ensure_ascii=False)}\n\n"
        return
    assert proc.stdout is not None
    async for raw in proc.stdout:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("type") == "paused":
            run_id = uuid.uuid4().hex[:12]
            PAUSED_RUNS[run_id] = obj.get("state") or {}
            light = {"type": "paused", "run_id": run_id,
                     "node": obj.get("node"), "content": obj.get("content", "")}
            yield f"data: {json.dumps(light, ensure_ascii=False)}\n\n"
        else:
            yield f"data: {line}\n\n"
    await proc.wait()
    yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"


class WorkflowRunRequest(BaseModel):
    workflow: str
    inputs: list = []     # 有序输入值，顺序与 CATALOG.inputs 对齐
    rounds: int = 1
    human_mode: bool = False   # True 时遇 human 节点暂停等审核
    model: str = ""            # 选的 LLM 模型（空=柳如烟当前模型）；注入给 ai-quant 直连厂商


@router.post("/workflow/run")
async def run_workflow(req: WorkflowRunRequest):
    """运行工作流，SSE 流式返回每一步事件。"""
    cmd = [_aiquant_python(), "-m", "workflow.run_json", req.workflow]
    cmd += [str(x) for x in (req.inputs or [])]
    if req.rounds:
        cmd.append(str(req.rounds))
    if req.human_mode:
        cmd.append("--pause")
    from backend.llm_catalog import resolve_model_env
    return StreamingResponse(_stream_proc(cmd, resolve_model_env(req.model)),
                             media_type="text/event-stream")


class WorkflowResumeRequest(BaseModel):
    run_id: str
    status: str = "approved"    # approved | revise
    feedback: str = ""
    model: str = ""             # 与 run 时同一模型（续跑定稿也走它）


@router.post("/workflow/resume")
async def resume_workflow(req: WorkflowResumeRequest):
    """带人工审核结果续跑暂停的工作流。"""
    state = PAUSED_RUNS.pop(req.run_id, None)
    if state is None:
        async def err():
            yield f"data: {json.dumps({'type': 'error', 'msg': '运行不存在或已过期'}, ensure_ascii=False)}\n\n"
        return StreamingResponse(err(), media_type="text/event-stream")

    fd, path = tempfile.mkstemp(suffix=".json", prefix="wf_state_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)

    cmd = [_aiquant_python(), "-m", "workflow.run_json", "--resume", path,
           "--status", req.status or "approved", "--feedback", req.feedback or ""]

    from backend.llm_catalog import resolve_model_env
    extra_env = resolve_model_env(req.model)

    async def gen():
        try:
            async for chunk in _stream_proc(cmd, extra_env):
                yield chunk
        finally:
            try:
                os.remove(path)
            except Exception:
                pass

    return StreamingResponse(gen(), media_type="text/event-stream")
