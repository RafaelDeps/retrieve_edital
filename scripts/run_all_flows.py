import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.flow_health import FlowStats, parse_flow_stats  # noqa: E402
from src.components.sinks.json_sink import LocalJSONSink  # noqa: E402
from src.processed_store import REEXTRACT_KEYS_ENV  # noqa: E402


FLOW_COMMANDS = (
    ("FAPES", [sys.executable, "-m", "src.flows.ingest_fapes_flow"]),
    ("FINEP", [sys.executable, "-m", "src.flows.ingest_finep_flow"]),
    ("CONIF", [sys.executable, "-m", "src.flows.ingest_conif_flow"]),
    ("PRPPG_IFES", [sys.executable, "-m", "src.flows.ingest_prppg_ifes_flow"]),
    ("PROEX_IFES", [sys.executable, "-m", "src.flows.ingest_proex_ifes_flow"]),
    ("CAPES", [sys.executable, "-m", "src.flows.ingest_capes_flow"]),
    ("CNPQ", [sys.executable, "-m", "src.flows.ingest_cnpq_flow"]),
)
# Fontes que só rodam quando citadas em --only. O Horizon Europe tem cadência
# semanal (dataset de ~126 MB que muda pouco) e roda no workflow próprio com
# `--only HORIZON`; fica fora da execução diária padrão.
OPTIONAL_FLOW_COMMANDS = (
    ("HORIZON", [sys.executable, "-m", "src.flows.ingest_horizon_flow"]),
)
REGISTRY_KEYS = {
    "FAPES": "fapes",
    "FINEP": "finep",
    "CONIF": "conif",
    "PRPPG_IFES": "prppg_ifes",
    "PROEX_IFES": "proex_ifes",
    "CAPES": "capes",
    "CNPQ": "cnpq",
    "HORIZON": "horizon",
}
LOG_PATH = Path("docs/flow_processing_log.md")
REGISTRY_PATH = Path("registry/processed_editais.json")
OUTPUT_PATH = Path("data/output")

RESULT_SUCCESS = "Sucesso"
RESULT_WARNING = "Atenção"
RESULT_FAILURE = "Falha"

# Código de saída atribuído a um fluxo encerrado por estourar o teto de duração.
TIMEOUT_RETURN_CODE = 124

# Após tantas execuções seguidas sem nenhum edital novo, o fluxo passa a ser
# reportado como suspeito. Foi a ausência desse sinal que deixou as quedas da
# FINEP e do CNPq invisíveis por meses.
ZERO_DELTA_ALERT_THRESHOLD = 7

# Fontes cujo volume normal é próximo de zero (ex.: ANEEL publica de 0 a 2
# chamadas por ano). Para elas, `delta 0` prolongado é o comportamento
# esperado e não deve gerar alerta.
LOW_VOLUME_FLOWS = frozenset()

# Teto de duração por fonte. Um fluxo que trava — portal pendurado, backoff longo
# do Mistral — não pode consumir a janela do job inteiro. Ao estourar, o processo
# é encerrado, a falha é registrada e os fluxos seguintes continuam.
# 7 fontes × 40 min = 280 min, abaixo das 6 h de um job do GitHub Actions.
# Para teste local mais longo: FLOW_TIMEOUT_SEC=7200 ou --timeout 7200.
DEFAULT_FLOW_TIMEOUT_SEC = 40 * 60
FLOW_TIMEOUT_ENV = "FLOW_TIMEOUT_SEC"
# Espera entre o SIGTERM e o SIGKILL ao encerrar uma fonte que estourou o teto.
TERMINATE_GRACE_SEC = 5

DELTA_REGEX = re.compile(r"\(delta (-?\d+)\)")
LOG_SEPARATOR = "| :-- | :-- | :-- | :-- |\n"


def load_registry_counts(workdir: Path) -> dict[str, int]:
    registry_path = workdir / REGISTRY_PATH
    if not registry_path.exists():
        return {value: 0 for value in REGISTRY_KEYS.values()}
    data = json.loads(registry_path.read_text(encoding="utf-8"))
    return {
        source: len(data.get(source, []))
        for source in REGISTRY_KEYS.values()
    }


