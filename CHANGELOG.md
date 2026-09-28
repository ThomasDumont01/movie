# Historique des versions

## 0.2.0 — 2026-09-28

- sortie M4V vidéo H.264/AAC disponible avec les profils haute qualité,
  équilibré et compact ;
- ancien export audio seul retiré afin que le format annoncé corresponde au
  contenu produit ;
- conversion ISO vers M4V intégrée à la pipeline vérifiée ;
- écriture atomique des métadonnées étendue au M4V ;
- estimation du temps restant fondée sur la vitesse récente ;
- chemins glissés depuis Finder acceptés avec leurs espaces échappés.

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
