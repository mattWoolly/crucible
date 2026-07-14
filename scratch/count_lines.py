groups = {
    "orchestrator/": [
        "orchestrator/__init__.py",
        "orchestrator/anthropic_client.py",
        "orchestrator/augmentation.py",
        "orchestrator/budget.py",
        "orchestrator/config.py",
        "orchestrator/constants.py",
        "orchestrator/critic.py",
        "orchestrator/errors.py",
        "orchestrator/json_extract.py",
        "orchestrator/llm.py",
        "orchestrator/models.py",
        "orchestrator/operator.py",
        "orchestrator/orchestrator.py",
        "orchestrator/otel.py",
        "orchestrator/plan_validation.py",
        "orchestrator/providers.py",
        "orchestrator/redaction.py",
        "orchestrator/roles.py",
        "orchestrator/synthesizer.py",
        "orchestrator/worker.py",
    ],
    "orchestrator/tools/": [
        "orchestrator/tools/__init__.py",
        "orchestrator/tools/files.py",
        "orchestrator/tools/registry.py",
        "orchestrator/tools/shell.py",
    ],
    "orchestrator/tui/": [
        "orchestrator/tui/__init__.py",
        "orchestrator/tui/app.py",
        "orchestrator/tui/commands.py",
        "orchestrator/tui/logo.py",
        "orchestrator/tui/observer.py",
        "orchestrator/tui/permissions.py",
        "orchestrator/tui/stats.py",
    ],
    "root/": ["run.py"],
}

grand = 0
for gname, files in groups.items():
    subtotal = 0
    for f in files:
        n = sum(1 for _ in open(f, encoding="utf-8"))
        subtotal += n
        print(f"  {f:42s} {n:5d}")
    print(f"  {gname:42s} ---- {subtotal:5d}  (subtotal)")
    grand += subtotal
print(f"GRAND TOTAL (non-blank source incl. comments): {grand}")
