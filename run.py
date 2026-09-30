"""
Scan CMS, then publish ONLY the website files to GitHub.
This is what the scheduled task runs. Log: data/last_run.log
"""
import datetime as dt
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PUBLISH = ["docs/index.html", "docs/events.json", "docs/calendar.ics"]
LOG = ROOT / "data" / "last_run.log"


def log(msg):
    LOG.parent.mkdir(exist_ok=True)
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M}  {msg}"
    print(line)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def git(*args, check=True):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=check)


def main():
    scan = subprocess.run([sys.executable, "scraper/scan.py"], cwd=ROOT, capture_output=True, text=True)
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "scan_output.log").write_text(scan.stdout + scan.stderr, encoding="utf-8")
    if scan.returncode != 0:
        log("SCAN FAILED: " + (scan.stdout.strip().splitlines() or ["see data/scan_output.log"])[-1])
        sys.exit(1)

    # Safety net: never publish anything except the three website files.
    tracked = git("ls-files").stdout.split()
    bad = [t for t in tracked if t not in PUBLISH and not t.startswith(("scraper/", "README", "requirements", "run.", "schedule", ".gitignore"))]
    if bad:
        log(f"STOPPED: unexpected files are tracked by git: {bad}. Remove them before publishing.")
        sys.exit(2)

    git("add", *PUBLISH)
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        log("Scan OK, nothing changed.")
        return
    git("commit", "-m", f"CMS scan {dt.datetime.now():%Y-%m-%d %H:%M}")
    push = git("push", check=False)
    if push.returncode != 0:
        log("SCAN OK but PUSH FAILED: " + push.stderr.strip()[-300:])
        sys.exit(3)
    log("Scan OK, website updated.")


if __name__ == "__main__":
    main()
