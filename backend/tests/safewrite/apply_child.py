"""Run one apply job in a real subprocess (crash tests). Usage: apply_child.py <request.json>

The request holds data_dir, job_id, policy and the items. Failpoints come from
GAMA_ENABLE_FAILPOINTS / GAMA_FAILPOINTS; an armed one ends the process with os._exit.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from gama.safewrite.env import WriteEnv  # noqa: E402
from gama.safewrite.writer import WriteItem, run_apply  # noqa: E402

req = json.loads(Path(sys.argv[1]).read_text())
env = WriteEnv(req["data_dir"], audio="off")
res = run_apply([WriteItem(**i) for i in req["items"]], env, job_id=req["job_id"], policy=req.get("policy", "stop_rollback"))
print(json.dumps({"status": res.status}))
