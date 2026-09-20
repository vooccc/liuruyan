# -*- mode: python ; coding: utf-8 -*-
import os
import platform as _platform
import sys as _sys
from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_submodules

_spec_dir = os.path.dirname(os.path.abspath(SPEC)) if 'SPEC' in dir() else os.path.dirname(os.path.abspath(__file__)) if '__file__' in dir() else os.path.abspath('.')
_harness_path = os.path.abspath(os.path.join(_spec_dir, 'packages', 'harness'))
if os.path.isdir(_harness_path) and _harness_path not in _sys.path:
    _sys.path.insert(0, _harness_path)

_entry = '_entry.py'
_pathex = ['..', _harness_path]

datas = []
hiddenimports = []
datas += collect_data_files('jedi')
datas += collect_data_files('parso')
# 首次启动时 seed 用的模板（paths.seed_models_json_if_missing 读取）
datas += [('../models.json.example', '.')]
# 打包 agents/templates 整个目录，供 seed_templates_if_missing 首次启动拷贝
datas += [('../agents/templates', 'agents/templates')]
# 打包 skills/ 目录，供 seed_skills_if_missing 首次启动拷贝。
# 排除 skill 本地开发产物（.venv / node_modules / __pycache__ / .git），
# 否则 jshook-reverse 的 node_modules (119MB) + webnovel-writer 的 .venv (62MB)
# 全打进 exe，sidecar 从 ~7MB 膨胀到 187MB。
# 不用 Tree() 因为它返回 3-tuple，Analysis(datas=) 只接受 2-tuple。
_skill_excludes = {'.venv', 'node_modules', '__pycache__', '.git'}
for _root, _dirs, _files in os.walk(os.path.join('..', 'skills')):
    _dirs[:] = [d for d in _dirs if d not in _skill_excludes]
    for _f in _files:
        if _f.endswith('.pyc'):
            continue
        _src = os.path.join(_root, _f)
        _dst = os.path.relpath(_root, '..')
        datas += [(_src, _dst)]

# 把当前架构的 ripgrep 二进制塞进 sidecar exe。
# 必须与 backend.tools.builtin.file_tools._find_rg() 的 _MEIPASS 查找路径对齐。
# 不打全架构是因为 sidecar 一次只跑一种 OS/arch，多打 = 浪费 25MB。
_machine = _platform.machine().lower()
if _machine in ('amd64', 'x86_64', 'x64'):
    _arch = 'x64'
elif _machine in ('arm64', 'aarch64'):
    _arch = 'arm64'
elif _machine in ('i386', 'i686', 'x86'):
    _arch = 'ia32'
else:
    _arch = _machine
if _sys.platform == 'win32':
    _plat = 'win32'
elif _sys.platform == 'darwin':
    _plat = 'darwin'
else:
    _plat = 'linux'
_rg_arch_tag = f'{_arch}-{_plat}'
_rg_src_dir = os.path.join('..', 'vendor', 'ripgrep', _rg_arch_tag)
if os.path.isdir(_rg_src_dir):
    datas += [(_rg_src_dir, f'vendor/ripgrep/{_rg_arch_tag}')]
else:
    print(f"[spec] WARN: ripgrep not found at {_rg_src_dir} — backend will fall back to Python search. "
          f"Run `python scripts/bootstrap_ripgrep.py` first to bundle rg.")
hiddenimports += collect_submodules(
    'backend',
    filter=lambda name: not any(p in name.split('.') for p in ['venv', 'build', 'dist', 'dist_nuitka', 'tests_core', 'test_*', '__pycache__'])
)
# ddgs 是 web_search 的免费兜底；web_tools.py 里 try/except import，静态分析抓不到
hiddenimports += collect_submodules('ddgs')
# read_file 文档支持库 — file_tools.py 里都是函数体内 lazy import，PyInstaller
# 静态分析抓得到顶层模块但子模块/数据文件可能漏。打包后跑 PDF/docx/pptx/xlsx/ipynb
# 任意一个 ImportError 都会让 read_file 失能，所以兜底全收。
hiddenimports += collect_submodules('pypdf')
hiddenimports += collect_submodules('pdfplumber')
hiddenimports += collect_submodules('docx')      # python-docx 模块名是 docx
hiddenimports += collect_submodules('pptx')      # python-pptx 模块名是 pptx
hiddenimports += collect_submodules('openpyxl')
hiddenimports += collect_submodules('nbformat')  # .ipynb 支持
# v2 harness 核心包（98 个子模块，包括 buses, channels, guards, llm 等）
hiddenimports += collect_submodules('liuruyan')

# 将 packages/harness 完整目录打入 backend/packages/harness，保障资源与动态加载
if os.path.isdir(_harness_path):
    datas += [(_harness_path, 'backend/packages/harness')]


a = Analysis(
    [_entry],
    pathex=_pathex,
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # dev-only 包，不该进 production exe
        'uv', 'pip', 'setuptools', 'pyinstaller', 'pytest', '_pytest',
        'backend.venv',
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='liuruyan-backend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
