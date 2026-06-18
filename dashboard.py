import os
import zipfile
from datetime import datetime
import time

import pandas as pd
import plotly.express as px
import streamlit as st

from streamlit_autorefresh import st_autorefresh


# ==========================================
# Authentification simple
# ==========================================

USERS = {
    "vero": "Industria2026!",
    "nathalie": "Industria2026!",
    "virginy": "Industria2026!"
}

if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

if not st.session_state.authenticated:

    st.title("🔒 Industria Audit")

    username = st.text_input("Nom d'utilisateur")
    password = st.text_input("Mot de passe", type="password")

    if st.button("Connexion"):
        if username in USERS and USERS[username] == password:
            st.session_state.authenticated = True
            st.session_state.user = username
            st.rerun()
        else:
            st.error("Nom d'utilisateur ou mot de passe invalide")

    st.stop()

st.set_page_config(page_title="Industria Audit", layout="wide")

st_autorefresh(interval=30000, key="dashboard_refresh")

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

REPORT_FILE = BASE_DIR / "rapport_qualite_catalogue.xlsx"
APPROVALS_FILE = BASE_DIR / "approbations_erreurs.csv"
HISTORY_FILE = BASE_DIR / "historique_audit.csv"

st.title("📊 Industria Catalogue Audit")
st.sidebar.success(
    f"Connecté : {st.session_state.user}"
)

@st.cache_data(ttl=60)
def read_excel_safely(file, sheet_name, retries=10, delay=2):
    for _ in range(retries):
        try:
            return pd.read_excel(file, sheet_name=sheet_name)

        except (EOFError, zipfile.BadZipFile):
            time.sleep(delay)

    return pd.read_excel(file, sheet_name=sheet_name)

df = read_excel_safely(REPORT_FILE, sheet_name="Tous les produits")
brand_summary = read_excel_safely(REPORT_FILE, sheet_name="Résumé par marque")

# Approbations
if os.path.exists(APPROVALS_FILE):
    approvals_df = pd.read_csv(APPROVALS_FILE)
else:
    approvals_df = pd.DataFrame(columns=["Internal_Variant_ID", "Type", "Erreur", "Date"])

approved_pairs = set(
    zip(
        approvals_df["Internal_Variant_ID"].astype(str),
        approvals_df["Erreur"].astype(str),
    )
)

def clean(value):
    if pd.isna(value) or str(value).strip() in ["", "None", "nan"]:
        return ""
    return str(value).strip()

def split_error_list(value):
    value = clean(value)
    if not value:
        return []
    return [x.strip() for x in value.split("|") if x.strip()]

def get_all_errors(row):
    errors = []

    for err in split_error_list(row.get("Erreurs critiques", "")):
        errors.append(("Critique", err))

    for err in split_error_list(row.get("Erreurs majeures", "")):
        errors.append(("Majeure", err))

    for err in split_error_list(row.get("Erreurs mineures", "")):
        errors.append(("Mineure", err))

    for err in split_error_list(row.get("Alertes catalogue", "")):
        errors.append(("Catalogue", err))

    return errors

def unresolved_errors(row):
    variant_id = str(row.get("Internal_Variant_ID", ""))
    return [
        (err_type, err)
        for err_type, err in get_all_errors(row)
        if (variant_id, err) not in approved_pairs
    ]

def approved_errors(row):
    variant_id = str(row.get("Internal_Variant_ID", ""))
    return [
        (err_type, err)
        for err_type, err in get_all_errors(row)
        if (variant_id, err) in approved_pairs
    ]

df["Erreurs restantes"] = df.apply(lambda row: len(unresolved_errors(row)), axis=1)
df["Erreurs approuvées"] = df.apply(lambda row: len(approved_errors(row)), axis=1)
df["Tout approuvé"] = (df["Erreurs restantes"] == 0) & (df["Erreurs approuvées"] > 0)

# Métriques principales
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Produits", len(df))
c2.metric("Conformes", len(df[df["Priorité"] == "Conforme"]))
c3.metric("Action requise", len(df[df["Priorité"] == "Action requise"]))
c4.metric("Critiques", len(df[df["Priorité"] == "Critique"]))
c5.metric("Erreurs approuvées", int(df["Erreurs approuvées"].sum()))

st.divider()

