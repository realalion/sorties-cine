#!/usr/bin/env python3
"""Fetch upcoming France theatrical releases from AlloCiné agenda pages."""
from __future__ import annotations

import argparse
import json
import re
import shutil
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from html import unescape
from pathlib import Path

MONTHS = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4,
    "mai": 5, "juin": 6, "juillet": 7, "août": 8, "aout": 8,
    "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
}
UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15"
ROOT = Path(__file__).resolve().parents[1]
ICS_DIR = ROOT / "ics"


def next_wednesday(d: date) -> date:
    return d + timedelta(days=(2 - d.weekday()) % 7)


def parse_fr_date(s: str):
    s = unescape(s).strip().lower()
    m = re.match(r"(\d{1,2})\s+(\w+)\s+(\d{4})", s)
    if not m:
        return None
    month = MONTHS.get(m.group(2))
    if not month:
        return None
    return date(int(m.group(3)), month, int(m.group(1)))


def clean_text(t: str) -> str:
    t = unescape(t or "")
    t = re.sub(r"<[^>]+>", "", t)
    return re.sub(r"\s+", " ", t).strip()


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "fr-FR"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def parse_week(html: str, week_date: date):
    cards = re.findall(
        r'<div class="card entity-card entity-card-list cf">(.*?)(?=<div class="card entity-card entity-card-list cf">|</ul>\s*<div class="pagination|</ul>\s*</section|$)',
        html,
        re.S,
    )
    films = []
    for c in cards:
        title_m = re.search(
            r'class="meta-title-link"[^>]*href="(/film/fichefilm_gen_cfilm=(\d+)\.html)"[^>]*>(.*?)</a>',
            c,
            re.S,
        )
        if not title_m:
            continue
        url, fid, title = title_m.group(1), title_m.group(2), clean_text(title_m.group(3))
        date_m = re.search(r'<span class="date">([^<]+)</span>', c)
        if not date_m:
            continue
        release = parse_fr_date(date_m.group(1))
        if release != week_date:
            continue
        poster_m = re.search(r'<img class="thumbnail-img"[^>]*src="([^"]+)"', c)
        poster = poster_m.group(1) if poster_m else None
        if poster:
            poster = re.sub(r"/c_\d+_\d+/", "/c_310_420/", poster)
        info_m = re.search(r'meta-body-info">(.*?)</div>', c, re.S)
        genres = []
        if info_m:
            genres = [clean_text(g) for g in re.findall(r'dark-grey-link"[^>]*>([^<]+)', info_m.group(1))]
        syn_m = re.search(r'class="content-txt\s*"[^>]*>(.*?)</div>', c, re.S)
        synopsis = clean_text(syn_m.group(1)) if syn_m else ""
        if len(synopsis) > 280:
            synopsis = synopsis[:277].rsplit(" ", 1)[0] + "…"
        dir_m = re.search(r"meta-body-direction.*?De</span>(.*?)</div>", c, re.S)
        directors = []
        if dir_m:
            directors = [clean_text(d) for d in re.findall(r'dark-grey-link"[^>]*>([^<]+)', dir_m.group(1))]
        films.append(
            {
                "id": fid,
                "title": title,
                "releaseDate": release.isoformat(),
                "genres": genres,
                "duration": None,
                "synopsis": synopsis,
                "poster": poster,
                "directors": directors,
                "allocineUrl": "https://www.allocine.fr" + url,
            }
        )
    return films


def fetch_duration(film):
    try:
        html = fetch(film["allocineUrl"])
    except Exception:
        return film["id"], None, None
    info = re.search(r'meta-body-info">(.*?)</div>', html, re.S)
    if not info:
        return film["id"], None, None
    text = clean_text(info.group(1))
    m = re.search(r"(\d+h(?:\s*\d+min)?|\d+\s*min)", text)
    dur = None
    if m:
        dur = m.group(1).replace(" ", "")
        if dur.startswith("0h"):
            dur = dur[2:]
    genres = [clean_text(g) for g in re.findall(r'dark-grey-link"[^>]*>([^<]+)', info.group(1))]
    return film["id"], dur, genres or None


def ics_escape(value: str) -> str:
    value = value.replace("\\", "\\\\")
    value = value.replace(";", "\\;")
    value = value.replace(",", "\\,")
    value = value.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")
    return value


