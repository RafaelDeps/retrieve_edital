"""
Curadoria de `data/output/`: mantém o status coerente com o prazo e remove o que
não é oportunidade de fomento.

Motivação: as regras de publicação (`src/components/transforms/publication_rules.py`)
impedem que ruído novo entre, mas não corrigem o que já está gravado. Levantamento
de 2026-07-29, sobre 211 arquivos: 24 cards de anexo/alteração, 29 cascas vazias,
4 órfãos do portal descontinuado do CNPq e 63 com prazo já encerrado — apenas 47
abertos com prazo futuro.

Duas ações distintas, com riscos distintos:

- **Status**: recalculado a cada execução. Um edital coletado como `aberto` não se
  corrige sozinho quando o prazo passa, então isso precisa rodar sempre — o
  runner unificado o faz. Não remove nada.
- **Remoção**: só do que não é edital (anexo, alteração, casca vazia, órfão de
  portal morto). Editais encerrados **não** são removidos.
- **Arquivamento** (issue #11, spec 008): encerrados com prazo há mais de
  `ARCHIVE_GRACE_DAYS` dias (padrão 30) saem de `data/output/` e vão para
  `data/arquivo/`, com o mesmo nome e conteúdo. Roda em todo modo com `--apply` ou
  `--refresh-status`. `data/arquivo/` é histórico congelado: nunca é percorrido.

Uso:
    python scripts/curate_output.py                # relatório, não altera nada
    python scripts/curate_output.py --refresh-status  # corrige status e arquiva encerrados antigos
    python scripts/curate_output.py --apply        # corrige status e remove ruído
"""

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.components.transforms.publication_rules import (  # noqa: E402
    SOURCE_PROFILES,
    CLOSED_SYNONYMS,
    FALLBACK_DESCRIPTION_PREFIX,
    STATUS_CLOSED,
    STATUS_OPEN,
    canonical_category,
)

from src.components.transforms import schedule_rules  # noqa: E402
from src.components.transforms.eligibility_rules import convert_legacy_publico  # noqa: E402

OUTPUT_DIR = Path("data/output")
REGISTRY_PATH = Path("registry/processed_editais.json")
ARCHIVE_DIR = Path("data/arquivo")
ARCHIVE_GRACE_ENV = "ARCHIVE_GRACE_DAYS"
DEFAULT_ARCHIVE_GRACE_DAYS = 30

SUPPORTING_TITLE_PREFIXES = ("[ANEXO]", "[ALTERAÇÃO]", "[ALTERACAO]")
DISCONTINUED_HOSTS = ("memoria2.cnpq.br",)

# Motivos de remoção, na ordem em que são avaliados.
REASON_SUPPORTING = "documento de apoio, não é edital"
REASON_SHELL = "casca vazia: sem descrição, cronograma nem prazo"
REASON_DISCONTINUED = "aponta para portal descontinuado"


def load_editais(output_dir: Path) -> List[Tuple[Path, dict]]:
    editais = []
    for path in sorted(output_dir.glob("*.json")):
        try:
            editais.append((path, json.loads(path.read_text(encoding="utf-8"))))
        except ValueError as exc:
            print(f"  ! ilegível, mantido: {path.name} ({exc})")
    return editais


def removal_reason(edital: dict) -> str:
    """Motivo para não manter o arquivo, ou string vazia para manter."""
    nome = (edital.get("nome") or "").strip()
    if nome.startswith(SUPPORTING_TITLE_PREFIXES):
        return REASON_SUPPORTING

    link = (edital.get("link") or "").lower()
    if any(host in link for host in DISCONTINUED_HOSTS):
        return REASON_DISCONTINUED

    descricao = (edital.get("descrição") or edital.get("descricao") or "").strip()
    if (
        descricao.startswith(FALLBACK_DESCRIPTION_PREFIX)
        and not edital.get("cronograma")
        and not (edital.get("data_encerramento") or "").strip()
    ):
        return REASON_SHELL
    return ""


# Fontes relidas por inteiro a cada execução (spec 005). Nelas a própria
# releitura reabre uma chamada prorrogada, e o `encerrado` gravado vem da origem
# (a chamada saiu da lista); o prazo gravado pode estar desatualizado justamente
# por isso, então o realinhamento não reabre essas chamadas.
SOURCES_REFRESHED_EACH_RUN = frozenset({"horizon"})


