"""Utilities de matching intelligent pour les formations Supmedical.

Règles de score:
- score > 85: match direct
- 55 <= score <= 85: suggestion
- score < 55: menu guidé
"""

from __future__ import annotations

import re
import unicodedata
from typing import Dict, Tuple, Optional, List

try:
    from rapidfuzz import fuzz, process
    RAPIDFUZZ_AVAILABLE = True
except Exception:
    RAPIDFUZZ_AVAILABLE = False


DIRECT_THRESHOLD = 85
SUGGEST_THRESHOLD = 55


def strip_accents(text: str) -> str:
    if not text:
        return ""
    normalized = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")


def normalize_text(text: str) -> str:
    """Minuscule + suppression accents + nettoyage espaces/ponctuation."""
    value = (text or "").lower().strip()
    value = strip_accents(value)
    value = re.sub(r"[^a-z0-9\s&-]", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _build_candidates(formations_db: Dict[str, Dict]) -> Dict[str, str]:
    """Map: candidate_normalized -> formation_name."""
    candidates: Dict[str, str] = {}
    for formation_name, info in formations_db.items():
        normalized_name = normalize_text(formation_name)
        if normalized_name:
            candidates[normalized_name] = formation_name

        for kw in info.get("keywords", []) or []:
            normalized_kw = normalize_text(kw)
            if normalized_kw:
                candidates[normalized_kw] = formation_name
    return candidates


def _alnum_tokens(text: str) -> List[str]:
    return re.findall(r"[a-z0-9]+", text or "")


def _keyword_tokens_contained(query: str, candidate: str) -> bool:
    """True si tous les tokens du candidat sont presents dans la phrase query.

    Exemple:
    - candidate: "gestionnaire parapharmacie"
    - query: "je veux une formation gestionnaire en parapharmacie"
    """
    query_tokens = set(_alnum_tokens(query))
    candidate_tokens = _alnum_tokens(candidate)
    if not candidate_tokens:
        return False
    return all(token in query_tokens for token in candidate_tokens)


def match_formation(message: str, formations_db: Dict[str, Dict]) -> Tuple[str, Optional[str], int]:
    """Retourne (mode, formation_name, score)."""
    query = normalize_text(message)
    if not query:
        return "menu", None, 0

    candidates = _build_candidates(formations_db)
    if not candidates:
        return "menu", None, 0

    # Exact/contains rapide => match direct
    for candidate, formation_name in candidates.items():
        if query == candidate or candidate in query or query in candidate:
            return "direct", formation_name, 100
        if _keyword_tokens_contained(query, candidate):
            return "direct", formation_name, 96

    # Fallback fuzzy
    if not RAPIDFUZZ_AVAILABLE:
        return "menu", None, 0

    choice_list = list(candidates.keys())
    matched = process.extractOne(query, choice_list, scorer=fuzz.token_set_ratio)
    if not matched:
        return "menu", None, 0

    best_candidate, score, _ = matched
    formation_name = candidates.get(best_candidate)
    score_int = int(score)

    if score_int > DIRECT_THRESHOLD:
        return "direct", formation_name, score_int
    if score_int >= SUGGEST_THRESHOLD:
        return "suggestion", formation_name, score_int
    return "menu", None, score_int


_EMOJI_NUMBERS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]


def _extract_simple_keywords(info: Dict, max_count: int = 2) -> List[str]:
    picked: List[str] = []
    seen = set()
    for kw in info.get("keywords", []) or []:
        cleaned = (kw or "").strip()
        if not cleaned:
            continue
        # Un "mot simple" = un seul token alphanumerique
        token_count = len(_alnum_tokens(normalize_text(cleaned)))
        if token_count != 1:
            continue
        key = normalize_text(cleaned)
        if key in seen:
            continue
        seen.add(key)
        picked.append(cleaned)
        if len(picked) >= max_count:
            break
    return picked


def format_formations_menu(formations_db: Dict[str, Dict]) -> str:
    lines = [
        "📚 Voici nos formations disponibles :",
    ]
    for idx, (formation_name, info) in enumerate(formations_db.items(), start=1):
        number = _EMOJI_NUMBERS[idx - 1] if idx - 1 < len(_EMOJI_NUMBERS) else f"{idx}."
        lines.append(f"{number} 🎓 {formation_name}")
        simple_kws = _extract_simple_keywords(info)
        if simple_kws:
            lines.append(f"   ↳ mot-clé: {', '.join(simple_kws)}")

    lines.append("✍️ Répondez avec un numéro (1, 2, 3, 4) ou juste un mot-clé.")
    lines.append("Exemples: parapharmacie, dispositifs, nutrition, compléments.")
    return "\n".join(lines)


def parse_menu_selection(message: str, formations_db: Dict[str, Dict]) -> Optional[str]:
    """Permet de choisir une formation via numero (1-9) ou emoji (1️⃣-9️⃣)."""
    raw = (message or "").strip()
    if not raw:
        return None

    formations = list(formations_db.keys())
    if not formations:
        return None

    number_to_formation = {
        str(idx): name for idx, name in enumerate(formations, start=1)
    }

    # Emojis "1️⃣ 2️⃣ ..."
    for idx, emoji in enumerate(_EMOJI_NUMBERS, start=1):
        if emoji in raw and str(idx) in number_to_formation:
            return number_to_formation[str(idx)]

    normalized = normalize_text(raw)

    # Message exactement "1" / "2" / ...
    if normalized in number_to_formation:
        return number_to_formation[normalized]

    # Message contenant un nombre: "je choisis 2"
    hits = re.findall(r"\b([1-9])\b", normalized)
    if hits and hits[0] in number_to_formation:
        return number_to_formation[hits[0]]

    return None


def looks_like_menu_request(message: str) -> bool:
    query = normalize_text(message)
    triggers = {
        "formation",
        "formations",
        "liste",
        "menu",
        "programme",
        "programmes",
    }
    return any(token in query.split() for token in triggers)
