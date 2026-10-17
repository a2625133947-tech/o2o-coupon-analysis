"""Discover optional Tableau build tools without launching any desktop application."""
import os
from pathlib import Path
import shutil
import sys


def tableau_app():
    configured = os.environ.get('O2O_TABLEAU_APP')
    candidates = [Path(configured)] if configured else sorted(Path('/Applications').glob('Tableau*.app'))
    candidates += sorted((Path.home() / 'Applications').glob('Tableau*.app')) if not configured else []
    for path in candidates:
        if path.is_dir():
            return path.resolve()
    raise FileNotFoundError('找不到 Tableau 应用目录；设置 O2O_TABLEAU_APP=/path/Tableau.app。数据准备需要本地 Hyper/RCC，不会启动界面。')


def resolve_tool(name):
    specs = {'hyperd': ('O2O_HYPERD', 'Contents/MacOS/hyper/hyperd'),
             'rcc': ('O2O_RCC', 'Contents/MacOS/rcc')}
    env, relative = specs[name]
    configured = os.environ.get(env)
    path = Path(configured).expanduser() if configured else tableau_app() / relative
    if not path.is_file() or not os.access(path, os.X_OK):
        raise FileNotFoundError(f'{name} 不可执行: {path}；设置 {env} 为有效可执行文件。')
    return path.resolve()


def schema_python():
    configured = os.environ.get('O2O_SCHEMA_PYTHON')
    candidates = [configured] if configured else [sys.executable, shutil.which('python3'), '/Library/Frameworks/Python.framework/Versions/3.11/bin/python3']
    import subprocess
    for value in filter(None, candidates):
        path = Path(value)
        if path.is_file() and subprocess.run([str(path), '-c', 'import lxml.etree'], capture_output=True).returncode == 0:
            return path.resolve()
    raise FileNotFoundError('找不到包含 lxml 的 Python；设置 O2O_SCHEMA_PYTHON=/path/python。')


def resolve_tableau_dependencies():
    """Return explicit validated tool paths; callers may persist these in provenance."""
    probe = Path(os.environ.get('O2O_TABLEAU_PROBE', str(Path(__file__).resolve().parents[1] / 'tableau/probe/connection_probe.twb'))).expanduser()
    if not probe.is_file():
        raise FileNotFoundError(f'缺少连接模板: {probe}；设置 O2O_TABLEAU_PROBE 为有效 .twb 文件。')
    configured_rcc = os.environ.get('O2O_RCC_RESOURCE') or os.environ.get('O2O_TABLEAU_RCC')
    resource = Path(configured_rcc).expanduser() if configured_rcc else tableau_app() / 'Contents/Resources/tablangres.rcc'
    if not resource.is_file():
        raise FileNotFoundError(f'缺少 Tableau RCC schema 资源: {resource}；设置 O2O_RCC_RESOURCE。')
    return {'hyperd': resolve_tool('hyperd'), 'rcc_resource': resource.resolve(),
            'schema_python': schema_python(), 'probe': probe.resolve()}
