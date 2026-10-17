#!/usr/bin/env python3
"""Rebuild manifest.json from the images in ITT-ProductPhotos/.

- Images whose bytes are unchanged (same md5) keep their entry as-is, so hand edits survive.
- Renamed or re-exported images inherit product line / category / product / tags / description
  from the closest old entry by name.
- Anything still unknown is described by Claude when ANTHROPIC_API_KEY is set; otherwise it is
  added with needs_review: true.
- Identical images are intentional: each keeps its own full entry and nothing is marked
  as a duplicate (shares_image_with just lists the other entries using the same file).
- manifest.json is only rewritten when the image list actually changed.
"""
import base64, datetime, difflib, hashlib, io, json, os, re, sys, urllib.request
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
IMAGE_ROOT = "ITT-ProductPhotos"
MANIFEST = ROOT / "manifest.json"
EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
LINES = ["Brand", "Content", "Demand", "Intent"]
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")


def slug(*parts):
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", "-".join(p for p in parts if p).lower())).strip("-")


def name_key(filename):
    """'ProductPhotos-Webinars(On-Demand).png' / 'Other-Webinars(On-Demand).png' -> 'webinarsondemand'."""
    stem = Path(filename).stem
    stem = stem.split("-", 1)[1] if "-" in stem else stem
    return re.sub(r"[^a-z0-9]", "", stem.lower())


def split_camel(s):
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s)
    return re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", s).strip()


def orientation(ar):
    return "square" if 0.95 <= ar <= 1.05 else ("landscape" if ar > 1 else "portrait")


def ask_claude(path, hint):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    img = Image.open(path).convert("RGB")
    img.thumbnail((1568, 1568))
    buf = io.BytesIO(); img.save(buf, "JPEG", quality=85)
    prompt = (
        "This is a product/placement screenshot or mockup from Informa TechTarget's GTM suite. "
        f"Filename hint: {hint}. Return ONLY a JSON object with keys: "
        f"product_line (one of {LINES}), category (product family, e.g. 'Web Advertising', "
        "'BrightTALK', 'Custom Content + Leads'), product (human-readable placement name), "
        "description (one sentence on what the image visually shows), tags (4-6 lowercase keywords)."
    )
    body = {"model": MODEL, "max_tokens": 600, "messages": [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                     "data": base64.b64encode(buf.getvalue()).decode()}},
        {"type": "text", "text": prompt}]}]}
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", json.dumps(body).encode(), {
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        text = "".join(b.get("text", "") for b in json.load(urllib.request.urlopen(req, timeout=120))["content"])
        data = json.loads(re.sub(r"```(json)?", "", text).strip())
        return data if data.get("product_line") in LINES else None
    except Exception as e:
        print(f"  Claude describe failed for {hint}: {e}", file=sys.stderr)
        return None


