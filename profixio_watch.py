#!/usr/bin/env python3
"""
Bevakar Växjö HF 1 och Växjö HF 2 (P12 Syd) på Profixio och skickar push via ntfy.sh.

Notiser: matchstart, mål/ställning, utvisningar, straffar, halvtid, slutresultat med
målskyttar, ändrad tid/plats. Live-delen kräver att sekretariatet registrerar matchen
live i Profixio; annars kommer slutnotisen när resultatet läggs in.

Matcher hittas automatiskt (se discover/--scan nedan), du behöver inga länkar.

Gratis i molnet: GitHub Actions kör --ci var 10:e minut (se watch.yml)
Server/hemmadator:  python3 profixio_watch.py --loop   (se Dockerfile + fly.toml)
Alternativ, cron varje minut:   * * * * * python3 /sökväg/profixio_watch.py
Felsökning, skickar inget:       python3 profixio_watch.py --debug
Sök upp alla matcher en gång:    python3 profixio_watch.py --scan [radie]   (standard 200)
Bara std-bibliotek, Python 3.9+.
"""
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from collections import Counter
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Stockholm")
except Exception:  # t.ex. Windows utan tzdata
    TZ = None

BASE = "https://www.profixio.com"
TEAMS = {  # lagnamn exakt som på Profixio -> lagsida
    "Växjö HF 2": BASE + "/app/lx/competition/leagueid28119/teams/1586443?k=1183422",
    "Växjö HF 1": BASE + "/app/lx/competition/leagueid28119/teams/1586442?k=1183421",
}
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "BYT-MIG-vhf2-x7k2q")   # ditt hemliga ntfy-ämne
SEED_IDS = [32694376, 32694540, 32694553, 32694554]   # kända matcher, startpunkt för sökning
BEFORE, AFTER, LATE = timedelta(minutes=15), timedelta(minutes=120), timedelta(hours=24)
HERE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("STATE_DIR", HERE))
STATE_FILE, SNAP_DIR = DATA / "state.json", DATA / "snapshots"
UA = "Mozilla/5.0 (compatible; profixio-watch/2.0; privat bevakning av två lag)"
DEBUG = "--debug" in sys.argv


# ---------- Tid, nät, notiser ----------
def now():
    return datetime.now(TZ) if TZ else datetime.now()


def parse_dt(s):
    d = datetime.strptime(s, "%Y-%m-%d %H:%M")
    return d.replace(tzinfo=TZ) if TZ else d


def fetch(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=30) as r:
        html = r.read().decode("utf-8", "replace")
    p = Page()
    p.feed(html)
    p.raw = html
    return p


def notify(title, body):
    if DEBUG:
        print(f"[NOTIS] {title}\n{body}\n")
        return
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}", data=body.encode("utf-8"),
        headers={"Title": title.encode("utf-8").decode("latin-1")}, method="POST")
    urllib.request.urlopen(req, timeout=30).read()


# ---------- HTML -> textrader, tabellrader, listor ----------
class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.lines, self.rows, self.lists, self.raw = [], [], [], ""
        self._skip, self._row, self._cell, self._ul, self._li = 0, None, None, [], []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif tag in ("ul", "ol"):
            self._ul.append([])
        elif tag == "li":
            self._li.append([])

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip -= 1
        elif tag in ("td", "th") and self._cell is not None:
            self._row.append(" ".join(self._cell))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None
        elif tag == "li" and self._li:
            txt = " ".join(self._li.pop())
            if self._ul:
                self._ul[-1].append(txt)
        elif tag in ("ul", "ol") and self._ul:
            self.lists.append(self._ul.pop())

    def handle_data(self, data):
        t = " ".join(data.split())
        if not t or self._skip:
            return
        self.lines.append(t)
        if self._cell is not None:
            self._cell.append(t)
        for b in self._li:
            b.append(t)


# ---------- Matchsidan ----------
DT = re.compile(r"(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})")
TIME = re.compile(r"^\d{1,2}:\d{2}$")
DAY = re.compile(r"^\d{1,2} [A-Za-z]{3}$")
SCORE = re.compile(r"^(\d+)\s*[-–:]\s*(\d+)$")
PLAYER = re.compile(r"^(?:[A-ZÅÄÖ]{1,3}\s+)?(\d{1,2})\s+(.+?)((?:\s+\d+){0,2})$")


