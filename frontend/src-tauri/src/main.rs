#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use tauri::AppHandle;
use tauri_plugin_shell::ShellExt;

#[cfg(not(debug_assertions))]
use std::io::{BufRead, BufReader};
#[cfg(all(not(debug_assertions), windows))]
use std::os::windows::process::CommandExt;
#[cfg(not(debug_assertions))]
use std::path::PathBuf;
#[cfg(not(debug_assertions))]
use std::process::{Child, Command, Stdio};
#[cfg(not(debug_assertions))]
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Mutex, OnceLock,
};
#[cfg(not(debug_assertions))]
use tauri::{Manager, RunEvent};

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

/// 用全局 OnceLock 保存 Python 后端进程句柄，app 退出时 kill。
#[cfg(not(debug_assertions))]
static BACKEND_CHILD: OnceLock<Mutex<Option<Child>>> = OnceLock::new();
#[cfg(not(debug_assertions))]
static BACKEND_KILLED: AtomicBool = AtomicBool::new(false);

#[cfg(not(debug_assertions))]
fn backend_slot() -> &'static Mutex<Option<Child>> {
    BACKEND_CHILD.get_or_init(|| Mutex::new(None))
}

/// Windows 上 `tauri_utils::platform::current_exe()` 会调用 `canonicalize()` 返回 `\\?\` 前缀路径，
/// 传给子进程作为 executable / current_dir 时可能让 Python 的 `os.getcwd()` / `sys.executable` 带上 `\\?\`。
#[cfg(all(not(debug_assertions), windows))]
fn strip_win_verbatim(path: PathBuf) -> PathBuf {
    let s = path.to_string_lossy();
    if let Some(rest) = s.strip_prefix(r"\\?\UNC\") {
        PathBuf::from(format!(r"\\{}", rest))
    } else if let Some(rest) = s.strip_prefix(r"\\?\") {
        PathBuf::from(rest)
    } else {
        path
    }
}

#[cfg(all(not(debug_assertions), not(windows)))]
fn strip_win_verbatim(path: PathBuf) -> PathBuf {
    path
}

#[cfg(not(debug_assertions))]
fn spawn_onedir_backend(app: &tauri::App) {
    let exe_name = if cfg!(windows) {
        "liuruyan-backend.exe"
    } else {
        "liuruyan-backend"
    };
    let resource_dir = strip_win_verbatim(
        app.path()
            .resource_dir()
            .expect("failed to resolve resource_dir"),
    );
    let in_bundle_dir = resource_dir.join("backend-dist").join(exe_name);
    let backend_exe = if in_bundle_dir.exists() {
        in_bundle_dir
    } else {
        resource_dir.join(exe_name)
    };

    if !backend_exe.exists() {
        panic!("failed to locate backend binary at {:?}", backend_exe);
    }

    let mut cmd = Command::new(&backend_exe);
    cmd.args(["--host", "127.0.0.1", "--port", "8001"])
        .env("LIURUYAN_PROD", "1")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if let Some(parent) = backend_exe.parent() {
        cmd.current_dir(parent);
    }
    #[cfg(windows)]
    cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW

    let mut child = cmd.spawn().expect("failed to spawn onedir backend");

    if let Some(stdout) = child.stdout.take() {
        std::thread::spawn(move || {
            let reader = BufReader::new(stdout);
            for line in reader.lines().map_while(Result::ok) {
                println!("[backend] {}", line);
            }
        });
    }
    if let Some(stderr) = child.stderr.take() {
        std::thread::spawn(move || {
            let reader = BufReader::new(stderr);
            for line in reader.lines().map_while(Result::ok) {
                eprintln!("[backend err] {}", line);
            }
        });
    }

    BACKEND_KILLED.store(false, Ordering::SeqCst);
    *backend_slot().lock().unwrap() = Some(child);
}

#[cfg(not(debug_assertions))]
fn kill_backend() {
    if BACKEND_KILLED.swap(true, Ordering::SeqCst) {
        return;
    }
    if let Some(mut child) = backend_slot().lock().unwrap().take() {
        let _ = child.kill();
        let _ = child.wait();
    }
    // Windows 兜底清扫：杀掉可能残留的同名子进程树（CREATE_NO_WINDOW 防闪黑窗）
    #[cfg(windows)]
    {
        const CREATE_NO_WINDOW: u32 = 0x08000000;
        let _ = Command::new("taskkill")
            .args(["/F", "/T", "/IM", "liuruyan-backend.exe"])
            .creation_flags(CREATE_NO_WINDOW)
            .output();
    }
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
            // Dev 模式不 spawn 后端进程：dev 时开发者靠 启动.bat 拉起 venv Python backend，
            // 走打包产物会和 venv 抢 127.0.0.1:8001 端口（10048）。
            // Prod（打包安装包后）拉起随包分发的 onedir backend-dist/liuruyan-backend.exe。
            #[cfg(not(debug_assertions))]
            {
                spawn_onedir_backend(_app);
            }

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|_app_handle, event| {
            #[cfg(not(debug_assertions))]
            if let RunEvent::ExitRequested { .. } | RunEvent::Exit = event {
                kill_backend();
            }
            #[cfg(debug_assertions)]
            let _ = event;
        });
}
