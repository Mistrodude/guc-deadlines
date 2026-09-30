# GUC deadlines

A robot that logs into cms.guc.edu.eg from your laptop twice a day, reads every course page, and puts every quiz, assignment, exam and cancelled lecture on a calendar website and on your phone's calendar.

## Where your login lives

Only on your laptop, in the operating system's password vault (Windows Credential Manager / macOS Keychain). It is never written to a file, never committed, and never sent anywhere except to CMS itself.

The public repo only ever receives three files: `docs/index.html`, `docs/events.json` (deadlines and course names) and `docs/calendar.ics`. `run.py` refuses to publish if anything else unexpected is tracked.

## Setup (once, about 10 minutes)

You need Python 3.10+ and Git installed, and your laptop signed in to GitHub (push to any repo once so Git remembers you).

1. Create a **public** GitHub repo called `guc-deadlines` and clone it to your laptop.
2. Copy everything from this folder into it, then in that folder:
   ```bash
   pip install -r requirements.txt
   python scraper/scan.py --save-login
   ```
   It asks for your GUC username and password (hidden while you type), plus an optional Anthropic API key, then tests the login. On a Mac, click "Always Allow" if Keychain asks.
3. First run, which also publishes the site:
   ```bash
   git add .
   git commit -m "setup"
   git push
   python run.py
   ```
4. Turn on the website: repo → Settings → Pages → Deploy from a branch → `main`, folder `/docs` → Save. After a minute it's live at `https://YOUR-GITHUB-NAME.github.io/guc-deadlines/`.
5. Make it automatic:
   - Windows: right-click `schedule-windows.ps1` → Run with PowerShell
   - Mac: `bash schedule-mac.sh`

   It runs at 8:00 and 18:00. If your laptop was off or asleep then, it runs as soon as it's back on.
6. Open the site on your phone and tap **Add to Google Calendar** or **Add to Apple Calendar**.

## When your GUC password changes

```bash
python scraper/scan.py --save-login
```

If you forget, the scan fails, nothing gets overwritten, and the website shows "hasn't scanned in over a day". The reason is in `data/last_run.log`.

## How accurate is it?

- Numeric dates are read the Egyptian way (12/10 = 12 October). If an announcement says "Monday" and the date isn't a Monday, the event is marked **Double-check** and tells you why.
- Things like "lectures 1-3" or "2.5 marks" are not mistaken for dates.
- With an Anthropic API key saved, every announcement is read twice: by the rules and by Claude Haiku. Events both agree on are confirmed; anything only one found is marked **Double-check**. Text is only re-sent when it changes (costs cents per month).
- Every event keeps the exact sentence it came from and a link back to CMS.
- If a professor edits an announcement and a future date disappears, it disappears from your calendar at the next scan. Past events are kept as history.

## Testing without logging in

Save CMS pages (Ctrl+S, "Webpage, HTML only") as `home.html` and `course_<id>.html` in a folder, then run `CMS_OFFLINE_DIR=./saved python scraper/scan.py`.