def main():
    old = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}
    old_images = old.get("images", [])
    by_path = {(i["path"], i["md5"]): i for i in old_images}
    by_md5 = {i["md5"]: i for i in old_images if not i.get("duplicate_of")}
    by_name = {}
    for i in old_images:  # match on the filename and on the product name
        by_name.setdefault(name_key(i["filename"]), i)
        by_name.setdefault(re.sub(r"[^a-z0-9]", "", i["product"].lower()), i)
    for i in old_images:  # category names last (e.g. 'Lead Generation'), only for exact hits
        by_name.setdefault(re.sub(r"[^a-z0-9]", "", i["category"].lower()), i)

    files = sorted(p for p in (ROOT / IMAGE_ROOT).rglob("*") if p.suffix.lower() in EXTS)
    entries, seen_md5, used_ids = [], {}, set()
    for p in files:
        rel = p.relative_to(ROOT / IMAGE_ROOT).as_posix()
        raw = p.read_bytes(); md5 = hashlib.md5(raw).hexdigest()
        with Image.open(p) as im:
            w, h, fmt = im.width, im.height, im.format
        ar = round(w / h, 3)
        stats = {"path": rel, "filename": p.name, "width": w, "height": h, "aspect_ratio": ar,
                 "orientation": orientation(ar), "format": fmt, "filesize_kb": round(len(raw) / 1024),
                 "md5": md5}

        if (rel, md5) in by_path:                           # unchanged file: keep entry (and hand edits)
            e = {**by_path[(rel, md5)], **stats}
            print(f"keep     {rel}")
            e.setdefault("needs_review", False)
        else:
            key = name_key(p.name)
            match = by_name.get(key)
            if not match:
                close = difflib.get_close_matches(key, by_name.keys(), n=1, cutoff=0.8)
                match = by_name[close[0]] if close else None
            if not match:
                match = next((v for k, v in by_name.items() if k.endswith(key) or key.endswith(k)), None)
            if not match:
                match = by_md5.get(md5)                     # same bytes under an unrecognised name
            ai = ask_claude(p, p.name)
            folders = rel.split("/")[:-1]
            if ai:
                base = {k: ai[k] for k in ("product_line", "category", "product", "description", "tags")}
                if match:  # keep the established taxonomy + id for continuity
                    base.update({k: match[k] for k in ("id", "product_line", "category", "product")})
                base["needs_review"] = False
                print(f"claude   {rel}")
            elif match:
                base = {k: match[k] for k in ("id", "product_line", "category", "product", "description", "tags")}
                base["needs_review"] = False
                print(f"renamed  {rel}  <- {match['path']}")
            else:
                base = {"product_line": folders[0] if folders else "Unclassified",
                        "category": folders[1] if len(folders) > 1 else "Unclassified",
                        "product": split_camel(Path(p.name).stem.split("-", 1)[-1]),
                        "description": "", "tags": [], "needs_review": True}
                print(f"REVIEW   {rel}")
            e = {**base, **stats}
        e.setdefault("needs_review", False)

        if not e.get("id") or e["id"] in used_ids:
            e["id"] = slug(e["product_line"], e["category"], e["product"])
            n, base_id = 2, e["id"]
            while e["id"] in used_ids:
                e["id"], n = f"{base_id}-{n}", n + 1
        used_ids.add(e["id"])
        e["duplicate_of"] = None                  # identical images are kept as full, separate entries
        seen_md5.setdefault(md5, []).append(e["id"])
        order = ["id", "path", "filename", "product_line", "category", "product", "description", "tags",
                 "width", "height", "aspect_ratio", "orientation", "format", "filesize_kb", "md5",
                 "duplicate_of", "shares_image_with", "needs_review"]
        entries.append({k: e.get(k) for k in order})

    for e in entries:  # informational only: other entries that use the exact same image file
        e["shares_image_with"] = [i for i in seen_md5[e["md5"]] if i != e["id"]]

    if entries == old_images:
        print("No changes; manifest.json left as is.")
        return
    lines = {}
    for e in entries:
        lines[e["product_line"]] = lines.get(e["product_line"], 0) + 1
    dupes = 0  # nothing is hidden; see shares_image_with
    header = {k: v for k, v in old.items() if k != "images"}
    header.update({
        "manifest_version": old.get("manifest_version", "1.0"),
        "name": old.get("name", "Informa TechTarget - Product Photos"),
        "generated": datetime.date.today().isoformat(),
        "image_root": IMAGE_ROOT,
        "total_images": len(entries), "unique_images": len(entries) - dupes,
        "duplicate_images": dupes, "product_lines": dict(sorted(lines.items())),
        "needs_review": sum(1 for e in entries if e["needs_review"]),
    })
    header.setdefault("fields", {})["duplicate_of"] = "always null: identical images are intentional and every entry is listed"
    header["fields"]["shares_image_with"] = "ids of other entries that use the same image file (informational, nothing is hidden)"
    header["fields"]["needs_review"] = "true when the entry was auto-added and its description/taxonomy still need a human check"
    header["images"] = entries
    MANIFEST.write_text(json.dumps(header, indent=2, ensure_ascii=False) + "\n")
    print(f"Wrote manifest.json: {len(entries)} images, {header['needs_review']} need review.")


if __name__ == "__main__":
    main()
