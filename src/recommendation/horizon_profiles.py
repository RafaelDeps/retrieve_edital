"""
Perfis dos professores a partir do Horizon ETL (spec 012).

O Horizon ETL (ifesserra-lab/horizon_etl) publica toda semana um zip público com
os pesquisadores do IFES. Daqui saem os professores do campus configurado, com:
- a titulação (a maior formação CONCLUÍDA, nunca suposta);
- o texto do perfil (o que é comparado com os editais);
- os itens (projetos, artigos, grupos…) que explicam cada indicação.

Uma cópia reduzida, só com o que é usado, fica em `registry/` e serve de reserva
quando o download falha.
"""

import hashlib
import io
import json
import logging
import os
import tempfile
import unicodedata
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

HORIZON_EXPORT_URL = (
    "https://github.com/ifesserra-lab/horizon_etl/raw/main/data/exports/exports_canonical.zip"
)
HORIZON_URL_ENV = "HORIZON_EXPORT_URL"
RESEARCHERS_MEMBER = "researchers_only_canonical.json"
# As tags (áreas do conhecimento) e descrições ficam nos arquivos de projetos e de
# grupos; o registro do pesquisador só traz os nomes (pedido do responsável,
# 27/11/2026). Na Serra, os grupos dão tags a 93 de 93 professores.
INITIATIVES_MEMBER = "initiatives_canonical.json"
GROUPS_MEMBER = "research_groups_canonical.json"
MAX_TAGS = 60
DESCRIPTION_CHARS = 250
CAMPUS_ENV = "RECOMENDACAO_CAMPUS"
DEFAULT_CAMPUS = "Serra"
RESERVE_FILENAME = "horizon_researchers_serra.json"
DOWNLOAD_TIMEOUT_SEC = 120

PROFILE_MAX_CHARS = 6000
MAX_PROJECTS = 20
MAX_ARTICLES = 20
MAX_ADVISORSHIPS = 10

ORIGIN_HORIZON = "horizon"
ORIGIN_RESERVE = "reserva"
ORIGIN_NONE = "nenhuma"

# Mesma ordem da spec 011 (`eligibility_rules.TITULACOES`).
DEGREE_ORDER = ("ensino_medio", "graduacao", "especializacao", "mestrado", "doutorado")
DEGREE_MAP = {
    "doutorado": "doutorado",
    "mestrado": "mestrado",
    "mestrado profissional": "mestrado",
    "especializacao": "especializacao",
    "aperfeicoamento": "especializacao",
    "graduacao": "graduacao",
    "ensino medio (2o grau)": "ensino_medio",
    "ensino medio": "ensino_medio",
    "curso tecnico/profissionalizante": "ensino_medio",
}


@dataclass
class Professor:
    nome: str
    campus: str
    titulacao: Optional[str]
    curso_titulacao: str
    tem_lattes: bool
    itens: List[str] = field(default_factory=list)
    texto: str = ""
    hash: str = ""
    temas: List[str] = field(default_factory=list)


def _plain(text: Any) -> str:
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(plain.lower().split())


def degree_rank(titulacao: Optional[str]) -> int:
    return DEGREE_ORDER.index(titulacao) if titulacao in DEGREE_ORDER else -1


def degree_of(education: List[Dict[str, Any]]) -> Tuple[Optional[str], str]:
    """(titulação, curso) da maior formação concluída; (None, "") sem nenhuma."""
    best, course = None, ""
    for entry in education or []:
        if not entry.get("end_year"):
            continue  # em andamento não conta
        titulacao = DEGREE_MAP.get(_plain(entry.get("degree")))
        if titulacao and degree_rank(titulacao) > degree_rank(best):
            best, course = titulacao, clean_course(entry.get("course_name") or "")
    return best, course


def fix_text(value: Any) -> str:
    """
    Espaços colapsados e mojibake desfeito ("InovaÃ§Ã£o" → "Inovação"), que
    aparece em parte dos títulos do Horizon (prévia real de 27/11/2026).
    """
    text = " ".join(str(value or "").split())
    if "Ã" in text or "Â" in text:
        try:
            text = text.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return text


