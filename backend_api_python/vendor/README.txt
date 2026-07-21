Local easy_tdx development wheel
================================

The wheel in this directory is a local, ignored development artifact. Build it
from the pinned sibling checkout before building the local backend image:

  cd /home/quantadinger/easy_tdx/web-ui
  NODE_OPTIONS=--max-old-space-size=1536 npm ci --ignore-scripts
  NODE_OPTIONS=--max-old-space-size=1536 npm run build

  cd /home/quantadinger/easy_tdx
  PIP_NO_CACHE_DIR=1 python -m pip wheel . --no-deps \
    --wheel-dir /home/quantadinger/QuantDinger/backend_api_python/vendor

Verify the filename, source commit, and SHA256 against easy_tdx-wheel.env and
easy_tdx-wheel.sha256. Public package publication and production artifact
delivery are deliberately deferred until the release stage.
