"""
Instruções e mapeamento comuns aos provedores de IA (Gemini e Mistral).

Os dois provedores mandam as mesmas instruções e convertem a resposta do mesmo
jeito, para que trocar de provedor (`LLM_PROVIDER`) não mude o JSON de saída.
"""

import io
import json
import unicodedata
from typing import Any, Dict, List, Tuple

import pdfplumber

from src.components.transforms import eligibility_rules
from src.domain.models import EditalDomain

SYSTEM_EXTRACTION = (
    "Você é um especialista em análise de editais públicos de fomento (FAPES, CNPq, etc)."
)
SYSTEM_CLASSIFY = (
    "Você é um assistente especializado em organizar documentos de editais de fomento."
)
SYSTEM_FINEP = (
    "Você é um classificador de editais de fomento. Responda apenas com o JSON solicitado."
)

FINEP_CATEGORIES = ("divulgação de conhecimento", "extensão", "inovação")
FINEP_DEFAULT_CATEGORY = "inovação"
FINEP_DESCRIPTION_MAX_CHARS = 4000

TITLE_ANEXO_TOKENS = ("anexo", "formulário", "declaração")
TITLE_ALTERACAO_TOKENS = ("alteração", "retificação", "aditivo")


def extract_text_with_pdfplumber(pdf_bytes: bytes) -> str:
    """Extrai o texto do PDF localmente; devolve "" quando não há texto (PDF escaneado)."""
    pages_text: List[str] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if text.strip():
                pages_text.append(text)
    return "\n\n".join(pages_text).strip()


class OffTopicError(Exception):
    """A IA avaliou que o documento não é oportunidade de fomento (spec 009)."""

    def __init__(self, motivo: str):
        super().__init__(motivo)
        self.motivo = motivo


class SupportingDocumentError(Exception):
    """A IA classificou o documento como de apoio, não como edital (spec 010)."""

    def __init__(self, motivo: str = "documento de apoio, não é um edital"):
        super().__init__(motivo)
        self.motivo = motivo


class DocumentReadError(Exception):
    """
    O documento ou a página não pôde ser lido (spec 010).

    Levantada pelo normalizador quando a fonte não trouxe prazo: sem leitura, não
    há como dizer se o item é fluxo contínuo. O roteiro comum a registra como
    erro do item, com nova tentativa em poucos dias.
    """

    def __init__(self, motivo: str):
        super().__init__(motivo)
        self.motivo = motivo


DOCUMENT_KIND_EDITAL = "edital"
DOCUMENT_KIND_SUPPORT = "apoio"
SUPPORTING_DOCUMENT_REASON = "documento de apoio, não é um edital"
MODALITY_CONTINUOUS = "fluxo-contínuo"

# Texto que declara inscrição sem prazo de encerramento (comparado sem acento).
# "A qualquer tempo/momento" fica só com a IA: quase todo edital diz que o órgão
# "poderá, a qualquer tempo, revogar" o edital (validação real de 26/11/2026).
CONTINUOUS_FLOW_TEXT_MARKERS = (
    "fluxo continuo",
    "sem prazo de encerramento",
)

_VOCAB = eligibility_rules.prompt_vocabulary()

# Elegibilidade (spec 011): quem pode propor, para recomendar editais pelo Lattes.
ELIGIBILITY_FIELDS = f"""ELEGIBILIDADE de quem PROPÕE (não invente; use "" ou [] quando o texto não disser):
"elegibilidade": {{"titulacao_minima": um de {_VOCAB["titulacoes"]} ou "", "areas_conhecimento": lista de grandes áreas do CNPq entre: {_VOCAB["areas"]}, "requisitos_resumo": "uma frase curta com os principais requisitos"}}
- As áreas seguem o TEMA do edital (título e objeto): um edital sobre saúde pública é "Ciências da Saúde"; sobre violência de gênero, "Ciências Humanas" e "Ciências Sociais Aplicadas"; sobre mineração, "Engenharias" e "Ciências Exatas e da Terra".
- Responda ["Todas as áreas"] SÓ quando o edital não tiver tema próprio e aceitar propostas de qualquer área (ex.: bolsa de produtividade, apoio a eventos de qualquer área). Use "Multidisciplinar" só para programa declaradamente multi ou interdisciplinar.
- A titulação mínima é a que o proponente JÁ POSSUI: exige "estar cursando doutorado" → "mestrado"; "estar cursando mestrado" → "graduacao". Quando quem propõe é uma instituição, empresa, FAP ou dirigente institucional (não uma pessoa pela sua formação), use "".
- O resumo traz só requisitos escritos no texto. Se não houver, use "". Nunca escreva frases genéricas como "consulte o portal" ou "conforme as regras do programa".
PÚBLICO: "publico_alvo": lista entre {_VOCAB["publicos"]}."""

