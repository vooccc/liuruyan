"""算力账号管理器：负责账号持久化、凭证自动续期、多账号池轮询与状态汇报。
"""
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .services.base import BaseAccountAdapter
from .services.google_antigravity import GoogleAntigravityAdapter, ANTIGRAVITY_43_MODELS

logger = logging.getLogger(__name__)

def _get_store_file() -> Path:
    try:
        from backend.paths import data_dir
        return data_dir() / "compute_accounts.json"
    except Exception:
        pass
    base = os.getenv("LIURUYAN_DATA_DIR")
    if base:
        return Path(base.strip()) / "compute_accounts.json"
    return Path("memory") / "compute_accounts.json"


ACCOUNTS_STORE_FILE = _get_store_file()


class ComputeAccountsManager:
    """算力账号全局管理单例"""

    _instance: Optional["ComputeAccountsManager"] = None

    def __init__(self):
        self._adapters: Dict[str, BaseAccountAdapter] = {
            "google": GoogleAntigravityAdapter(),
        }
        self._accounts: Dict[str, List[Dict[str, Any]]] = {
            "google": [],
            "grok": [],
            "codex": [],
        }
        self._active_index: Dict[str, int] = {
            "google": 0,
            "grok": 0,
            "codex": 0,
        }
        self._enabled_models: Dict[str, Optional[List[str]]] = {}
        self._synced_models: Dict[str, List[Dict[str, Any]]] = {}
        self._load()

    @classmethod
    def get_instance(cls) -> "ComputeAccountsManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _load(self) -> None:
        """从磁盘加载保存的账号（带多路径自动发现）"""
        candidate_paths = [
            ACCOUNTS_STORE_FILE,
            Path.home() / ".liuruyan-dev" / "compute_accounts.json",
            Path.home() / ".liuruyan" / "compute_accounts.json",
            Path("memory") / "compute_accounts.json",
        ]
        loaded = False
        for p in candidate_paths:
            try:
                if p.exists() and p.is_file():
                    with open(p, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, dict):
                        has_accs = any(len(data.get(k, [])) > 0 for k in self._accounts)
                        if has_accs or not loaded:
                            for k in self._accounts:
                                self._accounts[k] = data.get(k, [])
                            enabled_map = data.get("enabled_models")
                            if isinstance(enabled_map, dict):
                                self._enabled_models = {
                                    k: list(v) if isinstance(v, list) else None
                                    for k, v in enabled_map.items()
                                }
                            synced_map = data.get("synced_models")
                            if isinstance(synced_map, dict):
                                self._synced_models = {
                                    k: list(v) if isinstance(v, list) else []
                                    for k, v in synced_map.items()
                                }
                            loaded = True
                            if has_accs:
                                break
            except Exception as e:
                logger.debug("[ComputeAccounts] 尝试从 %s 加载失败: %s", p, e)

        logger.info(
            "[ComputeAccounts] 已从磁盘加载账号: Google=%s, Grok=%s, Codex=%s, 配置模型=%s, 同步模型=%s",
            len(self._accounts.get("google", [])),
            len(self._accounts.get("grok", [])),
            len(self._accounts.get("codex", [])),
            list(self._enabled_models.keys()),
            {k: len(v) for k, v in self._synced_models.items()},
        )

    def _save(self) -> None:
        """持久化保存至磁盘"""
        payload = dict(self._accounts)
        payload["enabled_models"] = self._enabled_models
        payload["synced_models"] = self._synced_models
        for target_path in [ACCOUNTS_STORE_FILE, Path("memory") / "compute_accounts.json"]:
            try:
                target_path.parent.mkdir(parents=True, exist_ok=True)
                with open(target_path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.error("[ComputeAccounts] 保存账号数据至 %s 异常: %s", target_path, e)

    def get_enabled_models(self, provider_id: str) -> Optional[List[str]]:
        """获取该提供商配置的显示模型列表（None 表示全部开启）"""
        return self._enabled_models.get(provider_id)

    def set_enabled_models(self, provider_id: str, models: Optional[List[str]]) -> None:
        """设置该提供商配置的显示模型列表并持久化"""
        if models is None:
            self._enabled_models.pop(provider_id, None)
        else:
            self._enabled_models[provider_id] = list(models)
        self._save()

    def get_synced_models(self, provider_id: str) -> Optional[List[Dict[str, Any]]]:
        """获取已从云端同步的模型列表"""
        return self._synced_models.get(provider_id)

    def set_synced_models(self, provider_id: str, models: List[Dict[str, Any]]) -> None:
        """保存已同步的云端模型列表并持久化"""
        self._synced_models[provider_id] = list(models)
        self._save()

    def get_active_models(self, provider_id: str) -> List[Dict[str, Any]]:
        """获取当前生效的模型清单（优先使用同步的云端 Recommended 列表，兜底用本地静态清单）"""
        synced = self._synced_models.get(provider_id)
        if synced and len(synced) > 0:
            return synced
        if provider_id == "google":
            return ANTIGRAVITY_43_MODELS
        return []

    def get_adapter(self, provider_id: str) -> Optional[BaseAccountAdapter]:
        return self._adapters.get(provider_id)

    def get_overview(self) -> List[Dict[str, Any]]:
        """获取全平台卡片概览（匹配 Cursor 助手界面布局）"""
        self._load()
        google_adapter = self.get_adapter("google")
        google_accounts = self._accounts.get("google", [])
        active_google_models = self.get_active_models("google")
        google_model_count = len(active_google_models) if google_adapter else 0

        grok_accounts = self._accounts.get("grok", [])
        codex_accounts = self._accounts.get("codex", [])

        return [
            {
                "id": "codex",
                "display_name": "Codex",
                "sub_title": "OpenAI Codex",
                "author": "",
                "status": "configured" if codex_accounts else "unconfigured",
                "account_count": len(codex_accounts),
                "model_count": len(codex_accounts) * 10,
                "icon": "codex",
                "can_add": True,
            },
            {
                "id": "grok",
                "display_name": "Grok",
                "sub_title": "xAI Grok",
                "author": "",
                "status": "configured" if grok_accounts else "unconfigured",
                "account_count": len(grok_accounts),
                "model_count": len(grok_accounts) * 4,
                "icon": "grok",
                "can_add": True,
            },
            {
                "id": "google",
                "display_name": "Google",
                "sub_title": "Google Antigravity",
                "author": "",
                "status": "configured" if google_accounts else "unconfigured",
                "account_count": len(google_accounts),
                "model_count": google_model_count if google_accounts else 0,
                "icon": "google",
                "can_add": True,
            },
        ]

    def list_accounts(self, provider_id: str) -> List[Dict[str, Any]]:
        """列出指定提供商的脱敏账号列表"""
        self._load()
        accs = self._accounts.get(provider_id, [])
        now = time.time()
        result = []
        for a in accs:
            expires_at = a.get("expires_at", 0)
            cooling_until = a.get("cooling_until", 0)
            status = "active"
            if cooling_until > now:
                status = "cooling"
            elif expires_at and expires_at < now and not a.get("refresh_token"):
                status = "expired"

            result.append({
                "id": a.get("id"),
                "email": a.get("email"),
                "name": a.get("name"),
                "picture": a.get("picture"),
                "status": status,
                "created_at": a.get("created_at"),
                "has_refresh_token": bool(a.get("refresh_token")),
            })
        return result

    def add_or_update_account(self, provider_id: str, account_info: Dict[str, Any]) -> Dict[str, Any]:
        """添加或更新账号凭据"""
        if provider_id not in self._accounts:
            self._accounts[provider_id] = []

        acc_id = account_info.get("id") or str(int(time.time() * 1000))
        account_info["id"] = acc_id
        account_info["created_at"] = account_info.get("created_at") or int(time.time())

        # 检查是否已存在（按 email 或 id 去重更新）
        email = account_info.get("email")
        existing_idx = -1
        for idx, item in enumerate(self._accounts[provider_id]):
            if (email and item.get("email") == email) or (item.get("id") == acc_id):
                existing_idx = idx
                break

        if existing_idx >= 0:
            self._accounts[provider_id][existing_idx].update(account_info)
        else:
            self._accounts[provider_id].append(account_info)

        self._save()
        logger.info("[ComputeAccounts] 成功保存 %s 账号: %s", provider_id, email or acc_id)
        return account_info

    def delete_account(self, provider_id: str, account_id: str) -> bool:
        """删除指定账号"""
        if provider_id not in self._accounts:
            return False
        before_len = len(self._accounts[provider_id])
        self._accounts[provider_id] = [
            a for a in self._accounts[provider_id] if a.get("id") != account_id and a.get("email") != account_id
        ]
        if len(self._accounts[provider_id]) < before_len:
            self._save()
            logger.info("[ComputeAccounts] 已删除 %s 账号: %s", provider_id, account_id)
            return True
        return False

    def set_primary_account(self, provider_id: str, account_id: str) -> bool:
        """将指定账号置为主力调度节点（移动到首位）"""
        if provider_id not in self._accounts:
            return False
        accs = self._accounts[provider_id]
        target_idx = -1
        for idx, a in enumerate(accs):
            if a.get("id") == account_id or a.get("email") == account_id:
                target_idx = idx
                break
        if target_idx > 0:
            target = accs.pop(target_idx)
            accs.insert(0, target)
            self._active_index[provider_id] = 0
            self._save()
            logger.info("[ComputeAccounts] 已将账号 %s 置为 %s 主力调度", account_id, provider_id)
            return True
        return target_idx == 0

    def toggle_account_enabled(self, provider_id: str, account_id: str, enabled: Optional[bool] = None) -> bool:
        """切换账号的入池状态（enabled）"""
        if provider_id not in self._accounts:
            return False
        for acc in self._accounts[provider_id]:
            if acc.get("id") == account_id or acc.get("email") == account_id:
                if enabled is None:
                    acc["enabled"] = not acc.get("enabled", True)
                else:
                    acc["enabled"] = bool(enabled)
                self._save()
                logger.info("[ComputeAccounts] 账号 %s 入池状态已更新为: %s", account_id, acc["enabled"])
                return acc["enabled"]
        return False

    async def get_valid_access_token(self, provider_id: str) -> Optional[str]:
        """获取当前轮到并有效的 Access Token，必要时自动刷新"""
        accs = self._accounts.get(provider_id, [])
        if not accs:
            return None

        adapter = self.get_adapter(provider_id)
        if not adapter:
            return None

        now = time.time()
        start_idx = self._active_index.get(provider_id, 0)
        n = len(accs)

        for i in range(n):
            curr_idx = (start_idx + i) % n
            acc = accs[curr_idx]
            # 避开未入池（禁用）的账号
            if acc.get("enabled", True) is False:
                continue
            # 避开冷却中（如 429 熔断）的账号（仅当账号池有多个账号时才跳过，避免单账号用户全局不可用）
            if n > 1 and acc.get("cooling_until", 0) > now:
                continue

            access_token = acc.get("access_token")
            expires_at = acc.get("expires_at", 0)
            refresh_token = acc.get("refresh_token")

            # 提前 60 秒自动刷新
            if expires_at and (expires_at - now < 60) and refresh_token:
                try:
                    logger.info("[ComputeAccounts] Token 即将过期，自动调用 refresh_token: %s", acc.get("email"))
                    refreshed = await adapter.refresh_access_token(refresh_token)
                    acc["access_token"] = refreshed.get("access_token", access_token)
                    acc["expires_at"] = refreshed.get("expires_at", now + 3600)
                    if refreshed.get("refresh_token"):
                        acc["refresh_token"] = refreshed.get("refresh_token")
                    self._save()
                    access_token = acc["access_token"]
                except Exception as e:
                    logger.warning("[ComputeAccounts] 自动刷新 Token 失败: %s", e)
                    continue

            if access_token:
                self._active_index[provider_id] = curr_idx
                return access_token

        return None

    def mark_cooling(self, provider_id: str, cooldown_seconds: int = 300) -> None:
        """遭遇 429 限流时，将当前账号标记冷却，并自动顺延至下一账号"""
        accs = self._accounts.get(provider_id, [])
        if len(accs) <= 1:
            # 单账号不进行全局冷却锁定，以便用户切换其他可用模型
            return
        curr_idx = self._active_index.get(provider_id, 0)
        if 0 <= curr_idx < len(accs):
            accs[curr_idx]["cooling_until"] = time.time() + cooldown_seconds
            logger.warning(
                "[ComputeAccounts] 账号 %s 遇到限流，进入冷却 %s 秒，自动切至下一账号",
                accs[curr_idx].get("email"),
                cooldown_seconds,
            )
            self._active_index[provider_id] = (curr_idx + 1) % len(accs)
            self._save()

    async def get_account_quota(
        self,
        provider_id: str,
        account_id: Optional[str] = None,
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        """获取指定账号（或当前首选账号）的实时配额情况（带 60s 缓存与严格防风控机制）"""
        self._load()
        adapter = self.get_adapter(provider_id)
        if not adapter or not hasattr(adapter, "fetch_user_quota_summary"):
            return {"ok": False, "error": "该提供商不支持配额统计"}

        accounts = self._accounts.get(provider_id, [])
        if not accounts:
            return {"ok": False, "error": "暂无已登录账号"}

        target_acc = None
        if account_id:
            for a in accounts:
                if a.get("id") == account_id:
                    target_acc = a
                    break
        if not target_acc:
            target_acc = accounts[0]

        now = time.time()
        token = target_acc.get("access_token")
        exp = target_acc.get("expires_at", 0)
        refresh_token = target_acc.get("refresh_token")

        if (not token or (exp and now >= exp - 60)) and refresh_token:
            try:
                new_token_data = await adapter.refresh_access_token(refresh_token)
                target_acc["access_token"] = new_token_data.get("access_token")
                exp_in = new_token_data.get("expires_in", 3599)
                target_acc["expires_at"] = now + exp_in
                self._save()
                token = target_acc["access_token"]
            except Exception as e:
                logger.error("[ComputeAccounts] 自动刷新 Token 失败: %s", e)

        if not token:
            return {"ok": False, "error": "账号令牌已失效，请重新授权"}

        project_id = target_acc.get("project_id")
        return await adapter.fetch_user_quota_summary(
            access_token=token,
            project_id=project_id,
            force_refresh=force_refresh,
        )

