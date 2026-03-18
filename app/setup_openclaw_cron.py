from __future__ import annotations

import json
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from bridge_common import APP_DIR


WSL_DISTRO = "Ubuntu-24.04"
GATEWAY_SERVICE = "openclaw-gateway.service"
JOB_NAME = "mirrorbot-x-watch"
JOB_DESCRIPTION = "mirrorbot の X シグナル取り込み"
JOB_EXPR = "*/20 * * * *"
JOB_TZ = "Asia/Tokyo"
DISCORD_CHANNEL_ID = "1483783703174971442"


@dataclass(frozen=True)
class Paths:
    prompt_path: Path
    batch_path: Path
    ingest_script_path: Path
    posts_json_path: Path
    posts_text_path: Path
    fetch_script_path: Path


def run_wsl(command: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["wsl", "-d", WSL_DISTRO, "bash", "-lc", command],
        check=check,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )


def to_wsl_path(path: Path) -> str:
    result = run_wsl(f"wslpath -a '{path.as_posix()}'")
    return result.stdout.strip()


def load_prompt(paths: Paths) -> str:
    template = paths.prompt_path.read_text(encoding="utf-8")
    return template.format(
        batch_file=to_wsl_path(paths.batch_path),
        ingest_script=to_wsl_path(paths.ingest_script_path),
        posts_json_file=to_wsl_path(paths.posts_json_path),
        posts_text_file=to_wsl_path(paths.posts_text_path),
        fetch_script_windows=paths.fetch_script_path.as_posix().replace("/", "\\"),
        posts_json_windows=paths.posts_json_path.as_posix().replace("/", "\\"),
        posts_text_windows=paths.posts_text_path.as_posix().replace("/", "\\"),
        repo_root_windows=APP_DIR.parent.as_posix().replace("/", "\\"),
    )


def read_jobs() -> dict[str, object]:
    result = run_wsl("cat ~/.openclaw/cron/jobs.json")
    return json.loads(result.stdout)


def write_jobs(payload: dict[str, object], temp_path: Path) -> None:
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp_wsl = to_wsl_path(temp_path)
    run_wsl(f"cp '{temp_wsl}' ~/.openclaw/cron/jobs.json")


def build_job(existing: dict[str, object] | None, message: str) -> dict[str, object]:
    now_ms = int(datetime.now().timestamp() * 1000)
    job_id = str(existing.get("id")) if existing else str(uuid.uuid4())
    created_at_ms = int(existing.get("createdAtMs", now_ms)) if existing else now_ms
    state = existing.get("state") if existing and isinstance(existing.get("state"), dict) else {}
    enabled = bool(existing.get("enabled")) if existing and isinstance(existing.get("enabled"), bool) else True

    return {
        "id": job_id,
        "name": JOB_NAME,
        "description": JOB_DESCRIPTION,
        "enabled": enabled,
        "createdAtMs": created_at_ms,
        "updatedAtMs": now_ms,
        "schedule": {
            "kind": "cron",
            "expr": JOB_EXPR,
            "tz": JOB_TZ,
            "staggerMs": 0,
        },
        "sessionTarget": "isolated",
        "wakeMode": "next-heartbeat",
        "payload": {
            "kind": "agentTurn",
            "message": message,
            "lightContext": True,
            "timeoutSeconds": 600,
        },
        "delivery": {
            "mode": "announce",
            "channel": "discord",
            "to": f"channel:{DISCORD_CHANNEL_ID}",
            "accountId": "default",
        },
        "state": state,
    }


def sync_job() -> str:
    paths = Paths(
        prompt_path=APP_DIR / "openclaw_cron_prompt.txt",
        batch_path=APP_DIR / "runtime" / "openclaw" / "signal_batch.json",
        ingest_script_path=APP_DIR / "openclaw_signal_ingest.py",
        posts_json_path=APP_DIR / "runtime" / "openclaw" / "x_posts.json",
        posts_text_path=APP_DIR / "runtime" / "openclaw" / "x_posts.txt",
        fetch_script_path=APP_DIR / "openclaw_x_text_fetch.py",
    )
    message = load_prompt(paths)
    jobs_payload = read_jobs()
    jobs = jobs_payload.get("jobs")
    if not isinstance(jobs, list):
        raise ValueError("jobs.json の jobs が配列ではありません。")

    existing = next((job for job in jobs if isinstance(job, dict) and job.get("name") == JOB_NAME), None)
    updated_job = build_job(existing, message)

    filtered = [job for job in jobs if not (isinstance(job, dict) and job.get("name") == JOB_NAME)]
    filtered.append(updated_job)
    jobs_payload["version"] = 1
    jobs_payload["jobs"] = filtered

    temp_path = APP_DIR / "runtime" / "cron_jobs.updated.json"
    temp_path.parent.mkdir(parents=True, exist_ok=True)

    run_wsl(f"systemctl --user stop {GATEWAY_SERVICE}")
    try:
        write_jobs(jobs_payload, temp_path)
    finally:
        run_wsl(f"systemctl --user start {GATEWAY_SERVICE}")

    return updated_job["id"]


def main() -> int:
    job_id = sync_job()
    print(job_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