DOCUMENT_RULE = f"""
IMPORTANTE para o TIPO DE DOCUMENTO e o PRAZO:
1. Diga se o documento é um edital, chamada ou programa com inscrição de propostas ("edital") ou um documento de apoio: termo, guia, diretriz, manual, resultado, portaria ou página apenas informativa ("apoio").
2. Diga se o documento declara inscrição a qualquer tempo, fluxo contínuo ou ausência de prazo de encerramento.
Inclua no JSON: "tipo_documento": "edital" ou "apoio", e "fluxo_continuo": true ou false.
3. {ELIGIBILITY_FIELDS}
"""

COMBINED_MARKER = "=== "
COMBINED_RULE = """
IMPORTANTE quando houver VÁRIOS DOCUMENTOS:
Os documentos vêm em ordem cronológica, separados por cabeçalhos "=== ... ===". Quando uma alteração muda uma data, vale a da alteração mais recente. O texto da "PÁGINA DO EDITAL", quando houver, pode trazer avisos posteriores ao edital (prorrogação, encerramento antecipado); quando trouxer, ele vale.
"""


def _plain(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text or "")
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(plain.lower().replace("-", " ").split())


def has_continuous_flow_marker(text: str) -> bool:
    plain = _plain(text)
    return any(marker in plain for marker in CONTINUOUS_FLOW_TEXT_MARKERS)


def document_kind(data: Any) -> str:
    """"edital" ou "apoio". Sem resposta legível, não recusa: vale como edital."""
    kind = data.get("tipo_documento") if isinstance(data, dict) else None
    if isinstance(kind, str) and _plain(kind) == DOCUMENT_KIND_SUPPORT:
        return DOCUMENT_KIND_SUPPORT
    return DOCUMENT_KIND_EDITAL


def declares_continuous_flow(data: Any, text: str = "") -> bool:
    """A IA disse que o documento declara fluxo contínuo, ou o texto o diz."""
    if isinstance(data, dict) and data.get("fluxo_continuo") is True:
        return True
    return has_continuous_flow_marker(text)


THEME_RULE = """
IMPORTANTE para o TEMA:
Diga se o documento é uma oportunidade de fomento: financiamento, bolsa ou apoio a pesquisa, extensão, inovação ou divulgação científica. Concurso público, licitação, vaga de emprego ou estágio e leilão NÃO são fomento.
Inclua no JSON o campo "tema": {{"e_fomento": true ou false, "motivo": "uma frase curta explicando"}}.
"""


def build_extraction_prompt(text: str, check_theme: bool = False) -> str:
    prompt = _base_extraction_prompt(text)
    marker = "\nTexto do Edital:\n"
    head, _, tail = prompt.rpartition(marker)
    rules = DOCUMENT_RULE
    if COMBINED_MARKER in (text or ""):
        rules += COMBINED_RULE
    if check_theme:
        rules += THEME_RULE.replace("{{", "{").replace("}}", "}")
    return head + rules + marker + tail


def theme_verdict(data: Any) -> Tuple[bool, str]:
    """(é fomento?, motivo). Sem resposta legível, não recusa: vale como fomento."""
    tema = data.get("tema") if isinstance(data, dict) else None
    if not isinstance(tema, dict) or not isinstance(tema.get("e_fomento"), bool):
        return True, ""
    return tema["e_fomento"], str(tema.get("motivo") or "").strip()