# Historique des audits
if os.path.exists(HISTORY_FILE):
    history_df = pd.read_csv(HISTORY_FILE)

    if not history_df.empty:
        history_df["Date"] = pd.to_datetime(
            history_df["Date"],
            format="mixed"
        )

        history_df = history_df.sort_values("Date").reset_index(drop=True)
        history_df["Date_str"] = history_df["Date"].dt.strftime("%Y-%m-%d %H:%M")

        st.subheader("📈 Progression des audits")

        last_audit = history_df.iloc[-1]

        st.success(
            f"Dernier audit : {last_audit['Date'].strftime('%Y-%m-%d %H:%M:%S')}"
        )

        if len(history_df) >= 2:
            available_dates = history_df["Date"].tolist()
            today_row = history_df.iloc[-1]

            col_a, col_b, col_c = st.columns(3)

            with col_a:
                compare_date = st.selectbox(
                    "Comparer avec l'audit",
                    available_dates[:-1],
                    index=len(available_dates[:-1]) - 1,
                    format_func=lambda d: d.strftime("%Y-%m-%d %H:%M")
                )

            with col_b:
                start_date = st.date_input(
                    "Début du graphique",
                    value=history_df["Date"].dt.date.min(),
                    min_value=history_df["Date"].dt.date.min(),
                    max_value=history_df["Date"].dt.date.max()
                )

            with col_c:
                end_date = st.date_input(
                    "Fin du graphique",
                    value=history_df["Date"].dt.date.max(),
                    min_value=history_df["Date"].dt.date.min(),
                    max_value=history_df["Date"].dt.date.max()
                )

            compare_row = history_df[history_df["Date"] == compare_date].iloc[0]

            d1, d2, d3 = st.columns(3)

            d1.metric(
                f"Conformes vs {compare_date.strftime('%Y-%m-%d %H:%M')}",
                int(today_row["Conformes"]),
                int(today_row["Conformes"] - compare_row["Conformes"])
            )

            d2.metric(
                f"Action requise vs {compare_date.strftime('%Y-%m-%d %H:%M')}",
                int(today_row["Action_requise"]),
                int(today_row["Action_requise"] - compare_row["Action_requise"])
            )

            d3.metric(
                f"Critiques vs {compare_date.strftime('%Y-%m-%d %H:%M')}",
                int(today_row["Critiques"]),
                int(today_row["Critiques"] - compare_row["Critiques"])
            )

            graph_df = history_df[
                (history_df["Date"].dt.date >= start_date)
                & (history_df["Date"].dt.date <= end_date)
            ].copy()

            if len(graph_df) >= 2:
                history_long = graph_df.melt(
                    id_vars="Date_str",
                    value_vars=["Conformes", "Action_requise", "Critiques"],
                    var_name="Statut",
                    value_name="Nombre"
                )

                fig_history = px.line(
                    history_long,
                    x="Date_str",
                    y="Nombre",
                    color="Statut",
                    markers=True,
                    title="Évolution de la qualité catalogue par audit"
                )

                fig_history.update_xaxes(type="category", title="Audit")
                fig_history.update_yaxes(title="Nombre")

                st.plotly_chart(fig_history, width="stretch")
            else:
                st.info("Choisis une plage avec au moins deux audits pour afficher le graphique.")
        else:
            st.info(
                "L’historique contient seulement un audit. "
                "Le graphique apparaîtra quand tu auras au moins deux audits."
            )

st.divider()

# Graphique priorité
priority_counts = df["Priorité"].value_counts().reset_index()
priority_counts.columns = ["Priorité", "Nombre"]

fig_priority = px.pie(
    priority_counts,
    names="Priorité",
    values="Nombre",
    title="Répartition du catalogue"
)

st.plotly_chart(fig_priority, width="stretch")

st.divider()

# Résumé par marque
st.subheader("🏷️ Résumé par marque")
st.dataframe(brand_summary, hide_index=True, height=350, width="stretch")

st.divider()

# Recherche et filtres
st.subheader("🔎 Recherche et filtres")

filtered_df = df.copy()

search = st.text_input("Recherche produit, marque, SKU ou UPC")

if search:
    search_lower = search.lower()
    filtered_df = filtered_df[
        filtered_df.apply(
            lambda row: search_lower in " ".join(row.fillna("").astype(str)).lower(),
            axis=1
        )
    ]

