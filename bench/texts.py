"""20 real short texts for B2 (varied languages/lengths). Salt per run to defeat prefix caching."""

import uuid

TEXTS = [
    "The quick brown fox jumps over the lazy dog.",
    "Der schnelle braune Fuchs springt über den faulen Hund.",
    "Le renard brun rapide saute par-dessus le chien paresseux.",
    "El rápido zorro marrón salta sobre el perro perezoso.",
    "Быстрая коричневая лиса перепрыгивает через ленивую собаку.",
    "Il volpe marrone salta velocemente sopra il cane pigro.",
    "De snelle bruine vos springt over de luie hond.",
    "A raposa marrom rápida pula sobre o cachorro preguiçoso.",
    "Szybki brązowy lis przeskakuje nad leniwym psem.",
    "Rävens snabba språng över den lata hunden förvånade alla.",
    "Kiire pruun rebane hüppab üle laisa koera.",
    "Nopea ruskea kettu hyppää laiskan koiran yli.",
    "Rychlá hnědá liška skáče přes líného psa.",
    "Gyors barna róka ugrik át a lusta kutyán.",
    "Vulpea maro rapidă sare peste câinele leneș.",
    "Hızlı kahverengi tilki tembel köpeğin üzerinden atlar.",
    "Швидка коричнева лисиця перестрибує через ледачого пса.",
    "Brzi smeđi lisac skače preko lijenog psa.",
    "Ang mabilis na kayumangging soro ay tumalon sa tamad na aso.",
    "Con cáo nâu nhanh nhẹn nhảy qua con chó lười biếng.",
]


def salted(text: str) -> str:
    return f"{text} [{uuid.uuid4().hex[:8]}]"
