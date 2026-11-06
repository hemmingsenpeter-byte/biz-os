"""uvicorn target: `uvicorn biz_kernel.api.main:app`. No DBOS launch in this process.

Approval decisions wake SOP runs through a DBOSClient on the DBOS system database.
Env: BIZ_DATABASE_URL, BIZ_DBOS_SYSTEM_URL, BIZ_APP_DIR, BIZ_REPO_ROOT.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path

from biz_kernel import db
from biz_kernel.api.app import create_app
from biz_kernel.cli.main import find_repo_root
from biz_kernel.loader import load_composition
from biz_kernel.runtime import build_runtime, capability_routes
from biz_kernel.workflows import runner
from biz_kernel.workflows.worker import dbos_system_url


def build_app():  # type: ignore[no-untyped-def]
    from dbos import DBOSClient

    app_dir = Path(os.environ["BIZ_APP_DIR"])
    root = Path(os.environ["BIZ_REPO_ROOT"]) if "BIZ_REPO_ROOT" in os.environ else None
    manifest = load_composition(app_dir, root or find_repo_root(app_dir))
    url = os.environ.get("BIZ_DATABASE_URL") or db.database_url()
    db.configure(url)
    sys_url = os.environ.get("BIZ_DBOS_SYSTEM_URL") or dbos_system_url(url)
    runner._CLIENT = DBOSClient(system_database_url=sys_url, lazy=True)  # type: ignore[attr-defined]
    app = create_app(manifest)
    if any(
        hasattr(importlib.import_module(f"biz_cap_{c}"), "routes") for c in manifest.capabilities
    ):
        # Capability routes need the runtime (bus, ports). Building it launches no DBOS.
        rt = build_runtime(app_dir, root or find_repo_root(app_dir))
        for name, router in capability_routes(rt):
            # Public paths only: /webhooks/* is what the tunnel exposes; /v1 stays private.
            app.include_router(router, prefix="/webhooks", tags=[name])
    return app


app = build_app() if "BIZ_APP_DIR" in os.environ else None
