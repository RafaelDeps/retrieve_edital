# Retrieve Edital

Agente especializado em download e raspagem de dados de editais (chamadas públicas) usando Python e Playwright. Suporta **FAPES** e **FINEP** com arquitetura ETL desacoplada.

## Arquitetura ETL

O projeto implementa o padrão **Source → Transform → Sink**:

- **Source (Extract)**: Extrai dados brutos da web (Playwright). Ex.: FAPES (múltiplas URLs), FINEP (chamadas abertas + página de detalhe por edital).
- **Transform**: Normaliza, valida e enriquece os dados (regras de negócio, IA para PDF/FINEP: Gemini por padrão, Mistral opcional). Produz objetos de domínio validados.
- **Sink (Load)**: Grava um arquivo JSON por edital em `data/output/` com `nome`, `órgão de fomento`, `cronograma`, `descrição`, `categoria`, `tags`, `anexos`.

Documentação detalhada: [docs/etl_architecture.md](docs/etl_architecture.md).

## Fluxos disponíveis

| Fluxo | Source | Uso | Saída |
|-------|--------|-----|--------|
| **FAPES** | `FapesSource` | Editais FAPES (múltiplas seções), com leitura do PDF por IA quando disponível | `data/output/*.json` |
| **FINEP** | `FinepSource` | Chamadas públicas FINEP (abertas), uma página de detalhe por chamada; categorização por IA pela descrição | `data/output/*.json` |
| **CONIF** | `ConifSource` | Editais do portal CONIF, restritos ao ano corrente, com deduplicação por `registry/processed_editais.json` e leitura do PDF principal por IA | `data/output/*.json` |

### Como rodar

