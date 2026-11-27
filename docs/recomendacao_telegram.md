# Recomendação semanal de editais no Telegram (spec 012)

Toda segunda, por volta das 08:00 de Brasília, o workflow **Weekly Recommendations** manda a um grupo
do Telegram os editais abertos ainda não recomendados. Cada edital vai com até 5 professores do
campus e o motivo de cada indicação.

## De onde vêm os dados

- **Professores**: do [Horizon ETL](https://github.com/ifesserra-lab/horizon_etl). Ele publica todo
  sábado o arquivo público `data/exports/exports_canonical.zip`, e daqui se usa só o
  `researchers_only_canonical.json`, filtrado pelo campus (`RECOMENDACAO_CAMPUS`, padrão Serra).
  - **Titulação**: a maior formação **concluída** no Lattes. Sem formação, fica "confirmar
    titulação".
  - **Perfil**: resumo, cursos, áreas, grupos, projetos, artigos e orientações.
  - **Reserva**: uma cópia reduzida fica em `registry/horizon_researchers_serra.json`, para quando o
    download falha.
- **Editais**: os abertos de `data/output/`, com a `elegibilidade` da spec 011.

## Como escolhe

1. **Filtros**: o edital está aberto, com prazo daqui a 3 dias ou mais, ou é de fluxo contínuo. A
   titulação do professor tem de ser igual ou maior que a exigida.
2. **Perfil com as tags como base**: as tags (áreas do conhecimento) do professor, dos projetos e dos
   grupos dele vêm do Horizon. Na Serra, os grupos dão tags a 93 de 93 professores. Depois vêm as
   descrições de projetos e grupos, o resumo, a formação e os títulos.
3. **Semelhança**: o cosseno entre os vetores de significado do edital e do perfil.
   - **Modelo**: embeddings do Gemini, `gemini-embedding-001`, nível gratuito.
   - **Cache**: os vetores ficam em `registry/embeddings_cache.json`.
   - **Limite por minuto**: o gratuito limita tokens por minuto; o serviço espera e tenta de novo.
4. **Centragem**: da semelhança tiram-se a média do professor (contra todos os abertos) e a do edital.
   Na prévia real, perfis "parecidos com tudo" apareciam em quase todos os editais; centrado, conta o
   quanto o professor combina com **este** edital.
5. **Limite**: semelhança bruta ≥ `RECOMENDACAO_LIMITE_MINIMO` e centrada ≥
   `RECOMENDACAO_DESVIOS` × desvio das centradas do edital.
6. **Evidência**: só entra quem tem algo concreto em comum com o edital. Isso é uma tag com pelo menos
   metade das palavras no texto do edital, ou um projeto, artigo ou grupo com palavras em comum.
7. **Escolha**: os 5 maiores; no empate, quem tem Lattes primeiro.
8. **Motivo**: a formação ("Doutorado em …") e as tags em comum (ou o item mais próximo). Não usa IA e
   não inventa nada.

## A mensagem

- **Seção principal**: editais brasileiros, do prazo mais próximo para o mais distante.
- **Horizon Europe**: uma seção à parte, com as 5 melhores combinações da semana.
- **Lembrete**: os já recomendados que fecham nos próximos 7 dias.
- **Partes**: até 3 900 caracteres cada, numeradas quando há mais de uma.
- **Sem repetição**: `registry/recomendacoes_enviadas.json` evita repetir. Ele só é gravado quando
  todas as partes foram enviadas.

## Configurar o bot e o grupo

1. **Criar o bot**: no Telegram, abra o **@BotFather**, mande `/newbot` e guarde o token.
2. **Pôr o bot no grupo**: crie o grupo e adicione o bot pelo usuário dele (terminado em `bot`).
3. **Pegar o ID do grupo**:
   - no @BotFather, `/setprivacy` → bot → **Disable**;
   - readicione o bot e mande `/start@usuario_do_bot` no grupo;
   - rode `curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates"`;
   - o `"chat":{"id":-100…}` é o ID.
4. **Guardar os valores**: os secrets do repositório `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID`, e as
   mesmas linhas no `.env` local.

## Rodar

```bash
# Prévia: mostra a mensagem no log, não envia
TELEGRAM_BOT_TOKEN= TELEGRAM_CHAT_ID= python -m src.flows.recommend_flow
# Envio real
python -m src.flows.recommend_flow
```

**Código de saída 1** quando não houve envio por falha: Horizon sem reserva ou Telegram recusando. O
resumo da execução no GitHub mostra os números (professores, recomendados, vetores novos, pendentes,
partes).

## Variáveis

| Variável | Padrão | Uso |
|---|---|---|
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | — | sem os dois, modo prévia |
| `RECOMENDACAO_CAMPUS` | `Serra` | campus dos professores |
| `RECOMENDACAO_LIMITE_MINIMO` | `0.6` | piso da semelhança bruta |
| `RECOMENDACAO_DESVIOS` | `1.5` | desvios-padrão acima de zero, na semelhança centrada |
| `RECOMENDACAO_MAX_PROFESSORES` | `5` | por edital |
| `RECOMENDACAO_MAX_HORIZON` | `5` | editais na seção do Horizon |
| `RECOMENDACAO_PRAZO_MINIMO_DIAS` | `3` | prazo mínimo |
| `RECOMENDACAO_LEMBRETE_DIAS` | `7` | janela do lembrete |
| `HORIZON_EXPORT_URL` | zip no GitHub | origem dos professores |

## Privacidade

São enviados só o nome, a titulação e títulos acadêmicos que já são públicos no Horizon ETL, a um
grupo restrito. Nada vai a professores individualmente.
