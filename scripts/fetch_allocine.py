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
import io
from PIL import Image

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


def parse_week(html: str, week_date: date, window: tuple[date, date] | None = None):
    """Parse une page agenda. Garde les films datés de la semaine, et aussi ceux
    dont la fiche indique un autre mercredi dans la fenêtre (AlloCiné laisse
    parfois un film avancé/reculé sur l'ancienne semaine de l'agenda)."""
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
        date_m = re.search(r'<span class="[^"]*\bdate\b[^"]*"[^>]*>([^<]+)</span>', c)
        if not date_m:
            continue
        release = parse_fr_date(date_m.group(1))
        if release != week_date:
            in_window = (
                window is not None
                and release is not None
                and release.weekday() == 2
                and window[0] <= release <= window[1]
            )
            if not in_window:
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


def parse_fiche_release_date(html: str):
    """Date de sortie France (« en salle ») lue sur une fiche AlloCiné."""
    info = re.search(r'meta-body-info">(.*?)</div>', html, re.S)
    if not info:
        return None
    block = info.group(1)
    theatrical = re.search(
        r'<span class="[^"]*\bdate\b[^"]*"[^>]*>([^<]+)</span>\s*<span class="meta-release-type">\s*en salle',
        block,
        re.S | re.I,
    )
    if theatrical:
        d = parse_fr_date(theatrical.group(1))
        if d:
            return d
    m = re.search(r"(\d{1,2}\s+[a-zéû]+\s+\d{4})", clean_text(block).lower())
    return parse_fr_date(m.group(1)) if m else None


def fetch_duration(film):
    try:
        html = fetch(film["allocineUrl"])
    except Exception:
        return film["id"], None, None, None
    official = parse_fiche_release_date(html)
    info = re.search(r'meta-body-info">(.*?)</div>', html, re.S)
    if not info:
        return film["id"], None, None, official
    text = clean_text(info.group(1))
    m = re.search(r"(\d+h(?:\s*\d+min)?|\d+\s*min)", text)
    dur = None
    if m:
        dur = m.group(1).replace(" ", "")
        if dur.startswith("0h"):
            dur = dur[2:]
    genres = [clean_text(g) for g in re.findall(r'dark-grey-link"[^>]*>([^<]+)', info.group(1))]
    return film["id"], dur, genres or None, official


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



# ---------------------------------------------------------------------------
# Flux d'abonnement unique (webcal) : calendrier.ics
# ---------------------------------------------------------------------------
FEED_PATH = ROOT / "calendrier.ics"
FEED_STATE_PATH = ROOT / "calendrier-state.json"
FEED_KEEP_PAST_DAYS = 14      # garder les sorties récentes 2 semaines
FEED_TOMBSTONE_DAYS = 180     # mémoriser les films retirés (SEQUENCE continue)
FEED_UID_DOMAIN = "realalion.github.io"


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _feed_fields(film: dict) -> dict:
    """Champs qui définissent le contenu d'un VEVENT du flux."""
    return {
        "title": film.get("title") or "Film",
        "releaseDate": film["releaseDate"],
        "genres": list(film.get("genres") or []),
        "duration": film.get("duration"),
        "directors": list(film.get("directors") or []),
        "cast": list(film.get("cast") or []),
        "synopsis": film.get("synopsis") or "",
        "allocineUrl": film.get("allocineUrl") or "",
    }


def _feed_description(f: dict) -> str:
    parts = []
    meta = []
    if f.get("genres"):
        meta.append(", ".join(f["genres"]))
    if f.get("duration"):
        meta.append(f["duration"])
    if meta:
        parts.append(" · ".join(meta))
    if f.get("directors"):
        parts.append("De " + ", ".join(f["directors"]))
    if f.get("cast"):
        parts.append("Avec " + ", ".join(f["cast"]))
    if f.get("synopsis"):
        parts.append(f["synopsis"])
    if f.get("allocineUrl"):
        parts.append("AlloCiné : " + f["allocineUrl"])
    parts.append("Sortie officielle au cinéma en France.")
    return "\n\n".join(parts)


