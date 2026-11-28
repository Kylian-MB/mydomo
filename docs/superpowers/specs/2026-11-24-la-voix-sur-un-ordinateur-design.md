# La voix sur un ordinateur (chantier D) : conception

Date : 2026-11-24. État : relu par un constructeur, à approuver par le propriétaire.
Branche : `claude/feat-voice-on-a-computer-fb81a5`.

Dans ce texte, « C/ » veut dire `packages/domo-core/src/domo_core/`.

## 1. Pourquoi

Sur un ordinateur, une personne parle à Domo et l'entend répondre, sans rien taper ni cliquer. C'est le premier canal vocal (feuille de route, chantier D). Il prépare les micros des pièces (E), l'application du téléphone (D bis) et l'application de l'ordinateur (F), qui réutiliseront les mêmes briques.

Il porte aussi une réponse du propriétaire qui n'est construite nulle part : **qui parle est un jugement de Domo**, pas une étiquette fixée par le code.

## 2. Ce que le propriétaire a décidé

Ses mots, dans l'ordre où il les a dits (2026-11-23 et 24). Tout ce qui n'est pas dans cette partie est une proposition de ma part (partie 9).

1. Où : « Page Web dans un premier temps, mais plus tard, un autre chantier créera l'application qui permettra aussi a domo de contrôler l'ordinateur et voir ce qu'il y a dessus ou d'afficher des choses dessus, plan, graphiques ou autre ».
2. Micro ouvert tant que la page est ouverte, voix allumée une fois, pas de mot de réveil pour l'instant : « Oui ». Avec ce cas : « si je suis par exemple sur discord avec un ami domo pourra toujours m'entendre et pourquoi pas, entendre mon ami dans mon casque ? » Entendre l'ami est laissé au chantier de l'application : « oui ».
3. Ce que le micro entend est transformé en texte à la maison par défaut, un fournisseur seulement s'il le permet ; Domo choisit sa voix ; le coût entre dans son budget : « oui ».
4. Cartes dites en mots, réponse en mots ; ce qui ne se montre qu'une fois n'est pas lu à voix haute sauf demande (risque dit une fois, puis fait) ; les secrets jamais à la voix : « oui ».
5. Reconnaître les voix dès ce chantier : « oui, n'oublie pas aussi que tout les micros n'ont pas la même voix qui y passe, c'est pourquoi l'intonation et la manière de parler devront aussi être un moyen de reconnaitre une personne ». Et : « un voix peu changer avec le temps ou si la personne est malade, Domo devra pouvoir s'y adapter ».
6. La phrase secrète dite à voix haute : « Pourquoi pas, c'est au jugement de Domo, il peu avoir aussi d'autre idée, c'est aussi quelque chose que domo peu créer lui même via un outil ». Et : « Une personne peu refuser la phrase secrète par exemple et demander a Domo de lui envoyer un sms pour être sûr si Domo a la possibilité de le faire. »
7. Les interruptions : « La conversation doit être fluide, comme si on parlais a une vrai personne, c'est aussi au jugement, par exemple, si il y a des micro, que Domo parle a quelqu'un dans la chambre et que quelqu'un dans le salon parle aussi, il ne faut pas que Domo se taise. »
8. Aucun son gardé, seulement le texte que Domo juge utile et les empreintes de voix : « ok ».
9. La machine : « Pour l'instant mon msi qui a une carte graphique, dans le futur je ferais une machine juste pour Domo ».
10. La façon de construire (un petit service de parole à la maison, écrit par nous, à côté du cœur) : « Oui ».
11. Quand Domo n'est pas sûr de qui parle : « Pour les chose ordinaire pas grave c'est au propriétaire de voir, perso ça me dérange pas que domo soit moins sûr si c'est pour allumer une lumière, pour les choses sensible une preuve n'est pas forcément demandé directement, le but est de dérangé le moins possible la personne avec des vérifications, ceux-ci sont a faire si il y a un vrai doute ».
12. Une personne refusée qui demande à être oubliée (dit à la session « Domo crée les personnes ») : « Description seulement ». Ses empreintes de voix sont donc toujours effacées.

