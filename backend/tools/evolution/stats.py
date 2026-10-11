"""
工具统计和分析系统

功能：
1. 收集工具调用统计数据
2. 分析工具性能和成功率
3. 生成可视化报告
4. 为 AI 优化提供数据支持
"""

import json
from pathlib import Path
from typing import Dict, List, Any, Optional
from datetime import datetime, timedelta
from collections import defaultdict
import statistics


class ToolStatsAnalyzer:
    """工具统计分析器"""

    def __init__(self, stats_file: str = None):
        if stats_file is None:
            from backend import paths
            self.stats_file = paths.tool_stats_jsonl_path()
        else:
            self.stats_file = Path(stats_file)
        self._cache: Optional[List[Dict[str, Any]]] = None
        self._cache_time: Optional[datetime] = None
        self._cache_ttl = 60  # 缓存 60 秒

    def _load_stats(self, force_reload: bool = False) -> List[Dict[str, Any]]:
        """加载统计数据（带缓存）"""
        now = datetime.now()

        # 检查缓存
        if (not force_reload and
            self._cache is not None and
            self._cache_time is not None and
            (now - self._cache_time).total_seconds() < self._cache_ttl):
            return self._cache

        # 读取文件
        stats = []
        if self.stats_file.exists():
            with open(self.stats_file, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        stats.append(json.loads(line.strip()))
                    except json.JSONDecodeError:
                        continue

        # 更新缓存
        self._cache = stats
        self._cache_time = now

        return stats

    def get_tool_summary(self, tool_name: Optional[str] = None) -> Dict[str, Any]:
        """获取工具统计摘要"""
        stats = self._load_stats()

        if tool_name:
            stats = [s for s in stats if s.get("name") == tool_name]

        if not stats:
            return {"error": "没有统计数据"}

        total_calls = sum(s.get("total_calls", 0) for s in stats)
        success_rates = [s.get("success_rate", 0) for s in stats if "success_rate" in s]

        return {
            "tool_name": tool_name or "all",
            "total_calls": total_calls,
            "avg_success_rate": statistics.mean(success_rates) if success_rates else 0,
            "data_points": len(stats),
            "time_range": {
                "start": stats[0].get("timestamp") if stats else None,
                "end": stats[-1].get("timestamp") if stats else None
            }
        }

    def get_backend_comparison(self, tool_name: str) -> Dict[str, Any]:
        """比较工具的不同后端性能"""
        stats = self._load_stats()
        tool_stats = [s for s in stats if s.get("name") == tool_name]

        if not tool_stats:
            return {"error": f"没有工具 {tool_name} 的统计数据"}

        # 按后端分组
        backend_data = defaultdict(lambda: {
            "calls": 0,
            "success_rates": [],
            "durations": []
        })

        for stat in tool_stats:
            for backend in stat.get("backends", []):
                name = backend.get("name")
                backend_data[name]["calls"] += 1
                if "success_rate" in backend:
                    backend_data[name]["success_rates"].append(backend["success_rate"])
                if "avg_duration_ms" in backend:
                    backend_data[name]["durations"].append(backend["avg_duration_ms"])

        # 计算平均值
        comparison = {}
        for name, data in backend_data.items():
            comparison[name] = {
                "total_calls": data["calls"],
                "avg_success_rate": statistics.mean(data["success_rates"]) if data["success_rates"] else 0,
                "avg_duration_ms": statistics.mean(data["durations"]) if data["durations"] else 0
            }

        return {
            "tool_name": tool_name,
            "backends": comparison
        }

    def get_time_series(self, tool_name: str, hours: int = 24) -> List[Dict[str, Any]]:
        """获取时间序列数据"""
        stats = self._load_stats()
        cutoff = datetime.now() - timedelta(hours=hours)

        # 过滤时间范围
        filtered = []
        for stat in stats:
            if stat.get("name") != tool_name:
                continue

            timestamp_str = stat.get("timestamp")
            if not timestamp_str:
                continue

            try:
                timestamp = datetime.fromisoformat(timestamp_str)
                if timestamp >= cutoff:
                    filtered.append(stat)
            except ValueError:
                continue

        return filtered

    def get_failure_analysis(self, tool_name: Optional[str] = None) -> Dict[str, Any]:
        """分析失败原因"""
        stats = self._load_stats()

        if tool_name:
            stats = [s for s in stats if s.get("name") == tool_name]

        # 收集所有错误
        error_counts = defaultdict(int)
        backend_errors = defaultdict(lambda: defaultdict(int))

        for stat in stats:
            for backend in stat.get("backends", []):
                backend_name = backend.get("name")
                errors = backend.get("recent_errors", [])

                for error in errors:
                    if error:
                        error_counts[error] += 1
                        backend_errors[backend_name][error] += 1

        return {
            "tool_name": tool_name or "all",
            "total_unique_errors": len(error_counts),
            "most_common_errors": sorted(
                error_counts.items(),
                key=lambda x: x[1],
                reverse=True
            )[:10],
            "errors_by_backend": dict(backend_errors)
        }

    def generate_optimization_report(self) -> Dict[str, Any]:
        """生成优化报告（用于 AI 分析）"""
        stats = self._load_stats()

        if not stats:
            return {"error": "没有统计数据"}

        # 获取最新的统计快照
        latest_stats = {}
        for stat in reversed(stats):
            tool_name = stat.get("name")
            if tool_name and tool_name not in latest_stats:
                latest_stats[tool_name] = stat

        # 分析每个工具
        tool_analysis = {}
        for tool_name, stat in latest_stats.items():
            success_rate = stat.get("success_rate", 0)
            total_calls = stat.get("total_calls", 0)

            # 后端分析
            backends = stat.get("backends", [])
            best_backend = max(
                backends,
                key=lambda b: b.get("success_rate", 0),
                default=None
            )
            worst_backend = min(
                backends,
                key=lambda b: b.get("success_rate", 0),
                default=None
            )

            tool_analysis[tool_name] = {
                "success_rate": success_rate,
                "total_calls": total_calls,
                "status": stat.get("status"),
                "backend_count": len(backends),
                "best_backend": best_backend.get("name") if best_backend else None,
                "worst_backend": worst_backend.get("name") if worst_backend else None,
                "recommendations": stat.get("recommendations", [])
            }

        return {
            "timestamp": datetime.now().isoformat(),
            "total_tools": len(tool_analysis),
            "tools": tool_analysis,
            "global_insights": self._generate_global_insights(tool_analysis)
        }

    def _generate_global_insights(self, tool_analysis: Dict[str, Any]) -> List[str]:
        """生成全局洞察"""
        insights = []

        # 统计低成功率工具
        low_success_tools = [
            name for name, data in tool_analysis.items()
            if data["success_rate"] < 0.8
        ]
        if low_success_tools:
            insights.append(
                f"有 {len(low_success_tools)} 个工具成功率低于 80%: {', '.join(low_success_tools)}"
            )

        # 统计单后端工具
        single_backend_tools = [
            name for name, data in tool_analysis.items()
            if data["backend_count"] == 1
        ]
        if single_backend_tools:
            insights.append(
                f"有 {len(single_backend_tools)} 个工具只有单一后端，建议添加备用后端以提高可靠性"
            )

        # 统计高调用量工具
        high_usage_tools = [
            (name, data["total_calls"])
            for name, data in tool_analysis.items()
            if data["total_calls"] > 100
        ]
        if high_usage_tools:
            high_usage_tools.sort(key=lambda x: x[1], reverse=True)
            top_3 = high_usage_tools[:3]
            insights.append(
                f"高频使用工具 (Top 3): {', '.join(f'{name}({calls}次)' for name, calls in top_3)}"
            )

        return insights

    def export_report(self, output_file: str = "tool_stats_report.json"):
        """导出完整报告"""
        report = {
            "generated_at": datetime.now().isoformat(),
            "optimization_report": self.generate_optimization_report(),
            "failure_analysis": self.get_failure_analysis()
        }

        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

        print(f"✓ 报告已导出到: {output_file}")


def analyze_tool_stats():
    """命令行入口：分析工具统计"""
    analyzer = ToolStatsAnalyzer()

    print("=== 工具统计分析 ===\n")

    # 总体摘要
    summary = analyzer.get_tool_summary()
    print(f"总调用次数: {summary.get('total_calls', 0)}")
    print(f"平均成功率: {summary.get('avg_success_rate', 0):.2%}")
    print(f"数据点数量: {summary.get('data_points', 0)}\n")

    # 优化报告
    opt_report = analyzer.generate_optimization_report()
    if "tools" in opt_report:
        print("=== 工具状态 ===")
        for tool_name, data in opt_report["tools"].items():
            print(f"\n{tool_name}:")
            print(f"  成功率: {data['success_rate']:.2%}")
            print(f"  调用次数: {data['total_calls']}")
            print(f"  状态: {data['status']}")
            print(f"  后端数量: {data['backend_count']}")

            if data.get("recommendations"):
                print("  建议:")
                for rec in data["recommendations"]:
                    print(f"    - {rec}")

    # 全局洞察
    if "global_insights" in opt_report:
        print("\n=== 全局洞察 ===")
        for insight in opt_report["global_insights"]:
            print(f"  • {insight}")

    # 导出报告
    analyzer.export_report()


if __name__ == "__main__":
    analyze_tool_stats()
