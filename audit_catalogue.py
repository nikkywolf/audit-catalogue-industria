import pandas as pd
import re
import os
import glob
import zipfile
import tempfile
import subprocess
from datetime import datetime
from collections import Counter
from urllib.parse import urlparse, unquote
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

DOWNLOAD_FOLDER = os.path.expanduser("~/Downloads/audit-catalogue-industria")

zip_files = glob.glob(
    os.path.join(DOWNLOAD_FOLDER, "products_export_*.zip")
)

csv_files = glob.glob(
    os.path.join(DOWNLOAD_FOLDER, "products_export_*.csv")
)

if zip_files:
    latest_zip = max(zip_files, key=os.path.getmtime)

    extract_folder = os.path.join(DOWNLOAD_FOLDER, "_extracted")
    os.makedirs(extract_folder, exist_ok=True)

    with zipfile.ZipFile(latest_zip, "r") as zip_ref:
        zip_ref.extractall(extract_folder)

    extracted_csv_files = glob.glob(
        os.path.join(extract_folder, "*.csv")
    )

    if not extracted_csv_files:
        raise FileNotFoundError("Aucun CSV trouvé dans le fichier ZIP.")

    INPUT_FILE = max(extracted_csv_files, key=os.path.getmtime)
    print(f"ZIP utilisé : {latest_zip}")
    print(f"CSV extrait utilisé : {INPUT_FILE}")

elif csv_files:
    INPUT_FILE = max(csv_files, key=os.path.getmtime)
    print(f"CSV utilisé : {INPUT_FILE}")

else:
    raise FileNotFoundError(
        "Aucun fichier products_export_*.zip ou products_export_*.csv trouvé dans ~/Downloads/audit-catalogue-industria"
    )

OUTPUT_CSV = "rapport_qualite_catalogue.csv"
OUTPUT_EXCEL = "rapport_qualite_catalogue.xlsx"

print(f"CSV utilisé : {INPUT_FILE}")

def is_empty(value):
    return pd.isna(value) or str(value).strip() == ""

def normalize_text(value):
    if is_empty(value):
        return ""
    return str(value).lower().strip()

def is_discontinued(row):
    titles = [
        row.get("FR_Title_Short", ""),
        row.get("FR_Title_Long", ""),
        row.get("US_Title_Short", ""),
        row.get("US_Title_Long", ""),
        row.get("FC_Title_Short", ""),
        row.get("FC_Title_Long", ""),
    ]
    text = " ".join([str(t).lower() for t in titles])
    return "***" in text or "(discontinué)" in text or "(discontinued)" in text

def brand_in_title(row, title_column):
    brand = normalize_text(row.get("Brand", ""))
    title = normalize_text(row.get(title_column, ""))
    if not brand or not title:
        return False
    return brand in title

def extract_image_filenames(images_value):
    if is_empty(images_value):
        return []

    raw = str(images_value)
    parts = re.split(r"[,|;]", raw)
    filenames = []

    for part in parts:
        part = part.strip()
        if not part:
            continue

        parsed = urlparse(part)
        path = parsed.path if parsed.path else part
        filename = unquote(path.split("/")[-1]).strip()

        if filename:
            filenames.append(filename)

    return filenames

def image_name_is_bad(filename):
    name = filename.lower()

    bad_patterns = [
        r"^img[_-]?\d+",
        r"^dsc[_-]?\d+",
        r"^image[_-]?\d*",
        r"^photo[_-]?\d*",
        r"^capture",
        r"^screenshot",
        r"untitled",
        r"copie",
        r"copy",
        r"\s",
    ]

    for pattern in bad_patterns:
        if re.search(pattern, name):
            return True

    if len(name) < 8:
        return True

    return False

def has_bad_image_name(images_value):
    filenames = extract_image_filenames(images_value)

    if not filenames:
        return False

    return any(image_name_is_bad(filename) for filename in filenames)

def has_h2_any(html, titles):
    if is_empty(html):
        return False
    for title in titles:
        pattern = rf"<h2>\s*{re.escape(title)}\s*</h2>"
        if re.search(pattern, str(html), flags=re.IGNORECASE):
            return True
    return False

def section_is_empty_any(html, titles):
    if is_empty(html):
        return True
    for title in titles:
        pattern = rf"<h2>\s*{re.escape(title)}\s*</h2>(.*?)(<h2>|$)"
        match = re.search(pattern, str(html), flags=re.IGNORECASE | re.DOTALL)
        if match:
            content = re.sub("<.*?>", "", match.group(1)).strip()
            return content == ""
    return False

def split_errors(series):
    counter = Counter()
    for value in series.dropna():
        value = str(value).strip()
        if not value:
            continue
        errors = [e.strip() for e in value.split("|") if e.strip()]
        counter.update(errors)
    return counter

