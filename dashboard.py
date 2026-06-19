from datetime import datetime

import pandas as pd
import plotly.express as px
import streamlit as st

from streamlit_autorefresh import st_autorefresh
from database import (
    approve_error,
    import_from_sqlite,
    import_legacy_files,
    latest_sync_run,
    load_approvals,
    load_brand_summary,
    load_history,
    load_processed_brands,
    load_report,
    load_todos,
    remove_approval,
    save_processed_brands,
    save_todos,
    table_count,
)


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

st_autorefresh(interval=300000, key="dashboard_refresh")

st.markdown(
    """
    <style>
    div[data-testid="stButton"] > button {
        min-height: 2rem;
        padding: 0.15rem 0.45rem;
    }
    .stMarkdown p {
        margin-bottom: 0.15rem;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

import_legacy_files()

if table_count("catalogue_report") == 0:
    legacy_db = BASE_DIR.parent / "audit-catalogue-industria" / "industria_catalogue.db"
    import_from_sqlite(legacy_db)

st.title("📊 Industria Catalogue Audit")
st.sidebar.success(
    f"Connecté : {st.session_state.display_name}"
)

st.sidebar.caption(
    f"Rôle : {st.session_state.role}"
)

last_sync = latest_sync_run()
if last_sync:
    st.sidebar.caption(
        f"Dernière sync : {last_sync['status']} "
        f"{last_sync.get('finished_at') or last_sync.get('started_at')}"
    )

st.sidebar.link_button("🚪 Déconnexion", "https://dashboardindustria.com/logout")

def load_dashboard_data():
    df = load_report()
    brand_summary = load_brand_summary()
    approvals_df = load_approvals()

    return df, brand_summary, approvals_df


def ensure_columns(dataframe, columns):
    for column in columns:
        if column not in dataframe.columns:
            dataframe[column] = ""
    return dataframe

df, brand_summary, approvals_df = load_dashboard_data()
df = ensure_columns(df, [
    "Internal_Variant_ID",
    "Brand",
    "FC_Title_Short",
    "SKU",
    "UPC",
    "Score",
    "Priorité",
    "Type de correction",
    "Erreurs critiques",
    "Erreurs majeures",
    "Erreurs mineures",
    "Alertes catalogue",
])
brand_summary = ensure_columns(brand_summary, ["Brand"])
full_df = df.copy()
all_brands = sorted(df["Brand"].dropna().unique())

processed_brands = load_processed_brands(all_brands)

produits_ecom = len(full_df)

approved_pairs = set(
    zip(
        approvals_df["Internal_Variant_ID"].astype(str),
        approvals_df["Erreur"].astype(str),
    )
)

IGNORED_WHEN_APPROVED_ERRORS = {
    "Produit invisible — vérifier si volontaire",
    "Produit invisible - vérifier si volontaire",
}


def is_ignored_product(row):
    variant_id = str(row.get("Internal_Variant_ID", ""))
    return any(
        (variant_id, error) in approved_pairs
        for error in IGNORED_WHEN_APPROVED_ERRORS
    )


full_df["Produit ignoré"] = full_df.apply(is_ignored_product, axis=1)

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

ignored_by_brand = ~full_df["Brand"].isin(processed_brands)
ignored_by_approval = full_df["Produit ignoré"]
produits_a_ignorer = int((ignored_by_brand | ignored_by_approval).sum())
ignored_products_df = full_df[ignored_by_approval].copy()

df = full_df[
    full_df["Brand"].isin(display_brands)
    & (full_df["Produit ignoré"] == False)
].copy()

page = st.radio(
    "Navigation",
    ["📊 Overview", "❌ Erreurs", "📋 To-Do List"],
    horizontal=True
)

def clean(value):
    if pd.isna(value) or str(value).strip() in ["", "None", "nan"]:
        return ""
    return str(value).strip()


def safe_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def toggle_row_state(state_key, row_id):
    if st.session_state.get(state_key) == row_id:
        st.session_state[state_key] = None
    else:
        st.session_state[state_key] = row_id


def table_cell(value):
    st.write(clean(value))


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

    if st.session_state.role == "admin":
        with st.expander(
            f"Produits ignorés individuellement ({len(ignored_products_df)})"
        ):
            if ignored_products_df.empty:
                st.info("Aucun produit ignoré individuellement.")
            else:
                ignored_search = st.text_input(
                    "Recherche produit ignoré",
                    key="ignored_products_search"
                )

                visible_ignored_df = ignored_products_df.copy()
                if ignored_search:
                    ignored_search_lower = ignored_search.lower()
                    visible_ignored_df = visible_ignored_df[
                        visible_ignored_df.apply(
                            lambda row: ignored_search_lower
                            in " ".join(row.fillna("").astype(str)).lower(),
                            axis=1
                        )
                    ]

                displayed_ignored_df = (
                    visible_ignored_df
                    .head(100)
                    .reset_index(drop=True)
                )

                if visible_ignored_df.empty:
                    st.info("Aucun produit ignoré ne correspond à la recherche.")
                else:
                    st.caption(f"Affichage des {len(displayed_ignored_df)} premiers produits ignorés filtrés.")

                    header = st.columns([0.6, 1.3, 3, 1.1, 1.1, 1.2])
                    header[0].markdown("")
                    header[1].markdown("**Marque**")
                    header[2].markdown("**Produit**")
                    header[3].markdown("**SKU**")
                    header[4].markdown("**UPC**")
                    header[5].markdown("**Priorité**")

                    with st.container(height=420):
                        for _, row in displayed_ignored_df.iterrows():
                            variant_id = str(row.get("Internal_Variant_ID", ""))
                            brand = clean(row.get("Brand", ""))
                            title = clean(row.get("FC_Title_Short", ""))
                            sku = clean(row.get("SKU", ""))
                            upc = clean(row.get("UPC", ""))
                            priority = clean(row.get("Priorité", ""))
                            is_open = st.session_state.get("open_ignored_row") == variant_id

                            row_cols = st.columns([0.35, 1.3, 3, 1.1, 1.1, 1.2])
                            row_cols[0].button(
                                "▾" if is_open else "▸",
                                key=f"toggle_ignored_{variant_id}",
                                on_click=toggle_row_state,
                                args=("open_ignored_row", variant_id),
                            )
                            with row_cols[1]:
                                table_cell(brand)
                            with row_cols[2]:
                                table_cell(title)
                            with row_cols[3]:
                                table_cell(sku)
                            with row_cols[4]:
                                table_cell(upc)
                            with row_cols[5]:
                                table_cell(priority)

                            if is_open:
                                with st.container(border=True):
                                    st.write(clean(row.get("Alertes catalogue", "")))

                                    if st.button(
                                        "Rétablir ce produit",
                                        key=f"restore_ignored_{variant_id}"
                                    ):
                                        for error in IGNORED_WHEN_APPROVED_ERRORS:
                                            remove_approval(variant_id, error)
                                        st.success("Produit rétabli.")
                                        st.rerun()

    # Historique des audits
    history_df = load_history()

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
        else:
            st.info("Un seul audit disponible pour l'instant.")


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

    display_limit = st.selectbox(
        "Nombre de résultats affichés",
        [50, 100, 200],
        index=1
    )

    displayed_products_df = (
        filtered_df
        .head(display_limit)
        .reset_index(drop=True)
    )

    if filtered_df.empty:
        st.info("Aucun produit ne correspond aux filtres.")
    else:
        st.caption(f"Affichage des {len(displayed_products_df)} premiers produits filtrés.")

        header = st.columns([0.35, 1.2, 2.8, 1, 1, 0.8, 1.2, 0.8, 0.8])
        header[0].markdown("")
        header[1].markdown("**Marque**")
        header[2].markdown("**Produit**")
        header[3].markdown("**SKU**")
        header[4].markdown("**UPC**")
        header[5].markdown("**Score**")
        header[6].markdown("**Priorité**")
        header[7].markdown("**Rest.**")
        header[8].markdown("**Appr.**")

        with st.container(height=620):
            for _, row in displayed_products_df.iterrows():
                variant_id = str(row.get("Internal_Variant_ID", ""))
                brand = clean(row.get("Brand", ""))
                title = clean(row.get("FC_Title_Short", ""))
                sku = clean(row.get("SKU", ""))
                upc = clean(row.get("UPC", ""))
                score = clean(row.get("Score", ""))
                priority = clean(row.get("Priorité", ""))
                correction_type = clean(row.get("Type de correction", ""))
                remaining = safe_int(row.get("Erreurs restantes", 0))
                approved_count = safe_int(row.get("Erreurs approuvées", 0))
                is_open = st.session_state.get("open_error_row") == variant_id

                row_cols = st.columns([0.35, 1.2, 2.8, 1, 1, 0.8, 1.2, 0.8, 0.8])
                row_cols[0].button(
                    "▾" if is_open else "▸",
                    key=f"toggle_error_{variant_id}",
                    on_click=toggle_row_state,
                    args=("open_error_row", variant_id),
                )
                with row_cols[1]:
                    table_cell(brand)
                with row_cols[2]:
                    table_cell(title)
                with row_cols[3]:
                    table_cell(sku)
                with row_cols[4]:
                    table_cell(upc)
                with row_cols[5]:
                    table_cell(score)
                with row_cols[6]:
                    table_cell(priority)
                with row_cols[7]:
                    table_cell(remaining)
                with row_cols[8]:
                    table_cell(approved_count)

                if is_open:
                    unresolved = unresolved_errors(row)
                    approved = approved_errors(row)

                    with st.container(border=True):
                        if correction_type:
                            st.caption(correction_type)

                        if unresolved:
                            st.markdown("##### À traiter")

                            for err_type, err in unresolved:
                                col_error, col_action = st.columns([4, 1])
                                with col_error:
                                    st.write(f"**{err_type}** — {err}")
                                with col_action:
                                    if st.button(
                                        "Approuver",
                                        key=f"approve_{variant_id}_{err_type}_{err}"
                                    ):
                                        approve_error(variant_id, err_type, err)
                                        st.success("Erreur approuvée.")
                                        st.rerun()
                        else:
                            st.success("Aucune erreur restante pour ce produit.")

                        if approved:
                            st.markdown("##### Déjà approuvées")
                            for err_type, err in approved:
                                col_error, col_action = st.columns([4, 1])
                                with col_error:
                                    st.write(f"✅ **{err_type}** — {err}")
                                with col_action:
                                    if st.button(
                                        "Retirer",
                                        key=f"remove_{variant_id}_{err_type}_{err}"
                                    ):
                                        remove_approval(variant_id, err)
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
