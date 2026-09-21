import sys, json, os

# directly test the load
from pathlib import Path

data_dir_env = os.environ.get("LIURUYAN_DATA_DIR")
print(f"LIURUYAN_DATA_DIR={data_dir_env}")
if data_dir_env:
    data_dir = Path(data_dir_env)
else:
    data_dir = Path(__file__).resolve().parent.parent.parent.parent
print(f"data_dir={data_dir}")

models_path = data_dir / "models.json"
print(f"models.json exists: {models_path.exists()}")

if models_path.exists():
    raw = models_path.read_text(encoding="utf-8-sig")
    print(f"file size: {len(raw)} bytes")
    config = json.loads(raw)
    print(f"top keys: {list(config.keys())}")
    providers = config.get("providers", {})
    print(f"providers count: {len(providers)}")
    for pid in list(providers.keys())[:3]:
        pdata = providers[pid]
        models = pdata.get("models", [])
        print(f"  provider {pid}: enabled={pdata.get('enabled',True)}, models={[m.get('id','?') if isinstance(m,dict) else m for m in models]}")
    print("...")
    for pid in list(providers.keys())[3:]:
        pdata = providers[pid]
        models = pdata.get("models", [])
        print(f"  provider {pid}: enabled={pdata.get('enabled',True)}, models={[m.get('id','?') if isinstance(m,dict) else m for m in models]}")