COURSE_PREFIXES = ("curso de ", "pos-graduacao em ", "programa de pos-graduacao em ", "doutorado em ",
                   "mestrado em ", "doctorado en ", "mestrado profissional em ")


def clean_course(course: str) -> str:
    """ "Doutorado em Educação" vira "Educação": o nível já vai no motivo."""
    text = fix_text(course)
    changed = True
    while changed:
        changed = False
        for prefix in COURSE_PREFIXES:
            if _plain(text).startswith(prefix):
                text = text[len(prefix):].strip()
                changed = True
    return text


def _names(items: Any, key: str = "name", limit: Optional[int] = None) -> List[str]:
    names = []
    for item in items or []:
        value = fix_text((item.get(key) if isinstance(item, dict) else item) or "")
        if value and value not in names:
            names.append(value)
    return names[:limit] if limit else names


def _by_id(items: Any) -> Dict[Any, dict]:
    return {item.get("id"): item for item in items or [] if isinstance(item, dict) and item.get("id") is not None}


def _tags_of(entry: Optional[dict]) -> List[str]:
    return _names((entry or {}).get("knowledge_areas"))


def build_professor(
    record: Dict[str, Any],
    initiatives: Optional[Dict[Any, dict]] = None,
    groups: Optional[Dict[Any, dict]] = None,
) -> Professor:
    """
    Perfil com as TAGS como base: áreas do professor, dos projetos e dos grupos,
    das mais frequentes para as menos. Depois, descrições de projetos e grupos,
    resumo, formação e títulos.
    """
    initiatives = initiatives or {}
    groups = groups or {}
    titulacao, curso = degree_of(record.get("academic_education") or [])
    articles = sorted(record.get("articles") or [], key=lambda a: a.get("year") or 0, reverse=True)
    projetos = _names(record.get("initiatives"), limit=MAX_PROJECTS)
    artigos = _names(articles, key="title", limit=MAX_ARTICLES)
    grupos = _names(record.get("research_groups"))
    orientacoes = _names(record.get("advisorships"), limit=MAX_ADVISORSHIPS)
    cursos = [clean_course(c) for c in _names(record.get("academic_education"), key="course_name")]
    resumo = fix_text(record.get("resume"))

    linked_projects = [initiatives.get(i.get("id")) for i in record.get("initiatives") or [] if isinstance(i, dict)]
    linked_groups = [groups.get(g.get("id")) for g in record.get("research_groups") or [] if isinstance(g, dict)]
    counts: Dict[str, int] = {}
    order: List[str] = []
    for tag in (
        _names(record.get("knowledge_areas"))
        + [t for entry in linked_groups for t in _tags_of(entry)]
        + [t for entry in linked_projects for t in _tags_of(entry)]
    ):
        key = _plain(tag)
        if key not in counts:
            order.append(tag)
            counts[key] = 0
        counts[key] += 1
    temas = sorted(order, key=lambda t: -counts[_plain(t)])[:MAX_TAGS]

    def short(entry: Optional[dict]) -> str:
        desc = fix_text((entry or {}).get("description"))
        return desc[:DESCRIPTION_CHARS]

    descricoes = [d for d in (short(e) for e in linked_groups + linked_projects[:MAX_PROJECTS]) if d]

    blocks = [
        ("Temas", temas),
        ("Resumo", [resumo] if resumo else []),
        ("Formação", cursos),
        ("Grupos de pesquisa", grupos),
        ("Projetos", projetos),
        ("Descrições", descricoes),
        ("Artigos", artigos),
        ("Orientações", orientacoes),
    ]
    texto = "\n".join(f"{label}: {'; '.join(values)}" for label, values in blocks if values)
    texto = texto[:PROFILE_MAX_CHARS]
    return Professor(
        nome=fix_text(record.get("name")),
        campus=((record.get("campus") or {}).get("name") or "").strip(),
        titulacao=titulacao,
        curso_titulacao=curso,
        tem_lattes=bool(resumo or record.get("academic_education")),
        itens=projetos + artigos + grupos + orientacoes,
        texto=texto,
        hash=hashlib.sha256(texto.encode("utf-8")).hexdigest(),
        temas=temas,
    )


