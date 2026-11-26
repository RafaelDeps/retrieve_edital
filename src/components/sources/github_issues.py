"""
Cliente mínimo da API do GitHub para as indicações da comunidade (spec 009).

Lê as issues abertas do portal (repositório público: o `GITHUB_TOKEN` do
workflow só aumenta o limite de leitura) e, quando existe `PORTAL_ISSUES_TOKEN`
— um token com permissão apenas de issues no portal —, comenta e fecha. Nenhum
token aparece em log: os erros mostram só o código HTTP.
"""

import logging
import os
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
COMUNIDADE_REPO_ENV = "COMUNIDADE_REPO"
DEFAULT_COMUNIDADE_REPO = "RafaelDeps/portal_edital"
READ_TOKEN_ENV = "GITHUB_TOKEN"
REPLY_TOKEN_ENV = "PORTAL_ISSUES_TOKEN"
TIMEOUT_SEC = 30
PAGE_SIZE = 100


class IndicacoesClient:
    def __init__(self, repo: Optional[str] = None, session: Optional[requests.Session] = None):
        self.repo = repo or os.getenv(COMUNIDADE_REPO_ENV) or DEFAULT_COMUNIDADE_REPO
        self.session = session or requests.Session()
        self._read_token = (os.getenv(READ_TOKEN_ENV) or "").strip()
        self._reply_token = (os.getenv(REPLY_TOKEN_ENV) or "").strip()

    @property
    def can_reply(self) -> bool:
        return bool(self._reply_token)

    def _headers(self, token: str) -> Dict[str, str]:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def list_open(self) -> List[dict]:
        """Issues abertas (sem pull requests), da mais antiga para a mais nova."""
        response = self.session.get(
            f"{GITHUB_API}/repos/{self.repo}/issues",
            params={"state": "open", "per_page": PAGE_SIZE, "sort": "created", "direction": "asc"},
            headers=self._headers(self._read_token),
            timeout=TIMEOUT_SEC,
        )
        if response.status_code != 200:
            raise requests.HTTPError(f"leitura das indicações: HTTP {response.status_code}")
        return [item for item in response.json() if "pull_request" not in item]

    def comment(self, number: int, text: str) -> None:
        response = self.session.post(
            f"{GITHUB_API}/repos/{self.repo}/issues/{number}/comments",
            json={"body": text},
            headers=self._headers(self._reply_token),
            timeout=TIMEOUT_SEC,
        )
        if response.status_code not in (200, 201):
            raise requests.HTTPError(f"comentário na indicação #{number}: HTTP {response.status_code}")

    def close(self, number: int) -> None:
        response = self.session.patch(
            f"{GITHUB_API}/repos/{self.repo}/issues/{number}",
            json={"state": "closed", "state_reason": "completed"},
            headers=self._headers(self._reply_token),
            timeout=TIMEOUT_SEC,
        )
        if response.status_code != 200:
            raise requests.HTTPError(f"fechar a indicação #{number}: HTTP {response.status_code}")
