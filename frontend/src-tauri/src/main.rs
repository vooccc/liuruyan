#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use tauri::{AppHandle, RunEvent};
use tauri_plugin_shell::ShellExt;

#[cfg(not(debug_assertions))]
use std::sync::{Mutex, OnceLock};
#[cfg(not(debug_assertions))]
use tauri_plugin_shell::process::{CommandChild, CommandEvent};

/// 安全的外部 URL scheme 白名单。
/// 拒绝 file:// / javascript: / data: 等可能被恶意角色注入用于钓鱼或脱沙箱的 scheme。
fn is_safe_external_url(url: &str) -> bool {
    let lower = url.trim().to_ascii_lowercase();
    lower.starts_with("http://")
        || lower.starts_with("https://")
        || lower.starts_with("mailto:")
        || lower.starts_with("tel:")
}

/// 前端外部链接拦截后调这个 command 让系统默认浏览器打开。
/// 通过系统默认 Shell 在外部浏览器中安全拉起链接。
#[tauri::command]
async fn open_external(app: AppHandle, url: String) -> Result<(), String> {
    if !is_safe_external_url(&url) {
        return Err(format!("Blocked unsafe URL scheme: {}", url));
    }
    app.shell()
        .open(&url, None)
        .map_err(|e| format!("shell.open failed: {}", e))
}

/// 用全局 OnceLock 保存 Python 后端 sidecar 句柄，app 退出时 kill。
/// 不用 `tauri::State<T>` 是因为它的生命周期绑到 AppHandle 引用，
/// 在 `Builder::run` 的闭包里很难借出去。
#[cfg(not(debug_assertions))]
static SIDECAR_CHILD: OnceLock<Mutex<Option<CommandChild>>> = OnceLock::new();

#[cfg(not(debug_assertions))]
fn sidecar_slot() -> &'static Mutex<Option<CommandChild>> {
    SIDECAR_CHILD.get_or_init(|| Mutex::new(None))
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_fs::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_process::init())
        .invoke_handler(tauri::generate_handler![open_external])
        .setup(|_app| {
            // Dev 模式不 spawn sidecar：dev 时开发者靠 启动.bat 拉起 venv Python backend，
            // 走 sidecar 会和 venv 抢 127.0.0.1:8001 端口（10048）。
            // Prod（打包 msi 安装后）只有 sidecar 这一条路，正常 spawn。
            //
            // 前端 waitForBackend 统一用 /health 轮询判定 ready，不依赖 Tauri event。
            // 看起来可以"listen backend-ready + poll race"省 200ms，但那样会在 dev 下
            // 触发 vite "new dependencies optimized, reloading" 全栏重启（几秒），赔本买卖。

            #[cfg(not(debug_assertions))]
            {
                // Prod：spawn PyInstaller bundled sidecar
                let sidecar = _app
                    .shell()
                    .sidecar("liuruyan-backend")
                    .expect("failed to locate sidecar binary")
                    .args(["--host", "127.0.0.1", "--port", "8001"])
                    .env("LIURUYAN_PROD", "1");

                let (mut rx, child) = sidecar.spawn().expect("failed to spawn sidecar");

                // 保存句柄，退出时清理
                *sidecar_slot().lock().unwrap() = Some(child);

                // 异步消费 sidecar 的 stdout/stderr，原样转发到 Tauri 进程日志
                tauri::async_runtime::spawn(async move {
                    while let Some(event) = rx.recv().await {
                        match event {
                            CommandEvent::Stdout(bytes) => {
                                print!("[backend] {}", String::from_utf8_lossy(&bytes));
                            }
                            CommandEvent::Stderr(bytes) => {
                                eprint!("[backend err] {}", String::from_utf8_lossy(&bytes));
                            }
                            CommandEvent::Error(err) => {
                                eprintln!("[backend error] {}", err);
                            }
                            CommandEvent::Terminated(payload) => {
                                eprintln!("[backend terminated] code={:?}", payload.code);
                            }
                            _ => {}
                        }
                    }
                });
            }

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|_app_handle, event| {
            if let RunEvent::ExitRequested { .. } = event {
                // Dev 模式 sidecar 没 spawn，也就没有 child/backend 进程要 kill；
                // venv Python 由 启动.bat 开的独立窗口单独负责。
                #[cfg(not(debug_assertions))]
                {
                    // PyInstaller onefile 有 bootloader + python 两个同名进程。
                    // child.kill() 先跑会让 python 脱离子树 → /T /PID 就找不到孤儿了。
                    // 所以 Windows 走 /IM（按映像名杀）+ CREATE_NO_WINDOW（防 taskkill 自己闪黑窗）。
                    #[cfg(windows)]
                    {
                        use std::os::windows::process::CommandExt;
                        const CREATE_NO_WINDOW: u32 = 0x08000000;
                        let _ = std::process::Command::new("taskkill")
                            .args(["/F", "/T", "/IM", "liuruyan-backend.exe"])
                            .creation_flags(CREATE_NO_WINDOW)
                            .output();
                    }
                    if let Some(child) = sidecar_slot().lock().unwrap().take() {
                        let _ = child.kill();
                    }
                }
            }
        });
}
