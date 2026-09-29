"""
GetDiagnosticsTool - 代码诊断工具

功能：
- 检查代码语法错误
- 检查类型错误（Python 使用 Jedi）
- 支持多种语言（当前支持 Python，未来可扩展）

依赖：
- Jedi（Python 代码分析）
"""

import asyncio
from pathlib import Path
from typing import List, Dict, Any, Optional

try:
    import jedi
    JEDI_AVAILABLE = True
except ImportError:
    JEDI_AVAILABLE = False


class GetDiagnosticsTool:
    """代码诊断工具"""

    def __init__(self, project_root: Path = None):
        """
        初始化诊断工具

        Args:
            project_root: 项目根目录
        """
        self.project_root = Path(project_root) if project_root else Path.cwd()
        self.jedi_available = JEDI_AVAILABLE

    async def get_diagnostics(
        self,
        file_path: str,
        severity: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        获取文件的诊断信息

        Args:
            file_path: 文件路径（相对于项目根目录）
            severity: 严重程度过滤 (error, warning, info, hint)

        Returns:
            诊断结果字典
        """
        try:
            # 解析文件路径
            if Path(file_path).is_absolute():
                full_path = Path(file_path)
            else:
                full_path = self.project_root / file_path

            if not full_path.exists():
                return {
                    "success": False,
                    "error": f"File not found: {file_path}"
                }

            # 确定语言
            language = self._detect_language(full_path)
            if not language:
                return {
                    "success": False,
                    "error": f"Unsupported file type: {full_path.suffix}"
                }

            # 目前只支持 Python
            if language != "python":
                return {
                    "success": False,
                    "error": f"Language '{language}' not supported yet. Currently only Python is supported."
                }

            if not self.jedi_available:
                return {
                    "success": False,
                    "error": "Jedi library not installed. Run: pip install jedi"
                }

            # 读取文件内容
            with open(full_path, "r", encoding="utf-8") as f:
                content = f.read()

            # 使用 Jedi 获取语法错误
            script = jedi.Script(code=content, path=str(full_path))
            syntax_errors = script.get_syntax_errors()

            # 转换为标准诊断格式
            diagnostics = []
            for error in syntax_errors:
                diagnostics.append({
                    "severity": 1,  # error
                    "message": error.get_message(),
                    "line": error.line,
                    "column": error.column,
                    "until_line": error.until_line if hasattr(error, 'until_line') else error.line,
                    "until_column": error.until_column if hasattr(error, 'until_column') else error.column
                })

            # 过滤严重程度
            if severity:
                severity_map = {
                    "error": 1,
                    "warning": 2,
                    "info": 3,
                    "hint": 4
                }
                target_severity = severity_map.get(severity.lower())
                if target_severity:
                    diagnostics = [
                        d for d in diagnostics
                        if d.get("severity") == target_severity
                    ]

            # 格式化结果
            formatted = self._format_diagnostics(diagnostics, str(file_path))

            return {
                "success": True,
                "file_path": str(file_path),
                "language": language,
                "diagnostic_count": len(diagnostics),
                "has_errors": len(diagnostics) > 0,
                "error_count": sum(1 for d in diagnostics if d.get("severity") == 1),
                "warning_count": sum(1 for d in diagnostics if d.get("severity") == 2),
                "info_count": sum(1 for d in diagnostics if d.get("severity") == 3),
                "hint_count": sum(1 for d in diagnostics if d.get("severity") == 4),
                "diagnostics": formatted
            }

        except Exception as e:
            return {
                "success": False,
                "error": str(e)
            }

    async def check_syntax(self, file_path: str) -> Dict[str, Any]:
        """
        检查文件语法错误

        Args:
            file_path: 文件路径

        Returns:
            语法错误列表
        """
        result = await self.get_diagnostics(file_path, severity="error")
        if result["success"]:
            return {
                "success": True,
                "file_path": result["file_path"],
                "has_errors": result["has_errors"],
                "error_count": result["error_count"],
                "errors": result["diagnostics"]
            }
        return result

    async def check_multiple_files(
        self,
        file_paths: List[str],
        severity: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        检查多个文件的诊断信息

        Args:
            file_paths: 文件路径列表
            severity: 严重程度过滤

        Returns:
            所有文件的诊断结果
        """
        results = []
        total_errors = 0
        total_warnings = 0

        for file_path in file_paths:
            result = await self.get_diagnostics(file_path, severity)
            if result["success"]:
                results.append(result)
                total_errors += result["error_count"]
                total_warnings += result["warning_count"]

        return {
            "success": True,
            "file_count": len(results),
            "total_errors": total_errors,
            "total_warnings": total_warnings,
            "files": results
        }

    def _detect_language(self, file_path: Path) -> Optional[str]:
        """
        检测文件语言

        Args:
            file_path: 文件路径

        Returns:
            语言标识符
        """
        suffix = file_path.suffix.lower()
        language_map = {
            ".py": "python",
            ".ts": "typescript",
            ".tsx": "typescriptreact",
            ".js": "javascript",
            ".jsx": "javascriptreact",
            ".json": "json",
            ".md": "markdown",
            ".html": "html",
            ".css": "css",
            ".scss": "scss",
            ".less": "less",
            ".go": "go",
            ".rs": "rust",
            ".java": "java",
            ".c": "c",
            ".cpp": "cpp",
            ".h": "c",
            ".hpp": "cpp"
        }
        return language_map.get(suffix)

    def _format_diagnostics(
        self,
        diagnostics: List[Dict],
        file_path: str
    ) -> List[Dict[str, Any]]:
        """
        格式化诊断信息

        Args:
            diagnostics: 原始诊断信息
            file_path: 文件路径

        Returns:
            格式化后的诊断信息
        """
        severity_names = {
            1: "error",
            2: "warning",
            3: "info",
            4: "hint"
        }

        formatted = []
        for diag in diagnostics:
            formatted.append({
                "severity": severity_names.get(diag.get("severity", 1), "error"),
                "message": diag.get("message", ""),
                "line": diag.get("line", 0),
                "column": diag.get("column", 0),
                "until_line": diag.get("until_line", diag.get("line", 0)),
                "until_column": diag.get("until_column", diag.get("column", 0)),
                "location": f"{file_path}:{diag.get('line', 0)}:{diag.get('column', 0)}"
            })

        return formatted

    async def cleanup(self):
        """清理资源"""
        pass  # Jedi 不需要清理资源


def get_diagnostics_tool(project_root: Path = None) -> GetDiagnosticsTool:
    """
    获取诊断工具实例

    Args:
        project_root: 项目根目录

    Returns:
        GetDiagnosticsTool 实例
    """
    if project_root is None:
        project_root = Path.cwd()
    return GetDiagnosticsTool(project_root)
