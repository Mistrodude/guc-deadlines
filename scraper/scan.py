"""
GUC CMS deadline robot.

Logs into cms.guc.edu.eg, reads every active course page, pulls every date out of
announcements (quizzes, assignments, exams, cancellations...), and writes:
  docs/events.json   -> read by the calendar website
  docs/calendar.ics  -> subscribe to it from Google / Apple Calendar

Your login is stored in your computer's own password vault (Windows Credential
Manager / macOS Keychain) via the `keyring` library. It is never written to a file
and never leaves this computer except to log into CMS.

Usage:
  python scraper/scan.py --save-login   store / update your GUC login (run once, and when it changes)
  python scraper/scan.py --check        only test the login
  python scraper/scan.py                full scan (run.py does this + pushes the website)

Testing only:
  CMS_OFFLINE_DIR   folder of saved pages instead of logging in
  TODAY             YYYY-MM-DD override
"""
import datetime as dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
DATA = ROOT / "data"
BASE = "https://cms.guc.edu.eg"
HOME_URL = BASE + "/apps/student/HomePageStn.aspx"
COURSE_URL = BASE + "/apps/student/CourseViewStn.aspx?id={id}&sid={sid}"
CAIRO = ZoneInfo("Africa/Cairo")
AI_MODEL = "claude-haiku-4-5-20251001"
VAULT = "guc-deadlines"


def secret(name):
    """Environment variable first (testing), then the OS password vault."""
    if os.environ.get(name):
        return os.environ[name]
    try:
        import keyring
        return keyring.get_password(VAULT, name)
    except Exception:
        return None


def save_login():
    import getpass
    import keyring
    print("Stored only in this computer's password vault. Nothing is written to the repo.")
    user = input("GUC username (e.g. firstname.lastname, no @student.guc.edu.eg): ").strip().split("@")[0]
    pwd = getpass.getpass("GUC password (hidden while typing): ")
    keyring.set_password(VAULT, "GUC_USERNAME", user)
    keyring.set_password(VAULT, "GUC_PASSWORD", pwd)
    key = getpass.getpass("Anthropic API key for the AI cross-check (optional, Enter to skip): ").strip()
    if key:
        keyring.set_password(VAULT, "ANTHROPIC_API_KEY", key)
    print("Saved.")


def today():
    if os.environ.get("TODAY"):
        return dt.date.fromisoformat(os.environ["TODAY"])
    return dt.datetime.now(CAIRO).date()


# --------------------------------------------------------------------------- fetching

class LoginError(Exception):
    pass


class CMS:
    """Fetches pages either live (NTLM login) or from a folder of saved pages."""

    def __init__(self):
        self.offline = os.environ.get("CMS_OFFLINE_DIR")
        if self.offline:
            return
        user = (secret("GUC_USERNAME") or "").strip()
        pwd = secret("GUC_PASSWORD") or ""
        if not user or not pwd:
            raise LoginError("No GUC login saved on this computer. Run: python scraper/scan.py --save-login")
        user = user.split("@")[0]
        from requests_ntlm import HttpNtlmAuth

        self.s = requests.Session()
        self.s.headers["User-Agent"] = "Mozilla/5.0 (GUC deadline robot)"
        # CMS accepts the student UPN; fall back to DOMAIN\user just in case.
        for login in (f"{user}@student.guc.edu.eg", f"GUC\\{user}"):
            self.s.auth = HttpNtlmAuth(login, pwd)
            r = self.s.get(HOME_URL, timeout=60)
            if r.status_code == 200 and "GridViewcourses" in r.text:
                self.home_html = r.text
                return
        raise LoginError(
            f"CMS login failed (HTTP {r.status_code}). Your password probably changed: "
            "run: python scraper/scan.py --save-login"
        )

    def home(self):
        if self.offline:
            return (Path(self.offline) / "home.html").read_text(encoding="utf-8", errors="ignore")
        return self.home_html

    def course(self, cid, sid):
        if self.offline:
            p = Path(self.offline) / f"course_{cid}.html"
            return p.read_text(encoding="utf-8", errors="ignore") if p.exists() else None
        r = self.s.get(COURSE_URL.format(id=cid, sid=sid), timeout=60)
        r.raise_for_status()
        return r.text


# --------------------------------------------------------------------------- parsing

