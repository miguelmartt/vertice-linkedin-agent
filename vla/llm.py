"""Claude Code en modo no interactivo, con tu suscripción.

En el VPS se autentica con CLAUDE_CODE_OAUTH_TOKEN (sale de `claude setup-token`
en tu ordenador). Se ejecuta sin herramientas (`--tools ""`), sin MCP, sin
ajustes de usuario ni de proyecto y en una carpeta vacía: solo genera texto,
validado contra un JSON Schema. Gasta del cupo de la suscripción, no de la API.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .db import DB
from .util import child_env, now


def claude_env() -> Dict[str, str]:
    """Solo lo que Claude Code necesita: ni los tokens del agente ni ANTHROPIC_API_KEY (siempre la suscripción)."""
    return child_env("DISABLE_AUTOUPDATER", prefixes=("CLAUDE_",))


class LLMError(Exception):
    pass


class LLM:
    def __init__(self, claude_bin: str, sandbox: Path, db: DB, tz: str, max_calls_per_day: int,
                 timeout: int, runner: Callable[..., Any] = subprocess.run):
        self.claude_bin = claude_bin
        self.sandbox = Path(sandbox)
        self.db = db
        self.tz = tz
        self.max_calls_per_day = max_calls_per_day
        self.timeout = timeout
        self.runner = runner

    def calls_left_today(self) -> int:
        return max(0, self.max_calls_per_day - self.db.llm_calls_on(now(self.tz).date().isoformat()))

    def run(self, task: str, system: str, prompt: str, schema: Dict[str, Any], model: str,
            timeout: Optional[int] = None) -> Dict[str, Any]:
        if self.calls_left_today() <= 0:
            raise LLMError(f"Tope diario de {self.max_calls_per_day} llamadas a Claude alcanzado")
        self.sandbox.mkdir(parents=True, exist_ok=True)
        cmd = [
            self.claude_bin, "-p",
            "--output-format", "json",
            "--json-schema", json.dumps(schema, ensure_ascii=False),
            "--tools", "",
            "--strict-mcp-config",
            "--setting-sources", "",
            "--no-session-persistence",
            "--model", model,
            "--system-prompt", system,
        ]
        started = time.monotonic()
        error: Optional[str] = None
        cost: Optional[float] = None
        try:
            proc = self.runner(
                cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                timeout=timeout or self.timeout, cwd=str(self.sandbox), env=claude_env(),
            )
            try:
                data = json.loads(proc.stdout or "{}")
            except json.JSONDecodeError:
                data = {}
            cost = data.get("total_cost_usd")
            if proc.returncode != 0 or data.get("is_error") or data.get("subtype") != "success":
                detail = data.get("result") or (proc.stderr or proc.stdout or "").strip()[-400:]
                error = f"Claude Code ha fallado ({data.get('subtype') or proc.returncode}): {detail}"
                raise LLMError(error)
            output = data.get("structured_output")
            if output is None:
                try:
                    output = json.loads(data.get("result") or "")
                except json.JSONDecodeError:
                    error = "Claude Code no ha devuelto JSON estructurado"
                    raise LLMError(error) from None
            return output
        except subprocess.TimeoutExpired:
            error = f"Claude Code no ha respondido en {timeout or self.timeout} s"
            raise LLMError(error) from None
        except FileNotFoundError:
            error = f"No encuentro el binario de Claude Code: {self.claude_bin}"
            raise LLMError(error) from None
        finally:
            self.db.log_llm(
                now(self.tz), task, error is None,
                int((time.monotonic() - started) * 1000), cost, error,
            )
