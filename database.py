import unicodedata


def _strip_accents(s: str) -> str:
    if not s:
        return s
    normalized = unicodedata.normalize("NFD", s)
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")


def _expand_keyword_variants(kw: str):
    """Return list of variants for a keyword:
    - original lowercased
    - accent-stripped
    - singular (remove final 's') if present
    - accent-stripped singular
    Preserves order and removes duplicates.
    """
    kw = (kw or "").strip()
    lower = kw.lower()
    no_acc = _strip_accents(lower)
    variants = [lower]
    if no_acc != lower:
        variants.append(no_acc)
    if lower.endswith("s") and len(lower) > 1:
        singular = lower[:-1].strip()
        if singular:
            variants.append(singular)
        if no_acc.endswith("s") and len(no_acc) > 1:
            singular_no_acc = no_acc[:-1].strip()
            if singular_no_acc:
                variants.append(singular_no_acc)

    seen = set()
    unique = []
    for v in variants:
        if v and v not in seen:
            seen.add(v)
            unique.append(v)
    return unique


formations_db = {
    "Gestionnaire Parapharmacie & Dermo-Conseiller": {
        "keywords": [
            "gestionnaire parapharmacie",
            "dermo-conseiller",
            "dermo-cosmétologie",
            "produits cosmétiques",
            "conseil cosmétique",
        ],
        "media_id": None,  # WhatsApp media ID (to be set after uploading OGG)
        "local_media": "media/parapharmacie.ogg",  # local filename for the OGG provided
        "registration_link": "https://supmedical.ma/formations/formation-gestionnaire-parapharmacie-et-dermo-conseiller-e-en-produits-cosmetiques/",
    },
    "Vente & Conseil Dispositifs Médicaux": {
        "keywords": [
            "dispositifs médicaux",
            "produits médicaux",
            "vente",
            "conseil",
            "réglementation dispositifs médicaux",
        ],
        "media_id": None,
        "local_media": "media/dispositifs_medicaux.ogg",
        "registration_link": "https://supmedical.ma/formations/formation-parapharamcie-en-vente-et-conseil-des-dispositifs-medicaux-produits-medicaux/",
    },
    "Conseiller en Nutrition & Bien-être": {
        "keywords": [
            "nutrition",
            "bien-être",
            "conseil nutritionnel",
            "compléments alimentaires",
            "bilan nutritionnel",
            "diététique",
        ],
        "media_id": None,
        "local_media": "media/nutrition.ogg",
        "registration_link": "https://supmedical.ma/formations/conseiller-en-nutrition-bien-etre/",
    },
    "Vente & Conseil Compléments Alimentaires": {
        "keywords": [
            "compléments alimentaires",
            "vente compléments",
            "conseil compléments",
            "réglementation compléments",
            "étiquetage",
        ],
        "media_id": None,
        "local_media": "media/complements_alimentaires.ogg",
        "registration_link": "https://supmedical.ma/formations/formation-vente-et-conseil-des-complements-alimentaires/",
    },
}

# --- Expand keywords to include accentless and singular variants ---
for _name, _info in formations_db.items():
    orig = _info.get("keywords", []) or []
    expanded = []
    for k in orig:
        expanded.extend(_expand_keyword_variants(k))
    # deduplicate preserving order
    _info["keywords"] = list(dict.fromkeys(expanded))

DEFAULT_GREETING = "Bonjour, merci de contacter ISUPMEDICAL ACADEMY. Comment puis-je vous aider ?"

__all__ = ["formations_db", "DEFAULT_GREETING"]