def parse_home(html):
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id=re.compile(r"GridViewcourses$"))
    if not table:
        raise ValueError("Course table not found on CMS home page (layout changed?)")
    courses = []
    for tr in table.find_all("tr")[1:]:
        tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if len(tds) < 6:
            continue
        full, active, season, cid, sid = tds[1], tds[2], tds[3], tds[4], tds[5]
        m = re.match(r"\(\|(\w+)\|\)\s*(.*?)\s*\(\d+\)\s*$", full)
        code, name = (m.group(1), m.group(2)) if m else (full, full)
        courses.append({
            "id": cid, "sid": sid, "code": code, "name": name,
            "season": season, "active": active.lower() == "active",
            "url": COURSE_URL.format(id=cid, sid=sid),
        })
    return courses


def text_units(el):
    """Split an HTML block into short text units (list items / paragraphs / sentences)."""
    if el is None:
        return []
    for br in el.find_all("br"):
        br.replace_with("\n")
    blocks = []
    leaves = el.find_all(["li", "p", "div", "td", "h1", "h2", "h3", "h4", "span"])
    leaves = [x for x in leaves if not x.find(["li", "p", "div", "td"])]
    raw = [x.get_text(" ", strip=False) for x in leaves] or [el.get_text("\n")]
    for chunk in raw:
        for line in chunk.split("\n"):
            line = " ".join(line.split())
            if not line:
                continue
            for sent in re.split(r"(?<=[.!?])\s+(?=[A-Z(])", line):
                if sent.strip():
                    blocks.append(sent.strip())
    seen, out = set(), []
    for b in blocks:
        if b not in seen:
            seen.add(b)
            out.append(b)
    return out


def parse_course(html):
    soup = BeautifulSoup(html, "html.parser")
    desc = soup.find(id=re.compile(r"ContentPlaceHoldercontent_desc$"))
    course = {"announcement": text_units(desc), "weeks": []}
    for card in soup.select("div.weeksdata"):
        h = card.find("h2")
        m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", h.get_text() if h else "")
        week = dt.date(*map(int, m.groups())).isoformat() if m else None
        ann, descr = [], []
        for block in card.find_all("div"):
            label = block.find("strong", recursive=False) or block.find("div", recursive=False)
            p = block.find("p", recursive=False)
            if not p or not label:
                continue
            lt = label.get_text(strip=True).lower()
            if lt.startswith("announcement"):
                ann += text_units(p)
            elif lt.startswith("description"):
                descr += text_units(p)
        items = []
        for d in card.find_all("div", id=re.compile(r"^content\d+$")):
            strong = d.find("strong")
            title = strong.get_text(" ", strip=True) if strong else d.get_text(" ", strip=True)
            title = re.sub(r"^\d+\s*-\s*", "", title)
            tm = re.search(r"\(([^()]*)\)\s*$", d.get_text(" ", strip=True))
            link = d.find_parent("div", class_="card-body")
            a = link.find("a", href=True) if link else None
            items.append({
                "id": d["id"][7:], "title": title,
                "type": (tm.group(1).strip() if tm else ""),
                "url": a["href"] if a else None,
            })
        course["weeks"].append({"week": week, "announcement": ann, "description": descr, "items": items})
    return course


# --------------------------------------------------------------------------- date reading (rules)

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4,
    "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9,
    "september": 9, "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
MON = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
WD = r"(?:(mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)(?:day|sday|nesday|rsday|urday)?\.?,?\s+(?:the\s+)?)?"

RX_DMY_TEXT = re.compile(WD + r"(\d{1,2})(?:st|nd|rd|th)?(?:\s+of)?\s+" + MON + r"\b\.?(?:,?\s+(\d{4}))?", re.I)
RX_MDY_TEXT = re.compile(WD + MON + r"\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s+(\d{4}))?", re.I)
RX_NUM = re.compile(WD + r"(?<![\w.])(\d{1,2})([/.\-])(\d{1,2})(?:\3(\d{4}|\d{2}))?(?![\d/])", re.I)
RX_TIME = re.compile(r"(?<![\d:])(\d{1,2})(?:[:.](\d{2}))?\s*(a\.?m\.?|p\.?m\.?)|(?<![\d.:])(\d{1,2}):(\d{2})(?!\d)", re.I)
RX_ROOM = re.compile(r"\b([A-Z]\d{1,2}\.\d{2,3}|[A-Z]\d{1,2}\s?-\s?\d{3})\b")
RX_CUE = re.compile(r"\b(on|by|until|till|due|before|deadline|date|at)\s*:?\s*$", re.I)


