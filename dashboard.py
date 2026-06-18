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
    "vero": {
        "password": "Industria2026!",
        "name": "Vero",
        "role": "admin"
    },
    "nathalie": {
        "password": "Industria2026!",
        "name": "Nathalie",
        "role": "editor"
    },
    "virginy": {
        "password": "Industria2026!",
        "name": "Virginy",
        "role": "editor"
    }
}

# Utilisateur connecté via Nginx Basic Auth
current_user = st.context.headers.get("X-Remote-User", "vero").lower()

if current_user not in USERS:
    st.error("Utilisateur non autorisé")
    st.stop()

st.session_state.authenticated = True
st.session_state.user = current_user
st.session_state.role = USERS[current_user]["role"]
st.session_state.display_name = USERS[current_user]["name"]

st.set_page_config(page_title="Industria Audit", layout="wide")

st_autorefresh(interval=30000, key="dashboard_refresh")

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

REPORT_FILE = BASE_DIR / "rapport_qualite_catalogue.xlsx"
APPROVALS_FILE = BASE_DIR / "approbations_erreurs.csv"
HISTORY_FILE = BASE_DIR / "historique_audit.csv"
TODO_FILE = BASE_DIR / "todo_list.csv"
BRAND_SETTINGS_FILE = BASE_DIR / "brand_settings.csv"

st.title("📊 Industria Catalogue Audit")
st.sidebar.success(
    f"Connecté : {st.session_state.display_name}"
)

st.sidebar.caption(
    f"Rôle : {st.session_state.role}"
)
st.sidebar.link_button("🚪 Déconnexion", "https://dashboardindustria.com/logout")

@st.cache_data(ttl=60)
def read_excel_safely(file, sheet_name, retries=10, delay=2):
    for _ in range(retries):
        try:
            return pd.read_excel(file, sheet_name=sheet_name)

        except (EOFError, zipfile.BadZipFile):
            time.sleep(delay)

    return pd.read_excel(file, sheet_name=sheet_name)

@st.cache_data(ttl=300)
def load_dashboard_data():
    df = read_excel_safely(REPORT_FILE, sheet_name="Tous les produits")
    brand_summary = read_excel_safely(REPORT_FILE, sheet_name="Résumé par marque")

    if os.path.exists(APPROVALS_FILE):
        approvals_df = pd.read_csv(APPROVALS_FILE)
    else:
        approvals_df = pd.DataFrame(
            columns=["Internal_Variant_ID", "Type", "Erreur", "Date"]
        )

    return df, brand_summary, approvals_df

def load_todos():
    if TODO_FILE.exists():
        return pd.read_csv(TODO_FILE)

    return pd.DataFrame(columns=[
        "ID",
        "Tache",
        "Description",
        "Assigne",
        "Statut",
        "Priorite",
        "Cree_par",
        "Date_creation",
        "Date_completion"
    ])


def save_todos(todos_df):
    todos_df.to_csv(TODO_FILE, index=False)
    
def load_processed_brands(all_brands):
    if BRAND_SETTINGS_FILE.exists():
        settings_df = pd.read_csv(BRAND_SETTINGS_FILE)

        if "Brand" in settings_df.columns:
            saved_brands = settings_df["Brand"].dropna().tolist()
            return [brand for brand in saved_brands if brand in all_brands]

    return all_brands


def save_processed_brands(processed_brands):
    settings_df = pd.DataFrame({
        "Brand": processed_brands
    })

    settings_df.to_csv(BRAND_SETTINGS_FILE, index=False)

df, brand_summary, approvals_df = load_dashboard_data()
full_df = df.copy()
all_brands = sorted(df["Brand"].dropna().unique())

processed_brands = load_processed_brands(all_brands)

produits_ecom = len(full_df)
produits_a_ignorer = len(
    full_df[~full_df["Brand"].isin(processed_brands)]
)

if st.session_state.role == "admin":
    processed_brands = st.sidebar.multiselect(
        "✅ Marques traitées",
        all_brands,
        default=processed_brands
    )

    if st.sidebar.button("💾 Sauvegarder les marques traitées"):
        save_processed_brands(processed_brands)
        st.sidebar.success("Marques traitées sauvegardées.")
        st.rerun()

