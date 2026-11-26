# Source: indicações da comunidade (spec 009)

O portal tem uma página "Contribua" e um formulário de issue
(`portal_edital/.github/ISSUE_TEMPLATE/indicar-edital.yml`; portal#10) para indicar um edital que
ainda não aparece no portal. A indicação vira uma issue em `RafaelDeps/portal_edital`, com título
`[Indicação] …` e seções fixas: link oficial, órgão, prazo e observações.

O fluxo `ingest_comunidade_flow` (fonte `ComunidadeSource`, `fonte_key: "comunidade"`) roda na
execução diária e lê essas indicações.

## O que acontece com cada indicação

| Situação | Resultado | Resposta na issue |
|---|---|---|
| sem link válido | não lida | ❌ Não publicado: link inválido |
| link já publicado ou arquivado, ou em qualquer índice de processados | não lida | ✅ Já está no portal |
| link que a fonte já resolveu (processados ou recusados de `comunidade`) | ignorada | — |
| além do teto (`COMUNIDADE_MAX_POR_EXECUCAO`, padrão 10) | fica para a próxima execução | — |
| link fora do ar | recusa de 3 dias | ❌ Não publicado: link inacessível |
| título ou texto com termo de outro assunto | recusa **sem IA** | ❌ Não publicado: fora do tema: concurso público (…) |
| IA diz que não é fomento | recusa | ❌ Não publicado: fora do tema: <motivo da IA> |
| regras de publicação recusam (prazo vencido, não é edital) | recusa | ❌ Não publicado: <motivo> |
| erro inesperado na leitura | recusa de 3 dias (spec 007) | ❌ Não publicado por enquanto… (não fecha) |
| IA indisponível | adiada (spec 004) | — |
| publicada | `data/output/` | ✅ Publicado no portal: <nome> |

- **Filtro sem IA** (`src/components/transforms/theme_filter.py`): concurso público, licitação,
  pregão eletrônico ou presencial, leilão, processo seletivo para contratação, vaga de estágio,
  seleção de estagiários e cargo efetivo. "Processo seletivo" sozinho **não** barra, porque seleção
  de bolsistas é fomento.
- **Filtro da IA**: com `check_theme`, a extração também responde
  `"tema": {"e_fomento", "motivo"}` (fomento é financiamento, bolsa ou apoio a pesquisa, extensão,
  inovação ou divulgação científica). O prompt das outras fontes não muda.
- **Dados do documento**: o órgão e o prazo do formulário são só reserva. O prazo vira o evento
  "Encerramento (prazo informado na indicação)" quando o documento não traz prazo.
- **Saída**: tag `indicação da comunidade` e `fonte_key: "comunidade"`.

## Respostas e tokens

- **Leitura**: o repositório do portal é público. O workflow usa o `GITHUB_TOKEN` só para ter um
  limite maior.
- **Resposta e fechamento**: exigem `PORTAL_ISSUES_TOKEN`. Sem ele, o fluxo funciona e só registra
  "Respostas puladas". Toda resposta termina com "— coletor do Portal de Editais (resposta
  automática)" e não repete dados do formulário.
- **Criar o token**: GitHub → *Settings → Developer settings → Fine-grained tokens → Generate new
  token*:
  - repositório: só `RafaelDeps/portal_edital`;
  - permissão: *Issues: Read and write*.

  Cadastrar como secret `PORTAL_ISSUES_TOKEN` no fork do coletor (e no `.env`, para rodar
  localmente).

## Ponto de contato com o portal

O formulário marca as issues com o rótulo `indicacao-edital`, mas o rótulo não existe no
repositório. Enquanto não for criado, o coletor acha as indicações pelo título `[Indicação]`.