def weekday_index(word):
    if not word:
        return None
    w = word.lower()[:3]
    return {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}[w]


def infer_year(month, day, ref):
    for y in (ref.year, ref.year + 1, ref.year - 1):
        try:
            d = dt.date(y, month, day)
        except ValueError:
            continue
        if -150 <= (d - ref).days <= 240:
            return d
    try:
        return dt.date(ref.year, month, day)
    except ValueError:
        return None


def find_dates(text, ref):
    """Return [(date, start, end, flags)] found in text. Numeric dates are DAY/MONTH (Egypt)."""
    found = []

    def add(d, m, flags):
        if d and not any(abs(m.start() - s) < 3 for _, s, _, _ in found):
            found.append((d, m.start(), m.end(), flags))

    for m in RX_DMY_TEXT.finditer(text):
        wd, day, mon, yr = m.group(1), int(m.group(2)), MONTHS[m.group(3).lower()[:3]], m.group(4)
        d = _mk(int(yr), mon, day) if yr else infer_year(mon, day, ref)
        add(d, m, _wd_flags(d, wd))
    for m in RX_MDY_TEXT.finditer(text):
        wd, mon, day, yr = m.group(1), MONTHS[m.group(2).lower()[:3]], int(m.group(3)), m.group(4)
        d = _mk(int(yr), mon, day) if yr else infer_year(mon, day, ref)
        add(d, m, _wd_flags(d, wd))
    for m in RX_NUM.finditer(text):
        wd, a, sep, b, yr = m.group(1), int(m.group(2)), m.group(3), int(m.group(4)), m.group(5)
        if not yr and (sep != "/" or not (wd or RX_CUE.search(text[max(0, m.start() - 12):m.start()]))):
            continue  # "Lecture 1-3", "2.5 marks", "1/2" are not dates
        if yr:
            y = int(yr) + (2000 if len(yr) == 2 else 0)
            dmy, mdy = _mk(y, b, a), _mk(y, a, b)
        else:
            dmy, mdy = infer_year(b, a, ref) if b <= 12 else None, infer_year(a, b, ref) if a <= 12 else None
        d, flags = dmy, []
        want = weekday_index(wd)
        if want is not None and dmy and dmy.weekday() != want:
            if mdy and mdy.weekday() == want:
                d, flags = mdy, ["read as month/day because the weekday only matches that way"]
            else:
                flags = [f"says {DAYS[want].title()} but {dmy.strftime('%d %b')} is a {dmy.strftime('%A')}"]
        elif dmy is None and mdy:
            d = mdy
        add(d, m, flags)
    return sorted(found, key=lambda x: x[1])


def _mk(y, m, d):
    try:
        return dt.date(y, m, d)
    except ValueError:
        return None


def _wd_flags(d, wd):
    want = weekday_index(wd)
    if d and want is not None and d.weekday() != want:
        return [f"says {DAYS[want].title()} but {d.strftime('%d %b')} is a {d.strftime('%A')}"]
    return []


def find_time(text, start):
    """First clock time at/after position `start` in the sentence."""
    for m in RX_TIME.finditer(text, start):
        if m.group(3):
            h, mi = int(m.group(1)), int(m.group(2) or 0)
            pm = m.group(3).lower().startswith("p")
            if h == 12:
                h = 0
            h += 12 if pm else 0
        else:
            h, mi = int(m.group(4)), int(m.group(5))
            if 1 <= h <= 7:  # "3:45" at GUC means afternoon
                h += 12
        if 0 <= h < 24 and 0 <= mi < 60:
            return f"{h:02d}:{mi:02d}"
    return None


