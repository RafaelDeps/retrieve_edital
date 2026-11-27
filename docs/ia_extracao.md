# Extração por IA: provedor, limites e adiamento

O coletor usa IA em três pontos. O provedor padrão é o **Gemini**, no **nível gratuito**. A
**Mistral** continua disponível como opção. Pela constituição 1.1.0 (Princípio V), nenhuma
funcionalidade pode depender de serviço pago, conta de faturamento ou cartão.

## Onde a IA entra

| Uso | Quem chama | O que devolve | Se a IA falhar |
|---|---|---|---|
| `extract_from_pdf` | `EditalNormalizer` (FAPES, CAPES, CNPq, CONIF, PRPPG/IFES, PROEX/IFES) | o edital estruturado | falha de extração → caminho sem IA; IA indisponível → **item adiado** |
| `classify_document_titles` | `FapesSource` | `{título: edital/anexo/alteração}` | heurística por palavras do título |
| `categorize_finep_by_description` | `EditalNormalizer` (FINEP) | uma das 3 categorias FINEP | `""` (mantém a categoria da fonte) |

Os três usos passam por `IExtractionService` (`src/core/interfaces.py`). A fábrica
`get_extraction_service()` (`src/components/transforms/extraction_service.py`) escolhe o provedor e
devolve **uma instância por processo**, compartilhada pela fonte e pelo normalizador. As instruções
(prompts) e o mapeamento da resposta ficam em `extraction_prompts.py` e são os mesmos para os dois
provedores.

Com o Gemini, o texto do PDF é extraído **localmente** (pdfplumber) e só o texto é enviado. PDF sem
texto (escaneado) não chama a IA.

### Campos a mais na extração (spec 010)

O prompt de extração pede sempre, na mesma chamada:
- **`tipo_documento`**: `"edital"` ou `"apoio"`. Apoio (termo, guia, diretriz, manual, resultado,
  portaria, página só informativa) levanta `SupportingDocumentError`, e o item é recusado com
  "documento de apoio, não é um edital". Sem o campo, vale "edital".
- **`fluxo_continuo`**: `true` quando o documento declara inscrição sem prazo. O texto lido também é
  procurado ("fluxo contínuo", "a qualquer momento/tempo", "sem prazo de encerramento").

Com vários documentos (original, alterações, página), uma regra a mais diz que vale a data da
alteração mais recente e o aviso da página.

### Elegibilidade (spec 011)

- **Extração completa**: o mesmo bloco de regras pede `elegibilidade` (titulação mínima de quem
  propõe, grandes áreas do CNPq, resumo dos requisitos) e `publico_alvo`. É a mesma chamada.
- **Horizon e FINEP sem documento lido**: uma leitura curta só da descrição (`read_eligibility`, até
  4 000 caracteres). Na FINEP, ela também devolve a categoria, no lugar de
  `categorize_finep_by_description`.
- **Validação**: as regras de `eligibility_rules.py` validam tudo contra vocabulários fechados.
- **Sem cota** na leitura curta: o item é publicado com `elegibilidade: null` e completado depois.
- **Carga do acervo**: a releitura diária completa os abertos sem elegibilidade, até
  `RELEITURA_ELEGIBILIDADE_MAX` (40) por execução.

### Leitura que falha (spec 010)

Quando havia documento ou página, mas a leitura falhou (download, PDF sem texto, IA sem edital
legível) e a fonte não deu prazo, o normalizador levanta `DocumentReadError`. O roteiro comum
registra o erro, e o item é tentado de novo em `TRANSFORM_ERROR_TTL_DAYS` (3). Sem leitura, não dá
para dizer se é fluxo contínuo. Com prazo da fonte, o caminho sem IA continua valendo.

## Configuração