def campus_name() -> str:
    return (os.getenv(CAMPUS_ENV) or DEFAULT_CAMPUS).strip() or DEFAULT_CAMPUS


def professors_from_records(
    records: List[Dict[str, Any]],
    campus: str,
    initiatives: Optional[List[dict]] = None,
    groups: Optional[List[dict]] = None,
) -> List[Professor]:
    wanted = _plain(campus)
    by_initiative, by_group = _by_id(initiatives), _by_id(groups)
    professors = [
        build_professor(record, by_initiative, by_group)
        for record in records or []
        if isinstance(record, dict) and _plain((record.get("campus") or {}).get("name")) == wanted
    ]
    return [p for p in professors if p.nome and p.texto]


def download_researchers(
    url: Optional[str] = None, session=None, timeout: int = DOWNLOAD_TIMEOUT_SEC
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Baixa o zip do Horizon: {"researchers", "initiatives", "groups"}. Projetos e
    grupos são opcionais (sem eles, o perfil fica sem as tags deles).
    """
    url = url or os.getenv(HORIZON_URL_ENV) or HORIZON_EXPORT_URL
    response = (session or requests).get(url, timeout=timeout)
    response.raise_for_status()
    result: Dict[str, List[Dict[str, Any]]] = {}
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = set(archive.namelist())
        for key, member in (("researchers", RESEARCHERS_MEMBER), ("initiatives", INITIATIVES_MEMBER),
                            ("groups", GROUPS_MEMBER)):
            if member not in names:
                if key == "researchers":
                    raise KeyError(member)
                logger.warning("Horizon sem %s: perfis sem essas tags.", member)
                result[key] = []
                continue
            with archive.open(member) as handle:
                data = json.load(handle)
            if not isinstance(data, list):
                raise ValueError(f"formato inesperado em {member}")
            result[key] = data
    return result


def _save_reserve(path: Path, professors: List[Professor], campus: str, today: date) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "baixado_em": today.isoformat(),
        "campus": campus,
        "professores": [asdict(p) for p in professors],
    }
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8", suffix=".tmp") as tmp:
        json.dump(payload, tmp, ensure_ascii=False, indent=1)
    os.replace(tmp.name, path)


def _load_reserve(path: Path, campus: str) -> List[Professor]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if _plain(payload.get("campus")) != _plain(campus):
        return []
    return [Professor(**p) for p in payload.get("professores") or []]


def load_professors(
    registry_dir: str = "registry",
    today: Optional[date] = None,
    downloader=download_researchers,
    campus: Optional[str] = None,
) -> Tuple[List[Professor], str]:
    """
    (professores, origem). Download bom: grava a reserva. Falha: usa a reserva
    da semana anterior; sem ela, devolve lista vazia com origem "nenhuma".
    """
    campus = campus or campus_name()
    reserve = Path(registry_dir) / RESERVE_FILENAME
    try:
        data = downloader()
        if isinstance(data, list):  # só os pesquisadores
            data = {"researchers": data}
        professors = professors_from_records(
            data.get("researchers") or [], campus, data.get("initiatives"), data.get("groups")
        )
        if not professors:
            raise ValueError(f"nenhum professor do campus {campus} no arquivo do Horizon")
    except (requests.RequestException, zipfile.BadZipFile, KeyError, ValueError) as exc:
        logger.error("Horizon ETL indisponível (%s): %s", type(exc).__name__, exc)
        professors = _load_reserve(reserve, campus)
        if professors:
            logger.warning("Usando a cópia da semana anterior: %s professores.", len(professors))
            return professors, ORIGIN_RESERVE
        return [], ORIGIN_NONE
    _save_reserve(reserve, professors, campus, today or date.today())
    return professors, ORIGIN_HORIZON