def _base_extraction_prompt(text: str) -> str:
    return f"""
Analise o seguinte texto OCR de um edital de fomento e extraia as informações estruturadas em formato JSON.

Para preencher o campo 'descrição', procure especificamente pela seção 'Objeto' ou pela seção 'Finalidade' (ou termos similares) no texto do edital e utilize-a para redigir um resumo claro e conciso.

O JSON deve seguir exatamente esta estrutura:
{{
    "nome": "Título oficial do edital",
    "descrição": "Resumo conciso baseado na seção 'Objeto' ou 'Finalidade' do edital",
    "orgão_fomento": "Nome da instituição (Ex: FAPES)",
    "categoria": "extensão, pesquisa, inovação ou outros",
    "status": "aberto",
    "data_abertura": "YYYY-MM-DD",
    "data_encerramento": "YYYY-MM-DD ou \"\"",
    "cronograma": [
        {{"evento": "Descrição da etapa", "data": "ISO YYYY-MM-DD ou texto original caso seja data relativa (ex: '5 dias úteis após...')", "chamada": "1ª chamada, ou \"\" se o edital não tiver chamadas"}}
    ],
    "tags": ["lista", "de", "palavras-chave", "(MÍNIMO 3 TAGS)"]
}}

IMPORTANTE para o CRONOGRAMA:
1. Priorize o formato ISO YYYY-MM-DD.
2. Se houver um intervalo (ex: 'Submissão: 10/11/2025 a 16/12/2025'), crie DOIS eventos: 'Início de <etapa>' com a primeira data e 'Encerramento de <etapa>' com a última, ambas em ISO (ex: 'Início da submissão' = '2025-11-10' e 'Encerramento da submissão' = '2025-12-16'). Nunca descarte a data final de um intervalo: ela costuma ser o prazo.
3. Se o texto disser 'A partir de 26/10/2026', use '2026-10-26'.
4. Se a data for relativa (ex: '5 dias úteis após o resultado preliminar'), mantenha o texto original para processamento posterior.
5. Cada etapa deve ter o campo "chamada" com o nome da chamada, fase, ciclo ou etapa do processo a que pertence (ex: '1ª chamada', '2ª chamada', 'Ciclo 2'). Use "" quando o edital não dividir o processo seletivo em chamadas. Inclua TODAS as etapas de TODAS as chamadas.

Texto do Edital:
{text}
"""


def build_classify_titles_prompt(titles: List[str]) -> str:
    return f"""
Classifique cada título de documento abaixo em uma das seguintes categorias:
- 'edital': O documento principal da chamada pública ou concurso.
- 'anexo': Documentos técnicos, formulários, declarações ou manuais complementares.
- 'alteração': Aditivos, retificações ou mudanças no edital original.

Retorne APENAS um JSON onde a chave é o título exato e o valor é a categoria.

Títulos:
{json.dumps(titles, indent=2, ensure_ascii=False)}
"""


def heuristic_title_classification(titles: List[str]) -> Dict[str, str]:
    """Reserva quando a IA não classifica: decide pelo vocabulário do título."""
    fallback: Dict[str, str] = {}
    for title in titles:
        lowered = title.lower()
        if any(token in lowered for token in TITLE_ANEXO_TOKENS):
            fallback[title] = "anexo"
        elif any(token in lowered for token in TITLE_ALTERACAO_TOKENS):
            fallback[title] = "alteração"
        else:
            fallback[title] = "edital"
    return fallback


def build_finep_prompt(description: str) -> str:
    return f"""
Classifique o edital de chamada pública FINEP abaixo em exatamente UMA destas categorias:
- divulgação de conhecimento: difusão científica, popularização da ciência, museus, feiras, eventos de divulgação, educação científica para o público.
- extensão: extensão universitária, projetos que levam conhecimento à comunidade, parcerias universidade-sociedade, ações extensionistas.
- inovação: PD&I, desenvolvimento tecnológico, inovação em empresas, subvenção econômica, startups, produtos/processos inovadores.

Retorne APENAS um JSON com uma única chave "categoria" e o valor sendo exatamente uma das três opções acima (use a grafia exata).

Descrição do edital:
{description[:FINEP_DESCRIPTION_MAX_CHARS]}
"""


