import os
import time
import subprocess
import shutil
from datetime import datetime

PROJECT_FOLDER = os.path.expanduser(
    "~/industria-apps/audit-catalogue-industria"
)

INCOMING_EXPORTS_FOLDER = os.path.join(PROJECT_FOLDER, "incoming_exports")
EXPORTS_FOLDER = os.path.join(PROJECT_FOLDER, "exports")

DOWNLOAD_FOLDERS = [
    INCOMING_EXPORTS_FOLDER,
    os.path.expanduser("~/Downloads/audit-catalogue-industria"),
    os.path.expanduser("~/Downloads"),
]

PROCESSED_FILE = os.path.join(
    PROJECT_FOLDER,
    "processed_exports.txt"
)

CHECK_INTERVAL_SECONDS = 20
DOWNLOAD_STABILITY_SECONDS = 8
INACCESSIBLE_LOG_INTERVAL_SECONDS = 600

os.makedirs(EXPORTS_FOLDER, exist_ok=True)
os.makedirs(INCOMING_EXPORTS_FOLDER, exist_ok=True)
for folder in DOWNLOAD_FOLDERS:
    if folder.startswith(os.path.expanduser("~/Downloads")):
        continue
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError:
        pass

last_inaccessible_log = {}

def load_processed():
    if not os.path.exists(PROCESSED_FILE):
        return set()

    with open(PROCESSED_FILE, "r") as f:
        return set(
            line.strip()
            for line in f
            if line.strip()
        )

def save_processed(filename):
    with open(PROCESSED_FILE, "a") as f:
        f.write(filename + "\n")

def file_is_finished(path):
    try:
        size_1 = os.path.getsize(path)
        time.sleep(DOWNLOAD_STABILITY_SECONDS)
        size_2 = os.path.getsize(path)

        return (
            size_1 == size_2
            and size_2 > 0
        )
    except Exception:
        return False

def log(message):
    print(
        f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}",
        flush=True
    )

def copy_export_to_repo(path, filename):
    destination = os.path.join(EXPORTS_FOLDER, filename)
    if os.path.abspath(path) != os.path.abspath(destination) and not os.path.exists(destination):
        shutil.copy2(path, destination)
    return destination

log("Surveillance des dossiers d'exports Lightspeed pour la v2...")
for folder in DOWNLOAD_FOLDERS:
    log(folder)
log(f"Projet audité : {PROJECT_FOLDER}")
log("Watcher actif.")

while True:
    try:
        processed = load_processed()

        zip_files = []
        for folder in DOWNLOAD_FOLDERS:
            try:
                folder_files = [
                    (folder, f)
                    for f in os.listdir(folder)
                    if f.endswith(".zip")
                    and f.startswith("products_export_")
                ]
                zip_files.extend(folder_files)
            except OSError as e:
                now = time.time()
                if now - last_inaccessible_log.get(folder, 0) >= INACCESSIBLE_LOG_INTERVAL_SECONDS:
                    log(f"Dossier non accessible pour le watcher: {folder} ({e})")
                    last_inaccessible_log[folder] = now
                continue

        zip_files.sort()

        for folder, filename in zip_files:

            if filename in processed:
                continue

            full_path = os.path.join(
                folder,
                filename
            )

            if not file_is_finished(full_path):
                continue

            log("")
            log(f"Nouvel export détecté : {filename}")
            audit_export_path = copy_export_to_repo(full_path, filename)
            log("Lancement de l'audit...")

            env = os.environ.copy()
            env["AUDIT_EXPORT_FILE"] = audit_export_path
            result = subprocess.run(
                ["/usr/bin/python3", "audit_catalogue.py"],
                cwd=PROJECT_FOLDER,
                env=env,
            )

            if result.returncode == 0:
                save_processed(filename)

                log("Audit terminé.")
                log(f"Export marqué comme traité : {filename}")

                sync_result = subprocess.run(
                    ["/usr/bin/python3", "sync_repo_to_github.py"],
                    cwd=PROJECT_FOLDER
                )
                if sync_result.returncode == 0:
                    log("GitHub synchronisé.")
                else:
                    log("Audit terminé, mais la synchronisation GitHub a échoué.")
            else:
                log("Erreur pendant l'audit.")

        time.sleep(CHECK_INTERVAL_SECONDS)

    except Exception as e:
        log(f"ERREUR WATCHER: {e}")
        time.sleep(CHECK_INTERVAL_SECONDS)
