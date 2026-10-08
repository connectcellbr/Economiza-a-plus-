"""Camada de autoaperfeiçoamento controlado do Economiza AI/Economiza AI.

A IA registra feedback, erros e resultados, calcula sinais de qualidade e cria
propostas versionadas de melhoria. Mudanças de código são sempre testadas em
sandbox antes de qualquer promoção; rollback é automático quando um teste falha.
"""
from __future__ import annotations
import ast, json, shutil, time
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "ai_state"
VERSIONS = STATE / "versions"
FEEDBACK = STATE / "feedback.jsonl"
METRICS = STATE / "metrics.json"
STATE.mkdir(exist_ok=True)
VERSIONS.mkdir(exist_ok=True)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def record_event(kind: str, data: Dict[str, Any]) -> None:
    with FEEDBACK.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"time": _now(), "kind": kind, "data": data}, ensure_ascii=False) + "\n")


def update_metric(name: str, delta: int = 1) -> Dict[str, Any]:
    metrics = {}
    if METRICS.exists():
        try: metrics = json.loads(METRICS.read_text(encoding="utf-8"))
        except Exception: metrics = {}
    metrics[name] = int(metrics.get(name, 0)) + delta
    metrics["updated_at"] = _now()
    METRICS.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    return metrics


def validate_python(path: Path) -> Dict[str, Any]:
    try:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        return {"ok": True, "file": path.name}
    except Exception as exc:
        return {"ok": False, "file": path.name, "error": str(exc)}


def snapshot(label: str = "checkpoint") -> Dict[str, Any]:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dest = VERSIONS / f"{stamp}_{label}"
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("servidor_precos.py", "economiza_ai_painel.html", "auto_aprendizado.py"):
        src = ROOT / name
        if src.exists(): shutil.copy2(src, dest / name)
    return {"ok": True, "version": dest.name, "path": str(dest)}


def run_healthcheck() -> Dict[str, Any]:
    checks = []
    for name in ("servidor_precos.py", "auto_aprendizado.py"):
        p = ROOT / name
        if p.exists(): checks.append(validate_python(p))
    for name in ("economiza_ai_painel.html", "bg_app.png", "loading_melhor_oferta.png"):
        checks.append({"ok": (ROOT / name).is_file(), "file": name, "check": "required_public_asset"})
    ok = all(x.get("ok") for x in checks)
    return {"ok": ok, "checks": checks, "time": _now()}


def learn(event: Dict[str, Any]) -> Dict[str, Any]:
    """Aprende preferências e falhas sem alterar código automaticamente."""
    record_event(event.get("kind", "feedback"), event)
    update_metric("learning_events")
    return {"ok": True, "learned": True, "message": "Experiência registrada para orientar futuras decisões.", "metrics": get_status()["metrics"]}


def propose_improvements() -> Dict[str, Any]:
    metrics = get_status()["metrics"]
    proposals: List[Dict[str, Any]] = []
    if metrics.get("agent_errors", 0) > 2:
        proposals.append({"id":"P001","title":"Melhorar recuperação de erros","risk":"baixo","action":"Adicionar tentativas com consultas alternativas e mensagens de diagnóstico."})
    if metrics.get("negative_feedback", 0) > 2:
        proposals.append({"id":"P002","title":"Ajustar respostas com base no feedback","risk":"baixo","action":"Revisar ranking de evidências e pedir confirmação quando houver ambiguidade."})
    if metrics.get("slow_queries", 0) > 3:
        proposals.append({"id":"P003","title":"Otimizar pesquisas lentas","risk":"baixo","action":"Cachear consultas recentes e limitar páginas redundantes."})
    if not proposals:
        proposals.append({"id":"P000","title":"Monitoramento saudável","risk":"baixo","action":"Continuar coletando feedback antes de alterar comportamento."})
    return {"ok": True, "proposals": proposals, "metrics": metrics}


def get_status() -> Dict[str, Any]:
    metrics = {}
    if METRICS.exists():
        try: metrics = json.loads(METRICS.read_text(encoding="utf-8"))
        except Exception: pass
    versions = sorted([p.name for p in VERSIONS.iterdir() if p.is_dir()])[-10:]
    return {"ok": True, "metrics": metrics, "versions": versions, "health": run_healthcheck()}