def ics_fold(line: str) -> str:
    """RFC 5545 line folding at 75 octets (UTF-8 safe)."""
    if len(line.encode("utf-8")) <= 75:
        return line
    out = []
    current = ""
    for ch in line:
        trial = current + ch
        limit = 75 if not out else 74  # continuation lines start with a space
        if len(trial.encode("utf-8")) > limit:
            out.append(current)
            current = " " + ch
        else:
            current = trial
    if current:
        out.append(current)
    return "\r\n".join(out)


def build_ics(film: dict) -> str:
    release = date.fromisoformat(film["releaseDate"])
    end = release + timedelta(days=1)
    title = film.get("title") or "Film"
    summary = f"🎬 {title} — sortie ciné"
    bits = []
    if film.get("genres"):
        bits.append(", ".join(film["genres"]))
    if film.get("duration"):
        bits.append(film["duration"])
    meta = " · ".join(bits)
    desc_parts = []
    if meta:
        desc_parts.append(meta)
    if film.get("synopsis"):
        desc_parts.append(film["synopsis"])
    if film.get("allocineUrl"):
        desc_parts.append(film["allocineUrl"])
    description = "\n\n".join(desc_parts)
    uid = f"sortie-cine-{film['id']}-{film['releaseDate']}@realalion.github.io"
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    url = film.get("allocineUrl") or ""

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Sorties Cine France//FR",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{now}",
        f"DTSTART;VALUE=DATE:{release.strftime('%Y%m%d')}",
        f"DTEND;VALUE=DATE:{end.strftime('%Y%m%d')}",
        f"SUMMARY:{ics_escape(summary)}",
        f"DESCRIPTION:{ics_escape(description)}",
    ]
    if url:
        lines.append(f"URL:{url}")
    lines.extend(
        [
            "TRANSP:TRANSPARENT",
            "STATUS:CONFIRMED",
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            f"DESCRIPTION:{ics_escape('🎬 Sortie au cinéma aujourd’hui')}",
            # 9h le matin du jour de sortie (minuit local + 9h pour un événement journée)
            "TRIGGER:PT9H",
            "END:VALARM",
            "END:VEVENT",
            "END:VCALENDAR",
        ]
    )
    folded = [ics_fold(line) for line in lines]
    return "\r\n".join(folded) + "\r\n"


def write_ics_files(films: list[dict]) -> int:
    if ICS_DIR.exists():
        shutil.rmtree(ICS_DIR)
    ICS_DIR.mkdir(parents=True, exist_ok=True)
    for film in films:
        path = ICS_DIR / f"{film['id']}.ics"
        path.write_text(build_ics(film), encoding="utf-8", newline="")
    # Help some hosts: also keep a tiny note
    (ICS_DIR / ".gitkeep").write_text("", encoding="utf-8")
    print(f"wrote {len(films)} ICS files in {ICS_DIR}")
    return len(films)


def write_data(films: list[dict]) -> Path:
    out = {
        "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": "AlloCiné (agenda sorties France)",
        "sourceUrl": "https://www.allocine.fr/film/agenda/",
        "region": "FR",
        "from": films[0]["releaseDate"] if films else None,
        "to": films[-1]["releaseDate"] if films else None,
        "count": len(films),
        "films": films,
    }
    path = ROOT / "data.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {path} ({len(films)} films)")
    return path


def fetch_all() -> list[dict]:
    today = date.today()
    start = next_wednesday(today)
    end = start + timedelta(weeks=8)
    all_films = []
    d = start
    while d <= end:
        url = f"https://www.allocine.fr/film/agenda/sem-{d.isoformat()}/"
        print("fetch", url)
        films = parse_week(fetch(url), d)
        print(f"  {len(films)} films")
        all_films.extend(films)
        d += timedelta(days=7)

    seen = {}
    for f in all_films:
        seen.setdefault(f["id"], f)
    films = list(seen.values())

    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(fetch_duration, f) for f in films]
        by_id = {f["id"]: f for f in films}
        for fut in as_completed(futs):
            fid, dur, genres = fut.result()
            if dur:
                by_id[fid]["duration"] = dur
            if genres:
                by_id[fid]["genres"] = genres

    return sorted(by_id.values(), key=lambda x: (x["releaseDate"], x["title"]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--ics-only",
        action="store_true",
        help="Regenerate ICS files from existing data.json without fetching",
    )
    args = parser.parse_args()

    if args.ics_only:
        data = json.loads((ROOT / "data.json").read_text(encoding="utf-8"))
        films = data["films"]
        write_ics_files(films)
        return

    films = fetch_all()
    write_data(films)
    write_ics_files(films)


if __name__ == "__main__":
    main()
