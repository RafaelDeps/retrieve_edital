"""
Escolha do documento que a IA lê (spec 010, RafaelDeps/retrieve_edital#9).

As páginas da CAPES listam os anexos do mais novo para o mais antigo, então a
"Alteração Edital nº 11" aparece antes do "Edital nº 11/2026". O seletor antigo
dava a mesma nota aos dois e ficava com o primeiro, a alteração, que muitas vezes
só diz "altera o item 5.2". Termos de outorga e portarias também passavam.

Aqui cada anexo recebe um papel (edital, alteração ou apoio) e a escolha devolve
o original e as alterações em ordem cronológica. Os textos vão juntos para a IA,
numa chamada só, com cabeçalhos que dizem o que é cada parte.
"""

import os
import re
import unicodedata
from typing import Dict, List, Optional, Sequence, Tuple

ROLE_EDITAL = "edital"
ROLE_ALTERACAO = "alteração"
ROLE_APOIO = "apoio"

# Precedência: apoio → alteração → edital → apoio. "Resultado Final do Edital nº 16"
# é apoio, e "Edital rerratificado" é alteração, mesmo tendo "edital" no título.
SUPPORT_TOKENS = (
    "anexo",
    "resultado",
    "aviso",
    "termo",
    "portaria",
    "manual",
    "faq",
    "perguntas",
    "modelo",
    "formulario",
    "declaracao",
    "carta",
    "diretriz",
    "ranqueamento",
    "decisao",
    "flyer",
    "apresentacao",
    "cartao",
    "inscritos",
    "listagem",
)
# "Ato nº 3/2026" (Aurora) é ato administrativo, não edital, mesmo com o arquivo
# chamado edital_n__3.pdf. Palavra inteira, para não casar com "contrato".
SUPPORT_PATTERNS = (re.compile(r"\bato n"),)
AMENDMENT_TOKENS = (
    "alteracao",
    "retificacao",
    "rerratificacao",
    "rerratificado",
    "aditivo",
    "errata",
)
EDITAL_TOKENS = ("edital", "chamada", "regulamento", "selecao publica")

MAX_AMENDMENTS_ENV = "MAX_ALTERACOES_POR_EDITAL"
DEFAULT_MAX_AMENDMENTS = 3
# Cerca de 30 mil tokens: folga grande sob o limite de 250 mil por minuto do Gemini.
MAX_COMBINED_CHARS = 120_000

ORIGINAL_HEADER = "=== EDITAL ORIGINAL: {title} ==="
AMENDMENT_HEADER = "=== ALTERAÇÃO {n}: {title} ==="
PAGE_HEADER = "=== PÁGINA DO EDITAL ==="

Attachment = Dict[str, str]
Part = Tuple[str, str]  # (título, texto)


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text or "")
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(plain.lower().replace("_", " ").replace("-", " ").split())


def classify_attachment(titulo: str, link: str = "") -> str:
    """Papel do anexo pelo título e pelo nome do arquivo."""
    filename = (link or "").split("?")[0].rstrip("/").replace("/@@display-file/file", "")
    filename = filename.rsplit("/", 1)[-1]
    haystack = f"{_normalize(titulo)} {_normalize(filename)}"
    if any(token in haystack for token in SUPPORT_TOKENS) or any(
        pattern.search(haystack) for pattern in SUPPORT_PATTERNS
    ):
        return ROLE_APOIO
    if any(token in haystack for token in AMENDMENT_TOKENS):
        return ROLE_ALTERACAO
    if any(token in haystack for token in EDITAL_TOKENS):
        return ROLE_EDITAL
    return ROLE_APOIO


def max_amendments() -> int:
    raw = os.getenv(MAX_AMENDMENTS_ENV)
    try:
        value = int(raw) if raw and raw.strip() else DEFAULT_MAX_AMENDMENTS
    except ValueError:
        value = DEFAULT_MAX_AMENDMENTS
    return value if value > 0 else DEFAULT_MAX_AMENDMENTS


def select_documents(
    anexos: Sequence[Attachment],
    newest_first: bool,
    limit: Optional[int] = None,
) -> Tuple[Optional[Attachment], List[Attachment]]:
    """
    (original, alterações) em ordem cronológica.

    `newest_first` diz como a página lista os anexos: a CAPES lista do mais novo
    para o mais antigo; a FINEP, na ordem em que publicou. Sem edital, a
    alteração mais nova vira o documento lido. Só PDFs entram (o espelho em
    formato aberto da FINEP é o mesmo documento).
    """
    limit = limit or max_amendments()
    chronological = list(reversed(anexos)) if newest_first else list(anexos)
    chronological = [a for a in chronological if (a.get("tipo") or "pdf") != "formato aberto"]
    roles = [classify_attachment(a.get("titulo", ""), a.get("link", "")) for a in chronological]
    edital_positions = [i for i, role in enumerate(roles) if role == ROLE_EDITAL]
    if edital_positions:
        # O edital mais recente: a página do Buffalo traz o Edital nº 16/2025 e o
        # nº 19/2026. Só valem as alterações publicadas depois dele: a do PAEP
        # lista também as alterações do edital do ano anterior.
        latest = edital_positions[-1]
        alteracoes = [
            a for i, a in enumerate(chronological) if i > latest and roles[i] == ROLE_ALTERACAO
        ]
        return chronological[latest], alteracoes[-limit:]
    alteracoes = [a for i, a in enumerate(chronological) if roles[i] == ROLE_ALTERACAO]
    if alteracoes:
        return alteracoes[-1], []
    return None, []


def combine_texts(
    original: Optional[Part],
    alteracoes: Sequence[Part] = (),
    page_text: str = "",
    max_chars: int = MAX_COMBINED_CHARS,
) -> Tuple[str, int]:
    """
    Texto único para a IA: original, alterações (da mais antiga para a mais nova)
    e a página, com cabeçalhos. Devolve (texto, alterações usadas).

    Acima do teto saem primeiro as alterações mais antigas, depois a página; o
    original nunca sai.
    """
    amendments = [part for part in alteracoes if (part[1] or "").strip()]
    page = (page_text or "").strip()

    def build(amends: Sequence[Part], with_page: bool) -> str:
        blocks = []
        if original and (original[1] or "").strip():
            blocks.append(ORIGINAL_HEADER.format(title=original[0]) + "\n" + original[1].strip())
        first = len(amendments) - len(amends) + 1
        for offset, (title, text) in enumerate(amends):
            blocks.append(AMENDMENT_HEADER.format(n=first + offset, title=title) + "\n" + text.strip())
        if with_page and page:
            blocks.append(PAGE_HEADER + "\n" + page)
        return "\n\n".join(blocks)

    kept = list(amendments)
    with_page = True
    text = build(kept, with_page)
    while len(text) > max_chars and kept:
        kept.pop(0)
        text = build(kept, with_page)
    if len(text) > max_chars and with_page:
        with_page = False
        text = build(kept, with_page)
    return text, len(kept)