def audit_row(row):
    score = 100
    critical = []
    major = []
    minor = []
    catalog_alerts = []
    correction_types = set()

    images = row.get("Images")

    if is_empty(images):
        score -= 15
        critical.append("Image manquante")
        correction_types.add("Image à ajouter")
    elif has_bad_image_name(images):
        score -= 3
        minor.append("Image possiblement mal nommée")
        correction_types.add("Image à renommer")

    if is_empty(row.get("FC_Description_Short")):
        score -= 10
        critical.append("FC description courte manquante")
        correction_types.add("Contenu à créer")

    if is_empty(row.get("US_Description_Short")):
        score -= 10
        critical.append("US description courte manquante")
        correction_types.add("Contenu à créer")

    fc_long = row.get("FC_Description_Long")
    us_long = row.get("US_Description_Long")

    if is_empty(fc_long):
        score -= 15
        critical.append("FC description longue manquante")
        correction_types.add("Contenu à créer")

    if is_empty(us_long):
        score -= 15
        critical.append("US description longue manquante")
        correction_types.add("Contenu à créer")

    if not is_empty(fc_long):
        if not has_h2_any(fc_long, ["DESCRIPTION"]):
            score -= 5
            major.append("FC H2 DESCRIPTION manquant")
            correction_types.add("HTML à corriger")
        elif section_is_empty_any(fc_long, ["DESCRIPTION"]):
            score -= 10
            critical.append("FC section DESCRIPTION vide")
            correction_types.add("HTML à corriger")

        if not has_h2_any(fc_long, ["UTILISATION"]):
            score -= 5
            major.append("FC H2 UTILISATION manquant")
            correction_types.add("HTML à corriger")
        elif section_is_empty_any(fc_long, ["UTILISATION"]):
            score -= 10
            critical.append("FC section UTILISATION vide")
            correction_types.add("HTML à corriger")

        if has_h2_any(fc_long, ["INGRÉDIENTS"]) and section_is_empty_any(fc_long, ["INGRÉDIENTS"]):
            score -= 10
            critical.append("FC section INGRÉDIENTS vide")
            correction_types.add("HTML à corriger")

    if not is_empty(us_long):
        if not has_h2_any(us_long, ["DESCRIPTION"]):
            score -= 5
            major.append("US H2 DESCRIPTION manquant")
            correction_types.add("HTML à corriger")
        elif section_is_empty_any(us_long, ["DESCRIPTION"]):
            score -= 10
            critical.append("US section DESCRIPTION vide")
            correction_types.add("HTML à corriger")

        if not has_h2_any(us_long, ["HOW TO USE", "USE"]):
            score -= 5
            major.append("US H2 HOW TO USE / USE manquant")
            correction_types.add("HTML à corriger")
        elif section_is_empty_any(us_long, ["HOW TO USE", "USE"]):
            score -= 10
            critical.append("US section HOW TO USE / USE vide")
            correction_types.add("HTML à corriger")

        if has_h2_any(us_long, ["INGREDIENTS"]) and section_is_empty_any(us_long, ["INGREDIENTS"]):
            score -= 10
            critical.append("US section INGREDIENTS vide")
            correction_types.add("HTML à corriger")

    if is_empty(row.get("FC_Meta_Title")):
        score -= 5
        major.append("FC meta title manquant")
        correction_types.add("SEO à corriger")

    if is_empty(row.get("FC_Meta_Description")):
        score -= 5
        major.append("FC meta description manquante")
        correction_types.add("SEO à corriger")

    if is_empty(row.get("US_Meta_Title")):
        score -= 5
        major.append("US meta title manquant")
        correction_types.add("SEO à corriger")

    if is_empty(row.get("US_Meta_Description")):
        score -= 5
        major.append("US meta description manquante")
        correction_types.add("SEO à corriger")

    if is_empty(row.get("FC_URL")):
        score -= 3
        major.append("FC URL manquante")
        correction_types.add("SEO à corriger")

    if is_empty(row.get("US_URL")):
        score -= 3
        major.append("US URL manquante")
        correction_types.add("SEO à corriger")

    if is_empty(row.get("FR_Meta_Title")):
        score -= 2
        minor.append("FR meta title manquant")
        correction_types.add("SEO à corriger")

    if is_empty(row.get("FR_Meta_Description")):
        score -= 2
        minor.append("FR meta description manquante")
        correction_types.add("SEO à corriger")

    if is_empty(row.get("FR_URL")):
        score -= 2
        minor.append("FR URL manquante")
        correction_types.add("SEO à corriger")

    if is_empty(row.get("FC_Category_1")) and is_empty(row.get("US_Category_1")):
        score -= 5
        major.append("Catégorie principale manquante")
        correction_types.add("Catégorie à corriger")

    title_columns = [
        "FR_Title_Short",
        "FR_Title_Long",
        "FC_Title_Short",
        "FC_Title_Long",
        "US_Title_Short",
        "US_Title_Long",
    ]

    brand_found = any(brand_in_title(row, col) for col in title_columns)

    if brand_found:
        score -= 5
        major.append("Marque présente dans un titre")
        correction_types.add("Titre à corriger")

    visible = str(row.get("Visible", "")).strip().upper()
    backorder_disabled = str(row.get("Stock_Disable_Sold_Out", "")).strip().upper()

    if visible == "N":
        catalog_alerts.append("Produit invisible — vérifier si volontaire")

    if visible == "S" and backorder_disabled == "Y":
        catalog_alerts.append("Visible lorsque disponible — configuration normale")

    if visible == "S" and backorder_disabled != "Y":
        catalog_alerts.append("Visible lorsque disponible mais backorder autorisé — à vérifier")

    if visible == "Y" and backorder_disabled == "Y":
        catalog_alerts.append("Produit visible avec backorder désactivé — à vérifier")

    score = max(score, 0)

    if critical:
        priority = "Critique"
    elif major:
        priority = "Action requise"
    elif minor:
        priority = "À surveiller"
    else:
        priority = "Conforme"

    return (
        score,
        priority,
        " | ".join(critical),
        " | ".join(major),
        " | ".join(minor),
        " | ".join(catalog_alerts),
        " | ".join(sorted(correction_types)),
    )

