use std::env;
use std::fs;
use std::path::PathBuf;

fn main() {
    // 确保 Tauri externalBin 所需的 sidecar 二进制文件存在，
    // 避免在 dev 开发调试态（cargo run / tauri dev）因缺少 sidecar 产物而中断编译。
    let target = env::var("TARGET").unwrap_or_else(|_| "x86_64-pc-windows-msvc".to_string());
    let ext = if target.contains("windows") { ".exe" } else { "" };
    let sidecar_name = format!("liuruyan-backend-{}{}", target, ext);

    let manifest_dir = PathBuf::from(env::var("CARGO_MANIFEST_DIR").unwrap_or_else(|_| ".".to_string()));
    let binaries_dir = manifest_dir.join("binaries");
    let target_path = binaries_dir.join(&sidecar_name);

    if !target_path.exists() {
        let _ = fs::create_dir_all(&binaries_dir);

        // 获取工程根目录 (frontend/src-tauri -> frontend -> project_root)
        let project_root = manifest_dir
            .parent()
            .and_then(|p| p.parent())
            .map(|p| p.to_path_buf())
            .unwrap_or_else(|| manifest_dir.clone());

        // 尝试从本地或同级工程查找已有编译产物
        let candidate_sources = [
            project_root.join("backend/dist/liuruyan-backend.exe"),
            project_root.join("../柳如烟/backend/dist/liuruyan-backend.exe"),
            project_root.join("../柳如烟/frontend/src-tauri/binaries").join(&sidecar_name),
        ];

        let mut copied = false;
        for src in candidate_sources.iter() {
            if src.exists() {
                println!("cargo:warning=[build.rs] Copying backend binary from {:?} to {:?}", src, target_path);
                if let Ok(_) = fs::copy(src, &target_path) {
                    copied = true;
                    break;
                }
            }
        }

        if !copied {
            // Dev 模式下 main.rs 只在 #[cfg(not(debug_assertions))] 时调用 sidecar，
            // 开发时依靠 venv 独立启动后端。为了通过 tauri_build::build() 的静态资源存在性校验，
            // 在缺失时自动创建一个占位文件。
            println!("cargo:warning=[build.rs] Sidecar binary not found. Creating placeholder for development build.");
            let _ = fs::write(&target_path, b"PLACEHOLDER_DEV_BINARY\n");
        }
    }

    tauri_build::build()
}

