# Movie

Movie est un outil macOS interactif pour numériser, convertir et renseigner des
médias sans avoir à connaître MakeMKV, FFmpeg ou les codecs.

La version publique actuelle est **0.1.0**.

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

## Installation

Movie nécessite :

- macOS et Python 3.12 ou plus récent ;
- [uv](https://docs.astral.sh/uv/) ;
- [MakeMKV](https://www.makemkv.com/download/) pour les DVD et les ISO ;
- FFmpeg et ffprobe : `brew install ffmpeg`.

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
DVD ── rip ──> MKV source ──┐
                            ├── convert si nécessaire ──> fichier final
ISO / fichier local ────────┘                                  │
                                                               └── tag
                                                        TMDB ou saisie manuelle
```

Chaque commande possède une seule responsabilité :

| Commande | Rôle | Modifie un média |
| --- | --- | --- |
| `doctor` | vérifie MakeMKV, FFmpeg et ffprobe | non |
| `drives` | affiche les lecteurs optiques | non |
| `scan` | analyse le DVD et affiche ses titres | non |
| `rip` | extrait un titre du DVD en MKV | crée un fichier |
| `convert` | change le format ou le codec | crée un fichier |
| `tag` | ajoute ou corrige les métadonnées | met à jour un fichier |
| `config` | enregistre les préférences | écrit la configuration |

Cette séparation garantit qu'une panne réseau ou une mauvaise jaquette ne peut
plus faire échouer une longue numérisation de DVD.

## Analyser un DVD

```bash
uv run movie scan
```

Movie détecte le lecteur, analyse le disque et affiche chaque titre avec sa
durée, ses chapitres et ses pistes. Une étoile signale le ou les titres
principaux possibles. Aucun fichier n'est créé.

`scan` est uniquement informatif. Son résultat n'est pas mis en cache :
`rip` refait toujours automatiquement une analyse récente du DVD avant de
proposer le titre à extraire.

## Numériser un DVD

```bash
uv run movie rip
```

Movie :

1. détecte et analyse le DVD ;
2. propose automatiquement le titre le plus long ;
3. demande un choix si plusieurs titres ont la même durée ;
4. extrait ce titre avec MakeMKV, sans réencodage ;
5. contrôle la vidéo, l'audio, les chapitres et la durée avec ffprobe ;
6. publie le MKV seulement si les vérifications réussissent.

`rip` ne contacte jamais Internet et n'ajoute aucune métadonnée. Le résultat
est le MKV source produit par MakeMKV. Movie ne choisit jamais arbitrairement
entre plusieurs éditions ou angles indiscernables.

La vérification bloque uniquement un résultat manifestement inutilisable ou
tronqué : fichier illisible, vidéo absente, audio totalement absent alors que
le DVD en annonce, durée nulle ou écart supérieur à 5 % et à 3 secondes. Les
pistes optionnelles absentes, les étiquettes de langue différentes et un nombre
de chapitres inférieur sont signalés sans détruire un film par ailleurs valide.
MakeMKV peut notamment annoncer un sous-titre pendant l'analyse puis le retirer
s'il découvre pendant la copie que cette piste est vide.

## Convertir un média

```bash
uv run movie convert
```

Movie demande le fichier source, le format final et, si nécessaire, la priorité
entre qualité et taille. Une image ISO est ouverte avec MakeMKV ; un fichier
multimédia ordinaire est lu directement avec FFmpeg.

| Sortie | Choix proposé | Traitement |
| --- | --- | --- |
| MKV | qualité source | copie de toutes les pistes sans réencodage |
| MP4 | qualité maximale | H.264 CRF 18, audio AAC 320 kbit/s |
| MP4 | équilibré | H.264 CRF 21, audio AAC 256 kbit/s |
| MP4 | compact | H.264 CRF 25, audio AAC 160 kbit/s |
| M4A | qualité maximale | une piste audio AAC 320 kbit/s |
| M4A | équilibré | une piste audio AAC 256 kbit/s |
| M4A | compact | une piste audio AAC 160 kbit/s |
| M4A | sans perte supplémentaire | une piste audio ALAC |

Le profil équilibré est recommandé dans la majorité des cas. Une conversion en
ALAC n'améliore pas une source déjà compressée ; elle évite seulement une perte
supplémentaire.

Une ISO destinée au MKV est extraite puis publiée directement, sans remuxage
inutile. Pour un M4A créé depuis un fichier, Movie demande la piste audio si
plusieurs pistes sont présentes. Pour un ISO, ce choix intervient après
l'extraction : Movie inspecte le MKV réellement produit afin qu'une piste
supprimée ou réordonnée par MakeMKV ne puisse pas fausser la sélection. Les
sous-titres bitmap des DVD ne sont pas copiés silencieusement vers un MP4 :
leur exclusion est annoncée et le MKV est recommandé pour les conserver.

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

`tag` fonctionne sur MKV, MP4 et M4A. Movie demande le fichier puis propose deux
modes.

### Film officiel avec TMDB

Movie ouvre une recherche dans le navigateur. Colle le lien direct de la bonne
fiche, par exemple :

```text
https://www.themoviedb.org/movie/181812-star-wars-the-rise-of-skywalker
```

Movie affiche le titre, l'année, les genres et le résumé avant confirmation. Il
intègre ensuite le titre, l'année, le résumé, les genres, le lien TMDB et la
jaquette. Aucune clé API et aucun compte ne sont nécessaires.

### Film personnel

Choisis `manuel`, puis renseigne :

- le titre, obligatoire ;
- l'année, facultative ;
- la description, facultative ;
- les genres, facultatifs ;
- une jaquette locale JPEG, PNG ou WebP, facultative.

Le nom du fichier n'est pas modifié. Les champs laissés vides effacent les
anciennes valeurs correspondantes, ce qui permet aussi de retirer une ancienne
identification TMDB.

### Sécurité de `tag`

La vidéo et l'audio ne sont jamais réencodés. Movie écrit une copie temporaire
sur le même volume, vérifie toutes les pistes et toutes les métadonnées, puis
remplace atomiquement le fichier original. En cas d'erreur ou d'interruption,
l'original reste intact. Une nouvelle jaquette remplace l'ancienne sans
supprimer les autres pièces jointes utiles du MKV.

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
`auto_run`, Movie demande toujours une décision lorsqu'un titre de DVD/ISO, une
piste audio ou une fiche TMDB ne peut pas être choisi sans risque.

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

- contrôle de l'espace libre avant une opération coûteuse ;
- travail temporaire sur le volume de destination ;
- vérification du résultat avec ffprobe ;
- blocage d'un résultat illisible, sans vidéo/audio utile ou fortement tronqué ;
- avertissement explicite pour les différences de pistes optionnelles, de
  langues, de chapitres ou une durée impossible à confirmer ;
- remontée des diagnostics MakeMKV pertinents, notamment les pistes ignorées,
  erreurs de lecture récupérables et corrections de synchronisation ;
- contrôle des métadonnées et de la jaquette après écriture ;
- refus d'écraser une destination ou un lien symbolique ;
- publication atomique ;
- nettoyage après réussite, erreur ou interruption.

## Movie, MakeMKV et `dd`

Movie ne remplace pas le moteur MakeMKV : il l'utilise. Pendant la copie des
données du DVD vers le MKV, la vitesse est donc essentiellement celle de
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
pas les pistes et ne garantit pas le traitement des protections DVD. Il est
adapté à la duplication bit à bit d'un disque lisible, notamment un disque de
données ou personnel. Pour obtenir un film MKV exploitable à partir d'un DVD
commercial, MakeMKV reste le composant approprié. Une image créée avec `dd`
peut néanmoins être conservée comme archive du support puis fournie à
`movie convert` si MakeMKV sait l'ouvrir.

## Tests et état de validation

```bash
uv sync --group dev
uv run pytest
uv run ruff check src tests
uv run pyright
```

État vérifié le 28 septembre 2026 pour la version 0.1.0 :

- 140 tests et 13 sous-tests réussissent ;
- la couverture automatisée atteint 83 % des lignes ;
- Ruff ne relève aucune erreur ;
- Pyright ne relève aucune erreur ni aucun avertissement ;
- les outils de développement sont déclarés et verrouillés dans le projet ;
- la distribution source et la wheel se construisent correctement ;
- de vrais appels FFmpeg/ffprobe vérifient MKV, MP4 H.264/AAC, M4A AAC,
  M4A ALAC, métadonnées manuelles/TMDB et remplacement des jaquettes ;
- la fiche TMDB fournie pour *Star Wars : L'Ascension de Skywalker* et sa
  jaquette ont été récupérées puis intégrées dans un MKV temporaire réel ;
- le parcours interactif réel de `movie tag`, sans argument, a été validé sur
  un média synthétique.

Un essai matériel réel a détecté le DVD `THE_RISE_OF_SKYWALKER`, analysé ses
cinq titres et extrait puis vérifié le titre court 3. Le long métrage complet
n'a pas été extrait : les titres 0, 1 et 2 ont tous une durée de 2:15:44 et
nécessitent un choix humain.

## Limites connues

- MakeMKV reste nécessaire pour ouvrir les DVD et les ISO ;
- les supports ou protections refusés par MakeMKV restent hors du contrôle de
  Movie ;
- les menus DVD ne sont pas représentés dans MKV, MP4 ou M4A ;
- les sous-titres bitmap ne sont ni convertis par OCR ni incrustés en MP4 ;
- la détection et la correction automatiques de l'entrelacement ne sont pas
  encore implémentées ;
- le traitement simultané de plusieurs épisodes ou titres n'est pas pris en
  charge ;
- aucune image ISO réelle ni aucun Blu-ray/UHD n'a encore été qualifié avec un
  support physique ;
- l'identification TMDB lit la page publique choisie par l'utilisateur et peut
  nécessiter une adaptation si le site change.
