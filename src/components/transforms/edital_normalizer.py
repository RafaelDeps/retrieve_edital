import os
import re
import io
import logging
from datetime import date, datetime
from typing import List, Dict, Optional
import pdfplumber

from src.core.interfaces import ITransform
from src.domain.models import RawEdital, EditalDomain
from src.components.transforms import publication_rules
from src.components.transforms.eligibility_rules import build_eligibility, is_eligibility
from src.core.interfaces import IExtractionService
from src.components.transforms.extraction_service import (
    AIUnavailableError,
    get_extraction_service,
)
from src.components.transforms.extraction_prompts import (
    DocumentReadError,
    OffTopicError,
    SupportingDocumentError,
    SUPPORTING_DOCUMENT_REASON,
    normalize_finep_category,
)

SUGGESTED_DEADLINE_EVENT = "Encerramento (prazo informado na indicação)"
from src.components.transforms.date_utils import normalize_schedule_dates
from src.components.transforms import schedule_rules
from src.components.transforms.schedule_rules import (  # noqa: F401 — reexportados
    END_EVENT_TOKENS,
    PUBLICATION_EVENT_TOKENS,
    START_EVENT_TOKENS,
    _extract_last_schedule_date,
    _is_iso_date,
    _match_any_token,
)

logger = logging.getLogger(__name__)

# RawEdital.document_role: o documento só complementa prazo e modalidade (FINEP).
DOCUMENT_ROLE_DEADLINE = "prazo"
# Fontes sem extração completa cuja descrição passa pela leitura curta de
# elegibilidade (spec 011), pelo órgão em maiúsculas.
ELIGIBILITY_SHORT_READ_AGENCIES = ("HORIZON EUROPE", "FINEP")


def combine_schedule_views(
    raw_events: List[Dict[str, str]], normalized_events: List[Dict[str, str]]
) -> List[Dict[str, str]]:
    """
    Etapas com o melhor dos dois cronogramas: o nome normalizado e a data bruta.

    A normalização guarda só a primeira data de "24/02/2026 a 02/03/2026"; o texto
    bruto preserva o fim do intervalo, que pode ser o prazo. Datas relativas que a
    normalização resolveu ("5 dias úteis após ...") vêm do normalizado.
    """
    combined = []
    for raw_item, normalized_item in zip(raw_events or [], normalized_events or []):
        raw_date = raw_item.get("data") or ""
        item = {
            "evento": normalized_item.get("evento") or raw_item.get("evento") or "",
            "data": raw_date if schedule_rules._all_dates(raw_date) else (normalized_item.get("data") or ""),
        }
        chamada = normalized_item.get("chamada") or raw_item.get("chamada")
        if chamada:
            item["chamada"] = chamada
        combined.append(item)
    return combined


def derive_opening_date(events: List[Dict[str, str]], today: Optional[date] = None) -> str:
    """
    Data de abertura da chamada do prazo vigente, ou vazio quando não há data.

    Ver schedule_rules.derive_opening: abertura da mesma chamada do prazo; sem
    prazo, a precedência abertura de inscrições > publicação > primeira data.
    """
    return schedule_rules.derive_schedule_fields(events, today or date.today())["data_abertura"]


def derive_closing_date(
    raw_events: List[Dict[str, str]],
    normalized_events: List[Dict[str, str]],
    today: Optional[date] = None,
) -> str:
    """
    Próximo prazo de encerramento a partir de hoje; se todos passaram, o último.

    Vazio é resposta legítima: chamada de fluxo contínuo não tem encerramento, e
    data solta de início não é prazo (schedule_rules.closing_candidates).
    """
    events = combine_schedule_views(raw_events, normalized_events)
    return schedule_rules.derive_closing(events, today or date.today())