def parse_match(p):
    L = p.lines
    i = next((k for k, t in enumerate(L) if DT.search(t)), None)
    if i is None or i < 3:
        return None
    m = DT.search(L[i])
    away, j, mid = L[i - 1], i - 2, []
    while j >= 0 and (TIME.match(L[j]) or DAY.match(L[j]) or SCORE.match(L[j])):
        mid.append(L[j])
        j -= 1
    home = L[j]
    score = next(((int(s.group(1)), int(s.group(2)))
                  for s in map(SCORE.match, mid) if s), None)
    tabs = " ".join(L[i + 1:i + 10])
    phase = ("live" if re.search(r"Match data\s+Live", tabs)
             else "pre" if "Pre-match" in tabs else "?")
    events = []
    if phase == "live":
        x = next((k for k in range(i, len(L)) if re.search(r"\bLive\b", L[k])), None)
        if x is not None:
            y = x + 1
            if y < len(L) and L[y] == "Lineups":
                y += 1
            z = next((k for k in range(y, len(L)) if L[k] in (home, away)), len(L))
            events = [t for t in L[y:z] if t.lower() != "no events yet"]
    return {"when": f"{m.group(1)} {m.group(2)}", "home": home, "away": away,
            "score": score, "phase": phase, "events": events}


def parse_players(p):
    """[{namn: [mål, utv]}, ...] för upp till två trupper (hemma, borta)."""
    teams = []
    for lst in p.lists:
        pl = {}
        for txt in lst:
            m = PLAYER.match(txt)
            if m:
                name = re.sub(r"\s+RGK$", "", m.group(2)).strip()
                n = [int(x) for x in m.group(3).split()]
                pl[name] = [n[0] if n else 0, n[1] if len(n) > 1 else 0]
        if len(pl) >= 5:
            teams.append(pl)
    return teams[:2]


def ours(c):
    return [n for n in (c["home"], c["away"]) if n in TEAMS]


KINDS = [("Utvisning", r"utvis|suspens|2\s*min"), ("Straff", r"\b7\s*m|straff|penalt"),
         ("Mål", r"\bmål\b|\bgoal\b"), ("Halvtid", r"halvtid|half.?time|paus"),
         ("Slut", r"\bslut\b|full.?time|ended|avslut"), ("Start", r"\bstart")]


def kind(text):
    return next((k for k, rx in KINDS if re.search(rx, text, re.I)), "Händelse")


def new_lines(old, new):
    c, out = Counter(old), []
    for x in new:
        if c[x] > 0:
            c[x] -= 1
        else:
            out.append(x)
    return out


def table_line(c):
    s = f"{c['score'][0]}–{c['score'][1]}" if c["score"] else "–"
    return f"{c['home']} {s} {c['away']}"


def final_text(c, pls):
    out = [table_line(c)]
    if c["score"] and len(pls) == 2:
        for s, name in enumerate((c["home"], c["away"])):
            if name in TEAMS:
                goals = sorted(((n, g[0]) for n, g in pls[s].items() if g[0] > 0),
                               key=lambda x: -x[1])
                if sum(g for _, g in goals) == c["score"][s]:   # kontroll mot slutresultatet
                    out.append(f"Mål {name}: " + ", ".join(f"{n} {g}" for n, g in goals))
                else:
                    out.append(f"({name}: målskyttar kunde inte tolkas säkert)")
        for s, name in enumerate((c["home"], c["away"])):
            u = [f"{n} {g[1]}" for n, g in pls[s].items() if g[1] > 0]
            if u:
                out.append(f"Utvisningar {name}: " + ", ".join(u))
    return "\n".join(out)


def snapshot(mid, p, prev_hash):
    h = hashlib.md5(" ".join(p.lines).encode()).hexdigest()
    if prev_hash == h:
        return h
    SNAP_DIR.mkdir(exist_ok=True)
    (SNAP_DIR / f"{mid}-{now():%Y%m%d-%H%M%S}.html").write_text(p.raw, encoding="utf-8")
    for old in sorted(SNAP_DIR.glob("*.html"))[:-150]:
        old.unlink()
    return h


class Blocked(Exception):
    """Profixio nekar anropet (403/429 eller spärrsida)."""