def _feed_vevent(fid: str, entry: dict) -> list[str]:
    f = entry["fields"]
    start = date.fromisoformat(f["releaseDate"])
    end = start + timedelta(days=1)
    lines = [
        "BEGIN:VEVENT",
        f"UID:sortie-cine-{fid}@{FEED_UID_DOMAIN}",
        f"DTSTAMP:{entry['modified']}",
        f"CREATED:{entry['created']}",
        f"LAST-MODIFIED:{entry['modified']}",
        f"SEQUENCE:{entry['sequence']}",
        f"DTSTART;VALUE=DATE:{start.strftime('%Y%m%d')}",
        f"DTEND;VALUE=DATE:{end.strftime('%Y%m%d')}",
        f"SUMMARY:{ics_escape('🎬 ' + f['title'])}",
        f"DESCRIPTION:{ics_escape(_feed_description(f))}",
    ]
    if f.get("allocineUrl"):
        lines.append(f"URL;VALUE=URI:{f['allocineUrl']}")
    if f.get("genres"):
        lines.append("CATEGORIES:" + ",".join(ics_escape(g) for g in f["genres"]))
    lines += ["TRANSP:TRANSPARENT", "STATUS:CONFIRMED", "END:VEVENT"]
    return lines


EXTRA_PATH = ROOT / "extra-films.json"
EXCLUDED_PATH = ROOT / "excluded-films.json"


def load_excluded_ids() -> set[str]:
    """Ids AlloCiné à ne jamais réintégrer dans le calendrier."""
    try:
        data = json.loads(EXCLUDED_PATH.read_text(encoding="utf-8"))
    except Exception:
        return set()
    return {str(i) for i in (data.get("ids") or [])}