13. Le texte de ce que Domo entend sans que ce soit pour lui (le jugement rapide, le tri de ce qu'il retient) reste à la maison par défaut, lu par un petit modèle local ; un fournisseur seulement s'il le permet ; sans modèle local assez bon, Domo ne retient rien de ce qui ne lui est pas adressé : « Oui ».
14. L'essai à la main attend son installation ; d'ici là les essais sont faits par la session : « Ça me va ».

15. Les modèles de parole (ajout du 2026-11-24, après l'approbation) : « ça doit marcher sur tous les PC ou serveurs ». La voix ne dépend d'aucune machine : Domo mesure là où il tourne et choisit lui-même ; rien n'est mesuré ni téléchargé sur son serveur par la session. Pour les essais de la session : « tu peu télécharger les model que tu veux pour le pc sur lequel tu travaille, il a une carte graphique aussi, le seul point est qu'il n'a plus beaucoup d'espace dans le disque C mais il en a sur les autres disques ».

16. La voix du plancher (2026-11-24, dit au coordinateur, sur son résumé) : la seule voix qui tient sans carte graphique ni programme séparé a une licence à usage restreint, et son dépôt d'origine est archivé. « Je suis ok » : elle est acceptée comme voix de départ **provisoire**. Sa licence est nommée dans la documentation et dans ce que Domo sait de ses modèles ; un réglage cache les modèles à licence restreinte ; les poids sont téléchargés, jamais livrés ; qui installe lit une phrase disant que cette voix a des conditions d'usage et où les lire. À faire plus tard : trouver une voix de plancher sous licence libre.

Décisions plus anciennes qui tiennent : tout doit pouvoir se dire et s'entendre ; Domo écrit tout ce qu'une personne lit ou entend ; Domo choisit ses modèles ; les personnes sont prévenues qu'il écoute ; ne pas lier la conception à Docker.

## 3. Buts, hors-buts, ce qui serait un échec

**Buts**
- Parler à Domo depuis la page et l'entendre répondre, sans clavier ni souris, de façon fluide.
- Pour chaque phrase entendue, donner à Domo de quoi juger qui parle : qui c'est sans doute, à quel point, d'après quels indices.
- Ne plus refuser d'office quelqu'un dont le code n'est pas sûr : Domo juge (constats POL-4, CNV-4, WEB-1 de l'audit).
- Une preuve qui se dit : la phrase secrète, convenue, changée et retirée en parlant (constat PPL-5).
- Un service de parole à la maison que les chantiers suivants réutilisent tel quel.

**Hors-buts**
- Les micros des pièces, le mot de réveil, le téléphone, l'application de l'ordinateur, le son que l'ordinateur joue (la voix d'un ami dans le casque).
- Créer la fiche d'une voix inconnue : c'est la session « Domo crée les personnes ». Ici, seulement le branchement.
- Que Domo crée lui-même un moyen de preuve par un outil : la porte reste ouverte (6.9), la construction est à placer par le coordinateur.
- Les discussions à plusieurs (chantier E).

**Ce qui serait un échec même si ça marche**
- Du son du micro qui sort de la maison sans que le propriétaire l'ait permis.
- Un secret ou une phrase secrète qui passe par un modèle, la discussion, le journal ou les logs.
- Domo qui coupe la parole ou se tait par automatisme, ou qui répond à ce qui ne lui est pas adressé.
- Une vérification demandée sans vrai doute.
- Une phrase toute faite écrite par le code et dite à une personne.
- Un `if` sur le nom du canal dans le cœur.

## 4. Contraintes

- Le socle reste dans le code : règles écrites du propriétaire et droits d'abord ; une preuve est vérifiée par du code ; les secrets ne sont jamais montrés à un modèle ; les limites de dépense.
- Ordre des fusions : « Domo juge d'abord » en premier. Tout ce qui touche `policy/barrier.py`, `conversation/speaker.py`, `conversation/service.py`, `identity/`, `channels/page/`, `channels/api/`, `web/static/chat.js` attend cette fusion. Le reste peut se construire avant (partie 8).
- « Domo juge d'abord » lit qui parle par **une seule** fonction, `Speaker.as_json(now)` : on l'élargit là, jamais par un second chemin. « Personne n'a parlé » reste distinct de « quelqu'un dont on n'est pas sûr a parlé ». La page d'aujourd'hui (un indice sûr, une personne connectée) doit se rendre exactement comme ses tests le fixent.
- `prompts/system.md` est plein (8 981 sur 9 000 caractères) : ce que Domo doit savoir de la voix va dans le texte du canal et les descriptions d'outils.
- Les numéros de migration ne sont pas choisis ici (0055 à 0057 et la suite sont pris par les autres sessions).
- Aucun nom de personne réelle, aucun nom de produit d'un service de maison dans le code. Les noms de modèles libres ne figurent que dans des fichiers de données et la documentation.

## 5. Les façons de construire

1. **Un service de parole à la maison, à côté du cœur** — retenu par le propriétaire. Un programme séparé transforme le son en texte, le texte en voix, et compare les voix. Le cœur l'atteint par le gardien, comme un modèle. Il parle le langage courant des fournisseurs, donc « à la maison » ou « chez un fournisseur » est un réglage. Il tourne dans Docker ou en dehors, avec ou sans carte graphique. Prix : une pièce de plus, des modèles à télécharger la première fois.
2. **Tout dans le cœur** — écarté. Le cœur devient lourd et lié à la carte graphique ; une des meilleures voix françaises libres est sous licence GPL, incompatible avec un cœur MIT qui l'importerait.
3. **La voix en direct d'un fournisseur** — écarté. Tout ce que le micro entend sort de la maison et Domo ne juge plus avant de parler.

