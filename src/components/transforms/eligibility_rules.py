"""
Elegibilidade dos editais para recomendação (spec 011, RafaelDeps/retrieve_edital#12).

O portal quer recomendar editais a partir do Currículo Lattes, e o responsável quer
uma mensagem semanal dizendo quais editais servem a quais professores (spec 012).
Para isso o edital precisa dizer quem pode propor: titulação mínima, grandes áreas
do conhecimento, público e um resumo dos requisitos.

A IA só sugere. Estas regras decidem o que entra: os vocabulários são fechados,
e um valor que não casa é descartado em vez de publicado (princípios II e III).
"""

import unicodedata
from typing import Any, Dict, Iterable, List

TITULACOES = ("ensino_medio", "graduacao", "especializacao", "mestrado", "doutorado")

# As 9 grandes áreas do CNPq, na grafia publicada (a mesma do Lattes).
GRANDES_AREAS = (
    "Ciências Exatas e da Terra",
    "Ciências Biológicas",
    "Engenharias",
    "Ciências da Saúde",
    "Ciências Agrárias",
    "Ciências Sociais Aplicadas",
    "Ciências Humanas",
    "Linguística, Letras e Artes",
    "Multidisciplinar",
)

PUBLICOS = (
    "estudante_graduacao",
    "pos_graduando",
    "pesquisador",
    "docente",
    "empresa",
    "startup",
    "ict",
)

# Vocabulário antigo do público (até a spec 010) → vocabulário novo.
LEGACY_PUBLICO = {
    "pesquisador": ["pesquisador"],
    "empresa": ["empresa"],
    "ict-empresa": ["ict", "empresa"],
    "estudante": ["estudante_graduacao", "pos_graduando"],
    "internacional": [],  # continua em `ambito_geografico`
}

REQUISITOS_MAX_CHARS = 300
# Resumos que não dizem requisito nenhum (validação real de 26/11/2026): viram "".
GENERIC_SUMMARY_MARKERS = (
    "consulte o portal",
    "verificar as regras",
    "verifique as regras",
    "conforme as regras",
    "regras gerais do programa",
    "conforme as condicoes estabelecidas",
)
ELIGIBILITY_KEYS = ("titulacao_minima", "areas_conhecimento", "requisitos_resumo")

ALL_AREAS_MARKERS = ("todas as areas", "qualquer area", "todas", "all areas", "any area")

# Sinônimos curtos que a IA costuma usar, já sem acento e em minúsculas.
AREA_SYNONYMS = {
    "ciencias exatas e da terra": "Ciências Exatas e da Terra",
    "exatas": "Ciências Exatas e da Terra",
    "ciencias exatas": "Ciências Exatas e da Terra",
    "ciencias biologicas": "Ciências Biológicas",
    "biologicas": "Ciências Biológicas",
    "engenharias": "Engenharias",
    "engenharia": "Engenharias",
    "ciencias da saude": "Ciências da Saúde",
    "saude": "Ciências da Saúde",
    "ciencias agrarias": "Ciências Agrárias",
    "agrarias": "Ciências Agrárias",
    "ciencias sociais aplicadas": "Ciências Sociais Aplicadas",
    "sociais aplicadas": "Ciências Sociais Aplicadas",
    "ciencias humanas": "Ciências Humanas",
    "humanas": "Ciências Humanas",
    "linguistica, letras e artes": "Linguística, Letras e Artes",
    "linguistica letras e artes": "Linguística, Letras e Artes",
    "letras e artes": "Linguística, Letras e Artes",
    "multidisciplinar": "Multidisciplinar",
    "interdisciplinar": "Multidisciplinar",
}

TITULACAO_SYNONYMS = {
    "ensino_medio": "ensino_medio",
    "ensino medio": "ensino_medio",
    "graduacao": "graduacao",
    "graduado": "graduacao",
    "superior completo": "graduacao",
    "especializacao": "especializacao",
    "especialista": "especializacao",
    "mestrado": "mestrado",
    "mestre": "mestrado",
    "doutorado": "doutorado",
    "doutor": "doutorado",
    "doutora": "doutorado",
}