def adjust_excel(writer):
    for sheet in writer.book.worksheets:
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions

        for cell in sheet[1]:
            cell.font = Font(bold=True)

        for column in sheet.columns:
            max_length = 0
            column_letter = get_column_letter(column[0].column)

            for cell in column:
                if cell.value is not None:
                    max_length = max(max_length, len(str(cell.value)))

            sheet.column_dimensions[column_letter].width = min(max_length + 3, 80)

df = pd.read_csv(INPUT_FILE, low_memory=False)

df["Discontinued"] = df.apply(is_discontinued, axis=1)
active_df = df[df["Discontinued"] == False].copy()

results = active_df.apply(audit_row, axis=1, result_type="expand")

active_df["Score"] = results[0]
active_df["Priorité"] = results[1]
active_df["Erreurs critiques"] = results[2]
active_df["Erreurs majeures"] = results[3]
active_df["Erreurs mineures"] = results[4]
active_df["Alertes catalogue"] = results[5]
active_df["Type de correction"] = results[6]

columns = [
    "Internal_ID",
    "Internal_Variant_ID",
    "Brand",
    "FC_Title_Short",
    "US_Title_Short",
    "SKU",
    "UPC",
    "Visible",
    "Stock_Disable_Sold_Out",
    "Score",
    "Priorité",
    "Type de correction",
    "Erreurs critiques",
    "Erreurs majeures",
    "Erreurs mineures",
    "Alertes catalogue",
]

available_columns = [col for col in columns if col in active_df.columns]
report_df = active_df[available_columns].copy()

summary_df = pd.DataFrame({
    "Indicateur": [
        "Produits total",
        "Produits discontinués exclus",
        "Produits actifs analysés",
        "Conformes",
        "À surveiller",
        "Action requise",
        "Critiques",
    ],
    "Valeur": [
        len(df),
        int(df["Discontinued"].sum()),
        len(active_df),
        int((active_df["Priorité"] == "Conforme").sum()),
        int((active_df["Priorité"] == "À surveiller").sum()),
        int((active_df["Priorité"] == "Action requise").sum()),
        int((active_df["Priorité"] == "Critique").sum()),
    ],
})

brand_summary = (
    active_df
    .groupby("Brand", dropna=False)
    .agg(
        Produits=("Brand", "size"),
        Score_moyen=("Score", "mean"),
        Conformes=("Priorité", lambda x: (x == "Conforme").sum()),
        A_surveillance=("Priorité", lambda x: (x == "À surveiller").sum()),
        Action_requise=("Priorité", lambda x: (x == "Action requise").sum()),
        Critiques=("Priorité", lambda x: (x == "Critique").sum()),
    )
    .reset_index()
)

brand_summary["Score_moyen"] = brand_summary["Score_moyen"].round(1)
brand_summary["% conformes"] = (brand_summary["Conformes"] / brand_summary["Produits"] * 100).round(1)
brand_summary["% critiques"] = (brand_summary["Critiques"] / brand_summary["Produits"] * 100).round(1)

brand_summary = brand_summary.sort_values(
    by=["Critiques", "Action_requise", "Produits"],
    ascending=[False, False, False]
)