| Variável | Padrão | Para quê |
|---|---|---|
| `LLM_PROVIDER` | `gemini` | `gemini` ou `mistral`. Outro valor deixa a IA indisponível, com erro no log |
| `GEMINI_API_KEY` | — | chave do Google AI Studio (nível gratuito) |
| `GEMINI_MODEL` | `gemini-3.5-flash-lite` | modelo |
| `GEMINI_MIN_INTERVAL_SEC` | `5` | intervalo mínimo entre chamadas, valendo para todas as threads (12 por minuto, abaixo do limite de 15) |
| `GEMINI_MAX_RETRIES` | `5` | novas tentativas em 429 por minuto ou erro temporário (500/503, rede) |
| `GEMINI_MAX_TOTAL_WAIT_SEC` | `300` | espera máxima acumulada por chamada |
| `GEMINI_TIMEOUT_SEC` | `120` | timeout de cada chamada |
| `MISTRAL_API_KEY`, `MISTRAL_LLM_MODEL`, `MISTRAL_OCR_MODEL` | — | só com `LLM_PROVIDER=mistral` (ver [mistral_usage.md](mistral_usage.md)) |

A chave nunca aparece em log, mensagem de erro, URL ou arquivo versionado.

## Limites do nível gratuito

Limites do `gemini-3.5-flash-lite` no gratuito (AI Studio, novembro de 2026): **15 chamadas por
minuto, 250 mil tokens por minuto e 500 chamadas por dia**. Eles valem para o **projeto do Google
inteiro**, somando todas as chaves e usos. Por isso o coletor deve ter um **projeto só dele**: outro
uso no mesmo projeto consome a mesma cota.

Medido em 25/11/2026, numa coleta local completa partindo de índices vazios (pior caso): 95
chamadas em 9,5 min, com pico de cerca de 13 por minuto. Um dia normal fica entre 1 e 10 chamadas.

| O Google responde | O coletor faz |
|---|---|
| 429 por **minuto** | espera o tempo indicado (`retryDelay`) e tenta de novo, até o teto |
| 429 por minuto que não passa | trata como cota diária esgotada |
| 429 por **dia** (`quotaId` com `PerDay`) | IA **indisponível** no resto da execução |
| chave inválida (400 `API_KEY_INVALID`, 401, 403) | IA **indisponível** no resto da execução |
| 500/503 ou falha de rede | tenta de novo até o teto; depois, falha só daquele item |

Sem `GEMINI_API_KEY`, a IA também fica indisponível desde o início, e o erro aparece uma vez no log.

### Item adiado

Com a IA indisponível, o normalizador **adia** o item:
- não monta o edital com os dados da página;
- não o recusa;
- não o grava.

Assim ele fica fora de `registry/processed_editais.json` e de `registry/rejected_editais.json`, e
é tentado de novo na próxima execução. O fluxo informa a contagem ao runner:

```text
[flow-stats] raw=12 new=4 deferred=8
```

O runner marca o fluxo como **Atenção** e escreve em `docs/flow_processing_log.md`:
"8 itens adiados por limite da IA; serão tentados na próxima execução."

A cota diária do gratuito volta à meia-noite do horário do Pacífico (cerca de 5h em Brasília). Se
os adiados aparecem com frequência, o volume passou do gratuito: a saída é reduzir chamadas, nunca
ativar cobrança.

## CI (GitHub Actions)

`run_scraper.yml` e `run_horizon_weekly.yml` repassam `LLM_PROVIDER=gemini`, `GEMINI_API_KEY` e
`MISTRAL_API_KEY` aos fluxos. Para cadastrar a chave no fork, vá em *Settings → Secrets and variables
→ Actions → New repository secret*, com o nome `GEMINI_API_KEY`.

## Testes

- `tests/step_defs/test_extracao_gemini.py` (cenários em `docs/features/extracao_gemini.feature`)
  roda offline, com cliente, relógio e espera falsos.
- O caso real (FAPES 10/2026 com o Gemini) é `integration`:

```bash
pytest -m integration tests/step_defs/test_extracao_gemini.py
```
