# Movie

Movie est un outil macOS interactif pour numériser, convertir et renseigner des
médias sans avoir à connaître MakeMKV, FFmpeg ou les codecs.

La version publique actuelle est **0.3.0**.

Son interface repose sur quelques commandes sans paramètres techniques :

```bash
uv run movie doctor
uv run movie drives
uv run movie scan
uv run movie rip
uv run movie convert
uv run movie tag
uv run movie config
```

Movie pose ensuite uniquement les questions nécessaires. Les commandes
opérationnelles `scan`, `rip`, `convert` et `tag` n'acceptent volontairement pas
d'options comme `--profile`, `--title` ou `--metadata-url`.

> **Compatibilité des disques :** `scan` et `rip` prennent en charge les DVD,
> Blu-ray et Blu-ray UHD que MakeMKV et le lecteur optique savent ouvrir. Un
> Blu-ray exige un lecteur Blu-ray ; un UHD exige en plus un lecteur et un
> firmware compatibles avec MakeMKV. Les CD audio et disques de données ne sont
> pas des disques vidéo MakeMKV et ne sont donc pas traités par `rip`.

Pour choisir sans réfléchir :

- un DVD, Blu-ray ou UHD à archiver fidèlement : `uv run movie rip` ;
- un fichier ou une ISO à changer de format : `uv run movie convert` ;
- un titre, une année ou une jaquette à ajouter : `uv run movie tag` ;
- seulement voir le contenu d'un disque vidéo : `uv run movie scan`.

Tu peux coller un chemin ou glisser un fichier depuis Finder dans Terminal ;
les espaces, guillemets et antislashs ajoutés par macOS sont acceptés.

## Installation

Movie nécessite :

