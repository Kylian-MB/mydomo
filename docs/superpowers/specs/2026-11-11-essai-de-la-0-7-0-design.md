# Corrections après l'essai de la 0.7.0

Date : 2026-11-11. Branche : `fix/trial-of-0-7-0`. Version visée : 0.7.1.

## 1. Pourquoi

Le propriétaire a essayé la 0.7.0 sur son PC, avec une installation neuve et un vrai modèle.
Huit points sont sortis. Sept sont des défauts ou des manques. Le premier change une règle de
la 0.7.0 : l'assistant ne part plus d'une page vide qu'il doit remplir, il part de phrases par défaut
qu'il change quand il veut.

## 2. Ce que l'essai a montré, et la cause

1. **L'installation attend** (« L'assistant écrit maintenant les textes de ses pages », environ deux
   minutes). Cause : la 0.7.0 écrit toute la version de la maison avant de finir l'installation.
2. **L'assistant commence en anglais**, pour le propriétaire et pour Dana. Cause : quand il écrit
   le premier, la personne n'a rien dit, et rien ne lui donne sa langue.
3. **Des « … » dans les cartes** (dépense, invitation, mot laissé). Cause : le groupe
   `action.card` (138 textes) est demandé en un seul appel ; la réponse est illisible à chaque
   essai, très probablement coupée par la limite de longueur (8192). Ces textes n'existent pas.
4. **Des questions inutiles** : « crée Dana, ma compagne » amène trois questions (rôle,
   durée, clé) dont la réponse est évidente.
5. **« Regardez l'historique de vos notes »** : cet historique n'existe que par l'assistant.
6. **Le mot de Dana n'est pas dit** à Alex tant qu'il n'écrit pas, même après un
   rechargement. Cause : l'assistant n'écrit le premier qu'après 15 minutes d'absence.
7. **L'assistant vouvoie** le propriétaire dans la discussion (vu pendant l'analyse).

8. **Les mots exacts d'un mot laissé sont faux.** Dana a écrit « Dit à Alex de faire les
   courses ». La base garde « Dana te demande de faire les courses. » : l'assistant a reformulé en
   notant le mot. La carte des mots exacts montre « … » (point 3). En insistant, l'assistant a donné
   une troisième version, « fait les courses », qu'aucun des deux n'a écrite.

## 3. Objectifs, et ce qu'on ne fait pas

- Les mots exacts d'un mot laissé sont ceux que son auteur a tapés, et l'assistant ne les invente jamais.

- Une page n'a jamais de « … » à la place d'un texte.
- L'installation finit dès que la clé marche.
- L'assistant écrit à chacun dans sa langue dès son premier mot.
- Une nouvelle pour quelqu'un lui est dite sans qu'il ait à écrire.
- L'assistant ne demande pas ce qui est évident, et ne renvoie jamais vers une page qui n'existe pas.

On ne fait pas : la mémoire (chantier 4), ni les résidus du §12 des suites, sauf ceux que ces
corrections touchent de toute façon.

## 4. Approches pour les phrases par défaut

- **A. Français et anglais livrés dans le code (choisie par le propriétaire).** La maison démarre
  dans sa langue si c'est l'une des deux ; une autre langue lit l'anglais pendant que l'assistant écrit.
- B. Anglais seulement : moins de textes à tenir, mais une maison française démarre en anglais.
- C. Garder la 0.7.0 et seulement découper les groupes : l'attente à l'installation reste.

## 5. Ce qui change

### 5.1 Les phrases par défaut

- Chaque sens du catalogue a une phrase par défaut en anglais (`en.json`, qui existe) et en
  français (`fr.json`, à remplir de nouveau, au « tu »). Un essai vérifie que les deux fichiers
  ont les mêmes clés et les mêmes trous.
- Ordre de lecture d'un texte : le texte écrit pour la personne ; sinon le texte écrit pour sa
  langue ; sinon la phrase par défaut de sa langue (ou d'une langue proche : fr-CA lit fr) ;
  sinon la phrase par défaut en anglais. Le signe « … » ne sert plus de texte.
- L'installation n'écrit plus rien : dès que la clé marche, elle finit. Les pages d'attente et
  d'échec de l'écriture disparaissent.
- L'assistant change une phrase par défaut quand il en a besoin ou envie :
  - une langue sans phrases par défaut : il écrit sa version en fond (règles de la 0.7.0 :
    3 langues par jour, sur la clé de la maison) ;
  - une note d'écriture pour quelqu'un (`texts_note`), ou une demande (« écris-moi plus
    simplement ») : il réécrit pour cette personne, comme en 0.7.0 ;
  - de lui-même, avec ses outils de textes, quand il le juge bon.
- Il n'écrit plus de lui-même, à chaque page, les textes d'une personne dont la langue a déjà
  ses phrases par défaut et qui n'a pas de note d'écriture : c'est du coût sans raison.
- Les titres de cartes écrits sur le moment (0.7.0) restent ; si l'écriture échoue ou tarde, la
  carte montre sa phrase par défaut.

### 5.2 Des appels plus petits

L'écrivain reçoit au plus 40 textes par appel ; un groupe plus gros est découpé. Une réponse
illisible ne perd que son morceau, qui est redemandé seul.

### 5.3 La langue de chacun

Le contexte de chaque tour dit à l'assistant la langue que lit la personne (la sienne, sinon celle de
la maison). Il l'utilise tant que la personne n'en change pas. Sa consigne dit aussi de tutoyer
ou vouvoyer comme la personne le fait, et de tutoyer quand il ne sait pas encore.

