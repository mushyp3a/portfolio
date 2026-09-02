#!/usr/bin/env python3
"""
import-immich.py — populate the Hugo photography section from Immich.

This is the ONLY piece that talks to Immich. It is NOT part of the Hugo
build and never ships anything to the browser. It is run by the scheduled
GitHub Action (see .github/workflows/hugo.yml) and can be run locally.

What it does
------------
1. Authenticates to Immich with an API key (server-side only).
2. Lists your albums and maps each mapped one to a photography subsection
   (content/photography/<slug>/). Mapping is the explicit ALBUM_MAP below.
3. For every asset in a mapped album, writes a photo page in camelCase front
   matter, pulling camera/lens/exposure/ISO/date from Immich EXIF.
 4. Stores the Immich asset id as `immichId`. The site's photo-image.html
    partial resolves the image URL via the `immichBaseURL` site param (your
    reverse proxy). There is no placeholder image — the live site serves
    bytes straight from Immich; `image` is left empty.

Image serving (important)
-------------------------
Immich's asset thumbnail endpoints require authentication, which a browser
<img> tag cannot provide. Serve images publicly through a reverse proxy
that injects your API key server-side — see proxy/Caddyfile.example.
Never put the key in client JavaScript or in this repo.

Usage
-----
  export IMMICH_URL="https://immich.example.com"
  export IMMICH_API_KEY="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
  python3 scripts/import-immich.py              # write files (append)
  python3 scripts/import-immich.py --clean      # wipe generated photos first
  python3 scripts/import-immich.py --dry-run    # list albums + mapping only

Targeted at the Immich v1/v3 REST API. If your Immich version differs,
adjust the endpoints in api_get()/get_album_assets().
"""

import os
import re
import sys
import json
import shutil
import datetime
import urllib.request
import urllib.error

IMMICH_URL = os.environ.get("IMMICH_URL", "").rstrip("/")
IMMICH_API_KEY = os.environ.get("IMMICH_API_KEY", "")
CONTENT_ROOT = "content/photography"
DRY_RUN = "--dry-run" in sys.argv
CLEAN = "--clean" in sys.argv

# Browser-like UA so Cloudflare bot protection doesn't 403 the client.
USER_AGENT = "Mozilla/5.0 (compatible; ImmichImporter/1.0)"

# Explicit album mapping: Immich album name -> (section slug, section title).
# Only listed albums become photography sections. Add/remove as needed.
# "Featured" is the curated highlight album and always sorts first in the
# sidebar; the rest of the albums are auto-filled below it.
ALBUM_MAP = {
    "Featured": ("featured", "Featured"),
    "Birds": ("birds", "Birds"),
    "Nature": ("nature", "Nature"),
    "Street": ("street", "Street"),
}