- macOS et Python 3.12 ou plus récent ;
- [uv](https://docs.astral.sh/uv/) ;
- [MakeMKV](https://www.makemkv.com/download/) pour les disques vidéo et les
  images ISO ;
- un lecteur compatible avec le support utilisé — un lecteur DVD ne lit pas un
  Blu-ray, et tous les lecteurs Blu-ray ne savent pas ouvrir un UHD ;
- FFmpeg et ffprobe : `brew install ffmpeg`.
- MKVToolNix : `brew install mkvtoolnix`, pour modifier rapidement les
  métadonnées MKV sans recopier le film.

Installation du projet :

```bash
git clone https://github.com/ThomasDumont01/movie.git
cd movie
uv sync
uv run movie doctor
```

`doctor` affiche les outils détectés et fournit une commande ou un lien
d'installation lorsqu'un prérequis manque.

## Fonctionnement général

```text
DVD / Blu-ray / UHD ── rip ──> MKV source ──┐
                                             ├── convert ──> fichier final
ISO / fichier local ─────────────────────────┘                    │
                                                                  └── tag
                                                           TMDB ou saisie manuelle
```

Chaque commande possède une seule responsabilité :

| Commande | Rôle | Modifie un média |
| --- | --- | --- |
| `doctor` | vérifie MakeMKV, FFmpeg, ffprobe et MKVToolNix | non |
| `drives` | affiche les lecteurs optiques | non |
| `scan` | analyse le disque vidéo et affiche ses titres | non |
| `rip` | extrait un titre du disque en MKV | crée un fichier |
| `convert` | change le format ou le codec | crée un fichier |
| `tag` | ajoute ou corrige les métadonnées | met à jour un fichier |
| `config` | enregistre les préférences | écrit la configuration |

Cette séparation et le travail local de `rip` garantissent qu'une panne réseau
au moment de la publication ne force pas à relire le disque : le MKV déjà
vérifié est conservé avec son chemin de récupération.

## Analyser un disque vidéo

```bash
uv run movie scan
```

Movie détecte le lecteur, analyse le disque et affiche chaque titre avec sa
durée, ses chapitres et ses pistes. Une étoile signale le ou les titres
principaux possibles. Aucun fichier n'est créé.

`scan` est uniquement informatif. Son résultat n'est pas mis en cache :
`rip` refait toujours automatiquement une analyse récente du disque avant de
proposer le titre à extraire.

## Numériser un DVD, Blu-ray ou UHD

```bash
uv run movie rip
```

Movie :

1. détecte et analyse le disque avec MakeMKV ;
2. propose automatiquement le titre le plus long ;
3. demande un choix si plusieurs titres ont la même durée ;
4. extrait ce titre avec MakeMKV, sans réencodage ;
5. contrôle la vidéo, l'audio, les chapitres et la durée avec ffprobe ;
6. publie le MKV seulement si les vérifications réussissent.

`rip` ne contacte jamais Internet et n'ajoute aucune métadonnée. Le résultat
est le MKV source produit par MakeMKV. Movie ne choisit jamais arbitrairement
entre plusieurs éditions ou angles indiscernables.

Movie ne maintient aucune liste artificielle de types autorisés : si MakeMKV
ouvre le support et expose au moins un titre vidéo, la même pipeline sûre est
utilisée pour le DVD, le Blu-ray, l'UHD et leurs images ISO. Si le lecteur, son
firmware, la version de MakeMKV ou les clés nécessaires ne permettent pas
l'ouverture, l'opération s'arrête avant toute création de fichier final.

La vérification bloque uniquement un résultat manifestement inutilisable ou
tronqué : fichier illisible, vidéo absente, audio totalement absent alors que
le support en annonce, durée nulle ou écart supérieur à 5 % et à 3 secondes. Les
pistes optionnelles absentes, les étiquettes de langue différentes et un nombre
de chapitres inférieur sont signalés sans détruire un film par ailleurs valide.
MakeMKV peut notamment annoncer un sous-titre pendant l'analyse puis le retirer
s'il découvre pendant la copie que cette piste est vide.

## Convertir un média

```bash
uv run movie convert
```

Movie demande le fichier source, le format final et, si nécessaire, la priorité
entre qualité et taille. Une image ISO est ouverte avec MakeMKV. Tout autre
fichier est identifié par son contenu avec FFmpeg : MPG, ASF, MTS, AVI, MOV,
WebM, MPEG-TS, WMV, FLV, VOB et les autres formats que FFmpeg sait
décoder peuvent donc servir d'entrée. L'extension seule ne décide pas si le
fichier est accepté.

Cette compatibilité d'entrée signifie que FFmpeg doit savoir décoder les flux
du fichier. Une combinaison exotique que le conteneur final ne sait pas porter
est refusée proprement, sans publier de résultat partiel.

Les sorties sont volontairement limitées à des associations conteneur/codecs
valides, lisibles et contrôlables :

| Sortie | Codecs produits | Usage conseillé |
| --- | --- | --- |
| MKV | codecs source, sans réencodage | archive fidèle avec toutes les pistes |
| MP4 | H.264 + AAC | compatibilité la plus universelle |
| M4V | H.264 + AAC | application Apple exigeant cette extension |
| MOV | H.264 + AAC | QuickTime et logiciels de montage |
| WebM | VP9 + Opus | publication web moderne, avec un encodage plus lent |
| AVI | MPEG-4 Part 2 + MP3 | ancien appareil ou logiciel |
| MPG | MPEG-2 + MP2 | ancien lecteur MPEG-2, sans créer un DVD avec menus |
| ASF | WMV2 + WMA2 | ancien conteneur multimédia Windows |
| WMV | WMV2 + WMA2 | ancien environnement Windows |
| FLV | FLV1 + MP3 | ancien lecteur Flash |
| TS | H.264 + AAC | transport ou diffusion MPEG-TS |
| MTS | H.264 + AAC | caméscope ou fichier de transport MPEG-TS |

Pour chaque sortie réencodée, Movie propose trois priorités :

| Famille | Haute qualité | Équilibré | Compact |
| --- | --- | --- | --- |
| MP4, M4V, MOV, TS, MTS | H.264 CRF 18, AAC 320 kbit/s | H.264 CRF 21, AAC 256 kbit/s | H.264 CRF 25, AAC 160 kbit/s |
| WebM | VP9 CRF 20, Opus 192 kbit/s | VP9 CRF 30, Opus 128 kbit/s | VP9 CRF 38, Opus 96 kbit/s |
| AVI, MPG, ASF, WMV, FLV | qualité 2, audio 320 kbit/s | qualité 4, audio 192 kbit/s | qualité 7, audio 128 kbit/s |

Le profil équilibré en MP4 reste le meilleur choix par défaut. Un format ancien
ne rend pas une vidéo meilleure ; AVI, MPG, ASF, WMV et FLV existent uniquement
pour les appareils qui les imposent. MKV est le seul choix proposé en copie directe,
car c'est le conteneur d'archive le plus apte à conserver les pistes hétérogènes.

Une ISO destinée au MKV est extraite puis publiée directement, sans remuxage
inutile. Pour toute autre sortie, elle est d'abord extraite en MKV temporaire,
vérifiée, puis convertie. Toutes les pistes audio sont conservées. Les pistes de
sous-titres ne sont jamais abandonnées silencieusement : leur exclusion des
sorties réencodées est annoncée, de même que la perte éventuelle de chapitres ou
d'étiquettes de langue dans les anciens conteneurs. Choisis MKV pour tout
conserver sans compromis.

Le fichier converti est créé dans le dossier configuré ou, par défaut, à côté
de la source. Une destination existante n'est jamais remplacée.

Avant `rip` et `convert`, Movie vérifie que la destination est joignable,
inscriptible et suffisamment grande. Si le dossier enregistré est déconnecté,
inaccessible ou trop petit, Movie explique le problème et demande un autre
dossier pour cette exécution uniquement. La configuration enregistrée n'est
pas modifiée.

## Ajouter des métadonnées

```bash
uv run movie tag
```

`tag` fonctionne sur MKV, MP4 et M4V. Ce sont les conteneurs pour lesquels
Movie peut écrire puis vérifier de manière fiable les informations **et** une
jaquette intégrée. Les autres formats acceptés par `convert` ne proposent pas
tous une représentation portable de la pochette ; `tag` les refuse donc au
lieu de produire un résultat trompeur.

Dans les trois formats acceptés, Movie dispose les informations dans les champs
du conteneur (titre, date, description, genres et source) et intègre la
jaquette. La vidéo, l'audio, les sous-titres et les chapitres sont conservés sans
réencodage.

### Film officiel avec TMDB

Movie ouvre une recherche dans le navigateur. Colle le lien direct de la bonne
fiche, par exemple :

```text
https://www.themoviedb.org/movie/181812-star-wars-the-rise-of-skywalker
```

Movie affiche le titre, l'année, les genres et le résumé avant confirmation. Il
intègre ensuite le titre, l'année, le résumé, les genres, le lien TMDB et la
jaquette. Pour un MKV, l'image panoramique officielle est également jointe à
l'intérieur du conteneur sous le nom `fanart`, sans ajouter de fichier au
dossier. Aucune clé API et aucun compte ne sont nécessaires.

Le nom du fichier est normalisé en minuscules sous la forme
`titre_normalisé.extension`. Les espaces, accents et signes de ponctuation
deviennent des séparateurs simples. L'année et l'identifiant TMDB restent dans
les métadonnées plutôt que dans le nom. Par exemple :

```text
star_wars_l_ascension_de_skywalker.mkv
```

### Film personnel

Choisis `manuel`, puis renseigne :

- le titre, obligatoire ;
- l'année, facultative ;
- la description, facultative ;
- les genres, facultatifs ;
- une jaquette locale JPEG, PNG ou WebP, facultative ;
- un arrière-plan panoramique local JPEG, PNG ou WebP, facultatif.

Le fichier reçoit le même nom normalisé. Par exemple, `La Clusaz Noël` devient
`la_clusaz_noel.mkv` ; son année reste enregistrée dans les métadonnées. Les
champs laissés vides effacent les anciennes valeurs correspondantes, ce qui
permet aussi de retirer une ancienne identification TMDB.

### Sécurité de `tag`

Pour un MKV, Movie utilise MKVToolNix afin de ne modifier que les en-têtes et les
illustrations du fichier existant. Les pistes vidéo, audio et sous-titres ne sont
ni réencodées ni recopiées, même si le film se trouve sur un NAS. Le résultat est
ensuite relu avec ffprobe ; les pistes, chapitres, durée, informations et
illustrations sont contrôlés avant le renommage atomique éventuel. Le gain de
temps et d'espace est important, mais cette écriture directe suppose que le
volume reste connecté et que l'opération ne soit pas interrompue brutalement.

Pour MP4 et M4V, Movie conserve le fonctionnement transactionnel : il écrit et
vérifie une copie temporaire locale avant de remplacer l'original. Les nouvelles
illustrations remplacent les anciennes sans réencodage des pistes. Le fanart est
intégré au MKV comme seconde illustration ; MP4 et M4V conservent leur jaquette
intégrée, mais ne disposent pas d'un rôle panoramique suffisamment portable pour
que Movie y annonce un fanart fiable.

## Configuration

```bash
uv run movie config
```

L'assistant configure :

- le dossier de sortie ;
- le lecteur optique par défaut ;
- le format et la qualité de conversion proposés ;
- `auto_run`, qui retire la confirmation finale lorsque tous les choix sont
connus ;
- le son d'alerte avant une question bloquante ;
- le délai avant l'affichage de la barre de progression ;
- l'ouverture automatique de la recherche TMDB pendant `tag`.

La configuration est enregistrée dans `~/.config/movie/config.json`. Même avec
`auto_run`, Movie demande toujours une décision lorsqu'un titre de disque/ISO ou
une fiche TMDB ne peut pas être choisi sans risque. `convert` demande également
toujours le format de sortie ; la configuration sert uniquement de proposition
par défaut.

## Progression, erreurs et fichiers temporaires

`scan`, `rip`, `convert` et `tag` utilisent une seule barre globale pendant une
opération longue. Elle apparaît après le délai configuré, ne recule jamais et
indique l'activité en cours. Dès que suffisamment de données sont disponibles,
elle affiche une estimation dynamique du temps restant. La fin indique la
durée réelle ; le temps passé à répondre à une question n'est pas compté. Une
opération courte produit une seule ligne de réussite chronométrée.

`Ctrl+C` arrête MakeMKV ou FFmpeg, supprime le travail temporaire et ne publie
aucun résultat incomplet.

Movie applique les garanties suivantes :

- contrôle de l'espace libre local et distant avant une opération coûteuse ;
- extraction et vérification de `rip` sur le disque local, hors du partage
  réseau ;
- vérification du résultat avec ffprobe ;
- blocage d'un résultat illisible, sans vidéo/audio utile ou fortement tronqué ;
- avertissement explicite pour les différences de pistes optionnelles, de
  langues, de chapitres ou une durée impossible à confirmer ;
- remontée des diagnostics MakeMKV pertinents, notamment les pistes ignorées,
  erreurs de lecture récupérables et corrections de synchronisation ;
- contrôle des métadonnées et de la jaquette après écriture ;
- refus d'écraser une destination ou un lien symbolique ;
- copie fermée et synchronisée vers un fichier caché du volume cible, puis
  publication atomique ;
- conservation du MKV local vérifié, avec son chemin affiché, si la publication
  réseau échoue ;
- nettoyage après réussite, erreur ou interruption.

## Movie, MakeMKV et `dd`

Movie ne remplace pas le moteur MakeMKV : il l'utilise. Pendant la copie des
données du disque vers le MKV, la vitesse est donc essentiellement celle de
MakeMKV et du lecteur optique.

MakeMKV utilisé directement peut terminer plus vite sur une numérisation
unique. Movie ajoute volontairement :

- une analyse préalable pour présenter les titres clairement ;
- une seconde ouverture du disque par la commande d'extraction MakeMKV ;
- une vérification finale complète avec ffprobe.

Ces étapes apportent une sélection interactive, des contrôles automatiques et
une publication sûre, mais elles ont un coût. Si le seul objectif est d'obtenir
un MKV le plus vite possible et que l'utilisateur maîtrise déjà MakeMKV,
l'application MakeMKV directe reste le chemin le plus court. Movie devient
pertinent pour disposer d'un seul workflow cohérent pour analyser, numériser,
vérifier, convertir et renseigner les fichiers.

`dd` répond à un autre besoin. Il copie les secteurs du disque vers une image
brute ou ISO ; il ne choisit pas un titre, ne produit pas un MKV, ne normalise
pas les pistes et ne garantit pas le traitement des protections vidéo. Il est
adapté à la duplication bit à bit d'un disque lisible, notamment un disque de
données ou personnel. Pour obtenir un film MKV exploitable à partir d'un disque
vidéo commercial, MakeMKV reste le composant approprié. Une image créée avec `dd`
peut néanmoins être conservée comme archive du support puis fournie à
`movie convert` si MakeMKV sait l'ouvrir.

## Tests et état de validation

```bash
uv sync --group dev
uv run pytest
uv run ruff check src tests
uv run pyright
```

État vérifié le 29 septembre 2026 pour la version 0.3.0 :

- 183 tests et 85 sous-tests réussissent ;
- la couverture automatisée atteint 86 % des lignes ;
- Ruff ne relève aucune erreur ;
- Pyright ne relève aucune erreur ni aucun avertissement ;
- les outils de développement sont déclarés et verrouillés dans le projet ;
- la distribution source et la wheel se construisent correctement ;
- de vrais appels FFmpeg/ffprobe créent et relisent MKV, MP4, M4V, MOV, WebM,
  AVI, MPG, ASF, WMV, FLV, TS et MTS, puis réutilisent chacun comme source d'un MP4 ;
- les métadonnées manuelles/TMDB et le remplacement des jaquettes sont aussi
  contrôlés avec de vrais médias ;
- la fiche TMDB fournie pour *Star Wars : L'Ascension de Skywalker* et sa
  jaquette ont été récupérées puis intégrées dans un vrai extrait MKV du DVD ;
- le DVD physique `THE_RISE_OF_SKYWALKER` a été détecté et analysé ;
- le titre principal 0 a été extrait intégralement en 32 min 46 s, puis relu
  par ffprobe et décodé de bout en bout par FFmpeg sans erreur ;
- le MKV final dure 2:16:04.2 et contient 1 vidéo, 3 audios, 7 sous-titres et
  44 chapitres ; MakeMKV en annonçait 45, différence conservée comme
  avertissement non destructif ;
- les profils MP4 et M4V sont aussi exécutés sur un extrait réel de ce film ;
- la publication atomique et la protection anti-écrasement ont été validées
  directement sur un partage Synology monté en SMB ;
- les rapports MakeMKV Blu-ray/UHD, les playlists MPLS et les tailles de titre
  supérieures à 64 Go sont couverts par les tests automatisés ; aucun Blu-ray
  physique n'était disponible pour cette validation.

## Limites, expliquées simplement

| Limite | Ce que cela signifie concrètement | Choix conseillé |
| --- | --- | --- |
| MakeMKV reste nécessaire | Movie pilote MakeMKV ; il ne réimplémente ni la lecture optique ni le déchiffrement. Si MakeMKV refuse un disque, Movie ne peut pas le forcer. | Vérifier d'abord le disque dans MakeMKV et relancer `doctor`. |
| Compatibilité Blu-ray/UHD dépendante du matériel | Movie accepte ces supports, mais ne peut pas ajouter à un lecteur les capacités optiques ou le firmware requis par MakeMKV. Certains UHD nécessitent un lecteur spécifiquement compatible. | Vérifier le support avec `movie scan` ; si MakeMKV ne peut pas l'ouvrir, utiliser un lecteur/firmware compatible. |
| Les opérations utilisent un espace temporaire local | `rip`, `convert` et `tag` vérifient leur résultat sur le Mac avant sa copie vers le NAS ; il faut donc disposer localement d'un espace adapté au résultat. | Libérer de l'espace sur le Mac avant une grosse opération ; Movie contrôle cette capacité avant de commencer. |
| Pas de CD audio ni de disque de données | MakeMKV expose des titres vidéo, pas les pistes d'un CD audio ni les fichiers d'un disque de données. | Employer un outil d'extraction audio ou une copie de fichiers adaptée à ces supports. |
| Un titre est extrait à la fois | Un disque de série avec plusieurs épisodes demande une exécution par épisode. | Relancer `rip` ou `convert` pour chaque titre voulu. |
| Les menus ne sont pas conservés | Un fichier vidéo contient le film et ses pistes, pas l'interface interactive du disque. | Conserver une image complète du disque si les menus sont indispensables. |
| Certains sous-titres optiques sont des images | Les VobSub des DVD et PGS des Blu-ray restent dans le MKV, mais ne sont pas transformés en texte ni intégrés aux sorties réencodées. | Choisir MKV pour conserver tous les sous-titres. |
| Les sorties ne couvrent pas chaque muxeur FFmpeg | FFmpeg expose aussi des flux bruts, protocoles, formats audio et conteneurs professionnels qui demandent des réglages particuliers. Les proposer aveuglément produirait des fichiers invalides ou trompeurs. | Utiliser l'une des douze sorties vérifiées ; ajouter un profil dédié lorsqu'un besoin réel apparaît. |
| Pas de désentrelacement automatique | Certains anciens DVD peuvent montrer des lignes pendant les mouvements. Movie préfère préserver la source plutôt qu'appliquer un filtre potentiellement mauvais. | Lire le MKV avec un lecteur qui désentrelace, ou ajouter plus tard un profil dédié. |
| Pas d'export audio seul | M4V est un format vidéo. Movie produit donc une vidéo H.264 avec ses pistes AAC, comme pour MP4. | Employer FFmpeg directement si le besoin est uniquement d'extraire le son. |
| TMDB est lu sans clé API | Movie analyse la page publique choisie. Un changement du site peut temporairement casser l'identification. | Utiliser la saisie manuelle si TMDB est indisponible. |
| Blu-ray physique encore à qualifier en 0.3.0 | La pipeline et les rapports Blu-ray sont couverts automatiquement, mais le matériel disponible n'a permis qu'une extraction DVD complète. | Faire un premier `scan`, puis conserver le MKV seulement après la vérification automatique et un contrôle de lecture. |