def get_output_file_stats(workdir: Path) -> tuple[int, list[str]]:
    output_dir = workdir / OUTPUT_PATH
    if not output_dir.exists():
        return 0, []
    files = [path.name for path in output_dir.iterdir() if path.is_file()]
    non_json = sorted(name for name in files if not name.endswith(".json"))
    json_count = sum(1 for name in files if name.endswith(".json"))
    return json_count, non_json


def current_timestamp() -> str:
    now = datetime.now(ZoneInfo("America/Sao_Paulo"))
    offset = now.strftime("%z")
    formatted_offset = f"{offset[:3]}:{offset[3:]}"
    return now.strftime("%Y-%m-%d %H:%M:%S ") + formatted_offset


def count_zero_delta_streak(workdir: Path, flow_name: str) -> int:
    """
    Conta quantas execuções seguidas do fluxo terminaram com `delta 0`.

    As linhas são inseridas logo abaixo do separador, então a leitura de cima
    para baixo já percorre da mais recente para a mais antiga.
    """
    log_path = workdir / LOG_PATH
    if not log_path.exists():
        return 0

    streak = 0
    for line in log_path.read_text(encoding="utf-8").splitlines():
        row = _parse_log_row(line)
        if row is None:
            continue
        row_flow, observations = row
        if row_flow != flow_name:
            continue
        delta_match = DELTA_REGEX.search(observations)
        if delta_match is None or int(delta_match.group(1)) != 0:
            break
        streak += 1
    return streak


def _parse_log_row(line: str) -> Optional[Tuple[str, str]]:
    """Extrai (fluxo, observações) de uma linha da tabela do log operacional."""
    stripped = line.strip()
    if not stripped.startswith("|") or ":--" in stripped:
        return None
    cells = [cell.strip() for cell in stripped.strip("|").split("|")]
    if len(cells) < 4:
        return None
    return cells[1].strip("`"), cells[3]


def resolve_result(
    flow_name: str,
    return_code: int,
    delta: int,
    zero_delta_streak: int,
    stats: Optional[FlowStats],
) -> str:
    """
    Traduz o resultado da execução para o log operacional.

    Distinção que o runner não fazia: um source que devolve **zero itens
    brutos** está quebrado; um que devolve **zero itens novos** apenas não
    encontrou novidade. Só o segundo caso é sucesso.
    """
    if return_code != 0:
        return RESULT_FAILURE
    if stats is not None:
        # Com estatísticas, a saúde da origem é fato, não inferência: zero itens
        # brutos é scraper quebrado, e qualquer item prova que a origem respondeu.
        # A regra de sequência abaixo é só um proxy para quando não há esse dado,
        # e deve ceder à evidência — do contrário um fluxo saudável cujo portal
        # não publica nada há semanas fica em alerta permanente, e alerta que
        # dispara sempre não informa nada.
        # Itens adiados por falta de IA (cota diária, chave) não se perdem, mas
        # o portal fica sem eles até a próxima execução: isso precisa aparecer.
        if stats.source_returned_nothing or stats.deferred_count > 0 or stats.closing_blocked:
            return RESULT_WARNING
        return RESULT_SUCCESS
    if REGISTRY_KEYS[flow_name] in LOW_VOLUME_FLOWS:
        return RESULT_SUCCESS
    if delta == 0 and zero_delta_streak >= ZERO_DELTA_ALERT_THRESHOLD:
        return RESULT_WARNING
    return RESULT_SUCCESS


