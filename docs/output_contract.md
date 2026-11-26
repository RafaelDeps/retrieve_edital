# Contrato dos dados de saída (`data/output/` e `data/arquivo/`)

Cada arquivo em `data/output/` (ou em `data/arquivo/`) é um edital. O `portal_edital` lê esses arquivos. Mudança de campo,
de vocabulário ou de nome de arquivo precisa ser combinada com quem mantém o portal (Constituição,
Princípio III). A exibição dos campos de chamadas e próxima etapa está em
RafaelDeps/portal_edital#11.

## Pastas de dados

| Pasta | O que tem | Quem altera |
|---|---|---|
| `data/output/` | editais **abertos**; **encerrados** há até N dias (carência, padrão 30); encerrados **sem data de prazo** | os fluxos (gravação) e o realinhamento diário (status, prazos, arquivamento) |
| `data/arquivo/` | editais **encerrados** há mais de N dias: histórico, no mesmo formato JSON e com o mesmo nome de arquivo (ou com sufixo `_xxxxxx` em caso de colisão) | só recebe arquivos do realinhamento; um arquivo sai dali apenas quando o edital é gravado de novo em `data/output/` (reabertura) |

- **Um link, uma pasta**: nenhum link aparece nas duas pastas ao mesmo tempo.
- **Congelado**: depois de arquivado, o status e os prazos não são mais recalculados.
- **Nada se perde**: a soma das duas pastas é o acervo completo.
- **Carência**: configurável por `ARCHIVE_GRACE_DAYS` (padrão 30). As regras ficam em
  `scripts/curate_output.py` (issue #11, spec 008).
- **Portal**: o que ele precisa fazer com a pasta nova está em RafaelDeps/portal_edital#14 (ver
  também a portal#3).

## Origem dos editais

`fonte_key` identifica a fonte que produziu o edital: `fapes`, `finep`, `conif`, `prppg_ifes`,
`proex_ifes`, `capes`, `cnpq`, `horizon` e, desde a spec 009, **`comunidade`**, para editais
indicados pelo formulário do portal e aceitos pelo coletor. Estes também trazem a tag
`indicação da comunidade`. Ver `docs/comunidade_source.md`.

## Modalidade "fluxo-contínuo" (spec 010, issue #9)

`modalidade` continua com os mesmos valores (`""` ou `"fluxo-contínuo"`), mas o significado de
`"fluxo-contínuo"` ficou mais amplo. Regra do responsável: o prazo está no edital ou na página; se
não estiver, é fluxo contínuo.

| Situação | `modalidade` |
|---|---|
| O edital, a página, o nome ou a descrição declaram "fluxo contínuo", "a qualquer momento/tempo" ou "sem prazo de encerramento" | `"fluxo-contínuo"` |
| O edital ou a página **foram lidos** e não têm prazo de encerramento | `"fluxo-contínuo"` |
| Tem prazo de encerramento e não declara fluxo contínuo | `""` |
| A leitura falhou (download, PDF sem texto, IA sem resposta) e a fonte não deu prazo | não é publicado: erro com nova tentativa em 3 dias |

- **Para o portal (ponto de contato)**: mais itens chegam com `"fluxo-contínuo"`, e itens abertos
  sem prazo e sem modalidade ficam raros (só de fontes que não mandam documento).
- **Documentos de apoio** (termos, guias, diretrizes, manuais, resultados, portarias, páginas só
  informativas) deixam de ser publicados.
- **Releitura**: o fluxo contínuo deduzido da falta de prazo é relido a cada 7 dias
  (`RELEITURA_FLUXO_CONTINUO_DIAS`), para pegar o edital que entra no site depois da coleta. A
  origem fica em `registry/fluxo_continuo.json`, fora do JSON do portal.

## Prazos e etapas

Todos os campos abaixo vêm do `cronograma` e são **recalculados todo dia** pelo realinhamento
(`scripts/curate_output.py --refresh-status`, chamado pelo runner), sem nova leitura do documento. As
regras ficam em `src/components/transforms/schedule_rules.py`.

| Campo | Tipo | Significado |
|---|---|---|
| `data_encerramento` | `AAAA-MM-DD` ou `""` | **Próximo** prazo de encerramento a partir de hoje; se todos já passaram, o último. `""` quando o edital não declara fim de inscrição. Data solta de início **nunca** vira prazo. |
| `data_abertura` | `AAAA-MM-DD` ou `""` | Abertura da **mesma chamada** do prazo atual. |
| `status` | `aberto` \| `encerrado` | Derivado do prazo (`encerrado` quando `data_encerramento` < hoje ou a origem declara encerrado). |
| `cronograma` | lista | Etapas `{"evento", "data", "chamada"?}`. `data` é ISO, ou texto quando relativa ("5 dias úteis após..."). `chamada` é opcional: só aparece quando a etapa pertence a uma chamada, fase ou ciclo nomeado. Períodos aparecem como dois eventos, "Início de …" e "Encerramento de …". |
| `chamadas` | lista | Uma entrada `{"nome", "abertura", "encerramento"}` por chamada, na ordem do edital. `[]` quando o edital não tem chamadas identificáveis. |
| `proxima_etapa` | objeto ou `null` | `{"evento", "data"}` da etapa de qualquer tipo com a menor data a partir de hoje. `null` quando não há etapa futura. |

### Exemplo: Edital FAPES 10/2026, com hoje = 25/11/2026

```json
{
  "status": "aberto",
  "data_abertura": "2026-10-14",
  "data_encerramento": "2026-12-15",
  "cronograma": [
    {"evento": "Início da submissão", "data": "2026-05-04", "chamada": "1ª chamada"},
    {"evento": "Encerramento da submissão", "data": "2026-07-07", "chamada": "1ª chamada"},
    {"evento": "Início da submissão", "data": "2026-10-14", "chamada": "2ª chamada"},
    {"evento": "Encerramento da submissão", "data": "2026-12-15", "chamada": "2ª chamada"}
  ],
  "chamadas": [
    {"nome": "1ª chamada", "abertura": "2026-05-04", "encerramento": "2026-07-07"},
    {"nome": "2ª chamada", "abertura": "2026-10-14", "encerramento": "2026-12-15"}
  ],
  "proxima_etapa": {"evento": "Encerramento da submissão", "data": "2026-12-15"}
}
```

Em 01/06/2026, o mesmo edital tem `data_encerramento` 2026-07-07 e `data_abertura` 2026-05-04. Em
01/01/2027, tem `data_encerramento` 2026-12-15 e `status` encerrado.

## Compatibilidade

Os campos `chamadas`, `proxima_etapa` e `cronograma[].chamada` são **aditivos**. Um JSON sem eles
continua válido, e o schema do portal (`z.object` não estrito) ignora campos que ainda não usa.
Editais extraídos antes da mudança do prompt só ganham `chamada` nas etapas quando são lidos de
novo (`python scripts/run_all_flows.py --reextrair-abertos`).
