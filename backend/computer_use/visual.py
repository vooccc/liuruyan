"""视觉闭环：before/after screenshot diff。

算法（粗但够 LLM 用）：
1. 两张图统一 resize 到 sample_size（默认 320×180，足够看"哪里变了"，token 省）
2. 转灰度 → 绝对差
3. 阈值（默认 30）以上的像素视为"已变"
4. 返回 changed_ratio（0-1）+ bbox(left, top, right, bottom)（原图坐标系）+ summary str

依赖：Pillow（必装）、numpy（已装），无 numpy 时退到纯 Pillow 慢路径。

使用：
    diff = compute_diff(before_b64, after_b64, before_w, before_h)
    diff = {
        "changed_ratio": 0.082,
        "changed_bbox": [120, 240, 540, 600],   # 原图坐标
        "sample_w": 320, "sample_h": 180,
        "summary": "屏幕约 8.2% 区域变化，集中在左下 (120,240)→(540,600)",
    }
"""
from __future__ import annotations

import base64
import io
import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# 算法常量
SAMPLE_W = 320
SAMPLE_H = 180
PIXEL_DIFF_THRESHOLD = 30  # 灰度差 ≤30 视为噪点
MIN_INTERESTING_RATIO = 0.001  # < 0.1% 视为"基本没变"


def _decode_image(b64: str):
    """base64 → PIL.Image（RGB）。失败抛 ValueError。"""
    from PIL import Image  # 懒导

    if not b64:
        raise ValueError("empty base64")
    try:
        raw = base64.b64decode(b64)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"invalid base64: {exc}") from exc
    try:
        img = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"PIL cannot open image: {exc}") from exc
    return img


def compute_diff(
    before_b64: str,
    after_b64: str,
    *,
    full_width: int,
    full_height: int,
    threshold: int = PIXEL_DIFF_THRESHOLD,
) -> Dict[str, Any]:
    """计算两张截图的差异。

    返回 dict（必有 'changed_ratio' 和 'summary'，可能含 'changed_bbox'）。
    任何异常路径都返一个安全的"unknown"结果——视觉 diff 失败不能让整个 action 失败。

    Args:
        before_b64 / after_b64: PNG base64 字符串
        full_width / full_height: 原图实际像素尺寸（用来把 bbox 还原到原坐标系）
        threshold: 像素灰度差 > 此值视为"改变"
    """
    try:
        before = _decode_image(before_b64)
        after = _decode_image(after_b64)
    except ValueError as exc:
        logger.warning("[computer_use] diff decode failed: %s", exc)
        return {"changed_ratio": None, "summary": f"diff failed: {exc}"}

    try:
        # 尺寸不一致就 resize 到 after 的尺寸再算（after 是真相）
        if before.size != after.size:
            before = before.resize(after.size)

        # 双双 sample-resize 到 SAMPLE_W × SAMPLE_H
        sw, sh = SAMPLE_W, SAMPLE_H
        before_s = before.resize((sw, sh)).convert("L")
        after_s = after.resize((sw, sh)).convert("L")

        try:
            import numpy as np
            ba = np.asarray(before_s, dtype="int16")
            aa = np.asarray(after_s, dtype="int16")
            diff = np.abs(aa - ba)
            mask = diff > threshold
            changed = int(mask.sum())
            total = sw * sh
            changed_ratio = changed / total if total else 0.0
            bbox_sample = _bbox_from_mask_numpy(mask)
        except ImportError:
            # 慢路径：纯 PIL，逐像素扫
            changed = 0
            min_x, min_y, max_x, max_y = sw, sh, -1, -1
            ba = before_s.load()
            aa = after_s.load()
            for y in range(sh):
                for x in range(sw):
                    if abs(aa[x, y] - ba[x, y]) > threshold:
                        changed += 1
                        if x < min_x: min_x = x
                        if y < min_y: min_y = y
                        if x > max_x: max_x = x
                        if y > max_y: max_y = y
            changed_ratio = changed / (sw * sh) if (sw * sh) else 0.0
            bbox_sample = (
                None if max_x < 0 else (min_x, min_y, max_x, max_y)
            )

        result: Dict[str, Any] = {
            "changed_ratio": round(changed_ratio, 4),
            "sample_w": sw,
            "sample_h": sh,
            "threshold": threshold,
        }

        if changed_ratio < MIN_INTERESTING_RATIO or bbox_sample is None:
            result["summary"] = (
                f"屏幕几乎无变化（{changed_ratio:.2%}）。"
                f" 操作可能被忽略，或目标区域无视觉反馈。"
            )
            return result

        # bbox 从 sample 坐标系 → 原图坐标系
        scale_x = full_width / sw
        scale_y = full_height / sh
        sx, sy, ex, ey = bbox_sample
        bbox_full = (
            int(sx * scale_x),
            int(sy * scale_y),
            int((ex + 1) * scale_x),
            int((ey + 1) * scale_y),
        )
        result["changed_bbox"] = list(bbox_full)
        cx = (bbox_full[0] + bbox_full[2]) // 2
        cy = (bbox_full[1] + bbox_full[3]) // 2
        result["summary"] = (
            f"屏幕约 {changed_ratio:.1%} 区域变化，"
            f"中心 ({cx}, {cy})，覆盖 ({bbox_full[0]},{bbox_full[1]}) → "
            f"({bbox_full[2]},{bbox_full[3]})"
        )
        return result
    except Exception as exc:  # noqa: BLE001
        logger.warning("[computer_use] diff compute failed: %s", exc)
        return {"changed_ratio": None, "summary": f"diff error: {exc}"}


def _bbox_from_mask_numpy(mask) -> Optional[Tuple[int, int, int, int]]:
    """numpy bool mask → (min_x, min_y, max_x, max_y)；全 False 时返 None。"""
    import numpy as np

    if not mask.any():
        return None
    rows = np.any(mask, axis=1)
    cols = np.any(mask, axis=0)
    y0, y1 = np.where(rows)[0][[0, -1]]
    x0, x1 = np.where(cols)[0][[0, -1]]
    return int(x0), int(y0), int(x1), int(y1)