top_critical_df = pd.DataFrame(split_errors(active_df["Erreurs critiques"]).most_common(50), columns=["Erreur critique", "Nombre"])
top_major_df = pd.DataFrame(split_errors(active_df["Erreurs majeures"]).most_common(50), columns=["Erreur majeure", "Nombre"])
top_minor_df = pd.DataFrame(split_errors(active_df["Erreurs mineures"]).most_common(50), columns=["Erreur mineure", "Nombre"])
catalog_alerts_df = pd.DataFrame(split_errors(active_df["Alertes catalogue"]).most_common(50), columns=["Alerte catalogue", "Nombre"])
correction_types_df = pd.DataFrame(split_errors(active_df["Type de correction"]).most_common(50), columns=["Type de correction", "Nombre"])

report_df.to_csv(OUTPUT_CSV, index=False)

with pd.ExcelWriter(OUTPUT_EXCEL, engine="openpyxl") as writer:
    summary_df.to_excel(writer, sheet_name="Résumé", index=False)
    brand_summary.to_excel(writer, sheet_name="Résumé par marque", index=False)
    report_df.to_excel(writer, sheet_name="Tous les produits", index=False)
    report_df[report_df["Priorité"] == "Critique"].to_excel(writer, sheet_name="Critiques", index=False)
    report_df[report_df["Priorité"] == "Action requise"].to_excel(writer, sheet_name="Action requise", index=False)
    report_df[report_df["Priorité"] == "À surveiller"].to_excel(writer, sheet_name="À surveiller", index=False)
    report_df[report_df["Alertes catalogue"] != ""].to_excel(writer, sheet_name="Alertes catalogue", index=False)
    top_critical_df.to_excel(writer, sheet_name="Top critiques", index=False)
    top_major_df.to_excel(writer, sheet_name="Top majeures", index=False)
    top_minor_df.to_excel(writer, sheet_name="Top mineures", index=False)
    catalog_alerts_df.to_excel(writer, sheet_name="Top alertes catalogue", index=False)
    correction_types_df.to_excel(writer, sheet_name="Types corrections", index=False)

    adjust_excel(writer)

print("Audit terminé.")
print(f"Produits total : {len(df)}")
print(f"Produits discontinués exclus : {df['Discontinued'].sum()}")
print(f"Produits actifs analysés : {len(active_df)}")
print("")

print("Priorités :")
print(active_df["Priorité"].value_counts())
print("")

print("Top erreurs critiques :")
for error, count in split_errors(active_df["Erreurs critiques"]).most_common(15):
    print(f"- {error} : {count}")

print("")
print("Top erreurs majeures :")
for error, count in split_errors(active_df["Erreurs majeures"]).most_common(15):
    print(f"- {error} : {count}")

print("")
print("Top erreurs mineures :")
for error, count in split_errors(active_df["Erreurs mineures"]).most_common(15):
    print(f"- {error} : {count}")

print("")
print("Alertes catalogue :")
for alert, count in split_errors(active_df["Alertes catalogue"]).most_common(15):
    print(f"- {alert} : {count}")

print("")
print("Types de correction :")
for correction, count in split_errors(active_df["Type de correction"]).most_common(15):
    print(f"- {correction} : {count}")

print("")

# ==========================================
# Historique audit
# ==========================================

HISTORY_FILE = "historique_audit.csv"

audit_datetime = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

historique_row = pd.DataFrame([{
    "Date": audit_datetime,
    "Produits": len(active_df),
    "Conformes": int((active_df["Priorité"] == "Conforme").sum()),
    "Action_requise": int((active_df["Priorité"] == "Action requise").sum()),
    "Critiques": int((active_df["Priorité"] == "Critique").sum()),
}])

if os.path.exists(HISTORY_FILE):
    historique_df = pd.read_csv(HISTORY_FILE)

    historique_df = pd.concat(
        [historique_df, historique_row],
        ignore_index=True
    )
else:
    historique_df = historique_row

historique_df = historique_df.sort_values("Date")
historique_df.to_csv(HISTORY_FILE, index=False)

print("")
print("Résumé par marque généré dans l'onglet Excel : Résumé par marque")
print(f"CSV généré : {OUTPUT_CSV}")
print(f"Excel généré : {OUTPUT_EXCEL}")
# ==========================================
# Synchronisation vers VPS
# ==========================================

print("")
print("Synchronisation vers le dashboard en ligne...")

VPS_DESTINATION = "ubuntu@144.217.80.100:/home/ubuntu/audit-catalogue-industria/"

files_to_sync = [
    OUTPUT_EXCEL,
    HISTORY_FILE,
]

try:
    subprocess.run(
        ["scp"] + files_to_sync + [VPS_DESTINATION],
        check=True
    )

    subprocess.run(
        [
            "ssh",
            "ubuntu@144.217.80.100",
            "sudo systemctl restart industria-dashboard"
        ],
        check=True
    )

    print("Dashboard en ligne mis à jour.")
except Exception as e:
    print(f"Erreur pendant la synchronisation VPS : {e}")
