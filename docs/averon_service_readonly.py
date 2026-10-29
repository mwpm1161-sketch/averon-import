"""Read-only Linux service inventory; run via SSH stdin, not on Windows.

Never starts/stops a unit, writes files, imports Averon, reads environment files,
or prints raw command lines. Unknown command arguments are redacted.
Disk Git HEAD is deliberately not asserted to attest already-loaded Python code.
"""
from pathlib import Path
import hashlib
import json
import re
import shlex
import subprocess

UNIT = "averon.service"


def read_command(arguments):
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=15, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def safe_path(value):
    return value if isinstance(value, str) and re.fullmatch(r"/(?:[A-Za-z0-9_.@+/-]|\\x[0-9a-fA-F]{2})+", value) else None


def safe_argv(arguments):
    result = []
    index = 0
    while index < len(arguments):
        value = arguments[index]
        if index == 0:
            result.append(safe_path(value) or "[redacted executable]")
        elif value in {"uvicorn", "averon_import.main:app", "averon_import.main"}:
            result.append(value)
        elif value in {"--proxy-headers", "--no-proxy-headers", "--factory"}:
            result.append(value)
        elif value in {"--host", "--port", "--workers", "--app-dir", "-m"} and index + 1 < len(arguments):
            following = arguments[index + 1]
            allowed = (
                (value == "--host" and following in {"127.0.0.1", "::1", "0.0.0.0"})
                or (value in {"--port", "--workers"} and following.isascii() and following.isdigit())
                or (value == "--app-dir" and safe_path(following) is not None)
                or (value == "-m" and following in {"uvicorn", "averon_import.main"})
            )
            result.extend([value, following if allowed else "[redacted]"])
            index += 1
        else:
            result.append("[redacted]")
        index += 1
    return result


def exec_summary(raw):
    if not raw:
        return None
    executable = re.search(r"(?:^|[ {;])path=([^ ;}]+)", raw)
    argv = re.search(r"argv\[\]=(.*?)(?:\s+;|\s*})", raw)
    try:
        arguments = shlex.split(argv.group(1)) if argv else []
    except ValueError:
        arguments = []
    return {"executable": safe_path(executable.group(1)) if executable else None,
            "argv_safe": safe_argv(arguments), "raw_sha256": hashlib.sha256(raw.encode()).hexdigest()}


def git_inventory(path):
    if path is None:
        return None
    root = read_command(["git", "-C", path, "rev-parse", "--show-toplevel"])
    sha = read_command(["git", "-C", path, "rev-parse", "HEAD"])
    if sha is None or not re.fullmatch(r"[0-9a-f]{40}", sha):
        return {"directory": path, "git_available": False}
    clean = read_command(["git", "--no-optional-locks", "-C", path, "status", "--porcelain", "--untracked-files=no"])
    return {"directory": path, "git_root": safe_path(root), "disk_checkout_sha": sha,
            "tracked_tree_clean": clean == "" if clean is not None else None}


def inventory():
    values = {key: read_command(["systemctl", "show", UNIT, "--no-pager", "--property=" + key, "--value"])
              for key in ("MainPID", "ActiveState", "SubState", "WorkingDirectory", "ExecStart",
                          "FragmentPath", "DropInPaths", "ExecMainStartTimestamp", "InvocationID")}
    pid = int(values["MainPID"]) if values["MainPID"] and values["MainPID"].isdigit() else 0
    cwd = executable = process_argv = None
    if pid:
        cwd = safe_path(str(Path(f"/proc/{pid}/cwd").resolve(strict=True)))
        executable = safe_path(str(Path(f"/proc/{pid}/exe").resolve(strict=True)))
        process_argv = safe_argv(Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8").strip("\0").split("\0"))
    dropins = []
    for name in shlex.split(values["DropInPaths"] or ""):
        path = safe_path(name)
        if path is None:
            dropins.append({"path": None, "status": "unverified_path"})
            continue
        raw = Path(path).read_bytes()
        # Hash the full file but output only path and hash, never Environment directives.
        dropins.append({"path": path, "sha256": hashlib.sha256(raw).hexdigest()})
    directories = {p for p in (cwd, safe_path(values["WorkingDirectory"])) if p is not None}
    for argument_list in (process_argv or [], (exec_summary(values["ExecStart"]) or {}).get("argv_safe", [])):
        for i, value in enumerate(argument_list[:-1]):
            if value == "--app-dir" and safe_path(argument_list[i + 1]):
                directories.add(argument_list[i + 1])
    return {"unit": UNIT, "main_pid": pid, "active_state": values["ActiveState"], "sub_state": values["SubState"],
            "working_directory": safe_path(values["WorkingDirectory"]), "process_cwd": cwd,
            "process_executable": executable, "process_argv_safe": process_argv,
            "exec_start": exec_summary(values["ExecStart"]), "fragment_path": safe_path(values["FragmentPath"]),
            "dropins": dropins, "started_at": values["ExecMainStartTimestamp"], "invocation_id": values["InvocationID"],
            "checkouts": [git_inventory(p) for p in sorted(directories)],
            "running_sha": None, "running_sha_status": "requires_build_record_tied_to_process_invocation"}


if __name__ == "__main__":
    try:
        print(json.dumps(inventory(), ensure_ascii=True))
    except Exception:
        print(json.dumps({"unit": UNIT, "status": "read_only_inventory_failed"}))
        raise SystemExit(1)
