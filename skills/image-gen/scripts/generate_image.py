#!/usr/bin/env python3
"""generate_image — 调用 OpenAI 兼容 /v1/images/generations 端点生图。

用法：
  python generate_image.py --prompt "一只猫在月光下弹琴"
  python generate_image.py --prompt "改成水彩画" --model agnes-image-2.0-flash --image path/to/photo.png
  python generate_image.py --prompt "赛博朋克城市" --model agnes-image-2.1-flash --size 1024x1024

输出：JSON（含 _runtime_block: "generated_image" sentinel，前端自动渲染图片卡片）

Provider 解析：自动从柳如烟 data 目录读 models.json + models.secrets.json，
找到拥有指定模型的 provider 取 base_url + api_key。
"""

import argparse
import base64
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


# ────────────────────────────────────────────────────────────────────────────
# Provider 配置解析（独立于柳如烟 backend，零 import 依赖）
# ────────────────────────────────────────────────────────────────────────────

def _strip_trailing_whitespace(path_str: str) -> str:
    """逐段清理路径分隔符之间的尾部空格（Windows 中文路径 bug）。
    
    例: 'D:\\workspace\\project \\backend' -> 'D:\\workspace\\project\\backend'
    """
    sep = os.sep
    parts = path_str.split(sep)
    cleaned = [seg.rstrip() for seg in parts]
    return sep.join(cleaned)


def _find_data_dir() -> Path:
    """定位柳如烟 data 目录（跟 backend/paths.py 逻辑一致）。"""
    env = os.environ.get("LIURUYAN_DATA_DIR")
    if env:
        return Path(_strip_trailing_whitespace(env)).expanduser()
    # 开发模式：脚本在 skills/image-gen/scripts/ 下，项目根在 ../../..
    project_root = Path(__file__).resolve().parent.parent.parent.parent
    # Windows 中文路径末尾空格 bug：强制 strip
    project_root = Path(_strip_trailing_whitespace(str(project_root)))
    models_json = project_root / "models.json"
    if models_json.exists():
        return project_root
    # fallback 1：当前工作目录
    cwd = Path.cwd()
    cwd = Path(_strip_trailing_whitespace(str(cwd)))
    if (cwd / "models.json").exists():
        return cwd
    # fallback 2：打包模式
    home_dir = Path.home() / ".liuruyan"
    if (home_dir / "models.json").exists():
        return home_dir
    # fallback 3：原来的 project_root
    return project_root


def _load_providers() -> Dict[str, Any]:
    """加载 providers 配置（含 api_key 注入）。"""
    data_dir = _find_data_dir()
    models_path = data_dir / "models.json"
    secrets_path = data_dir / "models.secrets.json"

    if not models_path.exists():
        return {}

    config = json.loads(models_path.read_text(encoding="utf-8-sig"))
    providers_raw = config.get("providers", {})

    # 注入 secrets
    secrets = {}
    if secrets_path.exists():
        try:
            secrets = json.loads(secrets_path.read_text(encoding="utf-8-sig"))
        except Exception:
            pass

    providers = {}
    for pid, pdata in providers_raw.items():
        if isinstance(pdata, dict):
            # 注入 api_key
            secret_key = f"provider:{pid}"
            if secret_key in secrets and not pdata.get("api_key"):
                pdata["api_key"] = secrets[secret_key]
            providers[pid] = pdata

    return providers


# image_gen 模式匹配（跟 capabilities.py 保持一致）
_IMAGE_GEN_PATTERNS = [
    re.compile(r"dall-?e", re.IGNORECASE),
    re.compile(r"\bsd-", re.IGNORECASE),
    re.compile(r"stable-diffusion", re.IGNORECASE),
    re.compile(r"flux", re.IGNORECASE),
    re.compile(r"midjourney", re.IGNORECASE),
    re.compile(r"image-?gen", re.IGNORECASE),
    re.compile(r"image-?\d", re.IGNORECASE),
    re.compile(r"agnes-image", re.IGNORECASE),
    re.compile(r"wanx", re.IGNORECASE),
    re.compile(r"cogview", re.IGNORECASE),
    re.compile(r"kolors", re.IGNORECASE),
]