def not_ours(st, mid):
    if mid not in st["notours"]:
        st["notours"].append(mid)
    st["ids"] = [i for i in st["ids"] if i != mid]
    st["matches"].pop(str(mid), None)


# ---------- Bevaka en match ----------
def poll(mid, st, t):
    try:
        p = fetch(f"{BASE}/app/lx/match/{mid}")
    except Exception as e:
        code = getattr(e, "code", None)
        if code in (404, 410):
            not_ours(st, mid)
        elif code in (401, 403, 429):
            raise Blocked(f"HTTP {code}")
        else:
            print(f"Match {mid}: {e}", file=sys.stderr)
        return
    cur = parse_match(p)
    if not cur and "profixio" not in p.raw.lower():
        raise Blocked("spärrsida (200 utan Profixio-innehåll)")
    if not cur or not ours(cur):
        if mid in SEED_IDS and not cur:
            print(f"Match {mid}: kunde inte tolka sidan", file=sys.stderr)
        else:
            not_ours(st, mid)
        return
    if mid not in st["ids"]:
        st["ids"].append(mid)
    pls = parse_players(p)
    start = parse_dt(cur["when"])
    old = st["matches"].get(str(mid))
    keep = old or {}
    new = {**cur, "players": pls, "last_poll": t.isoformat(),
           "hash": "" if DEBUG else snapshot(mid, p, keep.get("hash")),
           "final": keep.get("final", False),
           "last_change": keep.get("last_change", t.isoformat())}
    if DEBUG:
        print(json.dumps({k: v for k, v in new.items() if k != "hash"},
                         indent=1, ensure_ascii=False))
    who = " & ".join(ours(cur))

    if old is None:                                   # första gången: bara grundläge
        new["final"] = bool(cur["score"]) and t > start + timedelta(minutes=90)
        st["matches"][str(mid)] = new
        return
    if old.get("final"):
        st["matches"][str(mid)] = new
        return

    ev_new = new_lines(old.get("events", []), cur["events"])
    if cur["score"] != old.get("score") or ev_new or pls != old.get("players"):
        new["last_change"] = t.isoformat()
    final = False
    have = len(pls) == 2 and len(old.get("players", [])) == 2

    if old["phase"] == "pre" and cur["phase"] == "live":
        if t - start < timedelta(minutes=20):
            notify(f"{who}: Matchen har startat", table_line(cur))
        else:                                          # resultat inlagt i efterhand
            notify(f"{who}: Resultat", final_text(cur, pls))
            final = True
    elif ev_new:
        notify(f"{who}: {kind(ev_new[0])}", "\n".join(ev_new) + "\n" + table_line(cur))
        final = any(kind(e) == "Slut" for e in ev_new)
    else:
        if cur["score"] and cur["score"] != old.get("score"):
            body = table_line(cur)
            if have:
                for s in (0, 1):
                    for n, g in pls[s].items():
                        if g[0] > old["players"][s].get(n, [0, 0])[0]:
                            body = f"Mål: {n}\n" + body
            notify(f"{who}: Mål", body)
        if have:
            for s in (0, 1):
                for n, g in pls[s].items():
                    if g[1] > old["players"][s].get(n, [0, 0])[1]:
                        notify(f"{who}: Utvisning",
                               f"{n} ({(cur['home'], cur['away'])[s]})\n" + table_line(cur))

    quiet = t - datetime.fromisoformat(new["last_change"])
    if (not final and cur["phase"] == "live" and cur["score"]
            and t >= start + timedelta(minutes=40) and quiet >= timedelta(minutes=10)):
        notify(f"{who}: Slut", final_text(cur, pls))
        final = True
    new["final"] = final
    if final:
        for n in ours(cur):
            st["last_final"][n] = t.isoformat()
    st["matches"][str(mid)] = new


def due(ms, t):
    """'window' = varje körning, annars True/False beroende på hur länge sedan sist."""
    if ms is None:
        return True
    if ms.get("final"):
        return False
    start = parse_dt(ms["when"])
    age = t - datetime.fromisoformat(ms["last_poll"])
    if start - BEFORE <= t <= start + AFTER:
        return "window"
    if start + AFTER < t <= start + LATE:
        return age >= timedelta(minutes=9)
    return age >= timedelta(minutes=60)