def slugify(text):
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def api_get(path):
    url = f"{IMMICH_URL}{path}"
    req = urllib.request.Request(
        url,
        headers={
            "x-api-key": IMMICH_API_KEY,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def api_post(path, body):
    url = f"{IMMICH_URL}{path}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "x-api-key": IMMICH_API_KEY,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def get_albums():
    return api_get("/api/albums")


def _search_assets(page):
    """Normalise a search/metadata response to a list of asset dicts, or None
    if it carries no assets (so the caller can try the next endpoint)."""
    if not isinstance(page, dict):
        return None
    assets = page.get("assets")
    if isinstance(assets, list):
        return assets
    if isinstance(assets, dict):
        items = assets.get("items")
        if isinstance(items, list):
            return items
    return None


def get_album_assets(album_id):
    """List every asset in an album, tolerant across Immich API versions.

    The album-contents endpoint has changed repeatedly:
      - v3: GET /api/albums/{id} no longer embeds assets; the migration guide
        points at the search endpoint. The stable, version-spanning call is
        POST /api/search/metadata with {"albumIds":[id],...} (works on v2.7+
        and v3).
      - older v2: GET /api/albums/{id}/assets (array) or inline assets on the
        album object.
    Try each in turn; the first that responds 2xx with data wins.
    """
    # 1) POST /api/search/metadata (primary, v2.7+ & v3)
    try:
        page_num = 1
        size = 200
        body = {"albumIds": [album_id], "type": "IMAGE", "withExif": True,
                "size": size, "page": page_num, "order": "desc"}
        page = api_post("/api/search/metadata", body)
        items = _search_assets(page)
        if items is not None:
            assets = list(items)
            while len(items) == size:
                page_num += 1
                body["page"] = page_num
                page = api_post("/api/search/metadata", body)
                items = _search_assets(page) or []
                assets.extend(items)
                if len(items) < size:
                    break
            return assets
    except urllib.error.HTTPError:
        pass

    # 2) GET /api/albums/{id}/assets (older v2)
    try:
        page = api_get(f"/api/albums/{album_id}/assets?count=200&skip=0")
        if isinstance(page, list):
            return page
    except urllib.error.HTTPError:
        pass

    # 3) GET /api/albums/{id} with inline assets (very old v2)
    try:
        page = api_get(f"/api/albums/{album_id}")
        if isinstance(page, dict) and isinstance(page.get("assets"), list):
            return page["assets"]
    except urllib.error.HTTPError:
        pass

    # 4) POST /api/search/assets (legacy v3-rc)
    try:
        page = api_post(
            "/api/search/assets",
            {"albumIds": [album_id], "take": 200, "skip": 0, "order": "desc"},
        )
        if isinstance(page, dict):
            return page.get("assets", [])
        if isinstance(page, list):
            return page
    except urllib.error.HTTPError:
        pass

    return []


def exif_value(exif, *keys, default=""):
    node = exif
    for k in keys:
        if not isinstance(node, dict):
            return default
        node = node.get(k, {})
    if node in (None, {}):
        return default
    return str(node)


def build_front_matter(asset):
    # Immich returns EXIF under exifInfo (not "exif"); only when the search
    # call sets withExif:true. Normalise exifData whether it's a dict OR a list
    # of {"key","value"} items into one lowercase-keyed dict, then merge with
    # the flat exifInfo fields so either Immich shape parses.
    exif = asset.get("exifInfo") or asset.get("exif") or {}
    if not isinstance(exif, dict):
        exif = {}

    merged = {str(k).lower(): v for k, v in exif.items()}
    raw = exif.get("exifData")
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("key"):
                merged[str(item["key"]).lower()] = item.get("value", "")
    elif isinstance(raw, dict):
        merged.update({str(k).lower(): v for k, v in raw.items()})

    def ev(*keys, default=""):
        for k in keys:
            kl = str(k).lower()
            val = merged.get(kl)
            if val not in (None, ""):
                return str(val)
        return default

    date_raw = ev("fileCreatedAt") or ev("localDateTime") or ev("dateTimeOriginal") or ""
    try:
        date = datetime.datetime.fromisoformat(date_raw.replace("Z", "+00:00"))
        date_str = date.strftime("%Y-%m-%d")
    except Exception:
        date_str = ""

    make = ev("make")
    model = ev("model")
    camera = " ".join(p for p in (make, model) if p) or ""

    focal = ev("focalLength")
    fnumber = ev("fNumber")
    shutter = ev("exposureTime")
    iso = ev("iso")
    lens = ev("lensModel")

    city = str(ev("city") or "")
    country = str(ev("country") or "")
    location = ", ".join(p for p in (city, country) if p)

    raw_title = ev("description") or asset.get("originalFileName") or asset.get("id", "photo")
    title = re.sub(r"\.\w+$", "", str(raw_title)).strip() or "Untitled"

    asset_id = asset.get("id", "")
    return {
        "title": title,
        "date": date_str,
        "album": "",
        # Immich has no taxonomy; fill species/scientificName manually
        # (or via a sidecar mapping) if you want them.
        "species": "",
        "scientificName": "",
        "location": location,
        "camera": camera,
        "lens": lens,
        "focalLength": focal,
        "aperture": fnumber,
        "shutterSpeed": shutter,
        "iso": iso,
        # No placeholder: the live site resolves the image straight from
        # Immich via immichBaseURL + immichId (see photo-image.html /
        # photo-image-src.html). "image" is intentionally left empty.
        "image": "",
        "description": str(ev("description") or ""),
        "featured": False,
        "immichId": asset_id,
    }


def fm_to_md(fm):
    order = ["title", "date", "album", "species", "scientificName",
             "location", "camera", "lens", "focalLength", "aperture",
             "shutterSpeed", "iso", "image", "description", "featured",
             "immichId"]
    lines = ["---"]
    for key in order:
        val = fm.get(key, "")
        if isinstance(val, bool):
            lines.append(f"{key}: {str(val).lower()}")
        else:
            lines.append(f"{key}: {val!r}")
    lines += ["---", "", ""]
    return "\n".join(lines)


def write_section(slug, title):
    path = os.path.join(CONTENT_ROOT, slug, "_index.md")
    if os.path.exists(path):
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(f'---\ntitle: "{title}"\nimmichAlbum: ""\n---\n\n')


def clean_section(slug):
    folder = os.path.join(CONTENT_ROOT, slug)
    if not os.path.isdir(folder):
        return
    for name in os.listdir(folder):
        if name.endswith(".md") and name != "_index.md":
            os.remove(os.path.join(folder, name))


def main():
    if not IMMICH_URL or not IMMICH_API_KEY:
        sys.exit("Set IMMICH_URL and IMMICH_API_KEY environment variables.")

    albums = get_albums()
    mapped = [(a, ALBUM_MAP[a["albumName"]]) for a in albums
              if a.get("albumName") in ALBUM_MAP]

    if DRY_RUN:
        print(f"Found {len(albums)} album(s); {len(mapped)} mapped:")
        for a, (slug, title) in mapped:
            print(f'  {a["albumName"]!r} -> content/photography/{slug}/')
        return

    os.makedirs(CONTENT_ROOT, exist_ok=True)

    # Remove album folders whose album was deleted in Immich or unmapped, so
    # stale sections don't linger on the site. Only prunes real section
    # directories (those containing _index.md), never the top-level page.
    active_slugs = {slug for _, (slug, _) in mapped}
    for name in os.listdir(CONTENT_ROOT):
        path = os.path.join(CONTENT_ROOT, name)
        if (os.path.isdir(path) and name not in active_slugs
                and os.path.exists(os.path.join(path, "_index.md"))):
            shutil.rmtree(path)

    for a, (slug, title) in mapped:
        write_section(slug, title)
        if CLEAN:
            clean_section(slug)
        assets = get_album_assets(a["id"])
        for asset in assets:
            fm = build_front_matter(asset)
            fm["album"] = slug
            name = slugify(fm["title"]) or asset.get("id", "photo")
            out = os.path.join(CONTENT_ROOT, slug, f"{name}.md")
            with open(out, "w") as f:
                f.write(fm_to_md(fm))
            print(f"wrote {out}")


if __name__ == "__main__":
    main()
