$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$marinePython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $marinePython)) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw '创建 Python 环境失败。' }
    & $marinePython -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw '安装网站依赖失败。' }
}
& $marinePython -m streamlit run streamlit_app.py --server.address 0.0.0.0 --server.port 8501