KIND_RULES = [
    ("cancelled", r"\b(no (lecture|lectures|tutorial|tutorials|lab|labs|class|classes)|cancel+ed|postponed)\b"),
    ("exam", r"\b(mid-?term|final exam|finals?\b|exam)\b"),
    ("quiz", r"\bquiz(zes)?\b"),
    ("assignment", r"\b(assignment|homework|hw\s*\d|problem set)\b"),
    ("project", r"\b(project|milestone|report|deliverable|presentation|demo)\b"),
    ("lab", r"\b(lab|experiment)\b"),
    ("deadline", r"\b(due|deadline|submit|submission)\b"),
]
LABEL_RX = re.compile(r"\b(quiz|assignment|project|milestone|mid-?term|lab|report|sheet|homework|hw|deliverable|presentation|exam)\s*#?\s*(\d+)\b", re.I)


def classify(sentence):
    s = sentence.lower()
    for kind, rx in KIND_RULES:
        if re.search(rx, s):
            return kind
    return "info"


def make_title(kind, sentence):
    if kind == "cancelled":
        m = re.search(KIND_RULES[0][1], sentence, re.I)
        return (m.group(0)[0].upper() + m.group(0)[1:]) if m else "Cancelled"
    m = LABEL_RX.search(sentence)
    if m:
        label = f"{m.group(1).title()} {m.group(2)}"
        dueish = re.search(r"\b(due|deadline|submi)", sentence, re.I)
        return label + (" due" if kind in ("assignment", "project", "deadline") and dueish else "")
    low = sentence.lower()
    if kind == "exam":
        return "Midterm exam" if re.search(r"mid-?term", low) else "Final exam" if "final" in low else "Exam"
    if kind != "info":
        return {"exam": "Exam", "quiz": "Quiz", "assignment": "Assignment due", "project": "Project deadline",
                "lab": "Lab", "deadline": "Deadline"}[kind]
    short = re.sub(r"\s+", " ", sentence)
    return short[:70] + ("…" if len(short) > 70 else "")


def rule_events(units, ref, source):
    out = []
    for sent in units:
        hits = find_dates(sent, ref)
        for i, (d, s, e, flags) in enumerate(hits):
            seg_end = hits[i + 1][1] if i + 1 < len(hits) else len(sent)
            kind = classify(sent)
            out.append({
                "date": d.isoformat(), "time": find_time(sent[:seg_end], s),
                "kind": kind, "title": make_title(kind, sent),
                "where": ", ".join(dict.fromkeys(RX_ROOM.findall(sent))) or None,
                "text": sent, "source": source, "flags": flags, "by": ["rules"],
            })
    return out


# --------------------------------------------------------------------------- date reading (AI cross-check)

AI_PROMPT = """You extract calendar events from a GUC (German University in Cairo) course page.
Today is {today}. Semester: {season}. Course: {course}.
Numeric dates are DAY/MONTH/YEAR (Egyptian format): 12/10/2026 means 12 October 2026.
Return ONLY a JSON array (no prose, no code fences). One object per dated event:
{{"date":"YYYY-MM-DD","time":"HH:MM" or null (24h, Cairo time),"kind":one of
["quiz","exam","assignment","project","lab","deadline","cancelled","info"],
"title":"short, e.g. Quiz 1 or Assignment 2 due","where":rooms or null,
"quote":"the exact sentence the date came from"}}
Resolve relative dates ("next Monday") against the date the text was posted if given, else today.
Skip dates that are only lecture material ranges or past-semester references. If none, return [].

TEXT:
{text}"""


def ai_events(course, text_blocks, ref, cache):
    key = secret("ANTHROPIC_API_KEY")
    if not key or not text_blocks:
        return None
    text = "\n".join(text_blocks)
    h = hashlib.sha256((AI_MODEL + course["code"] + text).encode()).hexdigest()[:20]
    if h in cache:
        return cache[h]
    prompt = AI_PROMPT.format(today=ref.isoformat(), season=course["season"],
                              course=f'{course["code"]} {course["name"]}', text=text[:12000])
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages", timeout=90,
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": AI_MODEL, "max_tokens": 2000, "messages": [{"role": "user", "content": prompt}]},
        )
        r.raise_for_status()
        raw = "".join(b.get("text", "") for b in r.json()["content"])
        raw = re.sub(r"^```(?:json)?|```$", "", raw.strip()).strip()
        data = json.loads(raw)
        good = []
        for ev in data:
            try:
                dt.date.fromisoformat(ev["date"])
                good.append(ev)
            except Exception:
                continue
        cache[h] = good
        return good
    except Exception as e:
        print(f"  ! AI check skipped for {course['code']}: {e}")
        return None