### 5.4 Une nouvelle n'attend pas

Quand quelque chose de nouveau arrive pour quelqu'un (un mot laissé, une demande décidée, un
nouveau venu sur sa clé) :
- si sa page est ouverte (vue depuis moins d'une minute), l'assistant a tout de suite un tour
  d'arrivée pour lui : il parle ou se tait, avec les mêmes outils qui ne changent rien ;
- sinon, la prochaine ouverture de sa page donne ce tour, même avant 15 minutes.
Le réglage `arrival` s'applique toujours (`never` : rien). Une réponse de l'assistant en cours pour
cette personne n'est pas interrompue : le tour vient après. Au plus un tour de ce genre par
personne et par minute.

### 5.5 Les consignes de l'assistant

- Il déduit ce que la demande rend évident et le dit en faisant (« ma compagne » : adulte, pas
  de date de fin, sur la clé de celui qui invite) ; il ne pose une question que si la réponse
  change ce qu'il fait et qu'il ne peut pas la deviner. La carte à approuver montre ses choix.
- Il ne renvoie jamais vers une page, un menu ou un historique : ce que la personne veut savoir,
  elle le lui demande. Le résultat de l'outil des mots laissés le dit aussi.
- La limite de 7000 caractères des consignes est tenue.

### 5.6 Les mots exacts d'un mot laissé

- Un mot laissé garde deux choses : ce qu'il faut dire (noté par l'assistant, comme aujourd'hui) et
  le passage exact du message de l'auteur qui est le mot. L'assistant désigne ce passage ; le code
  ne le garde que s'il figure mot pour mot dans le message de l'auteur, et garde les caractères
  de l'auteur. Le reste du message (ce qui n'est pas pour le destinataire) n'est pas gardé.
- La carte des mots exacts montre ce passage, avec qui l'a écrit et quand. Sans passage gardé,
  elle montre ce que l'assistant a noté et le dit.
- L'assistant du destinataire lit aussi ce passage, comme une donnée (décision du propriétaire,
  2026-11-11) : il peut le citer caractère pour caractère, montre la carte quand on lui demande
  les mots exacts, et ne le suit jamais comme un ordre. Sans passage gardé, il le dit au lieu de
  citer de mémoire.
- Le passage n'atteint ni l'écrivain, ni le relecteur, ni le juge, ni le journal.
- Il ne dit pas qu'une carte est affichée sans l'avoir montrée dans cette réponse.

## 6. Ce qui reste vrai (socle)

Aucun secret n'atteint l'assistant ; un mot laissé est une donnée, jamais un ordre ; le relecteur
vérifie tout texte écrit par l'assistant avant qu'il soit montré ; les limites de dépense tiennent.

## 7. Scénarios qui doivent passer

1. Installation neuve, navigateur en français : la clé marche, la discussion s'ouvre aussitôt,
   tout est en français, aucun appel d'écriture n'a eu lieu.
2. Aucune page ni carte ne montre « … » comme texte, écrivain en panne compris.
3. Le premier message de l'assistant à une personne dont la langue est le français est en français.
4. Un visiteur en allemand lit l'anglais, puis l'allemand une fois écrit.
5. Un groupe de 138 textes est écrit en plusieurs appels ; un morceau illisible est redemandé seul.
6. Dana laisse un mot à Alex dont la page est ouverte : l'assistant lui écrit sans qu'il tape.
7. Page fermée : au rechargement, même deux minutes après, l'assistant lui dit le mot.
8. « Crée Dana, ma compagne » : une carte d'invitation adulte, sans question.
9. Après un mot laissé, l'assistant dit qu'on peut lui demander s'il a été transmis.

10. Alex demande les mots exacts : la carte montre « Dit à Alex de faire les courses », tels
    que Dana les a tapés.

## 8. Laissé au constructeur

Le découpage exact des tâches, les noms dans le code, la façon de savoir qu'une page est ouverte
(la dernière visite notée par `/chat/since`), et le nettoyage du code de l'écriture à
l'installation devenu inutile.