Dans la façon 1, le service est **écrit par nous** (petit, briques libres), plutôt que repris d'un service libre tout fait encore en version d'essai. Si ce dernier mûrit, Domo peut s'en servir à la place : même langage.

## 6. Comment ça marche

### 6.1 Vue d'ensemble

```
page (micro, détection de parole, lecture du son)
   │  WebSocket, sur la même adresse sûre que la page
cœur : canal « voix »  ──►  gardien  ──►  service de parole (maison) ou fournisseur
   │        texte + empreinte + mesures
   ├─ qui parle (indices)            6.5
   ├─ est-ce pour Domo ? couper ?    6.6
   ├─ preuve attendue ?              6.9
   └─ tour de conversation habituel  ──►  mots  ──►  voix  ──►  page
```

### 6.2 La page

- Un seul geste, une fois : allumer la voix. Le choix est retenu pour cet appareil ; aux visites suivantes la page écoute dès qu'elle s'ouvre (le navigateur a déjà l'autorisation du micro). Éteindre : le dire (« arrête d'écouter », en mots libres, Domo dispose d'un outil pour cela) ou cliquer.
- La page montre toujours si le micro écoute. Ce texte, comme les autres, est écrit par Domo (mécanisme de « Domo écrit tout »).
- Le micro est pris avec l'annulation d'écho du navigateur. La parole est détectée **dans la page** : seuls les moments de parole partent vers le serveur, en son brut (16 kHz, mono), par un WebSocket (`/chat/voice`, le premier du projet ; même session que la page). La page annonce aussi « quelqu'un commence à parler » dès le début, sans attendre la fin de la phrase.
- La détection de parole est un petit modèle servi par notre propre adresse (rien n'est chargé d'ailleurs). La règle de sécurité de la page gagne ce qu'il faut pour l'exécuter (`script-src 'self' 'wasm-unsafe-eval'`, à vérifier à la construction) et rien d'autre.
- Le son de Domo arrive par le même WebSocket, par morceaux, et se joue aussitôt. La page sait baisser le son, l'arrêter net, reprendre.
- Fichiers neufs (`voice.js`, les fichiers du détecteur) plutôt que des ajouts dans `chat.js`.

### 6.3 Le service de parole (`packages/domo-speech`)

Un petit serveur HTTP, sans état, qui ne garde aucun son et n'écrit aucun texte entendu dans ses logs.

| Route | Rôle | Forme |
|---|---|---|
| `POST /v1/audio/transcriptions` | son → texte, langue détectée, moments des mots | celle des fournisseurs |
| `POST /v1/audio/speech` | texte → son, par morceaux | celle des fournisseurs |
| `POST /v1/voice/print` | son → empreinte (vecteur) et mesures de l'intonation (hauteur médiane, étendue, débit) | à nous |
| `GET /health` | prêt, ou en train de se préparer (téléchargement des modèles) | à nous |

- **Aucun modèle n'est choisi d'avance** (décision 15). Le projet contient une liste de candidats libres, avec leur licence. Le service **mesure lui-même, sur la machine où il tourne**, ce qu'elle supporte (au premier démarrage, et quand la machine change), et propose à Domo, pour chaque usage, les candidats qui tiennent ; Domo choisit, comme pour ses autres modèles. **Le plancher** : de petits modèles qui tournent sur un simple processeur, sans carte graphique ; avec une carte graphique, de meilleurs. Si même le plancher ne tient pas, le service le dit, et les règles de 6.4 s'appliquent (un fournisseur seulement si le propriétaire le permet). Les poids sont téléchargés au premier besoin dans un dossier qui persiste, dont le propriétaire choisit l'emplacement.
- Une voix sous licence GPL n'est jamais importée dans notre code : elle tourne comme un programme à part que le service appelle.
- Dans Docker : un service `speech` de `compose.yaml`, joignable par le gardien seulement. Hors Docker : une commande (`domo-speech`), une adresse dans la configuration. Rien d'autre ne dépend de Docker.
- À l'installation, il est déclaré comme un fournisseur « à la maison » (type `openai_compatible`, sans clé). Le propriétaire peut en déclarer d'autres.

### 6.4 Choisir et compter

- Cinq nouvelles sortes de tâche pour le choix de Domo : **entendre**, **dire**, **empreinte de voix**, **attention** (6.6) et **tri de l'entendu** (6.6). Domo choisit le modèle de chacune comme il choisit déjà les autres.
- Règle du code (c'est une règle du propriétaire) : **entendre**, **empreinte**, **attention** et **tri de l'entendu** ne vont que vers un fournisseur à la maison, sauf réglage contraire du propriétaire (`speech.hear_outside`, faux par défaut ; un seul réglage pour le son et pour le texte de ce qui n'est pas adressé à Domo). **Dire** va où Domo veut. Ce qui est adressé à Domo va à son modèle habituel, comme un message tapé.
- **Attention** et **tri** sont des tâches de discussion ordinaires, servies par un petit modèle local déclaré comme fournisseur à la maison (le propriétaire a une carte graphique ; le guide d'installation dit comment en déclarer un). Sans modèle local : pas de tri (Domo ne retient rien de ce qui ne lui est pas adressé), et pour l'attention, Domo le dit au propriétaire et lui propose le choix : permettre un fournisseur, ou n'écouter qu'après un geste (la voix reste éteinte tant qu'il n'a pas choisi). Une phrase attendue comme preuve (6.9) est toujours transcrite à la maison, quel que soit le réglage.
- Un appel de parole ne passe pas par l'interface des modèles de discussion : un adaptateur à part, mais **le même comptage** (réserver, régler, limites fermes). Unités nouvelles : secondes entendues, caractères dits. À la maison le prix est zéro, l'appel est compté quand même.
- Le gardien relaie déjà du binaire. Son nettoyage des réponses (qui retire un secret renvoyé en écho) ne s'applique pas aux réponses de son, qu'il pourrait abîmer (9, choix 6).

### 6.5 Qui parle

**Ce que le canal donne à Domo pour chaque phrase.** Un `Speaker`, comme aujourd'hui, avec :

- la personne probable (une fiche, avec ou sans compte), ou personne ;
- le canal et le micro (`microphone` : l'appareil et son entrée de son) ;
- des indices, chacun avec son âge réel :
  - **la page** : la session connectée, datée de la connexion (forme de « Domo juge d'abord »), source `code` ;
  - **la voix** : pour, contre, ou inconnue, avec un **degré** de 0 à 1, source `code`. Toujours de force « faible » : une voix seule n'établit jamais quelqu'un ;
  - **l'intonation** : mesurée, comparée aux habitudes de la personne ; elle entre dans le degré et se lit aussi à part ;
  - **la manière de parler** : écrite par Domo lui-même (source `domo`), pour ou contre, quand ses mots et ses tournures collent ou non.

`Clue` gagne `degree` (nombre ou rien) ; `weighs` gagne « unknown » ; `Speaker` gagne `microphone`. `as_json(now)` rend ces champs en plus, et seulement quand ils existent : la page d'aujourd'hui se rend à l'identique.

**Trois états, calculés par le code comme des faits, pas comme un verdict :**

- **sûr** : un indice sûr du code désigne la personne (la page connectée, une preuve récente) et la voix colle, ou n'est pas encore apprise, ou il n'y a pas de voix (page tapée) ;
- **pas sûr** : il y a une personne probable, mais rien de sûr ne la confirme. Trois cas : la voix de la personne de la page colle mal (un rhume, un nouveau casque, ou quelqu'un d'autre) — la personne probable reste celle de la page ; la voix désigne nettement une autre personne connue — la personne probable est celle-là ; plus tard, une voix sans page ;
- **inconnu** : la voix de la personne de la page est apprise, celle-ci en est nettement loin, et elle ne ressemble à personne de connu.

L'état ne dépend que des indices du code. L'indice de source `domo` (la manière de parler) ne change pas l'état : il est lu par le juge et par le modèle qui discute. Il est écrit par Domo pendant un tour, avec un outil, et vaut pour les phrases suivantes ; aucun appel de modèle n'est ajouté avant la barrière.

**De qui sont les droits.** Toujours ceux de la personne probable, c'est-à-dire de celle qui parle, jamais ceux de la page par principe. Paul reconnu à sa voix devant la page d'Alex agit avec les droits de Paul ; sa phrase et la réponse vont dans **sa** discussion, pas dans celle d'Alex.

**Ce qu'une personne « pas sûre » peut entendre.** C'est le jugement de Domo. Le texte du canal lui dit qui parle sans doute et à quel point ; à lui de ne pas dire tout haut ce qui est privé quand il doute, comme on le ferait au téléphone. Le code ne garde que ceci : chercher dans la mémoire et lire ce qui est privé sont des choses sensibles (liste ci-dessous), donc jugées.

**Après une preuve.** Une preuve donnée reste un indice sûr pendant 15 minutes pour cette personne, ce canal et ce micro, prolongée tant qu'elle continue de parler (règle d'aujourd'hui). À la voix, elle ne vaut que pour les phrases dont la voix ne contredit pas cette personne.

**Prévenir.** La page montre toujours qu'elle écoute. À une voix qu'il entend pour la première fois, Domo dit qu'il écoute s'il le juge utile (décision ancienne : il décide s'il prévient, réglable) ; il retient l'avoir dit, par référence de voix.

**Ce que la barrière fait** (remplace le refus d'office, `policy/barrier.py` lignes 185-188 et 215-217). L'ordre ne change pas : règles écrites du propriétaire et droits manquants d'abord, puis l'identité, puis le juge.

| État | Chose ordinaire | Chose sensible |
|---|---|---|
| sûr | comme aujourd'hui | comme aujourd'hui |
| pas sûr | faite, avec les droits de la personne probable (réglable par le propriétaire : `voice.ordinary_when_unsure`, « faire » par défaut, ou « juger ») | **le juge**, avec les indices : faire, demander un oui, demander une preuve, ou refuser. Juge muet ou en panne : une preuve, jamais un « faire » silencieux |
| inconnu | pas de fiche, donc pas de droits : Domo peut répondre, pas agir | idem ; Domo peut demander à une personne de confiance |

Exemples. Alex enrhumé devant sa page : « pas sûr », personne probable Alex ; la lumière s'allume ; pour un virement, Domo juge avec le reste (sa manière de parler, l'heure, ce qu'il sait) et ne demande une preuve que s'il doute vraiment. Un ami devant la page d'Alex, voix nettement autre : « inconnu » ; Domo lui répond, n'agit pas pour lui, et laisse un mot à Alex.

**Ce qui est ordinaire et ce qui est sensible**, en mots simples (à approuver par le propriétaire). « Sensible » ne veut pas dire refusé ni preuve d'office : Domo juge d'abord et ne vérifie que s'il a un vrai doute.

- **Ordinaire** (fait sans histoire, même si Domo n'est pas sûr) : parler avec Domo ; ce qui agit dans la maison sans coûter, sans sortir et en pouvant se défaire (allumer une lumière, la musique, le volume), y compris par un outil que Domo a écrit ; poser un rappel pour soi ; changer son propre prénom.
- **Sensible** (jugé) : ce qui coûte de l'argent ; ce qui ne se défait pas ; ce qui sort de la maison (un message, un mail, un appel) ; les droits, les rôles, les accès, les personnes, les règles du propriétaire ; les secrets ; les appareils confiés ; chercher dans la mémoire, la changer ou l'effacer, lire ce qui est privé ; la confiance ; répondre à une demande en attente ou à un accord ; changer ou annuler un plan ; écrire, changer ou programmer un outil ; tout ce que le code ne sait pas classer.

En code, c'est `may_act_weakly(action_type, policy, *, risks=None)` de la session « Domo crée les personnes » (construit là-bas, non branché) : je le branche ici, et la barrière lui passe les risques déclarés de l'opération qu'un outil lance (`physical` seul est ordinaire ; risques illisibles : sensible). Si ce chantier arrive avant celui des personnes, je construis le prédicat sous le même nom et la même liste, remplacé au rebasage. Une preuve que les droits ou une règle du propriétaire exigent n'est jamais abaissée par le juge.

La section du juge sur qui parle (`prompts/yes_judge.md`, tenue par « Domo juge d'abord ») gagne la lecture d'un degré et de la source `domo`. Le modèle qui discute reçoit aussi, dans le texte du canal, qui parle sans doute et à quel point (CNV-4).

**Une voix inconnue devant la page de quelqu'un.** Domo lui répond à voix haute par un court échange sans outils et sans rien de la discussion de la personne de la page, et laisse un mot à cette personne (« quelqu'un que je ne connais pas m'a demandé… »). Rien d'autre n'est gardé. Quand la session « Domo crée les personnes » sera là, c'est à cet endroit que Domo pourra ouvrir une fiche (6.10).

**Apprendre et suivre les voix** (dans le cœur, `C/channels/voice/`) :

- Une **référence** par voix, à nous, opaque. Une voix inconnue a la sienne dès la première phrase, avant toute fiche. Les empreintes (des vecteurs, jamais du son) sont rangées par référence **et par micro**, plusieurs par personne.
- Domo apprend la voix de quelqu'un quand il est sûr par un autre moyen (la page connectée, une preuve) et que plusieurs phrases de suite se ressemblent, sur au moins deux visites. Avant cela, l'indice de voix dit « pas encore apprise ».
- Chaque personne a des empreintes **de fond**, qui bougent lentement, et des empreintes **récentes**, mises à jour un peu à chaque phrase sûre. Ainsi la voix qui change avec le temps est suivie.
- **Malade** : la voix colle moins, les autres indices tiennent. Domo ne traite pas la personne en inconnue ; ces phrases vont dans une empreinte **passagère** qui s'efface seule et n'abîme pas celles de fond.
- **Nouveau micro** : pas d'empreinte pour ce micro ; Domo compare à celles des autres micros avec moins de poids, et en apprend une pour celui-ci.
- « Ce n'était pas moi », dit comme on veut : Domo retire ce qu'il a appris à tort (un outil).
- Une phrase trop courte pour comparer donne un degré bas, pas un « contre ». Les phrases qui se suivent s'additionnent.

### 6.6 Est-ce pour Domo ? Faut-il se taire ?

Rien ici n'est décidé par le code. Un **jugement rapide** (un petit modèle à la maison choisi par Domo, sorte de tâche « attention », 6.4) reçoit la phrase, qui parle sans doute, d'où, les derniers échanges, et si Domo est en train de parler. Il répond :

- **pour Domo ou non** ;
- si Domo parlait : **se taire**, **reprendre**, ou **laisser tomber**.

Déroulé :
1. Quelqu'un commence à parler pendant que Domo parle : la page **baisse le son** tout de suite (un geste mécanique, pas un jugement), Domo continue.
2. À la fin de la phrase, le jugement rapide décide. Se taire : le son s'arrête, la phrase devient la suite de la conversation (elle est gardée, jamais refusée : forme de « Domo juge d'abord »). Reprendre : le son remonte.
3. « Stop », « tais-toi », « attends » : aucune liste de mots dans le code, c'est le jugement rapide qui comprend.

Avec un seul micro, tout vient du même endroit ; le jugement reçoit quand même le micro et le lieu, pour que plusieurs pièces n'aient rien à changer (une voix au salon ne coupe pas Domo qui parle dans la chambre).

Ce que Domo n'a pas eu le temps de dire reste écrit dans la discussion. La table des remises note pour le canal voix si le message a été dit en entier ou coupé.

**Ce qui n'est pas pour lui** n'entre dans aucune discussion. Ces phrases sont gardées en mémoire vive, puis données en un lot à Domo quand la pièce redevient calme : il en retient ce qu'il juge utile dans sa mémoire, par ses outils habituels, et le reste est jeté. Elles ne sont jamais écrites sur disque ni dans les logs.

### 6.7 Parler

- Un tour de conversation habituel : `Conversation.say` puis `answer`, avec un auditeur du canal voix. Les mots arrivent par morceaux ; l'auditeur les découpe en phrases, les envoie à « dire », et pousse le son à la page. Le texte est aussi dans la discussion, donc visible sur la page.
- Capacités du canal : pas de boutons, ne montre rien une fois, pas d'écran sûr, entend ce que Domo dit de lui-même, preuve possible : la phrase dite. Le texte du canal dit au modèle que ses mots seront **dits** : phrases courtes, pas de listes ni de mise en forme.
- Ce que Domo dit de lui-même (un mot laissé, un rappel) est dit à voix haute si la page écoute.
- Langues : la langue de chaque phrase est détectée ; Domo répond dans celle-là avec une voix de cette langue. Sans indice : la langue de la personne, puis celle de la maison.

### 6.8 Cartes, codes, secrets

- **Une carte** est dite par sa phrase (`card_sentence`, déjà là). Un oui, un non, un oui avec changement se disent en mots libres : c'est le chemin qui existe déjà pour le deuxième canal.
- **Ce qui ne se montre qu'une fois** reste sur la carte de la page ; Domo dit que c'est là. Si la personne demande de le lire, Domo dit le risque une fois (il retient l'avoir dit), puis le dit.
- **Un secret** (clé, mot de passe à ranger) : jamais à la voix. Le formulaire sûr reste sur la page, comme pour tout canal sans écran sûr.
- Constats WEB-3 et WEB-5 de l'audit (ce qui n'a pas de chemin en mots sur la page) : hors de ce chantier sauf ce qui est cité ici ; ils appartiennent à « Domo juge d'abord ».

### 6.9 La preuve qui se dit

- Nouveau moyen de preuve `spoken_phrase` dans `C/proofs/kinds.py`. La phrase est **rangée chez le gardien**, chiffrée, par référence ; le cœur ne la garde jamais (le texte entendu ne fait que le traverser en mémoire vive vers le gardien, comme un mot de passe tapé sur la page ; il n'est ni écrit, ni journalisé, ni montré à un modèle). La comparaison est faite **par le gardien** : il reçoit le texte entendu et répond « c'est elle », « presque » ou « rien à voir » (comparaison tolérante aux petites erreurs d'écoute ; essais limités comme pour un mot de passe).
- **Convenir** : la personne le demande ou Domo le propose (outil). Domo ouvre un moment d'écoute réservé : les deux phrases suivantes (la phrase, puis sa répétition) vont au coffre, pas au modèle, pas à la discussion. Domo dit une fois sa faiblesse : la pièce peut l'entendre. **Changer** et **retirer** : pareil, en mots (PPL-5).
- **Donner** : quand une preuve est attendue de cette personne sur ce canal, la phrase suivante passe d'abord par le gardien. « C'est elle » : preuve donnée, rien n'est écrit. Si la voix qui l'a dite contredit la personne (quelqu'un qui a entendu la phrase), la preuve est quand même vérifiée par le code, mais l'indice « contre » va au juge avec elle : il peut juger qu'elle ne suffit pas et en demander une autre. « Presque » : essai manqué, la phrase est jetée, Domo le dit. « Rien à voir » : c'était une phrase ordinaire (« non, envoie-moi plutôt un SMS »), elle suit le chemin habituel.
- **Quelle preuve** : Domo propose parmi celles que la personne a et que le canal porte ; la personne peut en demander une autre. Une preuve donnée ailleurs (la clé d'accès sur la page, un code par SMS le jour où un outil en envoie) vaut pour la demande en attente : c'est déjà ainsi que marchent les preuves. Si elle suffit est le jugement de Domo, dans la limite de ce que les droits ou une règle exigent.
- **La porte ouverte** : un moyen de preuve reste « un nom, une vérification par du code ». Un outil écrit par Domo pourra en ajouter un le jour où ce sera construit ; rien ici ne suppose que la liste est fermée.

### 6.10 Les fiches, l'oubli

Branchement sur `C/people/hooks.py` (session « Domo crée les personnes », noms de son plan) :

- `claim_reference(session, *, channel="voice", reference, person_id, now)` : « cette voix est cette personne ». Plusieurs références par personne.
- Les quatre avis (`merged`, `split`, `erased`, `forgotten`) : les empreintes suivent la fiche gardée, reviennent, ou sont **effacées**. Pour un oubli, toujours effacées, personne refusée comprise (décision 12).
- Au démarrage et de temps en temps, le canal compare ses références à `refs_of` / `person_of_ref` et efface les orphelines : un avis manqué ne laisse pas d'empreinte d'une personne oubliée.
- Si ce chantier fusionne avant celui des personnes : une petite table à nous tient le lien référence → personne, avec les mêmes noms, remplacée au rebasage.

### 6.11 Ce qui est gardé

| Quoi | Gardé ? |
|---|---|
| Le son | Jamais. En mémoire vive le temps de le traiter. |
| Ce qui est adressé à Domo | Le texte, dans la discussion de la personne qui a parlé. |
| Ce qui ne lui est pas adressé | Rien, sauf ce que Domo choisit de retenir dans sa mémoire. |
| Les empreintes de voix | Des vecteurs, par référence et par micro ; effacées avec la fiche ou à l'oubli. |
| La phrase secrète | Dans le coffre du gardien, jamais ailleurs. |

### 6.12 Le programme de texte (constat WEB-8)

`domo-talk` ne dit plus de phrases anglaises écrites dans son code : il les demande au serveur, écrites par Domo dans la langue de la personne. La preuve ne dépend plus de la commande `/prove` : quand Domo en attend une, le programme ouvre de lui-même la saisie cachée. Le programme ne gagne pas la voix dans ce chantier.

## 7. Les constats de l'audit, par leur nom

| Constat | Réponse |
|---|---|
| POL-4 (la barrière refuse qui le code n'a pas établi) | 6.5 : le refus d'office est remplacé ; pas sûr → fait ou jugé ; inconnu → Domo peut demander à une personne de confiance. |
| CNV-4 (l'identité est une porte du code, jamais jugée ; le modèle qui discute ne sait pas à quel point c'est sûr) | 6.5 : le juge reçoit les indices ; le texte du canal dit qui parle sans doute. |
| WEB-1 (un seul indice constant « sûr ») | L'âge réel vient de « Domo juge d'abord » ; ici s'ajoutent les indices faibles, « contre », « inconnue », et la source `domo` (6.5). |
| PPL-5 (convenir d'un nouveau moyen de preuve) | 6.9. |
| WEB-8 (phrases fixes et `/prove` du programme de texte) | 6.12. |
| CNV-5 (tours que personne n'a lancés) | Tenu par « Domo juge d'abord » (« personne n'a parlé ») ; ici, gardé distinct de « pas sûr ». |

## 8. Ordre de construction

**Avant la fusion de « Domo juge d'abord »** (rien de partagé) : le service de parole ; le choix et le comptage (6.4) ; la page (micro, détection, lecture) et la route du WebSocket ; le moyen de preuve `spoken_phrase` et sa comparaison au gardien ; les empreintes et leur apprentissage ; `domo-talk` sauf sa phrase « occupé ».

**Après** : l'élargissement de `Clue`, `Speaker` et `as_json` ; la barrière ; le jugement rapide branché sur les messages gardés ; la section du juge ; le branchement sur les fiches (après « Domo crée les personnes », ou avec la table d'attente).

## 9. Mes choix, et ce qu'ils coûtent s'ils sont faux

1. **Langues** : Domo répond dans la langue qu'on lui parle. Faux : une voix d'une autre langue surprend ; un réglage par personne corrige.
2. **Le service est écrit par nous.** Faux : du code de plus à tenir ; on bascule vers un service tout fait sans toucher au cœur.
3. **La liste des candidats** (quels modèles libres Domo peut essayer) est établie à la construction, après essai sur le PC de la session ; le choix est celui de Domo, sur sa machine (décision 15). Faux : un bon modèle absent de la liste ; on l'y ajoute, c'est un fichier de données.
6. Remplacé à la relecture : le son passe intact mais **surveillé** ; si un secret y apparaît, l'envoi est coupé (plus sûr que « le nettoyage ne touche pas le son »).
4. **Baisser le son dès que quelqu'un parle** est mécanique. Faux : un son qui baisse pour un bruit ; on peut le retirer et attendre le jugement.
5. **Les phrases non adressées sont données en lot** au calme, à un modèle local. Faux : Domo retient mal ou trop peu ; le lot peut être rendu plus rare ou coupé par le propriétaire.
5 bis. **Ce qu'une personne « pas sûre » peut entendre est le jugement de Domo**, pas une règle du code. Faux : quelqu'un qui imite une voix entend une chose privée ; Domo l'apprend comme une erreur, et le propriétaire peut écrire une règle.
5 ter. **Une bonne phrase secrète dite par une voix qui ne colle pas** est une preuve vérifiée, que le juge peut trouver insuffisante. Faux : une vérification de plus de temps en temps.
5 quater. **Prévenir une voix nouvelle qu'il écoute** : Domo le fait s'il le juge utile, une fois.
6. **Le nettoyage du gardien ne touche pas le son.** Faux : un secret pourrait théoriquement revenir caché dans du son ; à faire relire à la revue de sécurité.
7. **Apprendre une voix seulement après plusieurs phrases sur deux visites.** Faux dans un sens : Domo met du temps à te reconnaître ; dans l'autre : il apprend la voix d'un autre comme la tienne, corrigé par « ce n'était pas moi ».
8. **Une voix inconnue reçoit une réponse courte, sans outils, et un mot est laissé à la personne de la page.** Faux : un invité mal servi d'ici à ce que les fiches arrivent.
9. **La phrase secrète est comparée par le gardien avec tolérance.** Faux : trop tolérant, une phrase proche passe ; pas assez, on doit la redire. Le seuil se règle.
10. **Quelqu'un d'autre que la personne de la page** parle dans sa propre discussion, qu'il ne voit pas sur cette page. Faux : il ne retrouve ses mots que sur sa propre page ; les discussions à plusieurs du chantier E régleront l'affichage.
11. **« Faire » par défaut pour l'ordinaire quand Domo n'est pas sûr**, réglable. C'est ma lecture de la décision 11.
12. **`domo-talk` ne gagne pas la voix ici.**
13. **Entendre l'ami dans le casque, créer un moyen de preuve par un outil** : laissés à d'autres chantiers.

## 10. Les routes laissées ouvertes

- **Micros des pièces** : le canal porte déjà le micro et le lieu ; le jugement rapide les reçoit ; une voix par haut-parleur se règle là où le son est poussé.
- **Téléphone** : le même WebSocket et le même service de parole ; le mot de réveil se détecte sur le téléphone, avant tout envoi.
- **Application de l'ordinateur** : elle parle le même protocole que la page ; le son que l'ordinateur joue serait une seconde source, étiquetée, pour que Domo sache que ce n'est pas le micro.
- **Une voix inconnue devient un invité** : 6.5 et 6.10.
- **Hors Docker** : le service est une commande et une adresse.

## 11. Les essais

Par moi, pendant la construction : des voix enregistrées (plusieurs personnes, deux micros, une voix « enrhumée », une imitation), de vrais modèles sur cette machine, le navigateur piloté.

Par le propriétaire, le jour où Domo est en prod chez lui (décision 14) :
1. Ouvrir la page, allumer la voix, parler : Domo répond à voix haute, sans clavier ni souris.
2. Refaire à la voix l'essai du deuxième canal du chantier A : même discussion, une carte dite en mots, un oui dit à voix haute, un mot laissé qui arrive sans rien dire. **À rappeler au coordinateur à ce moment.**
3. Parler pendant un appel, avec et sans casque.
4. Quelqu'un d'autre parle devant la page : Domo ne le prend pas pour le propriétaire.
5. Convenir d'une phrase secrète, la donner, la changer.
6. Couper Domo en pleine phrase ; parler à quelqu'un d'autre pendant qu'il parle.

## 12. Laissé au constructeur

Le détail du protocole du WebSocket ; le format exact des morceaux de son ; les tables et leurs colonnes ; les seuils (degré, tolérance de la phrase, durée d'une empreinte passagère) ; les noms des outils ; le choix des bibliothèques dans le service ; le découpage des tâches.

## 13. Fichiers partagés que ce chantier touche

`C/conversation/speaker.py`, `C/conversation/channel.py`, `C/policy/barrier.py`, `C/proofs/kinds.py`, `C/proofs/service.py`, `C/prompts/yes_judge.md` (une section), `C/prompts/__init__.py` (texte du canal), `C/llm/` (tâches, comptage), `C/web/security.py`, les gabarits et fichiers statiques de la page, les fichiers de langue, `compose.yaml`, les README, `packages/domo-gatekeeper` (comparaison de la phrase, relais du son), `packages/domo-talk`. Neufs : `C/channels/voice/`, `packages/domo-speech`.
