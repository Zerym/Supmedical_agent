r"""Upload local OGGs to WhatsApp Cloud and update `database.py` with returned media IDs.

Usage:
  1. Fill `.env` with `WHATSAPP_TOKEN` and `WHATSAPP_PHONE_NUMBER_ID` (and other vars).
  2. Run: `.venv\Scripts\python.exe scripts\upload_and_update_db.py`

The script will:
 - import `database.formations_db` to discover `local_media` paths
 - call `main.upload_media_to_whatsapp(local_path)` for each file
 - write an updated `database.py` file with the new `media_id` values

Security: do NOT paste secrets in chat. Store them in your local `.env` (not committed).
"""

from dotenv import load_dotenv
import os
import importlib
import json
import traceback
import sys

ROOT_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

load_dotenv()

TEST_MODE = os.getenv("TEST_MODE", "0").lower() in ("1", "true", "yes")

if TEST_MODE:
    print("TEST_MODE is enabled — uploads will be mocked. To perform real uploads, unset TEST_MODE in .env.")


def safe_str(obj):
    return json.dumps(obj, ensure_ascii=False)


def generate_database_py(formations, default_greeting):
    # Build a new database.py content preserving helper functions and expanded keywords
    header = 'import unicodedata\n\n\n'
    header += 'def _strip_accents(s: str) -> str:\n'
    header += '    if not s:\n        return s\n'
    header += '    normalized = unicodedata.normalize("NFD", s)\n'
    header += '    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")\n\n\n'
    header += 'def _expand_keyword_variants(kw: str):\n'
    header += '    """Return list of variants for a keyword:\n'
    header += '    - original lowercased\n    - accent-stripped\n    - singular (remove final \"s\") if present\n    - accent-stripped singular\n    Preserves order and removes duplicates.\n    """\n'
    header += '    kw = (kw or "").strip()\n'
    header += '    lower = kw.lower()\n'
    header += '    no_acc = _strip_accents(lower)\n'
    header += '    variants = [lower]\n'
    header += '    if no_acc != lower:\n        variants.append(no_acc)\n'
    header += '    if lower.endswith("s") and len(lower) > 1:\n'
    header += '        singular = lower[:-1].strip()\n'
    header += '        if singular:\n            variants.append(singular)\n'
    header += '        if no_acc.endswith("s") and len(no_acc) > 1:\n'
    header += '            singular_no_acc = no_acc[:-1].strip()\n'
    header += '            if singular_no_acc:\n                variants.append(singular_no_acc)\n\n'
    header += '    seen = set()\n    unique = []\n    for v in variants:\n        if v and v not in seen:\n            seen.add(v)\n            unique.append(v)\n    return unique\n\n\n'

    body = 'formations_db = {\n'
    for name, info in formations.items():
        body += f"    {safe_str(name)}: {{\n"
        # keywords (list)
        kws = info.get("keywords", []) or []
        body += f"        \"keywords\": {safe_str(kws)},\n"
        media_id = info.get("media_id")
        # store as JSON string or null
        if media_id is None:
            body += f"        \"media_id\": None,\n"
        else:
            body += f"        \"media_id\": {safe_str(media_id)},\n"
        local_media = info.get("local_media", "") or ""
        body += f"        \"local_media\": {safe_str(local_media)},\n"
        reg = info.get("registration_link", "") or ""
        body += f"        \"registration_link\": {safe_str(reg)},\n"
        body += "    },\n"
    body += "}\n\n\n"

    expand_loop = "# --- Expand keywords to include accentless and singular variants ---\n"
    expand_loop += "for _name, _info in formations_db.items():\n"
    expand_loop += "    orig = _info.get(\"keywords\", []) or []\n"
    expand_loop += "    expanded = []\n"
    expand_loop += "    for k in orig:\n        expanded.extend(_expand_keyword_variants(k))\n"
    expand_loop += "    _info[\"keywords\"] = list(dict.fromkeys(expanded))\n\n\n"

    footer = f"DEFAULT_GREETING = {safe_str(default_greeting)}\n\n__all__ = [\"formations_db\", \"DEFAULT_GREETING\"]\n"

    return header + body + expand_loop + footer


def main():
    try:
        import database as db_mod
    except Exception as exc:
        print("Failed to import database module:", exc)
        return

    try:
        # import the upload helper from main
        from main import upload_media_to_whatsapp
    except Exception as exc:
        print("Failed to import upload helper from main:", exc)
        traceback.print_exc()
        return

    formations = db_mod.formations_db
    updated = {}
    for name, info in formations.items():
        local = info.get("local_media") or ""
        print(f"Processing formation: {name}")
        if not local:
            print(" - no local_media set, skipping")
            continue
        if not os.path.exists(local):
            print(f" - local file not found: {local}")
            continue
        print(f" - uploading {local} ...")
        try:
            media_id = upload_media_to_whatsapp(local)
        except Exception as exc:
            print(f" - upload failed: {exc}")
            media_id = None
        if media_id:
            print(f" - uploaded id: {media_id}")
            formations[name]["media_id"] = media_id
            updated[name] = media_id
        else:
            print(" - upload returned no id")

    if not updated:
        print("No media were uploaded. Exiting.")
        return

    # write a new database.py file with updated media_id values (backup first)
    db_path = os.path.join(os.path.dirname(__file__), "..", "database.py")
    db_path = os.path.normpath(db_path)
    backup = db_path + ".bak"
    try:
        if os.path.exists(db_path):
            print(f"Creating backup: {backup}")
            with open(db_path, "r", encoding="utf-8") as f:
                original = f.read()
            with open(backup, "w", encoding="utf-8") as f:
                f.write(original)

        new_content = generate_database_py(formations, getattr(db_mod, "DEFAULT_GREETING", ""))
        with open(db_path, "w", encoding="utf-8") as f:
            f.write(new_content)
        print(f"Updated {db_path} with {len(updated)} media_id(s). Backup at {backup}")
        for k, v in updated.items():
            print(f" - {k}: {v}")
    except Exception as exc:
        print("Failed to write updated database.py:", exc)
        traceback.print_exc()


if __name__ == "__main__":
    main()