def _is_image_gen_model(model_id: str) -> bool:
    return any(p.search(model_id) for p in _IMAGE_GEN_PATTERNS)


def _provider_matches(pdata: Dict, provider_selector: str) -> bool:
    selector = (provider_selector or "").strip().lower()
    if not selector:
        return False
    return selector in {
        str(pdata.get("id", "")).lower(),
        str(pdata.get("name", "")).lower(),
    }


def _provider_has_model(pdata: Dict, model_id: str) -> bool:
    for m in pdata.get("models", []):
        mid = m.get("id", "") if isinstance(m, dict) else str(m)
        if mid == model_id:
            return True
    return False


def _find_provider_for_model(providers: Dict, model_id: str, provider_selector: str = "") -> Optional[Dict]:
    """精确匹配 model_id 所在的第一个 enabled provider；可用 provider id/name 指定。"""
    for pid, pdata in providers.items():
        if not pdata.get("enabled", True):
            continue
        if provider_selector and not _provider_matches(pdata, provider_selector):
            continue
        if _provider_has_model(pdata, model_id):
            return pdata
    return None


def _find_any_image_gen(providers: Dict, provider_selector: str = ""):
    """找第一个有 image_gen 模型的 provider + model_id；可用 provider id/name 指定。"""
    for pid, pdata in providers.items():
        if not pdata.get("enabled", True):
            continue
        if provider_selector and not _provider_matches(pdata, provider_selector):
            continue
        for m in pdata.get("models", []):
            mid = m.get("id", "") if isinstance(m, dict) else str(m)
            if _is_image_gen_model(mid):
                return pdata, mid
    return None, None


# ────────────────────────────────────────────────────────────────────────────
# API 调用
# ────────────────────────────────────────────────────────────────────────────

def _build_images_url(base_url: str) -> str:
    url = base_url.rstrip("/").rstrip("#")
    if url.endswith("/v1"):
        return url + "/images/generations"
    if re.search(r"/v\d+(alpha|beta)?$", url):
        return url + "/images/generations"
    return url + "/v1/images/generations"


def _read_image_as_base64(image_path: str) -> Optional[str]:
    """读取本地图片文件，返回 base64 data URI。"""
    p = Path(image_path)
    if not p.exists():
        return None
    raw = p.read_bytes()
    # 推断 mime
    ext = p.suffix.lower()
    mime_map = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp"}
    mime = mime_map.get(ext, "image/png")
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


