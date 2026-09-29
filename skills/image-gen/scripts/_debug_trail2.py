import sys, os
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

env = os.environ.get("LIURUYAN_DATA_DIR", "")
print(f"env = {env!r} len={len(env)}", flush=True)

# Simulate exactly what _load_providers does
for ch in env:
    print(f"  U+{ord(ch):04X} {ch!r}", flush=True)

data_dir = Path(env).expanduser()
models_path = data_dir / "models.json"

print(f"data_dir = {data_dir!r}", flush=True)
print(f"models_path = {models_path!r}", flush=True)
print(f"models_path.exists() = {models_path.exists()}", flush=True)

# Also check data_dir.exists()
try:
    print(f"data_dir.exists() = {data_dir.exists()}", flush=True)
except Exception as e:
    print(f"data_dir.exists() error: {e}", flush=True)

# Compare with stripped version
data_dir2 = Path(env.rstrip()).expanduser()
models_path2 = data_dir2 / "models.json"
print(f"stripped models_path = {models_path2!r}", flush=True)
print(f"stripped exists = {models_path2.exists()}", flush=True)