def load_extra_films(recheck: bool = True) -> list[dict]:
    """Films suivis manuellement (hors fenêtre). Revérifie la date AlloCiné
    si elle est lisible sur la fiche ; sinon garde la date connue."""
    try:
        data = json.loads(EXTRA_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    changed = False
    for f in data.get("films", []):
        if not recheck or not f.get("allocineUrl"):
            continue
        try:
            html = fetch(f["allocineUrl"])
        except Exception:
            continue
        d = parse_fiche_release_date(html)
        if d and d.isoformat() != f["releaseDate"]:
            print(f"extra: {f['title']} {f['releaseDate']} -> {d.isoformat()}")
            f["releaseDate"] = d.isoformat()
            changed = True
    if changed:
        EXTRA_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return [f for f in data.get("films", []) if f.get("releaseDate")]


def write_feed(films: list[dict], today: date | None = None, extras: list[dict] | None = None) -> int:
    """Écrit calendrier.ics (flux d'abonnement) + calendrier-state.json.

    - UID stable par film (id AlloCiné) -> un changement de date met à jour
      l'événement existant au lieu d'en créer un doublon.
    - SEQUENCE +1 et DTSTAMP/LAST-MODIFIED rafraîchis quand la date change ;
      DTSTAMP/LAST-MODIFIED rafraîchis si un autre champ change.
    - Un film absent de data.json (date retirée / « Prochainement ») sort du
      flux, sauf s'il est sorti il y a moins de FEED_KEEP_PAST_DAYS jours.
    """
    today = today or date.today()
    now = _utc_stamp()
    try:
        state = json.loads(FEED_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        state = {}
    films_state = state.get("films", {})
    excluded = load_excluded_ids()
    current = {f["id"]: f for f in films if f.get("releaseDate") and f["id"] not in excluded}
    if extras is None:
        extras = load_extra_films()
    for f in extras:
        if f["id"] not in excluded:
            current.setdefault(f["id"], f)

    new_state = {}
    for fid, film in current.items():
        fields = _feed_fields(film)
        prev = films_state.get(fid)
        if prev is None:
            entry = {"created": now, "modified": now, "sequence": 0, "fields": fields}
        else:
            entry = dict(prev)
            entry["fields"] = fields
            if prev["fields"].get("releaseDate") != fields["releaseDate"]:
                entry["sequence"] = int(prev.get("sequence", 0)) + 1
                entry["modified"] = now
            elif prev["fields"] != fields or not prev.get("inFeed", True):
                entry["modified"] = now
                if not prev.get("inFeed", True):
                    entry["sequence"] = int(prev.get("sequence", 0)) + 1
        entry["inFeed"] = True
        entry.pop("removedOn", None)
        new_state[fid] = entry

    keep_from = today - timedelta(days=FEED_KEEP_PAST_DAYS)
    for fid, prev in films_state.items():
        if fid in new_state:
            continue
        rel = date.fromisoformat(prev["fields"]["releaseDate"])
        entry = dict(prev)
        if prev.get("inFeed", True) and keep_from <= rel < today:
            # Sorti récemment : on le garde dans le flux quelques jours
            entry["inFeed"] = True
        else:
            # Date retirée, film déplacé hors fenêtre, ou sorti depuis longtemps
            if entry.get("inFeed", True):
                entry["inFeed"] = False
                entry["removedOn"] = today.isoformat()
            removed = date.fromisoformat(entry.get("removedOn", today.isoformat()))
            if (today - removed).days > FEED_TOMBSTONE_DAYS:
                continue
        new_state[fid] = entry

    in_feed = sorted(
        ((fid, e) for fid, e in new_state.items() if e.get("inFeed")),
        key=lambda x: (x[1]["fields"]["releaseDate"], x[1]["fields"]["title"]),
    )
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//realalion//Sorties Cine France//FR",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Sorties Ciné",
        f"X-WR-CALDESC:{ics_escape('Dates de sortie officielles au cinéma en France (source AlloCiné). Mise à jour quotidienne.')}",
        "X-WR-TIMEZONE:Europe/Paris",
        "X-APPLE-CALENDAR-COLOR:#E8458B",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
        "X-PUBLISHED-TTL:PT6H",
    ]
    for fid, entry in in_feed:
        lines += _feed_vevent(fid, entry)
    lines.append("END:VCALENDAR")
    FEED_PATH.write_text("\r\n".join(ics_fold(l) for l in lines) + "\r\n", encoding="utf-8", newline="")

    out_state = {"films": dict(sorted(new_state.items()))}
    FEED_STATE_PATH.write_text(json.dumps(out_state, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {FEED_PATH} ({len(in_feed)} events)")
    return len(in_feed)



FALLBACK_COLORS = [
    "#E8458B", "#FF6B35", "#7C4DFF", "#00C2FF", "#2EE59D",
    "#FFD60A", "#FF3B5C", "#5B8CFF", "#FF8A5B", "#B967FF",
]


def vibrant_from_image(im: Image.Image) -> str:
    im = im.convert("RGB").resize((48, 48), Image.Resampling.BOX)
    pixels = list(im.getdata())
    scored = []
    for r, g, b in pixels:
        mx, mn = max(r, g, b), min(r, g, b)
        sat = (mx - mn) / (mx + 1e-6)
        lum = (r + g + b) / 3 / 255
        if lum < 0.18 or lum > 0.92:
            continue
        score = sat * 1.6 + (0.55 - abs(lum - 0.55))
        scored.append(((r, g, b), score))
    if not scored:
        r = sum(p[0] for p in pixels) // len(pixels)
        g = sum(p[1] for p in pixels) // len(pixels)
        b = sum(p[2] for p in pixels) // len(pixels)
        return f"#{r:02x}{g:02x}{b:02x}"
    scored.sort(key=lambda x: -x[1])
    top = [c for c, _ in scored[:12]]
    r = sum(c[0] for c in top) // len(top)
    g = sum(c[1] for c in top) // len(top)
    b = sum(c[2] for c in top) // len(top)
    mx, mn = max(r, g, b), min(r, g, b)
    if mx > mn:
        mid = (mx + mn) / 2
        boost = 1.18
        r = int(min(255, max(0, mid + (r - mid) * boost)))
        g = int(min(255, max(0, mid + (g - mid) * boost)))
        b = int(min(255, max(0, mid + (b - mid) * boost)))
    return f"#{r:02x}{g:02x}{b:02x}"


def enrich_colors(films: list[dict]) -> None:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def one(film):
        fb = FALLBACK_COLORS[hash(film["id"]) % len(FALLBACK_COLORS)]
        url = film.get("poster")
        if not url:
            return film["id"], fb
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://www.allocine.fr/"})
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
            return film["id"], vibrant_from_image(Image.open(io.BytesIO(raw)))
        except Exception:
            return film["id"], fb

    by_id = {f["id"]: f for f in films}
    with ThreadPoolExecutor(max_workers=10) as ex:
        for fut in as_completed([ex.submit(one, f) for f in films]):
            fid, color = fut.result()
            by_id[fid]["color"] = color


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


# Horizon de collecte : toutes les semaines d'agenda FR publiées par AlloCiné
# (pages /film/agenda/sem-YYYY-MM-DD/ uniquement, jamais /agenda/usa/).
# Le menu déroulant du site s'arrête plus tôt que les pages hebdo, d'où un
# parcours des mercredis jusqu'à EMPTY_STOP semaines consécutives sans sortie
# cinéma FR, avec un plafond de sécurité MAX_WEEKS.
MAX_WEEKS = 156
EMPTY_STOP = 8


def fetch_all(*, enrich_details: bool = True) -> list[dict]:
    today = date.today()
    start = next_wednesday(today)
    end_cap = start + timedelta(weeks=MAX_WEEKS)
    all_films = []
    d = start
    empty_streak = 0
    last_with_films = None
    while d <= end_cap:
        url = f"https://www.allocine.fr/film/agenda/sem-{d.isoformat()}/"
        print("fetch", url)
        films = parse_week(fetch(url), d, (start, end_cap))
        print(f"  {len(films)} films")
        if films:
            all_films.extend(films)
            empty_streak = 0
            last_with_films = d
        else:
            empty_streak += 1
            if empty_streak >= EMPTY_STOP:
                print(
                    f"stop: {EMPTY_STOP} mercredis consécutifs sans sortie FR "
                    f"(dernière semaine avec films: {last_with_films})"
                )
                break
        d += timedelta(days=7)

    seen = {}
    for f in all_films:
        seen.setdefault(f["id"], f)
    excluded = load_excluded_ids()
    films = [f for f in seen.values() if f["id"] not in excluded]
    for f in seen.values():
        if f["id"] in excluded:
            print(f"skip: {f['title']} ({f['id']}) exclu")
    by_id = {f["id"]: f for f in films}

    if enrich_details:
        official_dates = {}
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = [ex.submit(fetch_duration, f) for f in films]
            for fut in as_completed(futs):
                fid, dur, genres, official = fut.result()
                if dur and fid in by_id:
                    by_id[fid]["duration"] = dur
                if genres and fid in by_id:
                    by_id[fid]["genres"] = genres
                if official:
                    official_dates[fid] = official

        # L'agenda laisse parfois un film déjà sorti sur une semaine future,
        # avec une date de carte erronée (ex. Quemoun, sortie 5/07/2026, resté
        # sur l'agenda du 20/01/2027). On fait confiance à la fiche officielle.
        keep_from = today - timedelta(days=FEED_KEEP_PAST_DAYS)
        kept = []
        for f in films:
            official = official_dates.get(f["id"])
            if official is None:
                kept.append(f)
                continue
            if official < keep_from:
                print(
                    f"skip: {f['title']} ({f['id']}) sortie fiche {official.isoformat()} "
                    f"déjà passée (agenda {f['releaseDate']})"
                )
                continue
            if official.isoformat() != f["releaseDate"]:
                print(f"date fiche: {f['title']} {f['releaseDate']} -> {official.isoformat()}")
                f["releaseDate"] = official.isoformat()
            kept.append(f)
        films = kept
        by_id = {f["id"]: f for f in films}

    return sorted(by_id.values(), key=lambda x: (x["releaseDate"], x["title"]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--ics-only",
        action="store_true",
        help="Regenerate ICS files from existing data.json without fetching",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Collecte l'agenda sans enrichir les fiches ni écrire les fichiers",
    )
    args = parser.parse_args()

    if args.ics_only:
        data = json.loads((ROOT / "data.json").read_text(encoding="utf-8"))
        films = data["films"]
        if films and not films[0].get("color"):
            enrich_colors(films)
            write_data(films)
        write_ics_files(films)
        write_feed(films)
        return

    films = fetch_all(enrich_details=not args.dry_run)
    if args.dry_run:
        from collections import Counter
        counts = Counter(f["releaseDate"] for f in films)
        weeks = sorted(counts)
        print(f"dry-run: {len(films)} films, {len(weeks)} semaines")
        if weeks:
            print(f"dry-run: première semaine {weeks[0]}, dernière {weeks[-1]}")
        for w in weeks:
            print(f"  {w}: {counts[w]} films")
        return

    enrich_colors(films)
    write_data(films)
    write_ics_files(films)
    write_feed(films)


if __name__ == "__main__":
    main()
