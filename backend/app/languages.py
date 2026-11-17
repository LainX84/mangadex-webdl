"""MangaDex language identifiers with readable names and default folder codes.

Folder codes are user-configurable in the Settings page; these are only defaults
(ISO 639-2/B style three-letter codes, with a region suffix where MangaDex uses one).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

# (mangadex code, readable name, default folder code)
MANGADEX_LANGUAGES: list[tuple[str, str, str]] = [
    ("en", "English", "ENG"),
    ("it", "Italian", "ITA"),
    ("ja", "Japanese", "JPN"),
    ("ja-ro", "Japanese (Romanized)", "JPN-RO"),
    ("es", "Spanish (Spain)", "SPA"),
    ("es-la", "Spanish (Latin America)", "SPA-LA"),
    ("fr", "French", "FRE"),
    ("de", "German", "GER"),
    ("pt", "Portuguese (Portugal)", "POR"),
    ("pt-br", "Portuguese (Brazil)", "POR-BR"),
    ("ru", "Russian", "RUS"),
    ("pl", "Polish", "POL"),
    ("tr", "Turkish", "TUR"),
    ("ar", "Arabic", "ARA"),
    ("id", "Indonesian", "IND"),
    ("vi", "Vietnamese", "VIE"),
    ("th", "Thai", "THA"),
    ("ko", "Korean", "KOR"),
    ("ko-ro", "Korean (Romanized)", "KOR-RO"),
    ("zh", "Chinese (Simplified)", "CHI"),
    ("zh-hk", "Chinese (Traditional)", "CHI-HK"),
    ("zh-ro", "Chinese (Romanized)", "CHI-RO"),
    ("uk", "Ukrainian", "UKR"),
    ("cs", "Czech", "CZE"),
    ("hu", "Hungarian", "HUN"),
    ("ro", "Romanian", "RUM"),
    ("nl", "Dutch", "DUT"),
    ("sv", "Swedish", "SWE"),
    ("no", "Norwegian", "NOR"),
    ("da", "Danish", "DAN"),
    ("fi", "Finnish", "FIN"),
    ("el", "Greek", "GRE"),
    ("he", "Hebrew", "HEB"),
    ("hi", "Hindi", "HIN"),
    ("bn", "Bengali", "BEN"),
    ("fa", "Persian", "PER"),
    ("ms", "Malay", "MAY"),
    ("tl", "Filipino", "FIL"),
    ("bg", "Bulgarian", "BUL"),
    ("ca", "Catalan", "CAT"),
    ("hr", "Croatian", "HRV"),
    ("sr", "Serbian", "SRP"),
    ("lt", "Lithuanian", "LIT"),
    ("et", "Estonian", "EST"),
    ("mn", "Mongolian", "MON"),
    ("my", "Burmese", "BUR"),
    ("ne", "Nepali", "NEP"),
    ("kk", "Kazakh", "KAZ"),
    ("ta", "Tamil", "TAM"),
    ("te", "Telugu", "TEL"),
    ("ur", "Urdu", "URD"),
    ("uz", "Uzbek", "UZB"),
    ("az", "Azerbaijani", "AZE"),
    ("be", "Belarusian", "BEL"),
    ("ka", "Georgian", "GEO"),
    ("la", "Latin", "LAT"),
    ("eo", "Esperanto", "EPO"),
    ("sk", "Slovak", "SLO"),
    ("sl", "Slovenian", "SLV"),
    ("af", "Afrikaans", "AFR"),
    ("sq", "Albanian", "ALB"),
    ("ga", "Irish", "GLE"),
    ("jv", "Javanese", "JAV"),
    ("km", "Khmer", "KHM"),
    ("lo", "Lao", "LAO"),
    ("lv", "Latvian", "LAV"),
    ("mr", "Marathi", "MAR"),
    ("sw", "Swahili", "SWA"),
    ("ml", "Malayalam", "MAL"),
    ("kn", "Kannada", "KAN"),
    ("gu", "Gujarati", "GUJ"),
    ("pa", "Punjabi", "PAN"),
    ("si", "Sinhala", "SIN"),
    ("is", "Icelandic", "ICE"),
    ("mk", "Macedonian", "MAC"),
    ("hy", "Armenian", "ARM"),
    ("eu", "Basque", "BAQ"),
    ("gl", "Galician", "GLG"),
    ("cy", "Welsh", "WEL"),
    ("bs", "Bosnian", "BOS"),
    ("tg", "Tajik", "TGK"),
    ("ky", "Kyrgyz", "KIR"),
    ("tk", "Turkmen", "TUK"),
    ("am", "Amharic", "AMH"),
    ("ps", "Pashto", "PUS"),
    ("ku", "Kurdish", "KUR"),
    ("so", "Somali", "SOM"),
    ("yo", "Yoruba", "YOR"),
    ("ha", "Hausa", "HAU"),
    ("ig", "Igbo", "IBO"),
    ("zu", "Zulu", "ZUL"),
    ("xh", "Xhosa", "XHO"),
    ("mt", "Maltese", "MLT"),
    ("lb", "Luxembourgish", "LTZ"),
    ("fo", "Faroese", "FAO"),
    ("mg", "Malagasy", "MLG"),
    ("mi", "Maori", "MAO"),
    ("sm", "Samoan", "SMO"),
    ("to", "Tongan", "TON"),
    ("ceb", "Cebuano", "CEB"),
    ("haw", "Hawaiian", "HAW"),
    ("sa", "Sanskrit", "SAN"),
    ("bo", "Tibetan", "TIB"),
    ("dz", "Dzongkha", "DZO"),
]

LANGUAGE_NAMES = {code: name for code, name, _ in MANGADEX_LANGUAGES}


def seed_languages(session: Session) -> int:
    """Insert missing languages. Never changes user-edited folder codes."""
    from app.models import Language

    existing = {row.code: row for row in session.scalars(select(Language))}
    used_codes = {row.folder_code.upper() for row in existing.values()}
    added = 0
    for code, name, folder in MANGADEX_LANGUAGES:
        if code in existing:
            continue
        candidate = folder
        if candidate.upper() in used_codes:
            candidate = code.upper()
        used_codes.add(candidate.upper())
        session.add(Language(code=code, name=name, folder_code=candidate))
        added += 1
    return added


def ensure_language(session: Session, code: str) -> None:
    """Make sure an unknown language code reported by MangaDex can be stored."""
    from app.models import Language

    if session.get(Language, code) is not None:
        return
    folder = code.upper()
    taken = set(session.scalars(select(Language.folder_code)))
    while folder in taken:
        folder += "_"
    session.add(Language(code=code, name=LANGUAGE_NAMES.get(code, code), folder_code=folder))
    session.flush()