def build_observations(
    flow_name: str,
    before_counts: dict[str, int],
    after_counts: dict[str, int],
    output_json_count: int,
    non_json: list[str],
    return_code: int,
    stats: Optional[FlowStats] = None,
    zero_delta_streak: int = 0,
) -> tuple[str, str]:
    registry_key = REGISTRY_KEYS[flow_name]
    added = after_counts[registry_key] - before_counts[registry_key]
    non_json_message = "nenhum" if not non_json else ", ".join(non_json)
    result = resolve_result(
        flow_name=flow_name,
        return_code=return_code,
        delta=added,
        zero_delta_streak=zero_delta_streak,
        stats=stats,
    )
    observations = (
        f"Registry `{registry_key}`: {before_counts[registry_key]} -> "
        f"{after_counts[registry_key]} (delta {added}); "
        f"`data/output/` com {output_json_count} JSONs; "
        f"arquivos não-JSON: {non_json_message}."
    )
    if stats is not None:
        observations += f" Origem devolveu {stats.raw_count} itens brutos."
        if stats.source_returned_nothing:
            observations += (
                " A origem não devolveu nenhum item — verificar se o portal mudou."
            )
        if stats.created is not None:
            observations += (
                f" {stats.created} novas, {stats.updated} alteradas,"
                f" {stats.unchanged} sem mudança, {stats.closed} encerradas."
            )
        if stats.closing_blocked:
            observations += " Encerramento por ausência suspenso (ver log do fluxo)."
        if stats.deferred_count > 0:
            observations += (
                f" {stats.deferred_count} itens adiados por limite da IA;"
                " serão tentados na próxima execução."
            )
    if result == RESULT_WARNING and stats is None and added == 0:
        observations += (
            f" Sem editais novos há {zero_delta_streak} execuções seguidas —"
            " verificar se o source ainda funciona."
        )
    if return_code != 0:
        observations += f" Fluxo encerrou com exit code {return_code}."
    return result, observations.replace("|", "/")


def append_flow_log_row(
    workdir: Path,
    flow_name: str,
    before_counts: dict[str, int],
    return_code: int,
    stats: Optional[FlowStats] = None,
) -> str:
    after_counts = load_registry_counts(workdir)
    output_json_count, non_json = get_output_file_stats(workdir)
    registry_key = REGISTRY_KEYS[flow_name]
    added = after_counts[registry_key] - before_counts[registry_key]
    zero_delta_streak = (
        count_zero_delta_streak(workdir, flow_name) + 1 if added == 0 else 0
    )
    result, observations = build_observations(
        flow_name,
        before_counts,
        after_counts,
        output_json_count,
        non_json,
        return_code,
        stats=stats,
        zero_delta_streak=zero_delta_streak,
    )

    log_path = workdir / LOG_PATH
    if not log_path.exists():
        return result

    row = (
        f"| {current_timestamp()} | `{flow_name}` | {result} | "
        f"{observations} |\n"
    )
    lines = log_path.read_text(encoding="utf-8").splitlines(keepends=True)
    try:
        insert_at = lines.index(LOG_SEPARATOR) + 1
    except ValueError:
        insert_at = len(lines)
    lines.insert(insert_at, row)
    log_path.write_text("".join(lines), encoding="utf-8")
    return result


def _kill_process_group(process: subprocess.Popen) -> None:
    """
    Encerra o fluxo e todos os processos que ele abriu (ex.: o Chromium do Playwright).

    Primeiro SIGTERM, para o fluxo poder limpar; se não sair a tempo, SIGKILL.
    """
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=TERMINATE_GRACE_SEC)
            return
        except subprocess.TimeoutExpired:
            continue


