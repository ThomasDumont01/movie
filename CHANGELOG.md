# Historique des versions

## Non publié

- `convert` copie désormais directement le flux vidéo lorsque la source est déjà
  en H.264 et que la sortie est MP4, M4V, MOV, TS ou MTS : aucune recompression
  d'image, qualité visuelle source conservée et traitement nettement accéléré ;
- `convert` demande désormais toujours explicitement le format de sortie, même
  avec `auto_run` ; la configuration reste la valeur proposée par défaut.
- les entrées TS, MTS et M2TS forcent désormais le démultiplexeur MPEG-TS lorsque
  nécessaire, afin de retrouver la vidéo des enregistrements dont l'en-tête est
  mal détecté automatiquement par FFmpeg ;
- `convert` conserve désormais toutes les pistes audio par défaut et les encode
  séparément dans le codec de la sortie, sans les fusionner ;
- les sous-titres textuels compatibles sont conservés dans MP4, M4V, MOV et
  WebM ; les sous-titres image incompatibles restent signalés et sont tous
  préservés avec MKV ;
- les sources TS, MTS et M2TS destinées au MKV sont remuxées avec MKVToolNix,
  ce qui tolère leurs paquets vidéo sans timestamp tout en conservant vidéo,
  audios et sous-titres sans réencodage.
- la saisie manuelle de `tag` accepte désormais une date complète en notation
  française ou ISO, en plus d'une année seule, puis écrit et vérifie cette date
  exacte dans les métadonnées du conteneur.

## 0.3.0 — 2026-09-29

- `scan` et `rip` généralisés aux DVD, Blu-ray et Blu-ray UHD ouverts par
  MakeMKV, sans restriction artificielle sur le type de disque ;
- rapports Blu-ray, playlists MPLS et titres de plus de 64 Go couverts par les
  tests automatisés ;
- toutes les sources multimédias reconnues par FFmpeg peuvent alimenter
  `convert`, indépendamment de leur extension ;
- sorties MOV, WebM, AVI, MPG, ASF, WMV, FLV, MPEG-TS et MTS ajoutées aux
  sorties MKV, MP4 et M4V ;
- codecs, profils de qualité, capacités des conteneurs et règles de
  vérification centralisés dans un catalogue unique ;
- vérification spécifique des codecs, pistes audio, langues, chapitres et
  durée pour chaque sortie ;
- tests FFmpeg réels de chaque format en sortie puis comme nouvelle entrée ;
- nom des médias renseignés normalisé en `titre_normalisé.extension`, sans
  année ni identifiant de service dans le nom de fichier ;
- formats acceptés par `tag` issus du catalogue central et limités aux
  conteneurs dont les informations et la jaquette sont vérifiables ;
- `rip` extrait et vérifie désormais le MKV localement avant de le copier et de
  le publier atomiquement sur la destination, ce qui évite les verrous de
  lecture observés sur certains partages SMB Synology ;
- un MKV local déjà vérifié est conservé et signalé si sa publication réseau
  échoue, afin de ne jamais imposer une nouvelle lecture du disque ;
- `convert` et `tag` pour MP4/M4V utilisent le même staging local et la même
  publication synchronisée pour éviter les fichiers SMB encore verrouillés ;
- `tag` modifie désormais les métadonnées et illustrations MKV directement avec
  MKVToolNix, sans recopier les pistes ni le fichier complet, puis vérifie le
  résultat avec ffprobe avant son renommage éventuel ;
- ffprobe retente automatiquement la lecture lorsqu'un partage SMB conserve
  brièvement un fichier occupé après une écriture ;
- `tag` récupère le fanart panoramique officiel de la fiche TMDB et l'intègre
  directement au MKV comme seconde illustration, sans fichier compagnon ; la
  saisie manuelle accepte également un arrière-plan local.

## 0.2.0 — 2026-09-28

- sortie M4V vidéo H.264/AAC disponible avec les profils haute qualité,
  équilibré et compact ;
- ancien export audio seul retiré afin que le format annoncé corresponde au
  contenu produit ;
- conversion ISO vers M4V intégrée à la pipeline vérifiée ;
- écriture atomique des métadonnées étendue au M4V ;
- estimation du temps restant fondée sur la vitesse récente ;
- chemins glissés depuis Finder acceptés avec leurs espaces échappés ;
- publication atomique compatible avec les partages réseau SMB qui refusent
  les liens physiques, notamment les dossiers Synology montés sur macOS.

## 0.1.0 — 2026-09-28

Première version publique de Movie.

- commandes interactives `doctor`, `drives`, `scan`, `rip`, `convert`, `tag`
  et `config` ;
- extraction fidèle des DVD et ISO avec MakeMKV ;
- conversion vérifiée vers MKV et MP4/H.264/AAC ;
- métadonnées TMDB ou manuelles, avec jaquette facultative ;
- staging sur le volume cible et publication atomique ;
- destination de repli si le dossier configuré est inaccessible ou trop petit ;
- conversion des images ISO après inspection du MKV extrait ;
- progression monotone avec estimation dynamique et durée réelle ;
- diagnostics MakeMKV pertinents conservés et vérification commune des médias.
