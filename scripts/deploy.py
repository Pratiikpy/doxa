"""Push files to the live VM and restart the service.

There is no SSH key on this machine for the host, so deployment goes through the Azure agent
(`az vm run-command invoke`), which runs a shell script as root on the VM. Files travel base64-encoded
inside that script so no quoting or newline survives to corrupt them, and each one is checked against
its SHA-256 on arrival — a half-written module would take the service down on restart.

Run: python scripts/deploy.py [path ...]      (defaults to the files the proof deck needs)
"""
from __future__ import annotations

import base64
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RG, VM = "REACH-RG", "reach-vm"
# `az` is a .cmd shim on Windows; subprocess without a shell will not find it by bare name.
AZ = shutil.which("az") or "az"
REMOTE = "/home/azureuser/doxa"

DEFAULT = ["server.py", "proof.py", "checks/page_html.py", "nodes/content_nodes.py"]


def run(script: str, timeout: int = 900) -> str:
    """Pass the script via ``@file``.

    Base64 of a source file inline blows past Windows' 32k command-line limit, and the failure is an
    opaque WinError 206 rather than anything about the deploy.
    """
    tmp = ROOT / ".deploy-script.sh"
    tmp.write_text(script, encoding="utf-8", newline="\n")
    try:
        out = subprocess.run(
            [AZ, "vm", "run-command", "invoke", "-g", RG, "-n", VM,
             "--command-id", "RunShellScript", "--scripts", f"@{tmp}",
             "--query", "value[0].message", "-o", "tsv"],
            capture_output=True, text=True, timeout=timeout)
    finally:
        tmp.unlink(missing_ok=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[:600])
    return out.stdout


# One run-command payload will not carry an arbitrarily large file, and the failure is silent — the
# script simply produces no output. Base64 is appended in chunks and decoded once it is all there.
CHUNK = 48_000


def push(paths: list[str]) -> None:
    for rel in paths:
        local = ROOT / rel
        raw = local.read_bytes()
        b64 = base64.b64encode(raw).decode()
        digest = hashlib.sha256(raw).hexdigest()

        parts = [b64[i:i + CHUNK] for i in range(0, len(b64), CHUNK)]
        for n, part in enumerate(parts):
            redirect = ">" if n == 0 else ">>"
            out = run("set -e\n"
                      f"mkdir -p {REMOTE}/{Path(rel).parent.as_posix()}\n"
                      f"printf '%s' '{part}' {redirect} {REMOTE}/{rel}.b64\n"
                      f'echo "chunk {n + 1}/{len(parts)}"')
            if f"chunk {n + 1}/" not in out:
                raise RuntimeError(f"{rel} chunk {n + 1} did not land: {out.strip()[:200]}")

        lines = ["set -e",
                 f"base64 -d < {REMOTE}/{rel}.b64 > {REMOTE}/{rel}.new",
                 f"rm -f {REMOTE}/{rel}.b64"]
        lines += [
            f'got=$(sha256sum {REMOTE}/{rel}.new | cut -d" " -f1)',
            f'if [ "$got" != "{digest}" ]; then echo "CHECKSUM MISMATCH {rel}"; exit 1; fi',
            # Move only after the checksum matches, so a truncated transfer never becomes the file
            # the service imports on its next restart.
            f"mv {REMOTE}/{rel}.new {REMOTE}/{rel}",
            f"chown azureuser:azureuser {REMOTE}/{rel}",
            f'echo "ok {rel} {len(raw)} bytes"',
        ]
        result = run("\n".join(lines))
        if "ok " not in result:
            raise RuntimeError(f"{rel} did not land: {result.strip()[:300]}")
        print(f"  {rel}  {len(raw):,} bytes  {digest[:12]}")


def main() -> int:
    paths = sys.argv[1:] or DEFAULT
    missing = [p for p in paths if not (ROOT / p).exists()]
    if missing:
        print("not found:", ", ".join(missing))
        return 1

    push(paths)
    print(run("systemctl restart doxa.service && sleep 4 && "
              "systemctl is-active doxa.service && "
              "curl -s -o /dev/null -w 'local %{http_code}\\n' http://127.0.0.1:8792/health").strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
