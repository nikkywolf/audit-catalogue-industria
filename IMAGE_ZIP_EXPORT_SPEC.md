# Module Images - Structure des ZIP exportes

Quand l'utilisateur telecharge un ZIP d'images traitees, les fichiers doivent etre organises en sous-dossiers.

Structure obligatoire :

```text
Marque/
Gamme/
Produit/
images...
```

Exemple :

```text
Redken/
Acidic Bonding Concentrate/
Shampooing 300ml/
redken-acidic-bonding-concentrate-shampooing-300ml-principal.jpg
redken-acidic-bonding-concentrate-shampooing-300ml-lifestyle.jpg
redken-acidic-bonding-concentrate-shampooing-300ml-texture.jpg
```

Regles :

- creer un dossier par marque ;
- creer un sous-dossier par gamme ;
- creer un sous-dossier par produit ;
- si la gamme est absente, utiliser `sans-gamme` ;
- si le nom du produit est absent, utiliser le `product_id` ;
- nettoyer les noms de dossiers :
  - sans caracteres interdits ;
  - sans accents ;
  - pas de slash ;
  - pas de caracteres speciaux dangereux ;
- ne pas ecraser deux produits avec le meme nom ;
- ajouter le format ou le `product_id` au dossier produit si necessaire ;
- conserver les noms SEO des fichiers images ;
- permettre aussi un ZIP par produit, un ZIP par gamme et un ZIP par marque.

Portee des ZIP :

- ZIP produit : contient seulement `Marque/Gamme/Produit/images...` pour le produit choisi.
- ZIP gamme : contient tous les produits d'une gamme sous `Marque/Gamme/...`.
- ZIP marque : contient toutes les gammes et tous les produits d'une marque.
- ZIP global : contient toutes les marques.
