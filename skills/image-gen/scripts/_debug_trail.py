import sys, os
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

env = os.environ.get("LIURUYAN_DATA_DIR", "")
print(f"env raw = {env!r}", flush=True)
print(f"env len = {len(env)}", flush=True)

if env:
    data_dir = Path(env).expanduser()
    print(f"data_dir = {data_dir!r}", flush=True)
    print(f"data_dir exists = {data_dir.exists()}", flush=True)
    
    models_json = data_dir / "models.json"
    print(f"models_json = {models_json!r}", flush=True)
    print(f"models_json exists = {models_json.exists()}", flush=True)
    
    # Also check without trailing space
    data_dir2 = Path(env.strip()).expanduser()
    print(f"data_dir stripped = {data_dir2!r}", flush=True)
    mj2 = data_dir2 / "models.json"
    print(f"stripped models.json exists = {mj2.exists()}", flush=True)
    
    # List dir content
    if data_dir.exists():
        for item in data_dir.iterdir():
            if item.name == "models.json":
                print(f"FOUND in listing!", flush=True)