SYSTEM_ELIGIBILITY = (
    "Você é um analista de editais de fomento. Responda apenas com o JSON solicitado."
)
ELIGIBILITY_DESCRIPTION_MAX_CHARS = 4000


def build_eligibility_prompt(description: str, finep: bool = False) -> str:
    """
    Leitura curta (spec 011): só a descrição da fonte, para Horizon e FINEP, que
    não passam pela extração completa. Na FINEP também pede a categoria, no lugar
    da chamada separada de `build_finep_prompt`.
    """
    categoria = ""
    if finep:
        categoria = (
            '\nCATEGORIA: "categoria": exatamente uma de "divulgação de conhecimento", '
            '"extensão" ou "inovação".'
        )
    return f"""
Leia a descrição da chamada de fomento abaixo e responda só com um JSON.
{ELIGIBILITY_FIELDS}{categoria}

Descrição:
{(description or "")[:ELIGIBILITY_DESCRIPTION_MAX_CHARS]}
"""


def parse_eligibility_response(data: Any, finep: bool = False) -> Dict[str, Any]:
    """{"elegibilidade", "publico_alvo"[, "categoria"]}, já nos vocabulários fechados."""
    data = data if isinstance(data, dict) else {}
    parsed: Dict[str, Any] = {
        "elegibilidade": eligibility_rules.build_eligibility(data),
        "publico_alvo": eligibility_rules.normalize_publico(data.get("publico_alvo")),
    }
    if finep:
        parsed["categoria"] = normalize_finep_category(data.get("categoria"))
    return parsed


def normalize_finep_category(raw: Any) -> str:
    """Casa a resposta com uma categoria permitida; sem casamento, a categoria padrão."""
    category = str(raw or "").strip().lower()
    if category in FINEP_CATEGORIES:
        return category
    for allowed in FINEP_CATEGORIES:
        if category and (allowed in category or category in allowed):
            return allowed
    return FINEP_DEFAULT_CATEGORY


def map_schedule_item(item: Dict[str, Any]) -> Dict[str, str]:
    """Etapa do cronograma; `chamada` só entra quando a IA a informou."""
    mapped = {"evento": item.get("evento") or item.get("etapa", ""), "data": item.get("data") or ""}
    chamada = (item.get("chamada") or "").strip()
    if chamada:
        mapped["chamada"] = chamada
    return mapped


def map_to_domain(data: Dict[str, Any], text: str = "") -> EditalDomain:
    """
    Converte a resposta da IA. Com a declaração de fluxo contínuo (da IA ou de um
    marcador no texto lido), a modalidade já sai preenchida (spec 010).
    """
    tags = data.get("tags")
    edital = EditalDomain(
        nome=data.get("nome", "").upper(),
        descrição=data.get("descrição", ""),
        orgão_fomento=data.get("orgão_fomento", "FAPES").upper(),
        categoria=data.get("categoria", "outros").lower(),
        status=data.get("status", "aberto"),
        data_abertura=data.get("data_abertura") or "2026-01-01",
        data_encerramento=data.get("data_encerramento") or "",
        link="",  # o normalizador preenche com a URL
        cronograma=[map_schedule_item(item) for item in data.get("cronograma", [])],
        tags=tags if tags else ["fapes", "edital", "inovação"],
    )
    if declares_continuous_flow(data, text):
        edital.modalidade = MODALITY_CONTINUOUS
    # Spec 011: o documento foi lido, então a elegibilidade existe (talvez vazia).
    edital.elegibilidade = eligibility_rules.build_eligibility(data)
    edital.publico_alvo = eligibility_rules.normalize_publico(data.get("publico_alvo"))
    return edital