def generate_image(
    prompt: str,
    model: str = "",
    size: str = "1024x1024",
    n: int = 1,
    response_format: str = "",
    image_path: str = "",
    style: str = "",
    negative_prompt: str = "",
    provider_selector: str = "",
) -> Dict[str, Any]:
    """主逻辑：解析 provider → 调 API → 返回结构化结果。"""

    providers = _load_providers()
    if not providers:
        return {"error": "找不到 models.json，请确认柳如烟已正确配置 provider。"}

    resolved_model = model.strip() if model else ""

    if resolved_model:
        provider = _find_provider_for_model(providers, resolved_model, provider_selector)
        if not provider:
            suffix = f"，provider='{provider_selector}'" if provider_selector else ""
            return {"error": f"找不到拥有模型 '{resolved_model}' 的 provider{suffix}。请在设置里添加该模型，或检查 --provider。"}
    else:
        provider, resolved_model = _find_any_image_gen(providers, provider_selector)
        if not provider:
            suffix = f"（provider='{provider_selector}'）" if provider_selector else ""
            return {"error": f"没有找到可用的生图模型{suffix}。请在设置 → 模型管理里添加一个生图模型。"}

    base_url = (provider.get("base_url") or "").strip()
    api_key = (provider.get("api_key") or "").strip()

    if not base_url:
        return {"error": f"Provider '{provider.get('id', '?')}' 没有配置 base_url。"}

    endpoint = _build_images_url(base_url)

    # 构建请求体
    body: Dict[str, Any] = {
        "model": resolved_model,
        "prompt": prompt,
        "n": min(4, max(1, n)),
        "size": size,
    }

    # extra_body：agnes 特有参数（tags / image / response_format 等）
    extra: Dict[str, Any] = {}

    # 图生图支持（agnes extra_body 协议）
    if image_path:
        # 优先尝试作为本地文件读取
        img_data = _read_image_as_base64(image_path)
        if img_data:
            # agnes 协议：extra_body.image 传 URL/data URI 列表
            extra["tags"] = ["img2img"]
            extra["image"] = [img_data]
        elif image_path.startswith("http"):
            # 远程 URL 直接传
            extra["tags"] = ["img2img"]
            extra["image"] = [image_path]
        else:
            return {"error": f"图片文件不存在: {image_path}"}

    if "agnes" in resolved_model.lower():
        extra["response_format"] = response_format if response_format in ("url", "b64_json") else "url"
    elif response_format in ("url", "b64_json"):
        body["response_format"] = response_format

    if style:
        body["style"] = style
    if negative_prompt:
        body["negative_prompt"] = negative_prompt

    # 合并 extra_body
    if extra:
        body["extra_body"] = extra

    # 发请求（用 urllib，零外部依赖）
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    # provider extra_headers
    for k, v in (provider.get("extra_headers") or {}).items():
        headers[k] = v

    req_data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = Request(endpoint, data=req_data, headers=headers, method="POST")

    try:
        with urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        try:
            err_body = json.loads(e.read().decode("utf-8"))
            err_msg = (
                err_body.get("error", {}).get("message")
                or err_body.get("detail")
                or str(err_body)[:500]
            )
        except Exception:
            err_msg = str(e)
        return {
            "error": f"生图 API 返回错误 {e.code}: {err_msg}",
            "provider": provider.get("id", ""),
            "model": resolved_model,
            "endpoint": endpoint,
        }
    except URLError as e:
        return {"error": f"网络请求失败: {e.reason}", "endpoint": endpoint}
    except Exception as e:
        return {"error": f"请求异常: {e}"}

    # 解析结果
    images = data.get("data", [])
    if not images:
        return {"error": "生图 API 返回了空的 data 列表。", "raw": str(data)[:500]}

    results = []
    for img in images:
        item: Dict[str, Any] = {}
        if "url" in img:
            item["url"] = img["url"]
            item["display_type"] = "url"
        elif "b64_json" in img:
            item["data_uri"] = f"data:image/png;base64,{img['b64_json']}"
            item["display_type"] = "b64"
        if "revised_prompt" in img:
            item["revised_prompt"] = img["revised_prompt"]
        results.append(item)

    return {
        "_runtime_block": "generated_image",
        "images": results,
        "prompt": prompt,
        "model": resolved_model,
        "provider": provider.get("id", ""),
        "size": size,
        "count": len(results),
    }


# ────────────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="生成图片（调用 OpenAI 兼容 /v1/images/generations）"
    )
    parser.add_argument("--prompt", required=True, help="图片描述")
    parser.add_argument("--model", default="", help="生图模型 id（留空自动检测）")
    parser.add_argument("--provider", default="", help="服务商 id 或名称（可选；默认使用第一个匹配服务商）")
    parser.add_argument("--size", default="1024x1024", help="图片尺寸")
    parser.add_argument("--n", type=int, default=1, help="生成数量（1-4）")
    parser.add_argument("--format", default="", choices=["", "url", "b64_json"],
                        help="返回格式（留空则不主动传 response_format；Agnes 默认 url）")
    parser.add_argument("--image", default="", help="输入图片路径/URL（图生图模式）")
    parser.add_argument("--style", default="", help="风格提示（vivid/natural）")
    parser.add_argument("--negative-prompt", default="", help="负向提示词")

    args = parser.parse_args()

    result = generate_image(
        prompt=args.prompt,
        model=args.model,
        size=args.size,
        n=args.n,
        response_format=args.format,
        image_path=args.image,
        provider_selector=args.provider,
        style=args.style,
        negative_prompt=args.negative_prompt,
    )

    # 输出 JSON（前端 runtimeBlocks 通过 execute_command 的 stdout 识别 sentinel）
    print(json.dumps(result, ensure_ascii=True))
    sys.exit(0 if "_runtime_block" in result else 1)


if __name__ == "__main__":
    main()