# ---------- Hitta matcher ----------
ID_RX = re.compile(r"(?<!\d)(3\d{7})(?!\d)")     # Profixios matchnummer är 8 siffror


def candidates(raw):
    return {int(x) for x in ID_RX.findall(raw) + re.findall(r"/match/(\d+)", raw)}


def scan(st, radius):
    """Engångssökning: läser matchsidorna runt kända ID:n (1 sida/sekund)."""
    seeds = sorted(set(SEED_IDS) | set(st["ids"]))
    todo = sorted({i for s in seeds for i in range(s - radius, s + radius + 1)}
                  - set(st["notours"]) - set(st["ids"]))
    print(f"Söker i {len(todo)} sidor (cirka {len(todo) // 60 + 1} min) ...")
    blocked = 0
    for n, mid in enumerate(todo, 1):
        try:
            poll(mid, st, now())
        except Blocked as e:
            blocked += 1
            if blocked >= 5:
                print(f"Profixio blockerar anropen ({e}). Sökningen avbröts.", file=sys.stderr)
                return False
        except Exception as e:
            print(f"Match {mid}: {e}", file=sys.stderr)
        time.sleep(1)
        if n % 25 == 0:
            save(st)
            print(f"  {n}/{len(todo)}  hittade {len(st['ids'])} matcher för dina lag")
    print(f"Klart: {len(st['ids'])} matcher hittade.")
    return True


# ---------- Lagsidor: schemaändringar, nya matcher, reserv för resultat ----------
def team_check(st, t, name, url):
    ts = st["teams"].setdefault(name, {})
    last = ts.get("checked")
    if last and t - datetime.fromisoformat(last) < timedelta(minutes=9):
        return
    p = fetch(url)
    found = candidates(p.raw) - set(st["notours"]) - set(st["ids"])
    st["ids"] = sorted(set(st["ids"]) | set(sorted(found)[:40]))   # verifieras vid första läsningen
    L = p.lines
    idx = [i for i, x in enumerate(L) if re.match(r"^[A-Z][a-z]{2} \d{1,2} • \d{1,2}:\d{2}$", x)]
    matches = {}
    for n, i in enumerate(idx):
        b = L[i + 1:(idx[n + 1] if n + 1 < len(idx) else i + 12)][:10]
        if len(b) < 2:
            continue
        rnd = next((x for x in b if re.match(r"(Runde|Round|Omg)", x)), "")
        venue = b[b.index("•") + 2] if "•" in b and b.index("•") + 2 < len(b) else ""
        matches.setdefault(f"{rnd}|{b[0]}|{b[1]}", {
            "when": L[i], "venue": venue, "home": b[0], "away": b[1], "round": rnd})
    table = {}
    for row in p.rows:
        if len(row) >= 9 and re.fullmatch(r"\d+", row[0]):
            g = re.fullmatch(r"(\d+)\s*-\s*(\d+)", row[6])
            if g:
                table[name if name in row[1] else row[1]] = {
                    "pos": int(row[0]), "m": int(row[2]), "w": int(row[3]), "u": int(row[4]),
                    "l": int(row[5]), "p": int(row[-1]), "gf": int(g.group(1)), "ga": int(g.group(2))}
    if DEBUG:
        print(f"{name}: {len(matches)} matcher, tabell {len(table)} lag, "
              f"{len(found)} möjliga match-ID i sidans källkod {sorted(found)[:8]}")
    if not matches or not table:
        print(f"{name}: lagsidan gav ingen data", file=sys.stderr)
        return
    old = ts.get("data")
    if old:
        for k, b in matches.items():
            a = old["matches"].get(k)
            if a and (a["when"], a["venue"]) != (b["when"], b["venue"]):
                notify(f"{name}: Ändrad match",
                       f"{b['round']}: {b['home']} – {b['away']}\nVar: {a['when']}, {a['venue']}"
                       f"\nNu: {b['when']}, {b['venue']}")
            elif not a:
                notify(f"{name}: Ny match", f"{b['round']}: {b['home']} – {b['away']}\n"
                                             f"{b['when']}, {b['venue']}")
        o, n = old["table"].get(name), table.get(name)
        lf = st["last_final"].get(name)
        recent = lf and t - datetime.fromisoformat(lf) < timedelta(hours=2)
        if o and n and n["m"] > o["m"] and not recent:
            gf, ga = n["gf"] - o["gf"], n["ga"] - o["ga"]
            res = "Vinst" if gf > ga else "Förlust" if gf < ga else "Oavgjort"
            notify(f"{name}: {res}", f"{gf}–{ga} (tabellen)\nPlats {n['pos']}, {n['p']} p")
    ts["data"] = {"table": table, "matches": matches}
    ts["checked"] = t.isoformat()


