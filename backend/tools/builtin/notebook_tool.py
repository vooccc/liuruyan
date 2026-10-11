"""Jupyter notebook (.ipynb) cell editor — replace / insert / delete."""

from pathlib import Path
from typing import Optional

import nbformat


async def notebook_edit(
    notebook_path: str,
    cell_index: int = 0,
    new_source: str = "",
    edit_mode: str = "replace",
    cell_type: Optional[str] = None,
    **_unused,
) -> dict:
    """Edit a Jupyter notebook cell.

    - replace (default): overwrite cell at cell_index with new_source.
      Code cells have outputs/execution_count cleared on replace.
      Pass cell_type to convert between code/markdown.
    - insert: insert a new cell at cell_index. cell_type required.
      cell_index == total_cells appends at end.
    - delete: remove cell at cell_index (new_source / cell_type ignored).
    """
    if not notebook_path or not isinstance(notebook_path, str):
        raise ValueError("notebook_path 必填且须为字符串")
    path = Path(notebook_path)
    if not path.exists():
        raise FileNotFoundError(f"notebook 不存在: {notebook_path}")
    if path.suffix.lower() != ".ipynb":
        raise ValueError(f"文件不是 .ipynb: {notebook_path}")
    if edit_mode not in ("replace", "insert", "delete"):
        raise ValueError(
            f"edit_mode 必须是 replace / insert / delete，得到: {edit_mode}"
        )

    nb = nbformat.read(str(path), as_version=4)
    total_before = len(nb.cells)

    if edit_mode == "delete":
        if cell_index < 0 or cell_index >= total_before:
            raise IndexError(
                f"cell_index {cell_index} 越界 (共 {total_before} 个 cell)"
            )
        deleted = nb.cells.pop(cell_index)
        summary = {
            "action": "delete",
            "deleted_index": cell_index,
            "deleted_type": deleted.cell_type,
            "total_cells": len(nb.cells),
        }
        preview_index = None

    elif edit_mode == "insert":
        if cell_type not in ("code", "markdown"):
            raise ValueError(
                "insert 模式必须指定 cell_type='code' 或 'markdown'"
            )
        if cell_index < 0 or cell_index > total_before:
            raise IndexError(
                f"cell_index {cell_index} 越界 (insert 允许 0..{total_before})"
            )
        if cell_type == "code":
            new_cell = nbformat.v4.new_code_cell(source=new_source)
        else:
            new_cell = nbformat.v4.new_markdown_cell(source=new_source)
        nb.cells.insert(cell_index, new_cell)
        summary = {
            "action": "insert",
            "inserted_index": cell_index,
            "inserted_type": cell_type,
            "total_cells": len(nb.cells),
        }
        preview_index = cell_index

    else:  # replace
        if cell_index < 0 or cell_index >= total_before:
            raise IndexError(
                f"cell_index {cell_index} 越界 (共 {total_before} 个 cell)"
            )
        target = nb.cells[cell_index]
        if cell_type and cell_type != target.cell_type:
            if cell_type == "code":
                nb.cells[cell_index] = nbformat.v4.new_code_cell(source=new_source)
            elif cell_type == "markdown":
                nb.cells[cell_index] = nbformat.v4.new_markdown_cell(source=new_source)
            else:
                raise ValueError(
                    f"cell_type 必须是 code / markdown，得到: {cell_type}"
                )
        else:
            target.source = new_source
            if target.cell_type == "code":
                target.outputs = []
                target.execution_count = None
        summary = {
            "action": "replace",
            "replaced_index": cell_index,
            "cell_type": nb.cells[cell_index].cell_type,
            "total_cells": len(nb.cells),
        }
        preview_index = cell_index

    nbformat.write(nb, str(path))

    preview_cell = None
    if preview_index is not None and 0 <= preview_index < len(nb.cells):
        c = nb.cells[preview_index]
        src = c.source if isinstance(c.source, str) else "".join(c.source)
        preview_cell = {
            "index": preview_index,
            "type": c.cell_type,
            "source_preview": src[:200] + ("..." if len(src) > 200 else ""),
        }

    return {
        "success": True,
        **summary,
        "preview": preview_cell,
    }
