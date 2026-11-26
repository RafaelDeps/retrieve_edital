# Source CAPES

## Objetivo

Coletar os editais abertos da CAPES na página
`https://www.gov.br/capes/pt-br/assuntos/editais-e-resultados-capes` (`src/components/sources/capes_source.py`,
fluxo `src/flows/ingest_capes_flow.py`).

## Como lê

- **Listagem**: a seção "Editais Abertos" da página. Cada link vira uma entrada. `last_listing_count`
  conta as entradas antes da deduplicação.
- **Detalhe**: título, primeiro parágrafo descritivo como descrição, e os links de documento
  (`.pdf`, inclusive na forma `…/edital.pdf/@@display-file/file`).
- **Documento principal**: o PDF com mais pontos (`/centrais-de-conteudo/editais/`, "edital",
  "chamada"; menos pontos para "resultado" e "manual"). Ele segue para a extração por IA.
- **Filtro por ano**: um item cujo ano inferido seja anterior ao ano corrente é descartado.

A página é HTML estático: **não precisa de navegador**. Até a spec 006 a fonte usava o Playwright
só para pedir o HTML; uma requisição simples traz as mesmas entradas e os mesmos PDFs.

## Bloqueio no GitHub e desvio pelo Jina (issue #7, spec 006)

O `www.gov.br` **derruba toda conexão vinda dos servidores do GitHub Actions** (Azure, EUA), já no
início da conexão. O diagnóstico de 26/11/2026 (execução 45823559474) mostrou:
- `Connection reset by peer` na página inicial, nas páginas da CAPES e do CNPq, na API e no RSS;
- o mesmo resultado com qualquer identificação, IPv4, HTTP/1.1 e tentativas repetidas;
- outros sites do governo (`finep.gov.br`, `dados.gov.br`) respondendo do mesmo servidor;
- do Brasil, tudo funcionando direto.

Por isso todo acesso da fonte ao `www.gov.br` (listagem, página de detalhe e PDF) passa pelo
`GovBrFetcher` (`src/components/sources/govbr_fetcher.py`):

- **Acesso direto primeiro.** Se a conexão cair, o mesmo acesso é refeito pelo **Jina Reader**
  (`https://r.jina.ai/<url>`), um serviço público e gratuito que busca a página a partir dos
  servidores dele. As páginas vêm como o HTML original (`X-Return-Format: html`), e os PDFs como
  texto, que segue direto para a extração por IA.
- **O bloqueio é lembrado:** detectado uma vez, o resto da execução da fonte vai direto pelo Jina.
- **Erro HTTP do próprio `gov.br`** (404, 500) não é bloqueio e segue o tratamento normal.
- **No computador local**, com o acesso direto funcionando, o Jina não é chamado.

Configuração (todas opcionais):

| Variável | Padrão | Para quê |
|---|---|---|
| `JINA_API_KEY` | — | chave gratuita do Jina, se um dia o limite sem cadastro apertar; nunca aparece em log |
| `JINA_MIN_INTERVAL_SEC` | `4` | intervalo entre chamadas ao Jina (o gratuito sem cadastro aceita cerca de 20 por minuto) |
| `JINA_MAX_RETRIES` | `3` | novas tentativas em 429, 5xx ou erro de conexão |
| `JINA_MAX_TOTAL_WAIT_SEC` | `120` | espera máxima acumulada por acesso |
| `JINA_TIMEOUT_SEC` | `60` | timeout de cada chamada ao Jina |

O que aparece em `docs/flow_processing_log.md`:

| Situação | Resultado | Observação |
|---|---|---|
| acesso direto | Sucesso | (normal) |
| desvio pelo Jina | Sucesso | "www.gov.br recusou o acesso direto; N páginas buscadas pelo Jina." |
| direto e desvio falharam | Atenção | "Falha de rede: www.gov.br recusou a conexão e o desvio pelo Jina falhou (jina_429)." |

"Falha de rede" nunca aparece como "A origem não devolveu nenhum item — verificar se o portal
mudou", que continua valendo só quando o site respondeu sem itens.
