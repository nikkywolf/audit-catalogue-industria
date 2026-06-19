# Mise en ligne de la v2

Ce dossier contient la v2 du dashboard avec SQLite.

## Ce qui sera mis en ligne

- Le code v2 du dashboard.
- La base `industria_catalogue.db` déjà remplie avec les données importées de la v1.
- Les dépendances Python listées dans `requirements.txt`.

Le script ne copie pas `.env`, pour éviter d'écraser les secrets du serveur.

## Étape 1 - Tester en simulation

Depuis ce dossier:

```bash
cd ~/industria-apps/audit-catalogue-industria-v2
chmod +x deploy_v2_to_server.sh
./deploy_v2_to_server.sh
```

Ce mode ne modifie rien en ligne. Il montre seulement les fichiers qui seraient envoyés.

## Étape 2 - Déployer pour vrai

Quand la simulation est correcte:

```bash
./deploy_v2_to_server.sh --live
```

Le script va:

1. préparer la base SQLite locale;
2. créer une sauvegarde complète du dossier en ligne;
3. envoyer la v2;
4. installer les dépendances Python;
5. redémarrer le dashboard.

## Étape 3 - Vérifier le site

Ouvre:

```text
https://dashboardindustria.com
```

Vérifie:

- les métriques de produits;
- les marques;
- la page Erreurs;
- la To-Do List.

## Revenir à la v1 si nécessaire

Le script affiche le chemin de la sauvegarde créée sur le serveur, par exemple:

```text
/home/ubuntu/audit-catalogue-industria-backups/20260619-101500
```

Pour restaurer cette sauvegarde, il faudra se connecter au serveur et recopier cette sauvegarde vers:

```text
/home/ubuntu/audit-catalogue-industria
```
