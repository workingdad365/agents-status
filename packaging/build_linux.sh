#!/usr/bin/env bash
# GLIBC 2.28 환경에서 고정 Python으로 Linux 배포본 생성함.
set -euo pipefail

/opt/python/cp313-cp313/bin/python -m pip install uv==0.9.9
export PATH="/opt/python/cp313-cp313/bin:$PATH"
export UV_PROJECT_ENVIRONMENT=/tmp/agents-status-build
export UV_PYTHON_INSTALL_DIR=/tmp/agents-status-python
uv python install 3.13.9
uv sync --locked --no-install-project --no-dev --managed-python --python 3.13.9
uv pip install --python "$UV_PROJECT_ENVIRONMENT/bin/python" --no-deps -r packaging/requirements-build.txt
if [ -n "${RELEASE_TAG:-}" ]; then
  uv run --no-sync python packaging/build_release.py --check-tag "$RELEASE_TAG"
fi
uv run --no-sync python -m unittest discover -v
uv run --no-sync python packaging/build_release.py