def _plain(text: Any) -> str:
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(plain.lower().split())


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def normalize_area(valor: Any) -> List[str]:
    """Grandes áreas que o valor indica; "todas as áreas" dá as 9; o resto, nada."""
    plain = _plain(valor).strip(" .;")
    if not plain:
        return []
    if plain in ALL_AREAS_MARKERS:
        return list(GRANDES_AREAS)
    official = {_plain(area): area for area in GRANDES_AREAS}
    if plain in official:
        return [official[plain]]
    if plain in AREA_SYNONYMS:
        return [AREA_SYNONYMS[plain]]
    return []


MULTIDISCIPLINAR = "Multidisciplinar"


def normalize_areas(valores: Any) -> List[str]:
    """
    Lista sem duplicatas, na ordem oficial do CNPq.

    "Multidisciplinar" sozinha vira as 9: na validação real (27/11/2026) era assim
    que a IA dizia "aceita qualquer área" (FAPES InovaJR e Infraestrutura).
    """
    found = set()
    for valor in _as_list(valores):
        found.update(normalize_area(valor))
    if found == {MULTIDISCIPLINAR}:
        return list(GRANDES_AREAS)
    return [area for area in GRANDES_AREAS if area in found]


def normalize_titulacao(valor: Any) -> str:
    plain = _plain(valor).strip(" .;")
    return TITULACAO_SYNONYMS.get(plain, "")


def normalize_publico(valores: Any) -> List[str]:
    """Só o vocabulário novo, sem duplicatas, na ordem de `PUBLICOS`."""
    found = set()
    for valor in _as_list(valores):
        plain = _plain(valor).replace("-", "_").replace(" ", "_")
        if plain in PUBLICOS:
            found.add(plain)
    return [publico for publico in PUBLICOS if publico in found]


def convert_legacy_publico(valores: Any) -> List[str]:
    """Converte o vocabulário antigo; valores já novos passam como estão."""
    converted: List[str] = []
    for valor in _as_list(valores):
        plain = _plain(valor)
        converted.extend(LEGACY_PUBLICO.get(plain, [valor]))
    return normalize_publico(converted)


def clean_summary(texto: Any, max_chars: int = REQUISITOS_MAX_CHARS) -> str:
    """Espaços colapsados; acima do limite, cortado no fim de uma frase."""
    text = " ".join(str(texto or "").split())
    if any(marker in _plain(text) for marker in GENERIC_SUMMARY_MARKERS):
        return ""
    if len(text) <= max_chars:
        return text
    head = text[:max_chars]
    cut = head.rfind(". ")
    if cut > max_chars // 3:
        return head[: cut + 1]
    return head[: max_chars - 1].rstrip() + "…"


def build_eligibility(data: Any) -> Dict[str, Any]:
    """
    O bloco `elegibilidade` a partir da resposta da IA. Sempre tem as três chaves:
    campo ausente ou inválido vira vazio (lido, sem requisitos reconhecíveis).
    """
    raw = data.get("elegibilidade") if isinstance(data, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    return {
        "titulacao_minima": normalize_titulacao(raw.get("titulacao_minima")),
        "areas_conhecimento": normalize_areas(raw.get("areas_conhecimento")),
        "requisitos_resumo": clean_summary(raw.get("requisitos_resumo")),
    }


def is_eligibility(value: Any) -> bool:
    """Um bloco já lido (gravado num JSON), com as três chaves."""
    return isinstance(value, dict) and all(key in value for key in ELIGIBILITY_KEYS)


def prompt_vocabulary() -> Dict[str, str]:
    """Os vocabulários já formatados para o prompt."""
    return {
        "titulacoes": ", ".join(TITULACOES),
        "areas": "; ".join(GRANDES_AREAS),
        "publicos": ", ".join(PUBLICOS),
    }


__all__ = [
    "TITULACOES",
    "GRANDES_AREAS",
    "PUBLICOS",
    "LEGACY_PUBLICO",
    "REQUISITOS_MAX_CHARS",
    "normalize_area",
    "normalize_areas",
    "normalize_titulacao",
    "normalize_publico",
    "convert_legacy_publico",
    "clean_summary",
    "build_eligibility",
    "is_eligibility",
    "prompt_vocabulary",
]