def expected_status(edital: dict, today: date) -> str:
    """
    Mesma regra de `publication_rules.resolve_status`, aplicada ao JSON gravado.

    Prazo vencido vira `encerrado`; prazo futuro vira `aberto`, inclusive quando
    o arquivo estava marcado como encerrado e o prazo foi prorrogado — exceto nas
    fontes relidas a cada execução, em que só a releitura reabre.
    """
    current = (edital.get("status") or "").strip().lower()
    deadline = (edital.get("data_encerramento") or "").strip()
    keeps_closed = (
        current in CLOSED_SYNONYMS
        and edital.get("fonte_key") in SOURCES_REFRESHED_EACH_RUN
    )
    if deadline:
        try:
            if date.fromisoformat(deadline) < today:
                return STATUS_CLOSED
            return STATUS_CLOSED if keeps_closed else STATUS_OPEN
        except ValueError:
            pass
    return STATUS_CLOSED if current in CLOSED_SYNONYMS else STATUS_OPEN


# Host de origem -> órgão de fomento, para preencher `orgão_fomento` vazio.
# Um campo em branco aparece como lacuna no portal; o host do link é a evidência
# mais confiável disponível depois do fato.
HOST_AGENCIES = (
    ("fapes.es.gov.br", "FAPES"),
    ("finep.gov.br", "FINEP"),
    ("portal.conif.org.br", "CONIF"),
    ("sigpesq.ifes.edu.br", "PRPPG/IFES"),
    ("proex.ifes.edu.br", "PROEX/IFES"),
    ("gov.br/capes", "CAPES"),
    ("gov.br/cnpq", "CNPq"),
    ("ec.europa.eu", "Horizon Europe"),
)


KNOWN_AGENCIES = tuple(agency for _, agency in HOST_AGENCIES)


def expected_agency(edital: dict) -> str:
    """
    Órgão de fomento reduzido à fonte monitorada.

    O campo responde "de qual fonte este edital veio", e serve de filtro no
    portal. Valores compostos como `CAPES/SENAD` ou
    `CAPES E MINISTÉRIO DA IGUALDADE RACIAL (MIR)` fragmentavam o filtro em
    entradas de um item cada; os cofinanciadores continuam no título e na
    descrição. São resquícios de quando o órgão vinha da extração do PDF — o
    normalizer hoje sempre usa o órgão da fonte.

    O host do link é a evidência mais confiável depois do fato.
    """
    current = (edital.get("orgão_fomento") or "").strip()
    if current.upper() in {agency.upper() for agency in KNOWN_AGENCIES}:
        return current

    link = (edital.get("link") or "").lower()
    for host, agency in HOST_AGENCIES:
        if host in link:
            return agency

    # Sem host reconhecido, tenta o primeiro órgão citado no valor composto.
    for separator in ("/", " E ", " e ", ","):
        if separator in current:
            head = current.split(separator)[0].strip()
            if head.upper() in {agency.upper() for agency in KNOWN_AGENCIES}:
                return head
    return current


# `data_abertura` sem data real recebia 1º de janeiro do ano corrente, e o portal
# exibia isso como se fosse informação da fonte.
PLACEHOLDER_OPENING_REGEX = re.compile(r"\d{4}-01-01")


def expected_opening_date(edital: dict) -> str:
    """
    Data de abertura, ou vazio quando o valor é o antigo placeholder.

    O placeholder é reconhecido por ser 1º de janeiro **sem nenhuma etapa do
    cronograma que o confirme**. Um edital que realmente abra em 1º de janeiro e
    tenha isso no cronograma é preservado.
    """
    opening = (edital.get("data_abertura") or "").strip()
    if not PLACEHOLDER_OPENING_REGEX.fullmatch(opening):
        return opening
    for item in edital.get("cronograma") or []:
        if (item.get("data") or "").strip() == opening:
            return opening
    return ""


def _closing_from_loose_start(edital: dict) -> bool:
    """
    O prazo gravado é a data de uma etapa só de início, sem intervalo.

    Era o plano B antigo da derivação: sem etapa de encerramento, a data de
    "Início da vigência dos projetos" virava prazo. Não é prazo (Princípio II).
    """
    deadline = (edital.get("data_encerramento") or "").strip()
    if not deadline:
        return False
    return any(
        schedule_rules.is_start_event(event)
        and not schedule_rules._is_interval(event.get("data"))
        and deadline in schedule_rules._all_dates(event.get("data"))
        for event in edital.get("cronograma") or []
    )


