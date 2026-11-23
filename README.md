# Photothèque

Gestionnaire de photos non destructif, pensé pour prendre la suite de Picasa sous Ubuntu et Windows.
Backend Python (FastAPI + SQLite), interface web Bootstrap en français et en anglais.

## Principes

- **Les photos ne sont jamais modifiées.** Les retouches sont des directives stockées dans des
  fichiers et appliquées à la volée au moment de l'affichage.
- **Les fichiers sont la source de vérité**, la base SQLite (`index.db`) n'est qu'un index
  reconstructible :
  - `.picasa.ini` : lu tel quel, **jamais modifié** ;
  - `.phototheque.json` : directives de l'application, un fichier caché par dossier (retouches,
    tags, étoiles, légendes) ;
  - `albums.json` : albums (dossiers virtuels), dans le dossier de données.
- Si un dossier de photos est en lecture seule, les directives sont écrites dans
  `<données>/sidecars/`.

## Installation

```bash
# avec uv (recommandé)
uv venv && uv pip install -e .
# ou avec pip
python -m venv .venv && . .venv/bin/activate   # Windows : .venv\Scripts\activate
pip install -e .
```

Bootstrap et ses icônes sont embarqués : l'application fonctionne hors ligne.

### ffmpeg (vidéos)

Les vidéos utilisent ffmpeg. Sous Ubuntu, installer la version du système, plus complète et qui
fournit `ffprobe` : `sudo apt install ffmpeg`. Sous Windows, rien à faire : le paquet
`imageio-ffmpeg` (installé avec l'application) contient ffmpeg.

## Utilisation

```bash
python -m phototheque serve --library "D:\Photos"          # Windows
python -m phototheque serve --library ~/Photos              # Ubuntu
```

Le navigateur s'ouvre sur http://127.0.0.1:8765/. Les dossiers de la photothèque sont mémorisés.
Ils peuvent aussi être ajoutés ou retirés dans **Réglages**.

Autres commandes :

```bash
python -m phototheque scan                 # réindexer sans lancer le serveur (dossiers modifiés seulement)
python -m phototheque scan --deep          # vérifier chaque fichier
python -m phototheque scan --full          # reconstruire l'index à partir de zéro
python -m phototheque calibrate [--apply]  # étalonner le redressement Picasa (voir ci-dessous)
```

`scan --full` supprime `index.db` et le cache des miniatures, puis réindexe tout. Il refuse de
s'exécuter si l'application tourne. Rien n'est perdu de ce qui est stocké dans des fichiers
(retouches, tags, étoiles, albums, réglages, noms des visages, modèles d'IA) ; les analyses d'IA
sont recalculées au lancement suivant de `serve`.

Options : `--data DOSSIER` (par défaut `~/.phototheque`, ou variable `PHOTOTHEQUE_DATA`),
`--port`, `--host`, `--no-browser`.

## Fonctions

| Fonction | Détail |
|---|---|
| Navigation par dossier | Arborescence des racines. Un dossier affiche aussi les photos de tous ses sous-dossiers, regroupées par sous-dossier (option « Inclure les sous-dossiers », cochée par défaut, choix mémorisé). Icône 👁 au survol : retirer un dossier de la photothèque sans rien supprimer sur le disque (réintégration dans Réglages) |
| Nouvelles photos | Détectées automatiquement (surveillance des dossiers), puis indexées et analysées en priorité. Sur un partage réseau, utiliser « Réindexer » |
| Navigation par date | Années › mois › jours (date EXIF, sinon date du fichier) |
| Albums | Création, renommage, suppression, ajout ou retrait de photos sélectionnées |
| Tags | Mots-clés et personnes, attribution en lot, navigation par tag, recherche |
| Favoris | Étoiles, reprises de Picasa |
| Retouches | Quarts de tour, redressement avec grille, recadrage, effets, lignes fuyantes |
| Lignes fuyantes | Détection du point de fuite vertical, correction par rotation d'appareil virtuel (proportions justes), intensité réglable (80 % par défaut) |
| Améliorer | Équivalent du bouton de Google Photos : balance des blancs, exposition, contraste local (CLAHE), vibrance, netteté. Analyse au clic, paramètres enregistrés dans les directives, intensité réglable (0 à 150 %). « Avant / après » (bouton maintenu ou touche O) affiche l'original |
| Lignes fuyantes automatiques | Correction appliquée d'office aux photos portant certains tags (Réglages). Une correction retirée à la main n'est pas réappliquée |
| Diaporama | Barre en filigrane sur le bas de la photo dans la visionneuse (▶ lance le diaporama depuis la photo affichée), ou bouton « Diaporama » de la barre d'outils. Plein écran, durée réglable, pause (Espace), Échap pour quitter |
| Zoom (comme Picasa) | Clic gauche maintenu plus de 0,5 s sur la photo : zoom à 100 % sous le curseur (rendu pleine résolution), curseur en main ; glisser en gardant le bouton enfoncé pour se déplacer ; clic simple ou Échap pour revenir |
| Plein écran | Icône en filigrane en haut à droite de la photo, ou double-clic sur la photo |
| Propriétés | Date, dossier, dimensions, poids, appareil photo (et objectif), orientation EXIF si elle n'est pas normale. Petite pastille ronde multicolore à droite du nom quand le .picasa.ini contient des directives pour la photo (le détail s'affiche au survol : recadrage, redressement, filtres, favori, légende, mots-clés, visages, albums) |
| Miniatures | Au survol : nom du fichier et, en dessous, la légende si elle existe |
| Vidéos | MP4, MOV, AVI, MKV, 3GP, MTS, WMV… indexées comme les photos (date de tournage, durée), badge 🎞 (pellicule) avec la durée sur les miniatures (image prise à 10 % de la vidéo). Lecture dans la visionneuse : MP4 H.264 lu directement, vieux AVI (MJPEG, DivX, Xvid…) convertis à la volée en MP4 720p dans le cache, l'original n'étant jamais modifié. Seule retouche : le quart de tour. Le diaporama lit les vidéos en entier puis passe à la suite. Tags automatiques et visages calculés sur l'image de la miniature |
| HEIC et RAW | Photos d'iPhone (HEIC) et fichiers RAW (CR2, CR3, NEF, ARW, DNG, ORF, RW2, RAF, PEF…) : affichage, retouches non destructives et IA comme pour un JPEG. Pour les RAW, l'aperçu JPEG intégré est utilisé pour les miniatures ; le développement complet sert au zoom et à l'export |
| Export | JPEG retouché en pleine résolution (l'original pour une vidéo), accès à l'original |
| Langues et thème | Français et anglais ; thème clair, sombre ou automatique |
| IA (locale) | Tags automatiques, recherche en langage naturel, photos similaires, visages regroupés par personne |

Sélection dans la grille : clic, Ctrl+clic, Maj+clic. Double-clic pour ouvrir la visionneuse.
Navigation : flèches ← →, ou molette de la souris.

## Filtres

Tous les filtres sont appliqués à la volée sur l'original (`render.py`) : Pillow pour la couleur
et la tonalité, OpenCV pour la géométrie (perspective) et le contraste local, NumPy entre les deux.

| Filtre | Réalisation |
|---|---|
| Noir et blanc, Sépia | boutons de l'interface |
| Netteté | uniquement pour les retouches reprises de Picasa (`unsharp2`) |
| Améliorer (`autoenhance`) | voir `enhance.py` : balance des blancs « monde gris » atténuée, niveaux, gamma vers une médiane cible, CLAHE sur L (Lab) mélangé à 60 %, vibrance pondérée par la chroma, netteté légère |

## Reconnaissance d'image

Tout tourne en local avec ONNX Runtime (GPU utilisé si disponible : CUDA, ou DirectML sous Windows).
À activer dans **Réglages** ; les modèles sont téléchargés une fois dans `<données>/models`.

| Fonction | Modèle | Détail |
|---|---|---|
| Tags automatiques | CLIP ViT-B/32 quantifié (≈ 155 Mo, Hugging Face) | Vocabulaire bilingue d'environ 150 libellés (lieux, scènes, objets, animaux, événements). Tags marqués « IA » : ✓ pour valider, × pour refuser (le refus est mémorisé) |
| Recherche en langage naturel | même modèle | Bouton ✨ à côté de la recherche. Fonctionne mieux en anglais ; les libellés français du vocabulaire sont traduits automatiquement |
| Photos similaires | même modèle | Bouton dans la visionneuse |
| Visages | InsightFace buffalo_l (≈ 290 Mo) ou buffalo_s (≈ 125 Mo), GitHub | Détection SCRFD, signature ArcFace, regroupement par personne (onglet **Visages**) |

- Les noms des visages Picasa (`faces=` + `[Contacts2]`) sont repris : le groupe correspondant
  est nommé automatiquement et le nom s'étend aux autres photos de la même personne.
- Nommer un groupe écrit le nom dans les directives (`people`) de chaque photo : il survit à une
  reconstruction de l'index. Donner le nom d'un groupe existant fusionne les deux groupes.
- Supprimer un groupe (🗑 dans la barre d'outils du groupe) : ses visages ne sont plus regroupés et,
  si le groupe portait un nom, ce nom est retiré des photos concernées.
- Les traitements passent par la file `job` (pastille « IA : n » dans la barre du haut). CLIP et
  visages sont traités en alternance, les photos les plus récentes d'abord.
- L'onglet Visages n'affiche que les groupes d'au moins deux visages ; un interrupteur affiche
  aussi les visages isolés.
- Les modèles InsightFace sont réservés à un usage non commercial.
- Installation hors ligne des modèles : déposer les fichiers dans `<données>/models/clip-vit-b32/`
  (`vision_model.onnx`, `text_model.onnx`, `tokenizer.json`) et `<données>/models/buffalo_l/`
  (`det.onnx` = det_10g, `rec.onnx` = w600k_r50).

## Publication d'un album sur WordPress

Réglages › Publication sur WordPress : l'adresse du blog, l'identifiant et un **mot de passe
d'application** (dans WordPress : Comptes › Profil › Mots de passe d'application ; WordPress 5.6
ou plus, site en HTTPS). Le bouton « Tester la connexion » vérifie aussi que le compte peut
envoyer des médias et publier.

Dans l'onglet Albums, l'icône nuage d'un album ouvre la publication : titre, introduction,
statut (public par défaut, brouillon ou privé), nombre de colonnes, reprise des légendes.

- Chaque photo est envoyée dans la médiathèque telle qu'affichée (retouches appliquées), réduite à
  2048 px de côté par défaut, **sans EXIF** (ni position GPS ni appareil). Les vidéos sont ignorées.
- L'article contient un bloc Galerie (éditeur de blocs) dans l'ordre de l'album, avec les légendes ;
  la première photo devient l'image mise en avant.
- Republier un album met à jour son article, et ne renvoie que les photos nouvelles ou retouchées
  depuis le dernier envoi.
- Sites en permaliens simples : l'adresse `?rest_route=` est utilisée automatiquement.

**« Tester la connexion » refuse les identifiants ?** Le test indique la cause :

- *Adresse en http* : dans WordPress, Réglages › Général, « Adresse web de WordPress » et
  « Adresse web du site » doivent commencer par `https://`. Sinon WordPress peut ne pas détecter le
  HTTPS et désactiver les mots de passe d'application.
- *Aucun identifiant reçu* : l'hébergeur (OVH notamment, PHP en CGI/FastCGI) supprime l'en-tête
  `Authorization`. Solution la plus sûre : copier `wordpress/phototheque-auth.php` (fourni avec
  la Photothèque) dans `wp-content/mu-plugins/` du blog (créer le dossier au besoin). Cette petite
  extension « must-use », active sans manipulation, lit les identifiants dans l'en-tête de secours
  `X-Phototheque-Auth` que la Photothèque envoie aussi ; WordPress les vérifie comme d'habitude.
  Alternative selon l'hébergement, en tête du `.htaccess` (sur une seule ligne) :
  `SetEnvIf Authorization "(.*)" HTTP_AUTHORIZATION=$1` ou `CGIPassAuth On`.
- *Mot de passe refusé* : il faut un mot de passe d'application (pas celui du compte) et
  l'identifiant de connexion (pas le nom affiché).

## Identification des plantes (Pl@ntNet)

Service en ligne [Pl@ntNet](https://my.plantnet.org) (clé personnelle, plan gratuit : 500
identifications par jour). Dans Réglages › Identification des plantes :

- **Clé d'API** : celle de votre compte my.plantnet.org.
- **Tags concernés** : `fleur, plante, arbre` par défaut (tous types de tags, y compris les tags
  automatiques). Seules les photos portant un de ces tags et **sans légende** sont envoyées ; les
  vidéos sont ignorées.
- **Appels maximum par jour** : 500 par défaut. Compteur local recalé sur le nombre restant renvoyé
  par Pl@ntNet ; le service compte par jour UTC (remise à zéro à 1 h en hiver, 2 h en été).
- **Confiance minimale** : en dessous (20 % par défaut), le résultat est conservé mais pas mis en
  légende.
- **Identifier les plantes** lance le traitement en arrière-plan, des plus récentes aux plus
  anciennes, jusqu'au plafond du jour. Relancer le lendemain pour la suite : une photo déjà
  envoyée ne l'est plus (« Retenter les photos non reconnues » pour les rejouer).

La légende posée est « Nom commun (Nom scientifique) », enregistrée dans `.phototheque.json` ;
l'image envoyée est la photo telle qu'affichée (recadrage et redressement appliqués), petit côté
1280 px. Les propriétés de la photo affichent l'espèce, le score et la famille.

## Reprise de Picasa

La correspondance entre un fichier et sa section du `.picasa.ini` ignore la casse et les
différences de codage Unicode des accents (`IMG_001.JPG` = `[img_001.jpg]`).

| Donnée Picasa | Reprise |
|---|---|
| `rotate=rotate(n)` | quarts de tour (anti-horaires ; réglable) |
| `filters=tilt=1,a,s` | redressement de `−a` × facteur étalonné (la convention de signe de Picasa est inverse) |
| `filters=crop64=1,hex` | recadrage (4 × 16 bits / 65536) |
| `enhance`, `autolight`, `autocolor`, `warm` (J'ai de la chance, Contraste auto, Couleur auto, Réchauffer) | remplacés par une seule opération « Améliorer », à l'intensité par défaut des réglages (paramètres calculés au rendu ; un changement de l'intensité par défaut s'applique à ces photos) |
| `unsharp2` (Netteté) | conservé et appliqué (masque flou), sans bouton dans l'interface |
| `bw`, `sepia` | noir et blanc, sépia |
| `star=yes`, `caption=`, `keywords=` | étoile, légende, mots-clés |
| `faces=rect64(...),id` + `[Contacts2]` | visages et personnes (tags « Personnes ») |
| `[.album:jeton]` + `albums=` | albums Picasa |
| Mots-clés IPTC/XMP des JPEG | tags |

Les autres filtres (`redeye`, `finetune2`, `retouch`, `grain2`...) sont signalés dans la
visionneuse comme « non repris ». « Revenir aux retouches Picasa » annule les retouches faites
dans Photothèque pour une photo.

Un album Picasa modifié dans Photothèque est copié dans `albums.json` et prend le dessus. Un album
Picasa supprimé ne réapparaît pas au scan suivant.

### Étalonnage du redressement

L'unité de la valeur `tilt` n'a jamais été documentée par Google. Quand Picasa a « enregistré »
une photo, l'original se trouve dans `.picasaoriginals`. `calibrate` compare les deux images
(points ORB et similitude) et en déduit le nombre de degrés par unité. Le facteur par défaut
(10) est une valeur d'attente.

```bash
python -m phototheque calibrate --apply
```

L'étalonnage est aussi disponible dans **Réglages**.

## Directives : format de `.phototheque.json`

```json
{
  "version": 1,
  "photos": {
    "IMG_0003.jpg": {
      "ops": [
        {"op": "perspective", "pitch": -14.0, "roll": 0.0, "strength": 0.8},
        {"op": "crop", "l": 0.05, "t": 0.02, "r": 0.97, "b": 0.95}
      ],
      "tags": ["façade"],
      "removed_tags": ["plage"],
      "star": true
    }
  }
}
```

Opérations : `rotate90 {n}`, `straighten {deg}`, `crop {l,t,r,b}`,
`perspective {pitch, roll, strength, f35?}`, `bw`, `sepia`, `enhance`, `autocontrast`,
`autocolor`, `sharpen`, `warm`. Elles sont appliquées dans l'ordre de la liste. La présence de
`ops` signifie que l'utilisateur a pris la main sur les retouches Picasa de la photo.

## Architecture

```
phototheque/
  api.py          API REST + service de l'interface
  library.py      indexation, fusion Picasa + directives, tags, albums
  picasa.py       lecture des .picasa.ini (lecture seule)
  sidecar.py      fichiers .phototheque.json
  albums.py       albums.json
  metadata.py     EXIF (date, focale), IPTC/XMP
  render.py       rendu à la volée + cache disque
  perspective.py  détection et correction des lignes fuyantes
  calibrate.py    étalonnage du tilt Picasa
  db.py           schéma SQLite (index + file de jobs)
  static/         interface (HTML, JS, CSS, i18n, Bootstrap embarqué)
tests/            tests (pytest) + générateur de photothèque d'exemple
```

## Tests

```bash
pip install -e ".[dev]"
python tests/make_sample.py /tmp/Exemple      # photothèque d'exemple avec .picasa.ini
python -m pytest -q
```

## Performances

Mesuré avec `tests/bench.py` sur une photothèque factice de 200 000 photos (2 000 dossiers),
sur une machine à 2 cœurs. Les durées réelles dépendent surtout du disque.

| Mesure | v0.6 | v0.7 |
|---|---|---|
| Premier scan complet | 58 s | 63 s |
| Rescan sans changement | 5,3 s | 0,9 s (dossiers inchangés sautés) ; 6,8 s avec `--deep` |
| Pages de la grille, dossiers, dates, tags | 3 à 60 ms | identique |
| Grille dans le navigateur | toutes les vignettes chargées au fil du défilement | 50 à 70 vignettes dans la page, 5 Mo de mémoire, saut en bas instantané |
| « Tout sélectionner » sur 200 000 photos | chargeait toute la vue (navigateur figé) | instantané (sélection résolue par le serveur) |
| Tag sur 200 000 photos | une écriture par photo | 14 s (une écriture par dossier) |
| Mémoire des vecteurs CLIP | 410 Mo | 205 Mo (demi-précision) ; recherche 0,2 s au lieu de 0,03 s |
| Regroupement des visages | 107 visages/s, de plus en plus lent | 1 300 visages/s, stable (centres des groupes en mémoire) |

- **Rescan rapide** : un dossier dont la date de modification (et celles de son `.picasa.ini`
  et de ses directives) n'a pas changé n'est pas relu fichier par fichier. Ajouter, supprimer ou
  renommer une photo modifie la date du dossier. Une photo réécrite sur place par un autre
  logiciel n'est vue que par `scan --deep` (ou par la surveillance des dossiers, si elle est active).
- **Cache** : taille maximale réglable (10 Go par défaut, Réglages). Au-delà, les aperçus les moins
  récemment vus sont supprimés en premier, puis les miniatures. Les rendus du zoom (pleine
  résolution) sont limités aux 30 derniers. Vérification au démarrage puis toutes les 15 minutes.
- **Aperçus** : décodage JPEG réduit, préchargement des photos voisines dans la visionneuse.
- **Accès à la base** : les lectures de l'interface utilisent leur propre connexion (SQLite en
  mode WAL) et n'attendent jamais l'indexation ni l'IA. Les calculs (lecture des fichiers, EXIF,
  inférence IA) se font hors du verrou d'écriture.
- **Démarrage** : le serveur répond immédiatement ; l'indexation, la surveillance des dossiers et
  la purge du cache démarrent en arrière-plan.
- **Journal** : `<données>/phototheque.log` (durée de chaque phase du démarrage, requêtes de plus
  de 0,5 s, erreurs). À joindre en cas de lenteur.
- Banc d'essai : `python tests/bench.py generate /tmp/bench 200000` puis
  `python tests/bench.py run /tmp/bench /tmp/bench_data`.

## Feuille de route

- Modèle CLIP multilingue pour la recherche en français.
- Surveillance des dossiers (watchdog) au lieu du bouton « Réindexer ».
- Correction de la distorsion des objectifs (lensfunpy) avant la détection des lignes.
- Écriture optionnelle des retouches en XMP (`crs:CropAngle`, `crs:Crop*`) pour Lightroom et darktable.
- Lecture de la base Picasa (`db3`) pour les albums des versions antérieures à 3.9.