def save(st):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1), "utf-8")


def load():
    st = json.loads(STATE_FILE.read_text("utf-8")) if STATE_FILE.exists() else {}
    for k, v in (("matches", {}), ("ids", []), ("notours", []), ("teams", {}), ("last_final", {})):
        st.setdefault(k, v)
    return st


def cycle(st):
    """En omgång: lagsidor + matchsidor. Returnerar True om någon match pågår nu."""
    t = now()
    for name, url in TEAMS.items():
        try:
            team_check(st, t, name, url)
        except Exception as e:
            print(f"{name}: {e}", file=sys.stderr)
    ids = sorted((set(SEED_IDS) | set(st["ids"])) - set(st["notours"]))
    fresh, active = 0, False
    for mid in ids:
        d = due(st["matches"].get(str(mid)), t)
        if d is True and str(mid) not in st["matches"]:
            fresh += 1
            if fresh > 15:                        # högst 15 nya sidor per omgång
                continue
        if d:
            try:
                poll(mid, st, t)
            except Exception as e:
                print(f"Match {mid}: {e}", file=sys.stderr)
        active |= d == "window"
    return active


def main():
    args = sys.argv[1:]
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    first_run = not STATE_FILE.exists()
    st = load()
    if "--scan" in args:
        i = args.index("--scan")
        scan(st, int(args[i + 1]) if i + 1 < len(args) and args[i + 1].isdigit() else 200)
        save(st)
        return
    if "--ci" in args:                            # GitHub Actions: en körning, loopar under matchtid
        deadline = time.time() + 170 * 60
        if first_run:
            if not scan(st, 200):                 # blockerad: spara inget, misslyckas tydligt
                print("Avbryter: Profixio släpper inte in den här servern.", file=sys.stderr)
                sys.exit(1)
            try:
                notify("Profixio-bevakning", "Igång för: " + ", ".join(TEAMS))
            except Exception as e:
                print(f"ntfy: {e}", file=sys.stderr)
            st["last_scan"] = now().isoformat()
            save(st)
        while True:
            active = cycle(st)
            ls = st.get("last_scan")
            if not active and (not ls or now() - datetime.fromisoformat(ls) > timedelta(days=7)):
                ok = scan(st, 200)
                st["last_scan"] = (now() if ok else now() - timedelta(days=6)).isoformat()
            save(st)
            if not active or time.time() > deadline:
                return
            time.sleep(30)
    if "--loop" in args:                          # körs dygnet runt i molnet
        if first_run:
            try:
                notify("Profixio-bevakning", "Igång för: " + ", ".join(TEAMS))
            except Exception as e:
                print(f"ntfy: {e}", file=sys.stderr)
            ok = scan(st, 200)                    # hittar matcher automatiskt, tar ca 10 min
            st["last_scan"] = (now() if ok else now() - timedelta(days=6)).isoformat()
            save(st)
        while True:
            active = False
            try:
                active = cycle(st)
                ls = st.get("last_scan")
                if not active and (not ls or now() - datetime.fromisoformat(ls) > timedelta(days=7)):
                    ok = scan(st, 200)            # veckovis: hitta nya matcher
                    st["last_scan"] = (now() if ok else now() - timedelta(days=6)).isoformat()
                save(st)
            except Exception as e:
                print(f"Fel: {e}", file=sys.stderr)
            time.sleep(30 if active else 60)
    active = cycle(st)                            # cron-läge
    if active and not DEBUG:
        time.sleep(30)
        cycle(st)
    if first_run and not DEBUG:
        notify("Profixio-bevakning", "Igång för: " + ", ".join(TEAMS))
    if not DEBUG:
        save(st)


if __name__ == "__main__":
    main()