def merge_cronograma(
    source_events: Optional[List[Dict[str, str]]],
    extracted_events: Optional[List[Dict[str, str]]],
) -> List[Dict[str, str]]:
    """
    Une o cronograma da fonte com o extraído do PDF, sem perder nenhum dos dois.

    Antes o cronograma da fonte simplesmente substituía o do Mistral. Como FAPES,
    CAPES e PROEX/IFES não fornecem cronograma, a substituição era por lista
    vazia e apagava o que o OCR havia extraído — 74 editais ficaram sem
    cronograma, e 23 sem data alguma.

    A fonte tem precedência em caso de mesmo evento, porque o dado dela é
    estruturado; os eventos que só o PDF traz são acrescentados ao fim.
    """
    merged: List[Dict[str, str]] = []
    seen: set = set()
    for events in (source_events or [], extracted_events or []):
        for item in events:
            if not isinstance(item, dict):
                continue
            event_name = (item.get("evento") or "").strip()
            chamada = (item.get("chamada") or "").strip()
            # A mesma etapa ("Início da submissão") repete em cada chamada.
            key = (event_name.lower(), schedule_rules.normalize_call_name(chamada))
            if not event_name or key in seen:
                continue
            seen.add(key)
            merged_item = {"evento": event_name, "data": item.get("data") or ""}
            if chamada:
                merged_item["chamada"] = chamada
            merged.append(merged_item)
    return merged