**Requisitos:** Python 3.12+, dependências em `requirements.txt`, Playwright (`playwright install chromium`). Para a leitura de PDFs e a categorização FINEP: `GEMINI_API_KEY` no `.env` (nível gratuito; Mistral opcional com `LLM_PROVIDER=mistral`). Ver [docs/ia_extracao.md](docs/ia_extracao.md). CAPES e CNPq acessam o `www.gov.br` com desvio automático pelo Jina Reader (gratuito, sem cadastro; `JINA_API_KEY` opcional) quando a conexão direta é recusada, como acontece no GitHub Actions: ver [docs/cnpq_source.md](docs/cnpq_source.md#bloqueio-no-github-e-desvio-pelo-jina-issue-7-spec-006).

```bash
# Instalar dependências
pip install -r requirements.txt
playwright install chromium

# Pipeline FAPES (editais FAPES)
python -m src.flows.ingest_fapes_flow

# Pipeline FINEP (chamadas abertas; por padrão só a 1ª página)
python -m src.flows.ingest_finep_flow

# FINEP — todas as páginas da listagem
python -m src.flows.ingest_finep_flow --all

# Pipeline CONIF (apenas editais do ano corrente)
python -m src.flows.ingest_conif_flow
```

### Runner unificado e CI

```bash
# Todas as fontes diárias, em sequência (o HORIZON fica de fora)
python scripts/run_all_flows.py

# Só algumas fontes; o HORIZON só roda quando citado
python scripts/run_all_flows.py --only FINEP,CONIF
python scripts/run_all_flows.py --only HORIZON

# Teto por fonte: --timeout > FLOW_TIMEOUT_SEC > 40 min (teste local longo: 2 h)
FLOW_TIMEOUT_SEC=7200 python scripts/run_all_flows.py

# Ler de novo, com a IA, os editais abertos das fontes com OCR (~25 leituras).
# Use depois de mudar o prompt de extração, para o acervo ganhar os campos novos.
python scripts/run_all_flows.py --reextrair-abertos
```

O workflow diário (`run_scraper.yml`) roda as fontes diárias; o semanal (`run_horizon_weekly.yml`) roda
`--only HORIZON`. Os dois commitam `data/output/`, `registry/` (processados e recusados) e
`docs/flow_processing_log.md`. Se uma fonte falhar, o job fica vermelho, mas o que as outras coletaram é
salvo, e o resumo da execução mostra o resultado de cada fonte. No disparo manual do diário, o campo
`only` permite rodar só algumas fontes.

Variável opcional para FINEP: **`REFERENCE_YEAR`** (ano de referência para filtrar por prazo de envio). Ver [docs/finep_source.md](docs/finep_source.md) e `.env.example`.

## O que foi modificado / novidades

- **Novo source FINEP** (`FinepSource`): listagem em [chamadas abertas](http://www.finep.gov.br/chamadas-publicas/chamadaspublicas?situacao=aberta), entrada em cada link de detalhe para extrair descrição, cronograma (data de publicação + prazo de envio), tags (Tema(s)) e anexos (tabela Documentos). Filtro por ano de prazo configurável (`REFERENCE_YEAR` ou construtor).
- **Novo source CONIF** (`ConifSource`): leitura da listagem em `https://portal.conif.org.br/editais`, filtrando apenas URLs com o ano corrente, entrando na página de detalhe, baixando o PDF principal do edital para processamento via Mistral OCR e pulando URLs já registradas em `registry/processed_editais.json`.
- **Configuração de ano** (`src.config`): `get_reference_year()` para uso no filtro de prazos (FINEP).
- **Modelo de domínio** (`RawEdital`): campos opcionais `raw_cronograma`, `raw_tags`, `raw_anexos` para dados já estruturados na página de detalhe (ex.: FINEP).
- **Normalizador**:
  - Mapeamento explícito: data de publicação → `data_abertura`, prazo de envio da proposta → `data_encerramento`.
  - Para editais FINEP: categorização via **Mistral** pela descrição em uma de: **divulgação de conhecimento**, **extensão**, **inovação**.
- **Mistral** (`MistralExtractionService`): novo método `categorize_finep_by_description(description)` para classificar FINEP.
- **Novo fluxo** `ingest_finep_flow`: orquestra Source FINEP + Transform + Sink; parâmetro `max_pages` (ex.: 1 para teste).
- **Documentação**: [docs/finep_source.md](docs/finep_source.md), [docs/agents_and_skills.md](docs/agents_and_skills.md), e este README atualizado.

## Testes

```bash
# Suíte padrão: offline e independente da data. Os testes de integração aparecem como "skipped".
pytest

# Testes de integração: acessam o site real (FAPES) com o navegador do Playwright.
playwright install chromium
pytest -m integration

# Simular outra data: a suíte deve dar o mesmo resultado em qualquer dia.
pytest --fake-date=2030-06-15
```

Teste não pode depender da data real: quem compara com "hoje" ou com o ano corrente recebe a data
explicitamente (`today=` no `EditalNormalizer` e nas regras de publicação, `current_year=` nas
fontes). O CI roda a suíte duas vezes, com a data real e com `--fake-date=2030-06-15`, para pegar
testes que só passam hoje.

## Estrutura do repositório

```text
src/
├── core/           # Interfaces ISource, ITransform, ISink
├── config.py       # get_reference_year() (REFERENCE_YEAR)
├── domain/         # RawEdital, EditalDomain
├── components/
│   ├── sources/    # FapesSource, FinepSource
│   ├── transforms/ # EditalNormalizer, date_utils, schedule_rules, extraction_service, extraction_prompts, gemini_client, mistral_client
│   └── sinks/      # LocalJSONSink
└── flows/          # ingest_fapes_flow, ingest_finep_flow, ingest_conif_flow
docs/               # Arquitetura, backlog, features BDD, finep_source, agents_and_skills
data/output/        # JSONs gerados (1 por edital): abertos e encerrados há até 30 dias
data/arquivo/       # histórico: encerrados há mais de 30 dias (mesmo formato)
```

Os editais encerrados há mais de 30 dias (`ARCHIVE_GRACE_DAYS`) saem de `data/output/` e vão para
`data/arquivo/` no realinhamento diário. Um edital nunca está nas duas pastas: ver
[docs/output_contract.md](docs/output_contract.md#pastas-de-dados).

## Skills do agente

- Gerenciamento ágil (`agile-product-owner`)
- Versionamento (`gitflow`)
