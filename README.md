# Movie

Movie est un outil macOS interactif pour numériser, convertir et renseigner des
médias sans avoir à connaître MakeMKV, FFmpeg ou les codecs.

La version publique actuelle est **0.2.0**.

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

Pour choisir sans réfléchir :

- un DVD à archiver fidèlement : `uv run movie rip` ;
- un fichier ou une ISO à changer de format : `uv run movie convert` ;
- un titre, une année ou une jaquette à ajouter : `uv run movie tag` ;
- seulement voir le contenu du DVD : `uv run movie scan`.

Tu peux coller un chemin ou glisser un fichier depuis Finder dans Terminal ;
les espaces, guillemets et antislashs ajoutés par macOS sont acceptés.

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
| M4V | qualité maximale | H.264 CRF 18, audio AAC 320 kbit/s |
| M4V | équilibré | H.264 CRF 21, audio AAC 256 kbit/s |
| M4V | compact | H.264 CRF 25, audio AAC 160 kbit/s |

Le profil équilibré est recommandé dans la majorité des cas. MP4 et M4V
contiennent ici les mêmes codecs H.264/AAC. Choisis MP4 pour l'extension la plus
universelle, ou M4V lorsqu'une application Apple attend explicitement cette
extension vidéo.

Une ISO destinée au MKV est extraite puis publiée directement, sans remuxage
inutile. Une ISO destinée au MP4 ou au M4V est d'abord extraite en MKV
temporaire, vérifiée, puis convertie. Toutes les pistes audio sont conservées.
Les sous-titres bitmap des DVD ne sont pas copiés silencieusement vers un MP4
ou un M4V : leur exclusion est annoncée et le MKV est recommandé pour les
conserver.

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

`tag` fonctionne sur MKV, MP4 et M4V. Movie demande le fichier puis propose deux
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
`auto_run`, Movie demande toujours une décision lorsqu'un titre de DVD/ISO ou
une fiche TMDB ne peut pas être choisi sans risque.

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

État vérifié le 28 septembre 2026 pour la version 0.2.0 :

- 143 tests et 13 sous-tests réussissent ;
- la couverture automatisée atteint 85 % des lignes ;
- Ruff ne relève aucune erreur ;
- Pyright ne relève aucune erreur ni aucun avertissement ;
- les outils de développement sont déclarés et verrouillés dans le projet ;
- la distribution source et la wheel se construisent correctement ;
- de vrais appels FFmpeg/ffprobe vérifient MKV, MP4 H.264/AAC, M4V H.264/AAC,
  métadonnées manuelles/TMDB et remplacement des jaquettes ;
- la fiche TMDB fournie pour *Star Wars : L'Ascension de Skywalker* et sa
  jaquette ont été récupérées puis intégrées dans un vrai extrait MKV du DVD ;
- le DVD physique `THE_RISE_OF_SKYWALKER` a été détecté et analysé ;
- le titre principal 0 a été extrait intégralement en 32 min 46 s, puis relu
  par ffprobe et décodé de bout en bout par FFmpeg sans erreur ;
- le MKV final dure 2:16:04.2 et contient 1 vidéo, 3 audios, 7 sous-titres et
  44 chapitres ; MakeMKV en annonçait 45, différence conservée comme
  avertissement non destructif ;
- les profils MP4 et M4V sont aussi exécutés sur un extrait réel de ce film.

## Limites, expliquées simplement

| Limite | Ce que cela signifie concrètement | Choix conseillé |
| --- | --- | --- |
| MakeMKV reste nécessaire | Movie pilote MakeMKV ; il ne réimplémente ni la lecture optique ni le déchiffrement. Si MakeMKV refuse un disque, Movie ne peut pas le forcer. | Vérifier d'abord le disque dans MakeMKV et relancer `doctor`. |
| `rip` accepte les DVD | Le parcours de numérisation physique n'est pas encore qualifié pour Blu-ray ou UHD. | Utiliser un DVD, ou convertir ensuite une ISO que MakeMKV sait ouvrir. |
| Un titre est extrait à la fois | Un disque de série avec plusieurs épisodes demande une exécution par épisode. | Relancer `rip` ou `convert` pour chaque titre voulu. |
| Les menus ne sont pas conservés | Le MKV, MP4 ou M4V contient le film et ses pistes, pas l'interface interactive du DVD. | Conserver une image complète du disque si les menus sont indispensables. |
| Les sous-titres DVD sont des images | Ils restent dans le MKV, mais ne sont pas transformés en texte et ne sont pas intégrés au MP4/M4V. | Choisir MKV pour conserver tous les sous-titres. |
| Pas de désentrelacement automatique | Certains anciens DVD peuvent montrer des lignes pendant les mouvements. Movie préfère préserver la source plutôt qu'appliquer un filtre potentiellement mauvais. | Lire le MKV avec un lecteur qui désentrelace, ou ajouter plus tard un profil dédié. |
| Pas d'export audio seul | M4V est un format vidéo. Movie produit donc une vidéo H.264 avec ses pistes AAC, comme pour MP4. | Employer FFmpeg directement si le besoin est uniquement d'extraire le son. |
| TMDB est lu sans clé API | Movie analyse la page publique choisie. Un changement du site peut temporairement casser l'identification. | Utiliser la saisie manuelle si TMDB est indisponible. |
| ISO physique encore à qualifier en 0.2.0 | La pipeline ISO est couverte par les tests automatisés, mais aucune copie ISO complète de ce DVD n'a été menée jusqu'au bout pour cette version. | Conserver le MKV issu de `rip` comme résultat matériel validé. |