def run_command_capturing_output(
    command: list[str],
    workdir: Path,
    timeout_sec: int = DEFAULT_FLOW_TIMEOUT_SEC,
) -> tuple[int, str]:
    """
    Executa o fluxo repassando a saída ao console e guardando-a para análise.

    O teto vale pelo relógio, mesmo que o fluxo não escreva nada: uma thread lê a
    saída enquanto a principal espera o processo com prazo. Antes o prazo só era
    conferido a cada linha recebida, e um fluxo travado em silêncio segurava o job
    até a plataforma matá-lo, sem commit. Ao estourar, o grupo de processos
    inteiro é encerrado, para não deixar navegador órfão.
    """
    process = subprocess.Popen(
        command,
        cwd=workdir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    captured: list[str] = []
    assert process.stdout is not None

    def relay_output() -> None:
        for line in process.stdout:
            print(line, end="")
            captured.append(line)

    reader = threading.Thread(target=relay_output, daemon=True)
    reader.start()
    try:
        process.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        _kill_process_group(process)
        reader.join(timeout=TERMINATE_GRACE_SEC)
        aviso = f"[run_all_flows] Teto de {timeout_sec}s excedido; encerrando o fluxo.\n"
        print(aviso, end="")
        captured.append(aviso)
        return TIMEOUT_RETURN_CODE, "".join(captured)
    reader.join()
    return process.returncode, "".join(captured)


def run_flow(
    name: str,
    command: list[str],
    workdir: Path,
    timeout_sec: int = DEFAULT_FLOW_TIMEOUT_SEC,
) -> str:
    """
    Executa um fluxo e devolve o resultado registrado no log.

    Não interrompe a execução em caso de falha: com oito fontes, uma indisponível
    zerava a coleta de todas as outras. O erro é registrado e o runner segue; o
    exit code final reflete que houve falha, então a informação não se perde.
    """
    print(f"[run_all_flows] Starting {name} flow...")
    before_counts = load_registry_counts(workdir)
    return_code, output = run_command_capturing_output(command, workdir, timeout_sec)
    stats = parse_flow_stats(output)
    result = append_flow_log_row(workdir, name, before_counts, return_code, stats)
    if return_code != 0:
        print(
            f"[run_all_flows] {name} flow failed with exit code {return_code}; "
            "seguindo para os demais."
        )
    elif result == RESULT_WARNING:
        print(
            f"[run_all_flows] {name} flow completed with warnings — "
            "ver docs/flow_processing_log.md."
        )
    else:
        print(f"[run_all_flows] {name} flow completed successfully.")
    return result


def select_flows(only: Optional[str]) -> list:
    """
    Fluxos a executar. Sem `--only`, as fontes diárias; com, apenas as nomeadas.

    Fontes opcionais (HORIZON) só entram quando citadas. A ordem é sempre a
    canônica: diárias primeiro, depois opcionais. Nome desconhecido é erro
    explícito em vez de silenciosamente nada rodar.
    """
    if not only:
        return list(FLOW_COMMANDS)
    todos = FLOW_COMMANDS + OPTIONAL_FLOW_COMMANDS
    pedidos = [nome.strip().upper() for nome in only.split(",") if nome.strip()]
    conhecidos = {nome for nome, _ in todos}
    desconhecidos = [nome for nome in pedidos if nome not in conhecidos]
    if desconhecidos:
        raise SystemExit(
            f"[run_all_flows] Fluxo desconhecido: {', '.join(desconhecidos)}. "
            f"Disponíveis: {', '.join(sorted(conhecidos))}."
        )
    return [(nome, cmd) for nome, cmd in todos if nome in pedidos]


def refresh_edital_status(workdir: Path) -> None:
    """
    Realinha o `status` dos editais já gravados ao respectivo prazo.

    Um edital coletado como `aberto` não se corrige sozinho quando o prazo passa,
    então isso precisa rodar em toda execução — sem isso, o portal exibe
    oportunidade encerrada como vigente. Não remove arquivo algum.
    """
    from curate_output import curate  # import local: evita ciclo na importação

    print("[run_all_flows] Realinhando o status dos editais ao prazo...")
    curate(workdir=workdir, apply=True, refresh_status_only=True, today=date.today())


def resolve_flow_timeout(cli_value: Optional[int], environ=os.environ) -> int:
    """
    Teto por fonte, em segundos: opção de linha de comando > FLOW_TIMEOUT_SEC > padrão.

    Valor inválido é erro, não silêncio: um teto zerado mataria toda fonte e um
    texto qualquer cairia no padrão sem ninguém perceber.
    """
    if cli_value is not None:
        if cli_value <= 0:
            raise ValueError(f"--timeout precisa ser um inteiro positivo; recebeu {cli_value}.")
        return cli_value
    raw = environ.get(FLOW_TIMEOUT_ENV, "").strip()
    if not raw:
        return DEFAULT_FLOW_TIMEOUT_SEC
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value <= 0:
        raise ValueError(
            f"{FLOW_TIMEOUT_ENV} precisa ser um inteiro positivo (segundos); recebeu {raw!r}."
        )
    return value


# Fontes cujos editais passam por leitura de PDF com IA (OCR). Só eles ganham com
# a reextração quando o prompt muda; FINEP e Horizon vêm de dados estruturados.
OCR_SOURCES = frozenset({"fapes", "capes", "cnpq", "prppg_ifes", "proex_ifes", "conif"})


def select_reextraction_targets(output_dir: Path) -> dict[str, list[str]]:
    """Links dos editais abertos de fontes com OCR, agrupados por fonte (ordenados)."""
    targets: dict[str, list[str]] = {}
    for path in sorted(Path(output_dir).glob("*.json")):
        try:
            edital = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        fonte = (edital.get("fonte_key") or "").strip()
        link = (edital.get("link") or "").strip()
        if fonte in OCR_SOURCES and link and edital.get("status") == "aberto":
            targets.setdefault(fonte, []).append(link)
    return {fonte: sorted(links) for fonte, links in sorted(targets.items())}


STEP_SUMMARY_ENV = "GITHUB_STEP_SUMMARY"


def write_step_summary(resultados: dict, environ=os.environ) -> None:
    """
    Publica o resultado de cada fonte no resumo da execução do GitHub Actions.

    O passo de commit roda mesmo com falha parcial, então o job vermelho sozinho
    não diz o que falhou; o resumo diz. Fora do GitHub (variável ausente), nada
    é escrito.
    """
    summary_path = environ.get(STEP_SUMMARY_ENV)
    if not summary_path:
        return
    falhas = [nome for nome, r in resultados.items() if r == RESULT_FAILURE]
    linhas = ["### Resultado por fonte", "", "| Fonte | Resultado |", "| :-- | :-- |"]
    linhas += [f"| {nome} | {resultado} |" for nome, resultado in resultados.items()]
    if falhas:
        linhas += ["", f"**Falha em:** {', '.join(falhas)}"]
    with open(summary_path, "a", encoding="utf-8") as summary:
        summary.write("\n".join(linhas) + "\n")


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Runner unificado dos fluxos ETL.")
    parser.add_argument(
        "--only",
        help="executa apenas os fluxos nomeados, separados por vírgula (ex.: FINEP,CNPQ)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help=(
            "teto de duração por fluxo, em segundos; sobrepõe a variável "
            f"{FLOW_TIMEOUT_ENV} (padrão: {DEFAULT_FLOW_TIMEOUT_SEC}s = 40 min)"
        ),
    )
    parser.add_argument(
        "--reextrair-abertos",
        action="store_true",
        help=(
            "lê de novo, com a IA, os editais abertos das fontes com OCR "
            "(útil depois de mudar o prompt de extração)"
        ),
    )
    args = parser.parse_args(argv)
    try:
        timeout_sec = resolve_flow_timeout(args.timeout)
    except ValueError as erro:
        parser.error(str(erro))

    repo_root = Path(__file__).resolve().parents[1]
    selecionados = select_flows(args.only)

    reextract_links: set = set()
    reextract_file: Optional[str] = None
    if args.reextrair_abertos:
        targets = select_reextraction_targets(repo_root / OUTPUT_PATH)
        reextract_links = {link for links in targets.values() for link in links}
        print(
            f"[run_all_flows] Reextração: {len(reextract_links)} editais abertos em "
            f"{', '.join(targets) or 'nenhuma fonte'}."
        )
        selecionados = [
            (nome, cmd) for nome, cmd in selecionados if REGISTRY_KEYS.get(nome) in targets
        ]
        with tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False, encoding="utf-8"
        ) as lista:
            lista.write("\n".join(sorted(reextract_links)))
            reextract_file = lista.name
        os.environ[REEXTRACT_KEYS_ENV] = reextract_file

    resultados: dict[str, str] = {}
    try:
        for name, command in selecionados:
            resultados[name] = run_flow(name, command, repo_root, timeout_sec)
    finally:
        if reextract_file:
            os.environ.pop(REEXTRACT_KEYS_ENV, None)
            os.unlink(reextract_file)
    if args.reextrair_abertos:
        removidas = LocalJSONSink(str(repo_root / OUTPUT_PATH)).remove_older_duplicates(
            reextract_links
        )
        print(f"[run_all_flows] Duplicatas removidas: {removidas}.")

    refresh_edital_status(repo_root)

    falhas = [nome for nome, r in resultados.items() if r == RESULT_FAILURE]
    avisos = [nome for nome, r in resultados.items() if r == RESULT_WARNING]
    print(
        f"[run_all_flows] {len(resultados)} fluxos: "
        f"{len(resultados) - len(falhas) - len(avisos)} com sucesso, "
        f"{len(avisos)} com atenção, {len(falhas)} com falha."
    )
    if avisos:
        print(f"[run_all_flows] Atenção em: {', '.join(avisos)}")
    write_step_summary(resultados)
    if falhas:
        print(f"[run_all_flows] Falha em: {', '.join(falhas)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
