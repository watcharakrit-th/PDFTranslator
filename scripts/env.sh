# Source in a new shell before using uv or pdftr:   . scripts/env.sh
# Keeps every download, model and temp file inside the project's .cache folder
# (the Linux counterpart of scripts/env.ps1).
root="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
export UV_PYTHON_INSTALL_DIR="$root/.cache/python"
export UV_CACHE_DIR="$root/.cache/uv"
export HF_HOME="$root/.cache/huggingface"
export TORCH_HOME="$root/.cache/torch"
export TMPDIR="$root/.cache/tmp"
export PYTHONUTF8=1
mkdir -p "$TMPDIR"
echo "pdftr environment set (caches under $root/.cache)"