display_brands = st.sidebar.multiselect(
    "👁️ Marques affichées temporairement",
    processed_brands,
    default=processed_brands
)

df = df[df["Brand"].isin(display_brands)]

page = st.radio(
    "Navigation",
    ["📊 Overview", "❌ Erreurs", "📋 To-Do List"],
    horizontal=True
)


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

if page == "📊 Overview":
    # Métriques principales
    c1, c2, c3, c4, c5, c6, c7 = st.columns(7)
    c1.metric("Produits", len(df))
    c2.metric("Conformes", len(df[df["Priorité"] == "Conforme"]))
    c3.metric("Action requise", len(df[df["Priorité"] == "Action requise"]))
    c4.metric("Critiques", len(df[df["Priorité"] == "Critique"]))
    c5.metric("Erreurs approuvées", int(df["Erreurs approuvées"].sum()))
    c6.metric("Produits e-com", produits_ecom)
    c7.metric("Produits à ignorer", produits_a_ignorer)

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

                col_a, col_b, col_c = st.columns([3,1,1])

            with col_a:
                selected_compare_day = st.date_input(
                    "Comparer avec la date",
                    value=available_dates[-2].date(),
                    min_value=history_df["Date"].dt.date.min(),
                    max_value=history_df["Date"].dt.date.max()
                )

                compare_candidates = history_df[
                    history_df["Date"].dt.date <= selected_compare_day
                ]

                if compare_candidates.empty:
                    compare_row = history_df.iloc[0]
                else:
                    compare_row = compare_candidates.iloc[-1]

                compare_date = compare_row["Date"]

                st.caption(
                    f"Audit utilisé : {compare_date.strftime('%Y-%m-%d %H:%M')}"
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
                    id_vars=["Date", "Date_str"],
                    value_vars=["Conformes", "Action_requise", "Critiques"],
                    var_name="Statut",
                    value_name="Nombre"
                )

                fig_history = px.line(
                    history_long,
                    x="Date",
                    y="Nombre",
                    color="Statut",
                    markers=True,
                    title="Évolution de la qualité catalogue par audit"
                )

                fig_history.update_xaxes(title="Audit")
                fig_history.update_yaxes(title="Nombre")
                
                fig_history.update_layout(
                    autosize=True,
                    height=500,
                    width=None,
                    margin=dict(l=20, r=20, t=60, b=80)
                )
                
                st.container().plotly_chart(
                    fig_history,
                    use_container_width=True
                )
                
            else:
                st.info("Choisis une plage avec au moins deux audits pour afficher le graphique.")


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