def merge(rule_evs, ai_evs):
    """Rules and AI must agree for an event to count as confirmed."""
    if ai_evs is None:
        return rule_evs
    out = [dict(e) for e in rule_evs]
    for a in ai_evs:
        match = next((e for e in out if e["date"] == a["date"] and
                      (e["kind"] == a.get("kind") or e["title"].split()[0].lower() in a.get("title", "").lower())), None)
        if match:
            match["by"] = ["rules", "ai"]
            if not match["time"] and a.get("time"):
                match["time"] = a["time"]
            if not match["where"] and a.get("where"):
                match["where"] = a["where"]
        else:
            out.append({
                "date": a["date"], "time": a.get("time"), "kind": a.get("kind", "info"),
                "title": a.get("title") or "Event", "where": a.get("where"),
                "text": a.get("quote", ""), "source": "announcement", "flags": [], "by": ["ai"],
            })
    for e in out:
        if e["by"] == ["rules"]:
            e["flags"] = e["flags"] + ["only the rule reader found this"]
        elif e["by"] == ["ai"]:
            e["flags"] = e["flags"] + ["only the AI reader found this"]
    return out


# --------------------------------------------------------------------------- output

def ev_id(course_code, e):
    base = f'{course_code}|{e["kind"]}|{e["title"].lower()}|{e["date"]}'
    return hashlib.sha1(base.encode()).hexdigest()[:12]


def ics_escape(s):
    return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line):
    b = line.encode("utf-8")
    if len(b) <= 74:
        return line
    parts, cur = [], b""
    for ch in line:
        cb = ch.encode("utf-8")
        if len(cur) + len(cb) > (74 if not parts else 73):
            parts.append(cur.decode("utf-8"))
            cur = b""
        cur += cb
    parts.append(cur.decode("utf-8"))
    return "\r\n ".join(parts)


def write_ics(events, path, stamp):
    L = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//guc-deadlines//EN", "CALSCALE:GREGORIAN",
         "METHOD:PUBLISH", "X-WR-CALNAME:GUC deadlines", "X-WR-TIMEZONE:Africa/Cairo",
         "REFRESH-INTERVAL;VALUE=DURATION:PT6H", "X-PUBLISHED-TTL:PT6H"]
    for e in events:
        d = dt.date.fromisoformat(e["date"])
        L += ["BEGIN:VEVENT", f"UID:{e['id']}@guc-deadlines", f"DTSTAMP:{stamp}"]
        if e["time"]:
            h, m = map(int, e["time"].split(":"))
            start = dt.datetime(d.year, d.month, d.day, h, m, tzinfo=CAIRO).astimezone(dt.timezone.utc)
            dur = {"quiz": 60, "exam": 120, "lab": 120}.get(e["kind"], 30)
            L += [f"DTSTART:{start:%Y%m%dT%H%M%SZ}", f"DURATION:PT{dur}M"]
        else:
            L += [f"DTSTART;VALUE=DATE:{d:%Y%m%d}", f"DTEND;VALUE=DATE:{d + dt.timedelta(days=1):%Y%m%d}"]
        prefix = "⚠ " if e["flags"] and e["by"] != ["rules", "ai"] and e.get("flags_serious") else ""
        L.append(f"SUMMARY:{ics_escape(prefix + '[' + e['course'] + '] ' + e['title'])}")
        desc = e["text"] + ("\n\nCheck: " + "; ".join(e["flags"]) if e["flags"] else "") + "\n\n" + e["url"]
        L.append(f"DESCRIPTION:{ics_escape(desc)}")
        if e["where"]:
            L.append(f"LOCATION:{ics_escape(e['where'])}")
        L.append(f"URL:{e['url']}")
        if e["kind"] in ("quiz", "exam", "assignment", "project", "deadline"):
            L += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{ics_escape(e['title'])}", "TRIGGER:-P1D", "END:VALARM"]
        if e["kind"] == "cancelled":
            L.append("TRANSP:TRANSPARENT")
        L.append("END:VEVENT")
    L.append("END:VCALENDAR")
    path.write_text("\r\n".join(fold(x) for x in L) + "\r\n", encoding="utf-8")