def expected_schedule_fields(edital: dict, today: date) -> dict:
    """
    Prazo, abertura, chamadas e próxima etapa recalculados do cronograma salvo.

    Cada edital é processado uma vez; sem este recálculo, o prazo de uma chamada
    que já passou ficaria gravado e o edital seria marcado encerrado com a próxima
    chamada ainda por vir. Devolve só os campos que mudam. Sem etapas de
    encerramento no cronograma, o prazo atual é mantido (pode ter vindo da página
    da fonte), exceto quando ele é a data de uma etapa solta de início.
    """
    events = edital.get("cronograma") or []
    derived = schedule_rules.derive_schedule_fields(events, today)
    # Campos novos do contrato: sempre recalculados (não dependem de prazo declarado).
    wanted: dict = {
        "chamadas": derived["chamadas"],
        "proxima_etapa": derived["proxima_etapa"],
    }
    if schedule_rules.end_event_candidates(events):
        wanted["data_encerramento"] = derived["data_encerramento"]
        wanted["data_abertura"] = derived["data_abertura"]
    elif _closing_from_loose_start(edital):
        wanted["data_encerramento"] = ""
    return {
        campo: valor
        for campo, valor in wanted.items()
        if campo not in edital or edital[campo] != valor
    }


def expected_category(edital: dict) -> str:
    """
    Categoria reduzida ao vocabulário canônico.

    Quando o valor gravado não é conclusivo, o tema é procurado no título, na
    descrição e nas tags — é o que recupera os editais que ficaram com
    `chamadas`, valor que vazava do slug da URL da FAPES.
    """
    hint = " ".join(
        [
            edital.get("nome") or "",
            edital.get("descrição") or edital.get("descricao") or "",
            " ".join(edital.get("tags") or []),
        ]
    )
    return canonical_category(edital.get("categoria"), hint)


def backfill_source_profile(edital: dict) -> dict:
    """
    Preenche `ambito_geografico` e `fonte_key` a partir da fonte, quando vazios.

    São conhecimento estático e certo — a FAPES é estadual do ES, o Horizon é
    europeu. `publico_alvo`, `valor_estimado` e `trl_exigido` **não** são
    preenchidos aqui: dependem de evidência da origem ou do texto do PDF, e
    inventá-los seria pior que deixá-los vazios. Eles chegam na recoleta.
    """
    mudancas = {}
    perfil = SOURCE_PROFILES.get((edital.get("orgão_fomento") or "").strip().upper())
    if not perfil:
        return mudancas
    ambito, fonte_key = perfil
    if ambito and not (edital.get("ambito_geografico") or "").strip():
        mudancas["ambito_geografico"] = ambito
    if fonte_key and not (edital.get("fonte_key") or "").strip():
        mudancas["fonte_key"] = fonte_key
    # Campos do schema que podem faltar nos JSONs gravados antes da adição.
    for campo, vazio in (
        ("publico_alvo", []),
        ("valor_estimado", None),
        ("trl_exigido", ""),
        ("modalidade", ""),
    ):
        if campo not in edital:
            mudancas[campo] = vazio
    return mudancas


