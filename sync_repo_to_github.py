from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent

FILES_TO_SYNC = [
    "exports",
    "industria_export_sync.db",
    "rapport_qualite_catalogue.csv",
    "rapport_qualite_catalogue.xlsx",
    "historique_audit.csv",
    "processed_exports.txt",
]


def run_git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=BASE_DIR,
        text=True,
        capture_output=True,
        check=check,
    )


def has_changes() -> bool:
    result = run_git("status", "--porcelain", check=False)
    return bool(result.stdout.strip())


def main() -> None:
    existing_paths = [path for path in FILES_TO_SYNC if (BASE_DIR / path).exists()]
    if not existing_paths:
        print("Aucun fichier de données à synchroniser.")
        return

    run_git("add", *existing_paths)

    if not has_changes():
        print("Aucun changement GitHub à envoyer.")
        return

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    run_git("commit", "-m", f"Sync audit data {timestamp}")
    run_git("push", "origin", "main")
    print("Données synchronisées sur GitHub.")


if __name__ == "__main__":
    main()
