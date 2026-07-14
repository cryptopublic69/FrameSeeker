from __future__ import annotations

import json
from pathlib import Path

from server import app


target = Path(__file__).with_name("openapi.json")
target.write_text(
    json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
print(target)
