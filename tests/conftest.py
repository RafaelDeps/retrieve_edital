from datetime import date

import pytest

# Opção --fake-date: congela o relógio da sessão inteira numa data simulada.
# Serve para provar que a suíte não depende da data real (Constituição, Princípio VI):
# um teste que só passa "hoje" vira falha quando rodado com uma data futura.
FAKE_DATE_OPTION = "--fake-date"
FAKE_DATE_FORMAT = "AAAA-MM-DD"
# Módulos que continuam vendo o relógio real: durações do próprio pytest e os
# prazos de espera de processos/threads (o teto do runner usa process.wait com
# timeout; com o relógio congelado, ele estouraria na hora).
FREEZE_IGNORED_MODULES = ["_pytest", "pluggy", "subprocess", "threading"]

_active_freezer = None


def pytest_addoption(parser):
    parser.addoption(
        FAKE_DATE_OPTION,
        action="store",
        default=None,
        metavar=FAKE_DATE_FORMAT,
        help="Roda a suíte com o relógio congelado nesta data (ex.: 2030-06-15).",
    )


def pytest_configure(config):
    global _active_freezer
    fake_date = config.getoption(FAKE_DATE_OPTION)
    if not fake_date:
        return
    try:
        date.fromisoformat(fake_date)
    except ValueError as error:
        raise pytest.UsageError(
            f"{FAKE_DATE_OPTION} espera uma data no formato {FAKE_DATE_FORMAT}; "
            f"recebeu {fake_date!r}."
        ) from error

    from freezegun import freeze_time

    _active_freezer = freeze_time(fake_date, tick=True, ignore=FREEZE_IGNORED_MODULES)
    _active_freezer.start()


def pytest_unconfigure(config):
    global _active_freezer
    if _active_freezer is not None:
        _active_freezer.stop()
        _active_freezer = None


# Grupo de integração: testes que acessam site real e exigem o navegador do
# Playwright. Ficam sempre pulados na suíte padrão, em qualquer máquina, e só
# rodam quando selecionados com `pytest -m integration`.
INTEGRATION_MARKER = "integration"
INTEGRATION_SKIP_REASON = (
    "teste de integração (site real + navegador): rode com pytest -m integration"
)


def pytest_collection_modifyitems(config, items):
    marker_expression = config.getoption("-m") or ""
    if INTEGRATION_MARKER in marker_expression:
        return
    skip_integration = pytest.mark.skip(reason=INTEGRATION_SKIP_REASON)
    for item in items:
        if item.get_closest_marker(INTEGRATION_MARKER):
            item.add_marker(skip_integration)


# Fixture global de mock ou configuração inicial se necessário.
# Por hora, deixaremos vazio pois cada step map cuidará dos seus próprios dados raw.

@pytest.fixture
def raw_fapes_html_mock():
    return """
    <html>
        <body>
            <div data-titulo="Edital Fapes/CNPq 01/2026 - Tecnologia">Edital de Teste</div>
        </body>
    </html>
    """


# Variáveis de IA que nenhum teste offline deve herdar do ambiente nem do `.env`
# (os fluxos chamam load_dotenv ao serem importados): com a chave real no
# ambiente, a fábrica criaria um cliente de verdade e o teste abriria rede.
AI_ENV_VARS = ("LLM_PROVIDER", "GEMINI_API_KEY", "MISTRAL_API_KEY", "GEMINI_MODEL")


@pytest.fixture(autouse=True)
def _isolated_extraction_service(request, monkeypatch):
    """Cada teste começa sem instância de IA em cache e sem configuração herdada."""
    from src.components.transforms.extraction_service import reset_extraction_service

    if request.node.get_closest_marker("integration") is None:
        for name in AI_ENV_VARS:
            monkeypatch.delenv(name, raising=False)
    reset_extraction_service()
    yield
    reset_extraction_service()
