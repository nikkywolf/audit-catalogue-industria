import os
import time
import subprocess

DOWNLOAD_FOLDER = os.path.expanduser(
    "~/Downloads/audit-catalogue-industria"
)

PROJECT_FOLDER = os.path.expanduser(
    "~/industria-apps/audit-catalogue-industria"
)

PROCESSED_FILE = os.path.join(
    PROJECT_FOLDER,
    "processed_exports.txt"
)

os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

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
        time.sleep(5)
        size_2 = os.path.getsize(path)

        return (
            size_1 == size_2
            and size_2 > 0
        )
    except Exception:
        return False

print("Surveillance du dossier d'exports Lightspeed...")
print(DOWNLOAD_FOLDER)
print("Watcher actif.")

while True:
    try:
        processed = load_processed()

        zip_files = [
            f for f in os.listdir(DOWNLOAD_FOLDER)
            if f.endswith(".zip")
            and f.startswith("products_export_")
        ]

        zip_files.sort()

        for filename in zip_files:

            if filename in processed:
                continue

            full_path = os.path.join(
                DOWNLOAD_FOLDER,
                filename
            )

            if not file_is_finished(full_path):
                continue

            print("")
            print(f"Nouvel export détecté : {filename}")
            print("Lancement de l'audit...")

            result = subprocess.run(
                ["python3", "audit_catalogue.py"],
                cwd=PROJECT_FOLDER
            )

            if result.returncode == 0:
                save_processed(filename)

                print("Audit terminé.")
                print(
                    f"Export marqué comme traité : {filename}"
                )
            else:
                print(
                    "Erreur pendant l'audit."
                )

        time.sleep(5)

    except Exception as e:
        print(f"ERREUR WATCHER: {e}")
        time.sleep(10)