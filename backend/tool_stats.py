"""
工具执行统计模块 - 记录工具调用的成功率、失败原因、JSON 修复情况
"""
import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, asdict
from collections import defaultdict
import threading

logger = logging.getLogger(__name__)


@dataclass
class ToolCallRecord:
    """单次工具调用记录

    Note: agent_id / session_id 是后期加的字段，老 jsonl 行可能没有。
    _load_history 用 _from_dict 容错读，缺字段时填 None。
    """
    timestamp: float
    tool_name: str
    success: bool
    retry_count: int
    json_repair_layers: List[int]  # 触发了哪些层的 JSON 修复（1-4）
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    execution_time_ms: Optional[float] = None
    agent_id: Optional[str] = None  # 角色 id，用于 per-agent 工具行为画像
    session_id: Optional[str] = None  # 会话 id，用于按 session 回溯
    trace_id: Optional[str] = None  # 单次 tool_call 唯一 id，串起 SSE/messages/audit/stats 全链
    result_size_chars: Optional[int] = None  # 工具原始返回字符数（截断前）
    budget_truncated: Optional[bool] = None  # 是否被 ToolResultBudget 截断


class ToolStatsCollector:
    """工具统计收集器（单例）"""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self.records: List[ToolCallRecord] = []
        from backend import paths
        self.stats_file = paths.tool_stats_jsonl_path()
        self._initialized = True

        # 加载历史记录
        self._load_history()

    def _load_history(self):
        """加载历史统计数据（容错：老格式可能没有 agent_id/session_id 字段）"""
        if not self.stats_file.exists():
            return

        # 只接受 ToolCallRecord 已知的字段，多余字段忽略，缺失字段填 None
        valid_fields = {f for f in ToolCallRecord.__dataclass_fields__.keys()}
        try:
            with open(self.stats_file, 'r', encoding='utf-8') as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        data = json.loads(line)
                    except Exception:
                        continue
                    filtered = {k: v for k, v in data.items() if k in valid_fields}
                    try:
                        self.records.append(ToolCallRecord(**filtered))
                    except TypeError:
                        continue
        except Exception as e:
            logger.error("[STATS] Failed to load history: %s", e)

    def record_call(
        self,
        tool_name: str,
        success: bool,
        retry_count: int = 0,
        json_repair_layers: Optional[List[int]] = None,
        error_type: Optional[str] = None,
        error_message: Optional[str] = None,
        execution_time_ms: Optional[float] = None,
        agent_id: Optional[str] = None,
        session_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        result_size_chars: Optional[int] = None,
        budget_truncated: Optional[bool] = None,
    ):
        """记录一次工具调用"""
        record = ToolCallRecord(
            timestamp=time.time(),
            tool_name=tool_name,
            success=success,
            retry_count=retry_count,
            json_repair_layers=json_repair_layers or [],
            error_type=error_type,
            error_message=error_message,
            execution_time_ms=execution_time_ms,
            agent_id=agent_id,
            session_id=session_id,
            trace_id=trace_id,
            result_size_chars=result_size_chars,
            budget_truncated=budget_truncated,
        )

        self.records.append(record)

        # 追加写入文件
        try:
            with open(self.stats_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps(asdict(record), ensure_ascii=False) + '\n')
        except Exception as e:
            logger.error("[STATS] Failed to write record: %s", e)

    def get_summary(self, tool_name: Optional[str] = None, last_n: Optional[int] = None) -> Dict[str, Any]:
        """获取统计摘要"""
        records = self.records

        # 过滤
        if tool_name:
            records = [r for r in records if r.tool_name == tool_name]
        if last_n:
            records = records[-last_n:]

        if not records:
            return {"total": 0, "message": "No records found"}

        # 按工具分组统计
        by_tool = defaultdict(lambda: {
            "total": 0,
            "success": 0,
            "failed": 0,
            "json_repair_count": 0,
            "json_repair_layers": defaultdict(int),
            "retry_distribution": defaultdict(int),
            "error_types": defaultdict(int),
            "avg_execution_time_ms": []
        })

        for record in records:
            stats = by_tool[record.tool_name]
            stats["total"] += 1

            if record.success:
                stats["success"] += 1
            else:
                stats["failed"] += 1
                if record.error_type:
                    stats["error_types"][record.error_type] += 1

            # JSON 修复统计
            if record.json_repair_layers:
                stats["json_repair_count"] += 1
                for layer in record.json_repair_layers:
                    stats["json_repair_layers"][layer] += 1

            # 重试分布
            stats["retry_distribution"][record.retry_count] += 1

            # 执行时间
            if record.execution_time_ms is not None:
                stats["avg_execution_time_ms"].append(record.execution_time_ms)

        # 计算百分比和平均值
        result = {}
        for tool, stats in by_tool.items():
            total = stats["total"]
            result[tool] = {
                "total_calls": total,
                "success_rate": f"{stats['success'] / total * 100:.1f}%",
                "failure_rate": f"{stats['failed'] / total * 100:.1f}%",
                "json_repair_rate": f"{stats['json_repair_count'] / total * 100:.1f}%",
                "json_repair_layers": dict(stats["json_repair_layers"]),
                "retry_distribution": {
                    f"{k}_retries": f"{v / total * 100:.1f}%"
                    for k, v in stats["retry_distribution"].items()
                },
                "error_types": dict(stats["error_types"]),
                "avg_execution_time_ms": (
                    sum(stats["avg_execution_time_ms"]) / len(stats["avg_execution_time_ms"])
                    if stats["avg_execution_time_ms"] else None
                )
            }

        return result

    def print_summary(self, tool_name: Optional[str] = None, last_n: Optional[int] = None):
        """打印统计摘要（格式化输出）"""
        summary = self.get_summary(tool_name, last_n)

        if "message" in summary:
            print(f"\n[STATS] {summary['message']}")
            return

        print("\n" + "="*80)
        print("工具执行统计报告")
        if tool_name:
            print(f"工具: {tool_name}")
        if last_n:
            print(f"最近 {last_n} 次调用")
        print("="*80)

        for tool, stats in summary.items():
            print(f"\n【{tool}】")
            print(f"  总调用次数: {stats['total_calls']}")
            print(f"  成功率: {stats['success_rate']}")
            print(f"  失败率: {stats['failure_rate']}")
            print(f"  JSON 修复触发率: {stats['json_repair_rate']}")

            if stats['json_repair_layers']:
                print(f"  JSON 修复层分布:")
                for layer, count in sorted(stats['json_repair_layers'].items()):
                    print(f"    第 {layer} 层: {count} 次")

            if stats['retry_distribution']:
                print(f"  重试分布:")
                for retry, pct in sorted(stats['retry_distribution'].items()):
                    print(f"    {retry}: {pct}")

            if stats['error_types']:
                print(f"  错误类型:")
                for error, count in stats['error_types'].items():
                    print(f"    {error}: {count} 次")

            if stats['avg_execution_time_ms']:
                print(f"  平均执行时间: {stats['avg_execution_time_ms']:.1f} ms")

        print("\n" + "="*80 + "\n")

    def clear_history(self):
        """清空历史记录"""
        self.records.clear()
        if self.stats_file.exists():
            self.stats_file.unlink()
        logger.info("[STATS] History cleared")

    def clear_tool(self, tool_name: str) -> int:
        """删除某个工具的所有记录（内存 + 文件）。返回删除条数。

        实现思路：
        - 内存：原地过滤 records（保持单例不变）
        - 文件：重写 jsonl，把不属于该 tool_name 的行写回。空就删文件。

        失败语义：写盘失败保留内存改动，但下次进程重启会从老文件 _load_history
        重新加载——内存和盘短时间不一致。print 日志即可，不抛。

        典型调用方：UI 监测面板的「删除某条工具计数」按钮。
        """
        before = len(self.records)
        self.records = [r for r in self.records if r.tool_name != tool_name]
        removed = before - len(self.records)

        if removed > 0 and self.stats_file.exists():
            try:
                # 重写 jsonl 文件
                with open(self.stats_file, 'w', encoding='utf-8') as f:
                    for r in self.records:
                        f.write(json.dumps(asdict(r), ensure_ascii=False) + '\n')
                if not self.records:
                    # 空文件就删掉，下次 _load_history 直接跳过
                    try:
                        self.stats_file.unlink()
                    except OSError:
                        pass
            except Exception as e:
                logger.error("[STATS] Failed to rewrite stats file after clear_tool(%s): %s", tool_name, e)

        logger.info("[STATS] Cleared %s record(s) for tool '%s'", removed, tool_name)
        return removed


# 全局单例
_stats_collector = ToolStatsCollector()


def get_stats_collector() -> ToolStatsCollector:
    """获取统计收集器单例"""
    return _stats_collector