if page == "❌ Erreurs":

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
if page == "📋 To-Do List":
    st.divider()
    st.header("📋 To-Do List")

    todos_df = load_todos()
    
    
    if "todo_form_key" not in st.session_state:
        st.session_state.todo_form_key = 0

    with st.form("add_todo_form"):
        st.subheader("Ajouter une tâche")

        tache = st.text_input(
            "Tâche",
            key=f"new_task_name_{st.session_state.todo_form_key}"
        )

        description = st.text_area(
            "Description",
            key=f"new_task_description_{st.session_state.todo_form_key}"
        )
        assigne = st.selectbox(
            "Assigné à",
            [USERS[u]["name"] for u in USERS]
        )
        date_echeance = st.date_input(
            "Date d'échéance",
            value=None
        )

        submitted = st.form_submit_button("Ajouter")

        if submitted and tache.strip():
            new_id = 1 if todos_df.empty else int(todos_df["ID"].max()) + 1

            new_row = {
                "ID": new_id,
                "Tache": tache.strip(),
                "Description": description.strip(),
                "Assigne": assigne,
                "Statut": "À faire",
                "Date_echeance": date_echeance.strftime("%Y-%m-%d") if date_echeance else "",
                "Cree_par": st.session_state.display_name,
                "Date_creation": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "Date_completion": ""
            }

            todos_df = pd.concat(
                [todos_df, pd.DataFrame([new_row])],
                ignore_index=True
            )
            
            save_todos(todos_df)
            st.session_state.todo_form_key += 1
            st.success("Tâche ajoutée.")
            st.rerun()
            
    assignee_filter = st.selectbox(
        "Afficher les tâches de",
        ["Tout le monde"] + [USERS[u]["name"] for u in USERS]
    )

    st.subheader("Tâches actives")

    active_todos = todos_df[todos_df["Statut"] != "Terminé"]
    
    if assignee_filter != "Tout le monde":
        active_todos = active_todos[
            active_todos["Assigne"] == assignee_filter
        ]

    if active_todos.empty:
        st.info("Aucune tâche active.")
    else:
        for _, row in active_todos.iterrows():
            with st.expander(f"{row['Tache']} — échéance : {row.get('Date_echeance', '')}"):
                new_description = st.text_area(
                    "Description",
                    value=row["Description"],
                    key=f"desc_{row['ID']}"
                )

                if st.button(
                    "💾 Sauvegarder la description",
                    key=f"save_desc_{row['ID']}"
                ):
                    todos_df.loc[
                        todos_df["ID"] == row["ID"],
                        "Description"
                    ] = new_description

                    save_todos(todos_df)
                    st.success("Description mise à jour.")
                    st.rerun()
                st.write(f"Assigné à : {row['Assigne']}")
                st.write(f"Créé par : {row['Cree_par']}")
                st.write(f"Créé le : {row['Date_creation']}")

                if row["Statut"] == "En cours":
                    st.warning("🟡 En cours")
                elif row["Statut"] == "Terminé":
                    st.success("🟢 Terminé")
                else:
                    st.info("⚪ À faire")

                current_due = row.get("Date_echeance", "")

                new_due_date = st.date_input(
                    "Modifier l'échéance",
                    value=pd.to_datetime(current_due).date() if pd.notna(current_due) and current_due != "" else None,
                    key=f"due_{row['ID']}"
                )

                if st.button("Sauvegarder l'échéance", key=f"save_due_{row['ID']}"):
                    todos_df.loc[todos_df["ID"] == row["ID"], "Date_echeance"] = (
                        new_due_date.strftime("%Y-%m-%d") if new_due_date else ""
                    )
                    save_todos(todos_df)
                    st.rerun()

                col1, col2 = st.columns(2)

                with col1:
                    if st.button("Marquer en cours", key=f"progress_{row['ID']}"):
                        todos_df.loc[todos_df["ID"] == row["ID"], "Statut"] = "En cours"
                        save_todos(todos_df)
                        st.rerun()

                with col2:
                    if st.button("Terminer", key=f"done_{row['ID']}"):
                        todos_df.loc[todos_df["ID"] == row["ID"], "Statut"] = "Terminé"
                        todos_df.loc[todos_df["ID"] == row["ID"], "Date_completion"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        save_todos(todos_df)
                        st.rerun()
                        st.divider()

                    if st.button(
                        "🗑️ Supprimer la tâche",
                        key=f"delete_{row['ID']}"
                    ):
                        todos_df = todos_df[
                            todos_df["ID"] != row["ID"]
                        ]

                        save_todos(todos_df)
                        st.warning("Tâche supprimée.")
                        st.rerun()

        todo_tab_active, todo_tab_done = st.tabs([
            "📋 À faire / En cours",
            "✅ Terminées"
        ])

            with todo_tab_active:
                active_table = todos_df[
                    todos_df["Statut"] != "Terminé"
                ].copy()

                if assignee_filter != "Tout le monde":
                    active_table = active_table[
                        active_table["Assigne"] == assignee_filter
                    ]

                if "Priorite" in active_table.columns:
                    active_table = active_table.drop(columns=["Priorite"])

                st.dataframe(active_table, use_container_width=True)

    with todo_tab_done:
        done_table = todos_df[
            todos_df["Statut"] == "Terminé"
        ].copy()

        if assignee_filter != "Tout le monde":
            done_table = done_table[
                done_table["Assigne"] == assignee_filter
            ]

        if "Priorite" in done_table.columns:
            done_table = done_table.drop(columns=["Priorite"])

        st.dataframe(done_table, use_container_width=True)