def load_json(p, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


# --------------------------------------------------------------------------- main

def main():
    ref = today()
    if "--save-login" in sys.argv:
        save_login()
        sys.argv.append("--check")
    try:
        cms = CMS()
    except LoginError as e:
        print("LOGIN FAILED:", e)
        sys.exit(2)
    if "--check" in sys.argv:
        print("Login OK. Courses:", ", ".join(c["code"] for c in parse_home(cms.home())))
        return

    DOCS.mkdir(exist_ok=True)
    DATA.mkdir(exist_ok=True)
    prev = load_json(DOCS / "events.json", {"events": [], "items": []})
    prev_events = {e["id"]: e for e in prev.get("events", [])}
    prev_items = {i["id"]: i for i in prev.get("items", [])}
    ai_cache = load_json(DATA / "ai_cache.json", {})
    now_iso = dt.datetime.now(CAIRO).isoformat(timespec="minutes")

    courses = [c for c in parse_home(cms.home()) if c["active"]]
    if not courses:
        print("No active courses found; not touching existing data.")
        sys.exit(3)

    events, items, failed = [], [], []
    for c in courses:
        print(f"- {c['code']} {c['name']}")
        try:
            html = cms.course(c["id"], c["sid"])
        except Exception as e:
            print("  ! could not load:", e)
            html = None
        if not html:
            failed.append(c["code"])
            events += [e for e in prev_events.values() if e["course"] == c["code"]]
            items += [i for i in prev_items.values() if i["course"] == c["code"]]
            continue
        page = parse_course(html)
        evs = rule_events(page["announcement"], ref, "Course announcement")
        blocks = ["[Course announcement]"] + page["announcement"]
        for w in page["weeks"]:
            wref = dt.date.fromisoformat(w["week"]) if w["week"] else ref
            units = w["announcement"] + [u for u in w["description"] if not re.fullmatch(r"week\s*\d+", u, re.I)]
            if units:
                evs += rule_events(units, max(wref, ref - dt.timedelta(days=60)), f"Week of {w['week']}")
                blocks += [f"[Week of {w['week']}]"] + units
            for it in w["items"]:
                iid = f"{c['code']}-{it['id']}"
                items.append({
                    "id": iid, "course": c["code"], "title": it["title"], "type": it["type"],
                    "week": w["week"], "url": it["url"], "course_url": c["url"],
                    "first_seen": prev_items.get(iid, {}).get("first_seen", now_iso),
                })
        evs = merge(evs, ai_events(c, blocks, ref, ai_cache))
        seen = set()
        for e in evs:
            e.update(course=c["code"], course_name=c["name"], url=c["url"])
            e["id"] = ev_id(c["code"], e)
            if e["id"] in seen:
                continue
            seen.add(e["id"])
            e["first_seen"] = prev_events.get(e["id"], {}).get("first_seen", now_iso)
            e["flags_serious"] = any(f.startswith("says ") for f in e["flags"])
            events.append(e)
        print(f"  {len(seen)} dated events, {sum(len(w['items']) for w in page['weeks'])} files")

    # Keep history: past events that disappeared from CMS (announcement edited/cleared) stay.
    ids = {e["id"] for e in events}
    for e in prev_events.values():
        if e["id"] not in ids and e["date"] < ref.isoformat() and e["course"] not in failed:
            events.append(e)

    events.sort(key=lambda e: (e["date"], e["time"] or "99:99", e["course"]))
    items.sort(key=lambda i: (i["week"] or "", i["first_seen"]), reverse=True)
    out = {
        "generated_at": now_iso, "first_scan": prev.get("first_scan", now_iso), "today": ref.isoformat(),
        "season": courses[0]["season"], "failed_courses": failed,
        "ai_check": bool(secret("ANTHROPIC_API_KEY")),
        "courses": [{k: c[k] for k in ("code", "name", "url")} for c in courses],
        "events": events, "items": items[:400],
    }
    (DOCS / "events.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    (DATA / "ai_cache.json").write_text(json.dumps(ai_cache, ensure_ascii=False), encoding="utf-8")
    write_ics(events, DOCS / "calendar.ics", dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    print(f"Done: {len(events)} events, {len(items)} files. Failed: {failed or 'none'}")
    if failed and len(failed) == len(courses):
        sys.exit(4)


if __name__ == "__main__":
    main()
