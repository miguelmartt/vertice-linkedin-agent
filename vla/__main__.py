"""Línea de órdenes: `python -m vla <orden>`."""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import signal
import subprocess
import sys
from datetime import timedelta
from typing import List, Optional

from . import __version__, sources
from .config import Config, ConfigError, load_env_file
from .util import parse_date


def setup_logging() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s", stream=sys.stdout)


def cmd_run(cfg: Config) -> int:
    from .app import App

    missing = cfg.missing("telegram", "profile") + ([] if cfg.dry_run else cfg.missing("linkedin"))
    if missing:
        print("Faltan variables: " + ", ".join(missing), file=sys.stderr)
        return 2
    app = App(cfg)

    def stop(*_: object) -> None:
        app.stop.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    return 0 if app.run_forever() else 1


def cmd_check(cfg: Config, claude: bool = False) -> int:
    """Comprueba configuración y conexiones sin cambiar nada.

    Con `claude`, además hace una llamada real mínima a Claude Code (gasta una del cupo diario).
    """
    from .app import App

    ok = True

    def line(passed: bool, label: str, detail: str = "", required: bool = True) -> None:
        nonlocal ok
        ok = ok and (passed or not required)
        mark = "✔" if passed else ("✘" if required else "·")
        print(f"{mark} {label}{': ' + detail if detail else ''}")

    for feature in ("telegram", "linkedin", "profile"):
        missing = cfg.missing(feature)
        line(not missing, f"Variables de {feature}", ", ".join(missing) if missing else "completas",
             required=not (feature == "linkedin" and cfg.dry_run))
    path = shutil.which(cfg.claude_bin)
    if path:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=60)
        line(out.returncode == 0, "Claude Code", (out.stdout or out.stderr).strip().splitlines()[0]
             if (out.stdout or out.stderr) else "")
    else:
        line(False, "Claude Code", f"no encuentro {cfg.claude_bin}")
    line(bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")), "CLAUDE_CODE_OAUTH_TOKEN",
         "definido" if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") else "falta (sale de `claude setup-token`)")

    app = App(cfg)
    if cfg.telegram_token:
        try:
            me = app.tg.me()
            line(True, "Telegram", f"@{me.get('username')}")
            try:
                app.tg.chat()
                line(True, "Chat con el bot", "abierto")
            except Exception as exc:
                # El servicio arranca igual y te contesta en cuanto le escribas.
                hint = (f"abre @{me.get('username')} en Telegram y pulsa Iniciar"
                        if "chat not found" in str(exc) else str(exc)[:200])
                line(False, "Chat con el bot", hint, required=False)
        except Exception as exc:
            line(False, "Telegram", str(exc)[:200])
    profile = app.db.get_meta("li_profile") or {}
    expires = parse_date((app.db.get_meta("li_token") or {}).get("expires_at"))
    if profile.get("sub") and expires and expires > app.now():
        line(True, "LinkedIn", f"{profile.get('name')}, permiso hasta el {expires:%d/%m/%Y}")
    else:
        line(False, "LinkedIn", "sin conectar o caducado: /linkedin en Telegram", required=False)
    current = app.now()
    for label, fn in (
        ("Hugging Face", lambda: sources.huggingface(current, cfg.hf_min_likes)),
        ("GitHub", lambda: sources.github_trending(current, cfg.github_min_stars, cfg.github_owners, cfg.github_token)),
        ("Hacker News", lambda: sources.hackernews(current, cfg.hn_min_points)),
        ("Tus repos", lambda: sources.own_repos(cfg.github_owners, cfg.github_token, cfg.own_exclude, current)),
    ):
        try:
            line(True, label, f"{len(fn())} temas ahora")
        except Exception as exc:
            line(False, label, str(exc)[:200], required=False)
    if claude:
        try:
            schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
            result = app.llm.run("check", "Responde solo con el JSON pedido.", "Devuelve ok = true.", schema,
                                 cfg.model, timeout=180)
            line(result.get("ok") is True, "Claude Code responde", f"modelo {cfg.model}")
        except Exception as exc:
            line(False, "Claude Code responde", str(exc)[:300])
    print(f"Claude: {app.llm.calls_left_today()} llamadas disponibles hoy (tope {cfg.llm_max_calls_per_day})")
    return 0 if ok else 1


def cmd_collect(cfg: Config, limit: int) -> int:
    """Busca novedades, las guarda y enseña los mejores temas de cada tipo, sin Claude ni Telegram."""
    from .app import FRESH_DAYS, App

    app = App(cfg)
    new = app.collect()
    print(f"Temas nuevos: {sum(new.values())} {new}")
    cands = app.db.candidates((app.now() - timedelta(days=FRESH_DAYS)).isoformat())
    for kind in (*sources.OUTSIDE_KINDS, "own"):
        group = [c for c in cands if c["kind"] == kind][:limit]
        if group:
            print(f"\n{sources.KIND_LABEL[kind]}")
        for c in group:
            print(f"  {c['score']:4.2f}  {c['title'][:70]:70}  {sources.metrics_line(c)}")
    errors = (app.db.get_meta("last_collect") or {}).get("errors") or {}
    for name, error in errors.items():
        print(f"✘ {name}: {error}")
    return 0


def cmd_simple(cfg: Config, action: str) -> int:
    from .app import App

    app = App(cfg)
    if action == "draft":
        app.draft(manual=True)
    elif action == "status":
        print(app.status_text())
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vla", description="Agente de LinkedIn")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--env-file", help="archivo KEY=valor con la configuración (se lee como datos, sin shell)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="servicio: bot de Telegram, programador y trabajador")
    check = sub.add_parser("check", help="comprueba configuración y conexiones")
    check.add_argument("--claude", action="store_true", help="hace también una llamada real mínima a Claude Code")
    collect = sub.add_parser("collect", help="busca novedades y enseña los temas en consola")
    collect.add_argument("--limit", type=int, default=5)
    sub.add_parser("draft", help="prepara un borrador y lo manda por Telegram ahora")
    sub.add_parser("status", help="estado en consola")
    args = parser.parse_args(argv)

    setup_logging()
    try:
        if args.env_file:
            load_env_file(args.env_file)
        cfg = Config.from_env()
    except OSError as exc:
        print(f"No puedo leer {args.env_file}: {exc.strerror}", file=sys.stderr)
        return 2
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    if args.command == "run":
        return cmd_run(cfg)
    if args.command == "check":
        return cmd_check(cfg, claude=args.claude)
    if args.command == "collect":
        return cmd_collect(cfg, args.limit)
    return cmd_simple(cfg, args.command)


if __name__ == "__main__":
    sys.exit(main())
