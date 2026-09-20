#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
柳如烟 (Ruyan AI) · 本地安装包一键上传至 GitHub Releases
功能：
1. 自动检测本地网络代理（支持 Clash / Mihomo 7897 / 7890 等），防止大文件上传被 GFW 重置
2. 自动读取当前版本号（来自 frontend/src-tauri/tauri.conf.json）
3. 自动检索本地构建出的最新安装包，并将发布文件名规范化为英文 (柳如烟 -> liuruyan)
4. 检查并创建 GitHub Release（支持自动生成发布日志）
5. 流式上传安装包并展示实时进度条，输出公开下载链接
"""

import os
import sys
import json
import glob
import re
import socket
from pathlib import Path
from typing import Any
import urllib.request
import urllib.error
import urllib.parse

# 强制 UTF-8 控制台输出
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def get_project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def setup_proxy():
    """自动检测并挂接本地加速代理，防止直连 uploads.github.com 时因 SSL EOF 被重置"""
    if os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY") or os.environ.get("ALL_PROXY"):
        return

    common_proxies = [
        ("127.0.0.1", 7897),  # Clash Verge / Mihomo
        ("127.0.0.1", 7890),  # Clash Classic
        ("127.0.0.1", 10809), # v2rayN HTTP
        ("127.0.0.1", 10808), # v2rayN SOCKS
        ("127.0.0.1", 1080),  # Shadowsocks
    ]

    found = None
    for host, port in common_proxies:
        s = socket.socket()
        s.settimeout(0.3)
        try:
            s.connect((host, port))
            found = f"http://{host}:{port}"
            break
        except Exception:
            pass
        finally:
            s.close()

    if found:
        print(f"  [net] 已自动挂接本地网络加速代理: {found}")
        proxy_handler = urllib.request.ProxyHandler({"http": found, "https": found})
        opener = urllib.request.build_opener(proxy_handler)
        urllib.request.install_opener(opener)


class ProgressFileReader:
    """带实时进度展示的文件读取器"""
    def __init__(self, filepath: Path):
        self.filepath = filepath
        self.total_size = filepath.stat().st_size
        self.file = open(filepath, "rb")
        self.uploaded = 0
        self.last_pct = -1

    def read(self, size=-1):
        chunk = self.file.read(size)
        if chunk:
            self.uploaded += len(chunk)
            pct = int((self.uploaded / self.total_size) * 100)
            if pct != self.last_pct and (pct % 5 == 0 or pct == 100):
                self.last_pct = pct
                mb_uploaded = self.uploaded / (1024 * 1024)
                mb_total = self.total_size / (1024 * 1024)
                print(f"\r  [upload] 上传进度: {pct:>3}% ({mb_uploaded:5.1f} / {mb_total:5.1f} MB)", end="", flush=True)
                if pct == 100:
                    print()
        return chunk

    def close(self):
        self.file.close()

    def __len__(self):
        return self.total_size


def get_version(root: Path) -> str:
    tauri_conf = root / "frontend" / "src-tauri" / "tauri.conf.json"
    if tauri_conf.exists():
        try:
            data = json.loads(tauri_conf.read_text(encoding="utf-8"))
            if "version" in data:
                return data["version"]
        except Exception:
            pass

    pkg_json = root / "frontend" / "package.json"
    if pkg_json.exists():
        try:
            data = json.loads(pkg_json.read_text(encoding="utf-8"))
            if "version" in data:
                return data["version"]
        except Exception:
            pass

    return "2.0.18"


def find_installer(root: Path) -> Path | None:
    search_dirs = [
        root.parent / "lry-target" / "release" / "bundle" / "nsis",
        root.parent / "lry-target" / "release" / "bundle" / "msi",
        root / "frontend" / "src-tauri" / "target" / "release" / "bundle" / "nsis",
        root / "frontend" / "src-tauri" / "target" / "release" / "bundle" / "msi",
    ]

    candidates = []
    for s_dir in search_dirs:
        if s_dir.exists():
            for ext in ("*.exe", "*.msi"):
                for f in s_dir.glob(ext):
                    candidates.append(f)

    if not candidates:
        return None

    # 按修改时间倒序排列，取最新的安装包
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def get_github_token(root: Path) -> str:
    # 1. 环境变量优先
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        return token

    # 2. 本地持久化 token 文件（已加入 .gitignore，安全无泄露风险）
    token_file = root / ".github_token"
    if token_file.exists():
        token = token_file.read_text(encoding="utf-8").strip()
        if token:
            return token

    print("\n============================================================")
    print("  首次使用需配置 GitHub Personal Access Token (PAT)")
    print("============================================================")
    print("获取方式：")
    print("1. 访问 https://github.com/settings/tokens/new")
    print("2. 勾选权限：'repo' (完整仓库与 Release 发布权限)")
    print("3. 生成后将 token 粘贴在下方：\n")

    user_token = input("请输入 GitHub Token: ").strip()
    if not user_token:
        print("[ERROR] 未提供 Token，取消发布。")
        sys.exit(1)

    save_choice = input("是否保存到本地 .github_token 供下次免输入使用? (y/n): ").strip().lower()
    if save_choice in ("y", "yes", ""):
        token_file.write_text(user_token, encoding="utf-8")
        print("  [OK] 已保存在本地 .github_token (已加入 .gitignore，绝不上库)")

    return user_token


def api_request(url: str, token: str, method: str = "GET", data: Any = None, headers: dict | None = None, timeout: int = 60):
    req_headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "Liuruyan-Release-Script",
    }
    if headers:
        req_headers.update(headers)
    if data is not None and "Content-Length" not in req_headers:
        if hasattr(data, "__len__"):
            req_headers["Content-Length"] = str(len(data))

    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return 500, str(e).encode("utf-8")


def main():
    print("============================================================")
    print("  柳如烟 (Ruyan AI) · 一键发布安装包至 GitHub Releases")
    print("============================================================")

    root = get_project_root()
    setup_proxy()

    version = get_version(root)
    tag = f"v{version}"
    repo = "vooccc/liuruyan"

    print(f"  目标仓库   : {repo}")
    print(f"  发布版本号 : {tag}")

    installer = find_installer(root)
    if not installer or not installer.exists():
        print("\n[ERROR] 未在本地找到已构建的安装包！")
        print("请先运行 '一键打包.bat' 生成安装包，然后再执行发布。")
        sys.exit(1)

    file_size_mb = installer.stat().st_size / (1024 * 1024)
    print(f"  待上传产物 : {installer.name} ({file_size_mb:.2f} MB)")
    print(f"  本地路径   : {installer}")
    print("------------------------------------------------------------")

    token = get_github_token(root)

    # 1. 检查或创建 Release
    print(f"\n[1/3] 正在检查/创建 GitHub Release ({tag})...")
    status, body = api_request(f"https://api.github.com/repos/{repo}/releases/tags/{tag}", token)

    release_data = None
    if status == 200:
        release_data = json.loads(body.decode("utf-8"))
        print(f"  [info] 发现已存在的 Release (ID: {release_data['id']})")
    elif status == 404:
        # 创建新的 Release
        create_payload = json.dumps({
            "tag_name": tag,
            "target_commitish": "main",
            "name": f"柳如烟 (Ruyan AI) {tag} 正式版",
            "body": f"### 柳如烟 {tag} 发布说明\n\n- 自动化桌面客户端安装包发布\n- 支持 Windows 10 / 11 开箱即用",
            "draft": False,
            "prerelease": False,
            "generate_release_notes": True
        }).encode("utf-8")

        status, body = api_request(
            f"https://api.github.com/repos/{repo}/releases",
            token,
            method="POST",
            data=create_payload,
            headers={"Content-Type": "application/json"}
        )
        if status in (200, 201):
            release_data = json.loads(body.decode("utf-8"))
            print(f"  [OK] Release 创建成功！(ID: {release_data['id']})")
        else:
            print(f"[ERROR] 创建 Release 失败 (HTTP {status}): {body.decode('utf-8', errors='ignore')}")
            sys.exit(1)
    else:
        print(f"[ERROR] 检查 Release 失败 (HTTP {status}): {body.decode('utf-8', errors='ignore')}")
        sys.exit(1)

    # 目标上传文件名统一规范化为英文 (柳如烟 -> liuruyan)
    target_asset_name = installer.name.replace("柳如烟", "liuruyan")
    print(f"  发布文件名 : {target_asset_name}")

    # 2. 检查是否有同名文件，若有先清理以便覆盖更新
    print("\n[2/3] 正在检查文件是否已存在...")
    for asset in release_data.get("assets", []):
        if asset.get("name") in (target_asset_name, installer.name):
            print(f"  [info] 发现已存在同名资产 (Asset ID: {asset['id']})，正在覆盖清理...")
            del_status, _ = api_request(f"https://api.github.com/repos/{repo}/releases/assets/{asset['id']}", token, method="DELETE")
            if del_status == 204:
                print("  [OK] 已清理旧资产")
            break

    # 3. 上传安装包文件
    upload_url_template = release_data.get("upload_url", "")
    quoted_name = urllib.parse.quote(target_asset_name)
    upload_url = upload_url_template.split("{")[0] + f"?name={quoted_name}"

    print(f"\n[3/3] 正在流式上传安装包 ({file_size_mb:.2f} MB)...")
    reader = ProgressFileReader(installer)

    try:
        status, body = api_request(
            upload_url,
            token,
            method="POST",
            data=reader,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": str(installer.stat().st_size),
            },
            timeout=900
        )
    finally:
        reader.close()

    if status in (200, 201):
        asset_info = json.loads(body.decode("utf-8"))
        download_url = asset_info.get("browser_download_url")
        print("\n============================================================")
        print("  🎉 安装包已成功发布至 GitHub Releases！")
        print("============================================================")
        print(f"  Release 页面 : {release_data.get('html_url')}")
        print(f"  直链下载地址 : {download_url}")
        print("============================================================")
    else:
        err_msg = body.decode("utf-8", errors="ignore")
        print(f"\n[ERROR] 安装包上传失败 (HTTP {status}): {err_msg}")
        if "EOF" in err_msg or status == 500:
            print("\n💡 提示：网络连接被中断，请确认本地 Clash / VPN 代理已正常开启（如 7897 端口）。")
        sys.exit(1)


if __name__ == "__main__":
    main()
