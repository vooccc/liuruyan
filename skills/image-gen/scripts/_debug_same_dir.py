import sys, os
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

print(f"__file__ = {__file__!r}", flush=True)
print(f"resolve = {Path(__file__).resolve()!r}", flush=True)

env = os.environ.get("LIURUYAN_DATA_DIR")
print(f"LIURUYAN_DATA_DIR = {env!r}", flush=True)

if env:
    data_dir = Path(env).expanduser()
    print(f"env data_dir = {data_dir}", flush=True)
    print(f"env data_dir exists = {data_dir.exists()}", flush=True)
else:
    project_root = Path(__file__).resolve().parent.parent.parent.parent
    print(f"project_root from __file__ = {project_root}", flush=True)
    models_json = project_root / "models.json"
    print(f"models_json = {models_json}", flush=True)
    print(f"exists = {models_json.exists()}", flush=True)
    if models_json.exists():
        print(f"readable = {len(models_json.read_bytes())} bytes", flush=True)
    else:
        # Try with different parent count
        for parents in [1, 2, 3, 4, 5]:
            p = Path(__file__).resolve().parent
            for _ in range(parents - 1):
                p = p.parent
            print(f"  {parents} parents up: {p / 'models.json'} exists={(p / 'models.json').exists()}", flush=True)