def drop_registry_keys(registry_path: Path, keys: List[str], apply: bool) -> int:
    """
    Remove chaves do índice para que o edital possa ser recoletado.

    Usado só nas cascas vazias: são editais reais cuja extração falhou, e sem
    liberar a chave eles nunca voltariam. Anexos e órfãos mantêm a chave, para
    não serem baixados de novo à toa.
    """
    if not registry_path.exists() or not keys:
        return 0
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    targets = set(keys)
    removed = 0
    for source, entries in registry.items():
        if not isinstance(entries, list):
            continue
        kept = [entry for entry in entries if entry not in targets]
        removed += len(entries) - len(kept)
        registry[source] = kept
    if apply and removed:
        registry_path.write_text(
            json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return removed


def archive_grace_days() -> int:
    """Dias que um encerrado fica em data/output/ depois do prazo (padrão 30)."""
    raw = os.getenv(ARCHIVE_GRACE_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_ARCHIVE_GRACE_DAYS
    try:
        value = int(raw)
    except ValueError:
        value = -1
    if value < 0:
        print(f"{ARCHIVE_GRACE_ENV} inválido ({raw!r}); usando {DEFAULT_ARCHIVE_GRACE_DAYS}.")
        return DEFAULT_ARCHIVE_GRACE_DAYS
    return value


def archive_due(edital: dict, today: date, grace: int) -> bool:
    """
    Encerrado cujo prazo ficou para trás há mais de `grace` dias.

    Sem prazo (ou com prazo ilegível) não há como contar a carência: fica em
    data/output/, onde a falta de evidência continua visível.
    """
    if (edital.get("status") or "").strip().lower() != STATUS_CLOSED:
        return False
    try:
        deadline = date.fromisoformat((edital.get("data_encerramento") or "").strip())
    except ValueError:
        return False
    return deadline < today - timedelta(days=grace)


def archive_file(path: Path, edital: dict, archive_dir: Path) -> Path:
    """
    Move o JSON para o arquivo, com o mesmo nome.

    Nome já usado por **outro** edital ganha sufixo do link (como no sink); o
    mesmo edital substitui a cópia anterior.
    """
    archive_dir.mkdir(parents=True, exist_ok=True)
    target = archive_dir / path.name
    link = (edital.get("link") or "").strip()
    if target.exists():
        try:
            existing_link = (json.loads(target.read_text(encoding="utf-8")).get("link") or "").strip()
        except (OSError, ValueError):
            existing_link = ""
        if existing_link != link:
            suffix = hashlib.md5((link or path.stem).encode()).hexdigest()[:6]
            target = archive_dir / f"{path.stem}_{suffix}{path.suffix}"
    os.replace(path, target)
    return target


def curate(
    workdir: Path, apply: bool, refresh_status_only: bool, today: date
) -> Dict[str, int]:
    output_dir = workdir / OUTPUT_DIR
    if not output_dir.is_dir():
        print(f"Diretório inexistente: {output_dir}")
        return {}

    editais = load_editais(output_dir)
    print(f"{len(editais)} arquivos em {OUTPUT_DIR}\n")

    status_changes: List[Tuple[Path, str, str]] = []
    publico_changes: List[Tuple[Path, list, list]] = []
    category_changes: List[Tuple[Path, str, str]] = []
    agency_changes: List[Tuple[Path, str, str]] = []
    opening_changes: List[Tuple[Path, str, str]] = []
    schedule_changes: List[Tuple[Path, list]] = []
    profile_changes: List[Tuple[Path, list]] = []
    removals: Dict[str, List[Path]] = {}
    shell_keys: List[str] = []
    archivals: List[Tuple[Path, dict]] = []
    grace = archive_grace_days()

    for path, edital in editais:
        reason = "" if refresh_status_only else removal_reason(edital)
        if reason:
            removals.setdefault(reason, []).append(path)
            if reason == REASON_SHELL:
                shell_keys.append(path.stem)
                link = (edital.get("link") or "").strip()
                if link:
                    shell_keys.append(link)
            continue

        # O cronograma vem primeiro: o status e a abertura dependem do prazo recalculado.
        dirty = False
        schedule = expected_schedule_fields(edital, today)
        if schedule:
            schedule_changes.append((path, sorted(schedule)))
            edital.update(schedule)
            dirty = True

        wanted_status = expected_status(edital, today)
        wanted_category = expected_category(edital)
        wanted_agency = expected_agency(edital)
        wanted_opening = expected_opening_date(edital)
        perfil = backfill_source_profile(edital)
        if perfil:
            profile_changes.append((path, sorted(perfil)))
            edital.update(perfil)
            dirty = True
        if wanted_opening != (edital.get("data_abertura") or "").strip():
            opening_changes.append(
                (path, edital.get("data_abertura") or "", wanted_opening)
            )
            edital["data_abertura"] = wanted_opening
            dirty = True
        if wanted_agency and wanted_agency != (edital.get("orgão_fomento") or "").strip():
            agency_changes.append(
                (path, edital.get("orgão_fomento") or "", wanted_agency)
            )
            edital["orgão_fomento"] = wanted_agency
            dirty = True
        if wanted_status != (edital.get("status") or "").strip():
            status_changes.append((path, edital.get("status") or "", wanted_status))
            edital["status"] = wanted_status
            dirty = True
        if wanted_category != (edital.get("categoria") or "").strip():
            category_changes.append(
                (path, edital.get("categoria") or "", wanted_category)
            )
            edital["categoria"] = wanted_category
            dirty = True
        # Spec 011: público no vocabulário novo (ict-empresa → ict + empresa…).
        publico = edital.get("publico_alvo") or []
        wanted_publico = convert_legacy_publico(publico)
        if publico and wanted_publico != publico:
            publico_changes.append((path, publico, wanted_publico))
            edital["publico_alvo"] = wanted_publico
            dirty = True
        if dirty and apply:
            path.write_text(
                json.dumps(edital, ensure_ascii=False, indent=4), encoding="utf-8"
            )
        # Depois do recálculo: o que venceu há muito tempo é encerrado e arquivado
        # na mesma execução.
        if archive_due(edital, today, grace):
            archivals.append((path, edital))

    verb = "" if apply else " (simulação)"
    if publico_changes:
        print(f"Público convertido{verb}: {len(publico_changes)}")
    print(f"Status a corrigir{verb}: {len(status_changes)}")
    for path, before, after in status_changes[:10]:
        print(f"  {before or '(vazio)'!r} -> {after!r}  {path.name[:64]}")
    if len(status_changes) > 10:
        print(f"  ... e outros {len(status_changes) - 10}")

    print(f"\nCategorias a canonizar{verb}: {len(category_changes)}")
    for path, before, after in category_changes[:10]:
        print(f"  {before or '(vazio)'!r} -> {after!r}  {path.name[:64]}")
    if len(category_changes) > 10:
        print(f"  ... e outros {len(category_changes) - 10}")

    if profile_changes:
        print(f"\nCampos de fonte a preencher{verb}: {len(profile_changes)}")
        campos = sorted({c for _, cs in profile_changes for c in cs})
        print(f"  campos: {', '.join(campos)}")

    if schedule_changes:
        print(f"\nPrazos e etapas recalculados do cronograma{verb}: {len(schedule_changes)}")
        campos = sorted({c for _, cs in schedule_changes for c in cs})
        print(f"  campos: {', '.join(campos)}")

    if opening_changes:
        print(
            f"\nDatas de abertura inventadas a limpar{verb}: {len(opening_changes)}"
        )
        for path, before, _ in opening_changes[:5]:
            print(f"  {before!r} -> '' (sem prova no cronograma)  {path.name[:52]}")
        if len(opening_changes) > 5:
            print(f"  ... e outras {len(opening_changes) - 5}")

    if agency_changes:
        print(f"\nÓrgãos a preencher{verb}: {len(agency_changes)}")
        for path, before, after in agency_changes[:5]:
            print(f"  {before or '(vazio)'!r} -> {after!r}  {path.name[:60]}")

    total_removed = 0
    for reason, paths in removals.items():
        print(f"\nRemover — {reason}{verb}: {len(paths)}")
        for path in paths[:8]:
            print(f"  {path.name[:72]}")
        if len(paths) > 8:
            print(f"  ... e outros {len(paths) - 8}")
        total_removed += len(paths)
        if apply:
            for path in paths:
                path.unlink()

    archived = 0
    if archivals:
        print(
            f"\nArquivados em {ARCHIVE_DIR}/ (encerrados há mais de {grace} dias){verb}: "
            f"{len(archivals)}"
        )
        for path, _ in archivals[:8]:
            print(f"  {path.name[:72]}")
        if len(archivals) > 8:
            print(f"  ... e outros {len(archivals) - 8}")
    for path, edital in archivals:
        if not apply:
            continue
        try:
            archive_file(path, edital, workdir / ARCHIVE_DIR)
            archived += 1
        except OSError as exc:
            print(f"  Falha ao arquivar {path.name}: {exc} (o arquivo fica em {OUTPUT_DIR})")

    freed = drop_registry_keys(workdir / REGISTRY_PATH, shell_keys, apply)
    if shell_keys:
        print(
            f"\nChaves liberadas no registry{verb}: {freed} "
            "(cascas vazias podem ser recoletadas)"
        )

    print(
        f"\nResumo{verb}: {len(status_changes)} status, "
        f"{len(category_changes)} categorias, {len(agency_changes)} órgãos, "
        f"{len(opening_changes)} aberturas, {len(profile_changes)} perfis, "
        f"{total_removed} removidos, {len(archivals)} arquivados."
    )
    if not apply:
        print("Nada foi alterado. Use --apply para efetivar.")
    return {
        "status": len(status_changes),
        "categories": len(category_changes),
        "agencies": len(agency_changes),
        "openings": len(opening_changes),
        "profiles": len(profile_changes),
        "schedules": len(schedule_changes),
        "removed": total_removed,
        "archived": len(archivals),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="efetiva as mudanças (por padrão apenas relata)",
    )
    parser.add_argument(
        "--refresh-status",
        action="store_true",
        help="recalcula o status e arquiva encerrados antigos; não remove arquivo algum",
    )
    args = parser.parse_args()

    curate(
        workdir=Path(__file__).resolve().parents[1],
        apply=args.apply or args.refresh_status,
        refresh_status_only=args.refresh_status,
        today=date.today(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
