"""
Instruções e mapeamento comuns aos provedores de IA (Gemini e Mistral).

Os dois provedores mandam as mesmas instruções e convertem a resposta do mesmo
jeito, para que trocar de provedor (`LLM_PROVIDER`) não mude o JSON de saída.
"""

import io
import json
from typing import Any, Dict, List, Tuple

import pdfplumber

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


THEME_RULE = """
IMPORTANTE para o TEMA:
Diga se o documento é uma oportunidade de fomento: financiamento, bolsa ou apoio a pesquisa, extensão, inovação ou divulgação científica. Concurso público, licitação, vaga de emprego ou estágio e leilão NÃO são fomento.
Inclua no JSON o campo "tema": {{"e_fomento": true ou false, "motivo": "uma frase curta explicando"}}.
"""


def build_extraction_prompt(text: str, check_theme: bool = False) -> str:
    prompt = _base_extraction_prompt(text)
    if not check_theme:
        return prompt
    marker = "\nTexto do Edital:\n"
    head, _, tail = prompt.rpartition(marker)
    return head + THEME_RULE.replace("{{", "{").replace("}}", "}") + marker + tail


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


def map_to_domain(data: Dict[str, Any]) -> EditalDomain:
    tags = data.get("tags")
    return EditalDomain(
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