class EditalNormalizer(ITransform[RawEdital, EditalDomain]):
    """
    Normalizes the RawEdital data into a validated EditalDomain object.
    Applies regex cleaning on metadata and uses Mistral for high-accuracy extraction.
    """
    
    def __init__(
        self,
        extraction_service: Optional[IExtractionService] = None,
        today: Optional[date] = None,
    ):
        # Provedor escolhido por LLM_PROVIDER (Gemini por padrão); uma instância
        # por processo, compartilhada com a fonte.
        self.extraction_service = extraction_service or get_extraction_service()
        # Data de referência das regras de publicação (prazo vencido, status).
        # `None` usa o dia real; os testes a fixam para não depender de quando rodam.
        self.today = today
        # Recusas desta execução, como {url: motivo}. O fluxo as registra no
        # índice de recusados para não repetir OCR de um edital que o portão já
        # rejeitou — sem isso, ele volta como novo em toda execução.
        self.rejections: Dict[str, str] = {}
        # Itens não lidos porque a IA ficou indisponível (cota diária, chave).
        # Não passam pela extração básica nem entram nas recusas: ficam fora dos
        # dois índices e voltam na próxima execução. O fluxo informa a contagem
        # ao runner, que marca Atenção.
        self.deferred: List[str] = []
        # Fluxo contínuo publicado nesta execução, como {url: origem} ("declarada"
        # ou "sem-prazo"). O roteiro comum grava em registry/fluxo_continuo.json,
        # e o runner relê os "sem-prazo" de tempos em tempos (spec 010).
        self.continuous_flow: Dict[str, str] = {}
        # Itens publicados sem elegibilidade porque a leitura curta não aconteceu
        # (cota da IA, spec 011). O fluxo conta em `eligibility_pending`.
        self.eligibility_pending: List[str] = []

    def _reference_day(self) -> date:
        return self.today or date.today()

    def process(self, raw_data: RawEdital) -> Optional[EditalDomain]:
        """
        Normaliza o RawEdital em EditalDomain.

        Devolve `None` quando o item não é uma oportunidade publicável — anexo
        solto, registro sem conteúdo extraído ou prazo já encerrado. Os sete
        fluxos já ignoram item nulo, então nada mais precisa mudar.
        """
        # Mandatory validation
        if not raw_data.title or raw_data.title.strip() == "":
            raise ValueError("O título do edital não pode ser nulo ou vazio.")

        # Title normalization: remove extra spaces and newlines
        clean_title = re.sub(r'\s+', ' ', raw_data.title).strip().upper()

        # Agency standardization
        community = getattr(raw_data, "community", None) or None
        if community:
            # Indicação da comunidade (spec 009): o órgão digitado no formulário
            # é só reserva; o do documento, lido pela IA, vence.
            clean_agency = (community.get("orgao") or "").strip().upper()
        else:
            raw_orgao = (raw_data.raw_agency or "FAPES").upper()
            clean_agency = "FAPES" if "FAPES" in raw_orgao else raw_orgao

        description = raw_data.raw_description or ""
        cronograma: List[Dict[str, str]] = []
        anexos: List[Dict[str, str]] = []
        tags: List[str] = []
        status = (raw_data.raw_status or "aberto").strip().lower() or "aberto"

        # Use structured data from detail page (e.g. FINEP chamadapublica) when present
        if getattr(raw_data, "raw_cronograma", None):
            cronograma = list(raw_data.raw_cronograma)
        if getattr(raw_data, "raw_anexos", None):
            anexos = list(raw_data.raw_anexos)
        if getattr(raw_data, "raw_tags", None):
            tags = list(raw_data.raw_tags)

        # Collect nested attachments (merge with raw_anexos if any)
        if raw_data.attachments:
            for att in raw_data.attachments:
                anexos.append({
                    "titulo": att.title,
                    "link": att.url,
                    "tipo": att.document_type
                })

        normalized_cronograma = normalize_schedule_dates(cronograma)
        raw_cronograma = list(cronograma)

        schedule_fields = schedule_rules.derive_schedule_fields(
            combine_schedule_views(raw_cronograma, normalized_cronograma), self._reference_day()
        )
        data_abertura = schedule_fields["data_abertura"]
        data_encerramento = schedule_fields["data_encerramento"]

        # Mistral extraction from PDF if available
        # Only extract full content for 'edital' or 'alteração' types, and if pdf_content is present
        pdf_text = getattr(raw_data, "pdf_text", None)
        has_document = bool(raw_data.pdf_content or pdf_text)
        if has_document and getattr(raw_data, "document_role", "") == DOCUMENT_ROLE_DEADLINE:
            return self._complete_deadline_from_document(
                raw_data, clean_title, normalized_cronograma, data_encerramento, tags, anexos, status
            )
        if has_document and raw_data.document_type in ["edital", "alteração"]:
            try:
                # Documento como arquivo (acesso direto) ou já como texto (desvio
                # do gov.br pelo Jina, spec 006): a IA recebe o mesmo texto.
                # Indicações perguntam também se o documento é fomento (spec 009).
                theme = {"check_theme": True} if community else {}
                if raw_data.pdf_content:
                    mistral_domain = self.extraction_service.extract_from_pdf(
                        raw_data.pdf_content, f"{clean_title}.pdf", **theme
                    )
                else:
                    mistral_domain = self.extraction_service.extract_from_text(
                        pdf_text, f"{clean_title}.pdf", **theme
                    )
                
                if mistral_domain:
                    # Enrich/Merge Mistral result with metadata
                    mistral_domain.link = raw_data.url
                    if community:
                        mistral_domain.orgão_fomento = (
                            (mistral_domain.orgão_fomento or "").strip().upper() or clean_agency
                        )
                    else:
                        mistral_domain.orgão_fomento = clean_agency
                    mistral_domain.status = status
                    # Force the category from the source website as requested
                    if raw_data.source_category:
                        mistral_domain.categoria = raw_data.source_category
                    # Add tags based on document type
                    if raw_data.document_type == "alteração":
                        mistral_domain.tags.append("alteração")
                        mistral_domain.nome = f"[ALTERAÇÃO] {mistral_domain.nome}"
                    
                    mistral_domain.tags = list(dict.fromkeys((mistral_domain.tags or []) + tags))
                    mistral_domain.anexos = anexos

                    # Une o cronograma da fonte ao extraído do PDF. Antes o da
                    # fonte substituía o do Mistral, e como FAPES, CAPES e
                    # PROEX/IFES não fornecem cronograma, a substituição era por
                    # lista vazia: apagava o que o OCR já havia extraído.
                    merged = merge_cronograma(
                        normalized_cronograma, mistral_domain.cronograma
                    )
                    mistral_domain.cronograma = normalize_schedule_dates(merged)

                    # Rederiva as datas do cronograma resultante. A fonte tem
                    # precedência quando trouxe a data; senão vale o que o
                    # cronograma unido revelar, e só então o que o Mistral pôs
                    # nos campos escalares.
                    mistral_domain.data_abertura = (
                        data_abertura
                        or derive_opening_date(mistral_domain.cronograma, self._reference_day())
                        or (mistral_domain.data_abertura or "")
                    )
                    mistral_domain.data_encerramento = (
                        data_encerramento
                        or derive_closing_date(merged, mistral_domain.cronograma, self._reference_day())
                        or (mistral_domain.data_encerramento or "")
                    )
                    if community and not mistral_domain.data_encerramento:
                        self._apply_suggested_deadline(mistral_domain, community)

                    return self._publish_or_discard(mistral_domain, raw_data, document_read=True)
                else:
                    logger.warning(f"A IA não devolveu edital para {clean_title}; seguindo com a extração básica.")
            except SupportingDocumentError as e:
                logger.info("Documento de apoio, não publicado: %s", clean_title[:70])
                if raw_data.url:
                    self.rejections[raw_data.url] = e.motivo or SUPPORTING_DOCUMENT_REASON
                return None
            except OffTopicError as e:
                logger.info("Indicação fora do tema (%s): %s", e.motivo, clean_title[:70])
                if raw_data.url:
                    self.rejections[raw_data.url] = f"fora do tema: {e.motivo}"
                return None
            except AIUnavailableError as e:
                logger.warning("Item adiado (IA indisponível: %s): %s", e.reason, clean_title[:70])
                if raw_data.url:
                    self.deferred.append(raw_data.url)
                return None
            except Exception as e:
                logger.error(f"Erro na extração por IA de {clean_title}: {e}")

        # Spec 010: sem prazo da fonte, uma leitura que falhou não permite dizer
        # se o item é fluxo contínuo. Vira erro do item (nova tentativa em poucos
        # dias) em vez de seguir para a extração básica e sair "aberto sem prazo".
        read_failure = (getattr(raw_data, "read_failure", "") or "").strip()
        if has_document and raw_data.document_type in ["edital", "alteração"] and not read_failure:
            read_failure = "a IA não devolveu edital legível"
        if read_failure and not data_encerramento:
            raise DocumentReadError(f"{read_failure}: {clean_title[:70]}")

        # Basic/Fallback extraction (for anexos or if Mistral fails)
        combined_text = (description + " " + clean_title).lower()
        category = raw_data.source_category or "outros"
        eligibility, read_publico, short_category = self._short_eligibility(raw_data, clean_title, description)
        if short_category:
            category = short_category
        elif "FINEP" in (raw_data.raw_agency or "").upper() and description:
            try:
                classified = self.extraction_service.categorize_finep_by_description(description)
                if classified:
                    category = classified
                    logger.info("FINEP edital categorizado pela IA: %s", category)
                else:
                    # Classificador indisponível: mantém a categoria da fonte em
                    # vez de gravar um palpite como se fosse classificação.
                    logger.warning(
                        "Classificação FINEP indisponível; mantendo categoria da fonte: %s",
                        category,
                    )
            except Exception as e:
                logger.warning("Categorização FINEP pela IA falhou, usando fallback: %s", e)
        if not tags:
            tags = ["fapes", "documento"]
            if "FINEP" in (raw_data.raw_agency or "").upper():
                tags = ["finep", "chamada pública"]
        
        if raw_data.document_type == "anexo":
            if "anexo" not in tags:
                tags.append("anexo")
            clean_title = f"[ANEXO] {clean_title}"
        elif raw_data.document_type == "alteração":
            if "alteração" not in tags:
                tags.append("alteração")
            clean_title = f"[ALTERAÇÃO] {clean_title}"
            
        if "FINEP" not in (raw_data.raw_agency or "").upper():
            if "extensão" in combined_text:
                category = "extensão"
                if "extensão" not in tags:
                    tags.append("extensão")
            elif "pesquisa" in combined_text:
                category = "pesquisa"
                if "pesquisa" not in tags:
                    tags.append("pesquisa")
            elif "inovação" in combined_text:
                category = "inovação"
                if "inovação" not in tags:
                    tags.append("inovação")
        else:
            if category not in tags:
                tags.append(category)

        if "bolsa" in combined_text and "bolsa" not in tags:
            tags.append("bolsa")

        edital = EditalDomain(
            publico_alvo=read_publico,
            elegibilidade=eligibility,
            nome=clean_title,
            descrição=description or f"Edital de fomento {clean_agency}: {clean_title}",
            orgão_fomento=clean_agency,
            categoria=category,
            status=status,
            data_abertura=data_abertura,
            data_encerramento=data_encerramento,
            link=raw_data.url,
            cronograma=normalized_cronograma,
            tags=tags,
            anexos=anexos
        )
        return self._publish_or_discard(edital, raw_data)

    def _short_eligibility(self, raw_data: RawEdital, clean_title: str, description: str):
        """
        (elegibilidade, público lido, categoria FINEP) para o caminho sem documento.

        Horizon reescreve tudo toda semana: o que já foi lido vem em
        `known_elegibilidade` e não é lido de novo. Sem cota, o item sai com
        `None` e fica pendente; a publicação não espera por um campo opcional.
        """
        known = getattr(raw_data, "known_elegibilidade", None)
        if is_eligibility(known):
            return known, [], ""
        agency = (raw_data.raw_agency or "").strip().upper()
        reader = getattr(self.extraction_service, "read_eligibility", None)
        if agency not in ELIGIBILITY_SHORT_READ_AGENCIES or reader is None or not description.strip():
            return None, [], ""
        is_finep = agency == "FINEP"
        try:
            # O título diz o tema; no Horizon a descrição é só metadado da chamada.
            answer = reader(f"Título: {clean_title}\n{description}", f"{clean_title}.txt", finep=is_finep)
        except NotImplementedError:
            return None, [], ""
        except AIUnavailableError as e:
            logger.warning("Elegibilidade pendente (IA indisponível: %s): %s", e.reason, clean_title[:70])
            answer = {}
        except Exception as e:  # a leitura curta nunca derruba o item
            logger.warning("Elegibilidade pendente (%s): %s", type(e).__name__, clean_title[:70])
            answer = {}
        if not isinstance(answer, dict):
            return None, [], ""  # serviço que não implementa a leitura curta
        if not answer:
            if raw_data.url:
                self.eligibility_pending.append(raw_data.url)
            return None, [], ""
        return answer.get("elegibilidade"), answer.get("publico_alvo") or [], answer.get("categoria") or ""

    def _complete_deadline_from_document(
        self,
        raw_data: RawEdital,
        clean_title: str,
        source_cronograma: List[Dict[str, str]],
        source_closing: str,
        tags: List[str],
        anexos: List[Dict[str, str]],
        status: str,
    ) -> Optional[EditalDomain]:
        """
        Documento lido só para achar prazo e modalidade (spec 010, FINEP).

        Nome, descrição, tags, público e âmbito continuam os da fonte: o nome da
        API é o oficial, e trocá-lo mudaria o arquivo. Da IA vêm o cronograma
        (unido ao da fonte), o encerramento, a declaração de fluxo contínuo e a
        categoria, na mesma chamada (sem `categorize_finep_by_description`).
        Anexo classificado como apoio conta como lido sem prazo.
        """
        domain = None
        try:
            if raw_data.pdf_content:
                domain = self.extraction_service.extract_from_pdf(raw_data.pdf_content, f"{clean_title}.pdf")
            else:
                domain = self.extraction_service.extract_from_text(raw_data.pdf_text, f"{clean_title}.pdf")
        except SupportingDocumentError:
            logger.info("Anexo lido é de apoio; sem prazo nele: %s", clean_title[:70])
            domain = EditalDomain(
                nome="", descrição="", orgão_fomento="", categoria="", status="",
                data_abertura="", data_encerramento="", link="", cronograma=[], tags=[],
                elegibilidade=build_eligibility({}),
            )
        except AIUnavailableError as e:
            logger.warning("Item adiado (IA indisponível: %s): %s", e.reason, clean_title[:70])
            if raw_data.url:
                self.deferred.append(raw_data.url)
            return None
        if domain is None and not source_closing:
            raise DocumentReadError(f"a IA não devolveu edital legível: {clean_title[:70]}")

        merged = normalize_schedule_dates(
            merge_cronograma(source_cronograma, domain.cronograma if domain else [])
        )
        reference = self._reference_day()
        edital = EditalDomain(
            nome=clean_title,
            descrição=raw_data.raw_description or f"Edital de fomento {raw_data.raw_agency}: {clean_title}",
            orgão_fomento=(raw_data.raw_agency or "").upper(),
            categoria=normalize_finep_category(domain.categoria) if domain else (raw_data.source_category or "outros"),
            status=status,
            data_abertura=derive_opening_date(merged, reference) or (domain.data_abertura if domain else ""),
            data_encerramento=source_closing
            or derive_closing_date(merged, merged, reference)
            or ((domain.data_encerramento or "") if domain else ""),
            link=raw_data.url,
            cronograma=merged,
            tags=list(dict.fromkeys(tags + ([raw_data.source_category] if raw_data.source_category else []))),
            anexos=anexos,
            modalidade=(domain.modalidade or "") if domain else "",
            publico_alvo=list(domain.publico_alvo or []) if domain else [],
            elegibilidade=domain.elegibilidade if domain else None,
        )
        return self._publish_or_discard(edital, raw_data, document_read=domain is not None)

    @staticmethod
    def _apply_suggested_deadline(edital: EditalDomain, community: Dict[str, str]) -> None:
        """
        Prazo do formulário como reserva, quando o documento não traz prazo.

        Fica marcado no cronograma como "Prazo informado na indicação": é a
        evidência que temos, e quem lê sabe de onde ela veio.
        """
        prazo = (community.get("prazo") or "").strip()
        iso = ""
        for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
            try:
                iso = datetime.strptime(prazo, fmt).date().isoformat()
                break
            except ValueError:
                continue
        if not iso:
            return
        edital.cronograma = list(edital.cronograma or []) + [
            {"evento": SUGGESTED_DEADLINE_EVENT, "data": iso}
        ]
        edital.data_encerramento = iso

    def _publish_or_discard(
        self, edital: EditalDomain, raw_data: RawEdital, document_read: bool = False
    ) -> Optional[EditalDomain]:
        """
        Aplica as regras de publicação e ajusta o status ao prazo.

        Item não publicável volta como `None`; os fluxos já o ignoram.
        """
        verdict = publication_rules.evaluate(edital, raw_data, today=self.today)
        if not verdict.publishable:
            logger.info(
                "Edital não publicado (%s): %s", verdict.reason, edital.nome[:70]
            )
            if raw_data.url:
                self.rejections[raw_data.url] = verdict.reason
            return None
        edital.status = publication_rules.resolve_status(edital, today=self.today)
        # Contrato com o portal (docs/output_contract.md): chamadas e próxima etapa,
        # derivadas do cronograma final, nos dois caminhos (fonte e IA).
        derived = schedule_rules.derive_schedule_fields(edital.cronograma or [], self._reference_day())
        edital.chamadas = derived["chamadas"]
        edital.proxima_etapa = derived["proxima_etapa"]
        edital.categoria = publication_rules.canonical_category(
            edital.categoria, publication_rules.category_hint_text(edital)
        )
        edital.modalidade, origin = publication_rules.resolve_modalidade_and_origin(
            edital, raw_data, document_read
        )
        if origin and raw_data.url:
            self.continuous_flow[raw_data.url] = origin
        edital.ambito_geografico = publication_rules.resolve_ambito_geografico(
            edital, raw_data
        )
        edital.fonte_key = publication_rules.resolve_fonte_key(edital, raw_data)
        edital.publico_alvo = publication_rules.resolve_publico_alvo(edital, raw_data)
        return edital