col1, col2, col3, col4 = st.columns(4)

with col1:
    brands = ["Toutes"] + sorted(filtered_df["Brand"].dropna().unique().tolist())
    selected_brand = st.selectbox("Marque", brands)

with col2:
    priorities = ["Toutes"] + sorted(filtered_df["Priorité"].dropna().unique().tolist())
    selected_priority = st.selectbox("Priorité", priorities)

with col3:
    correction_types = ["Tous"] + sorted(
        set(
            item.strip()
            for value in filtered_df["Type de correction"].dropna()
            for item in str(value).split("|")
            if item.strip()
        )
    )
    selected_correction = st.selectbox("Type de correction", correction_types)

with col4:
    approval_filter = st.selectbox(
        "Approbation",
        ["À traiter seulement", "Tous", "Tout approuvé seulement"]
    )

if selected_brand != "Toutes":
    filtered_df = filtered_df[filtered_df["Brand"] == selected_brand]

if selected_priority != "Toutes":
    filtered_df = filtered_df[filtered_df["Priorité"] == selected_priority]

if selected_correction != "Tous":
    filtered_df = filtered_df[
        filtered_df["Type de correction"]
        .fillna("")
        .str.contains(selected_correction, regex=False)
    ]

if approval_filter == "À traiter seulement":
    filtered_df = filtered_df[filtered_df["Erreurs restantes"] > 0]

if approval_filter == "Tout approuvé seulement":
    filtered_df = filtered_df[filtered_df["Tout approuvé"] == True]

st.write(f"Produits affichés : {len(filtered_df)}")

st.subheader("📦 Produits")

visible_columns = [
    "Brand",
    "FC_Title_Short",
    "SKU",
    "UPC",
    "Score",
    "Priorité",
    "Type de correction",
    "Erreurs restantes",
    "Erreurs approuvées",
]

available_visible_columns = [col for col in visible_columns if col in filtered_df.columns]

st.dataframe(
    filtered_df[available_visible_columns],
    hide_index=True,
    height=500,
    width="stretch"
)

st.divider()

st.subheader("📌 Détails des erreurs")

detail_df = filtered_df.head(100)
st.caption("Affichage des 100 premiers produits filtrés.")

for _, row in detail_df.iterrows():
    variant_id = str(row.get("Internal_Variant_ID", ""))
    brand = clean(row.get("Brand", ""))
    title = clean(row.get("FC_Title_Short", ""))
    sku = clean(row.get("SKU", ""))

    unresolved = unresolved_errors(row)
    approved = approved_errors(row)

    label_icon = "✅" if len(unresolved) == 0 and len(approved) > 0 else "⚠️"
    label = f"{label_icon} {brand} — {title} — {sku}"

    with st.expander(label):
        if unresolved:
            st.markdown("### À traiter")

            for err_type, err in unresolved:
                st.write(f"**{err_type}** — {err}")

                if st.button(
                    "Approuver cette erreur",
                    key=f"approve_{variant_id}_{err_type}_{err}"
                ):
                    new_row = pd.DataFrame([{
                        "Internal_Variant_ID": variant_id,
                        "Type": err_type,
                        "Erreur": err,
                        "Date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    }])

                    approvals_df = pd.concat(
                        [approvals_df, new_row],
                        ignore_index=True
                    ).drop_duplicates(
                        subset=["Internal_Variant_ID", "Erreur"],
                        keep="last"
                    )

                    approvals_df.to_csv(APPROVALS_FILE, index=False)
                    st.success("Erreur approuvée.")
                    st.rerun()
        else:
            st.success("Aucune erreur restante pour ce produit.")

        if approved:
            st.markdown("### Déjà approuvées")
            for err_type, err in approved:
                st.write(f"✅ **{err_type}** — {err}")

                if st.button(
                    "Retirer l'approbation",
                    key=f"remove_{variant_id}_{err_type}_{err}"
                ):
                    approvals_df = approvals_df[
                        ~(
                            (approvals_df["Internal_Variant_ID"].astype(str) == variant_id)
                            & (approvals_df["Erreur"].astype(str) == err)
                        )
                    ]

                    approvals_df.to_csv(APPROVALS_FILE, index=False)
                    st.warning("Approbation retirée.")
                    st.rerun()
