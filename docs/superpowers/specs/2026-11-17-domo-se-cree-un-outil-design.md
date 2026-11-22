# L'assistant se crée un outil

Date : 2026-11-17. Première livraison du chantier C de la feuille de route (`2026-11-12-feuille-de-route-refondation.md`, « Les outils que l'assistant se crée »). Version visée : 0.10.0. Branche : `feat/domo-makes-tools`, ouverte après la fusion de la 0.9.1.

**État : approuvée par le propriétaire le 2026-11-17**, en discussion (ses réponses sont en partie 2 ; les précisions de la partie 5.8 lui ont été présentées).

## 1. Pourquoi

Aujourd'hui l'assistant a 68 outils, tous écrits par le projet. Une action que le catalogue (`policy/catalog.py`) ne connaît pas est refusée, et en ajouter une demande de toucher six endroits du code et de sortir une version. Quand aucun outil ne permet ce qu'on lui demande, il dit que ce n'est pas possible.

La règle du propriétaire dit le contraire : l'application est une base de vie, le modèle est la réflexion, et les outils que l'assistant se crée finissent son cerveau. Un outil qui manque n'est jamais un refus.

## 2. Ce que le propriétaire a décidé

Dit le 2026-11-17.

- Le chantier C est découpé ; la première livraison est « L'assistant se crée un outil ».
- « Tout ce que je veux c'est que l'assistant ait un endroit pour s'améliorer et se compléter tout seul, peu importe la demande qui pourra lui être faite. »
- C'est à l'assistant de juger s'il doit faire un outil. Il n'a pas à le dire : « tout doit se faire de manière totalement transparente pour les utilisateurs ».
- Se construire un outil ne l'empêche jamais de faire ce qu'on lui a demandé : la demande attend, et il la fait quand l'outil est prêt, sans qu'on le lui rappelle. Si l'échéance tombe avant (un rappel dans 5 minutes, un outil prêt dans 10), il trouve un autre moyen. Plusieurs demandes qui attendent le même outil sont toutes traitées quand il est terminé.
- Le contrôle d'un outil est automatique, sans vérification humaine, « et assez sécurisé quand même ». Il a accepté l'ensemble proposé (partie 6).
- Le code tourne dans un conteneur à part, le bac à sable (approche A, partie 4).
- Un outil peut en appeler un autre, y compris sous condition. L'assistant décide quand un outil se lance (une heure, un événement).
- Deux sortes d'outils : pour une personne, liés à ses droits ; pour la maison, qui valent pour tout le monde et restent quand la personne qui les a demandés perd ses droits ou s'en va.
- « Outils de départ » : ce que le projet livre et qui pourrait être à l'assistant a la même forme que ses outils (il peut le lire, le changer, le remplacer). Il changera peut-être d'avis plus tard.
- Beaucoup de ce qui existe aurait pu être construit par l'assistant : il veut qu'on revoie ce qui peut partir (partie 9).

## 3. Buts, non-buts, ce qui serait un échec

**Buts**

- L'assistant écrit un programme, le fait contrôler, s'en sert et s'en ressert, sans nouvelle version de l'application.
- Une demande qui a besoin d'un outil pas encore prêt est tenue quand même.
- Le catalogue des actions est ouvert : une sorte d'action nouvelle existe par une ligne en base, décrite par ses risques, et jugée comme les autres.
- Ce qui est posé ici sert tel quel aux livraisons suivantes (sortie sur internet, connecteurs, maison).

**Non-buts de cette livraison**

- Internet ou le réseau de la maison depuis un outil (livraison suivante ; exemple à réussir alors : aider à ajouter un appareil à Home Assistant).
- Les connecteurs, les compétences (skills), les agents spécialisés, l'assistant qui lit son propre code, les rapports de panne.
- Installer des paquets : un outil n'utilise que la bibliothèque standard de Python.
- Retirer une fonction existante (partie 9).
- Une page ou une carte pour les outils : tout se demande à l'assistant.

**Ce qui serait un échec, même si ça marche**

- L'assistant annonce qu'il crée un outil, ou demande un accord pour le faire.
- Une demande perdue ou oubliée parce que l'outil n'était pas prêt.
- Du code écrit par un modèle qui tourne dans le cœur, voit un secret, ou fait une action sans passer par les droits et le journal.
- Un outil qui donne à quelqu'un plus que ses droits.

## 4. Approches

- **A. Un conteneur à part, avec un petit lanceur de confiance** (retenue). De vrais programmes Python, enfermés. Le même endroit accueillera les connecteurs. La plus grosse à construire.
- **B. Une boîte WebAssembly dans le cœur.** Pas de conteneur, mais Python y est très limité, les connecteurs n'y tourneront pas, et le code d'un modèle tournerait dans le programme du cœur.
- **C. Des recettes sans code.** Très sûr et petit, mais l'assistant bute dès qu'une demande sort des briques prévues.

Le propriétaire a choisi A : c'est la seule qui tient « peu importe la demande », et rien n'y sera à refaire.

## 5. Ce qui est construit

### 5.1 Le bac à sable

Un nouveau service `sandbox` dans `compose.yaml`, avec le même durcissement que les autres (`x-hardened`), **sans aucun réseau** (`network_mode: none`). Le cœur lui parle par une socket Unix posée dans un volume partagé qui ne contient qu'elle.

Dedans, un **lanceur** écrit par le projet (nouveau paquet `packages/domo-sandbox`) : il reçoit du cœur un code, une entrée et des limites, lance le code dans un processus à part sous un compte sans droits, et renvoie la sortie. Il ne garde rien entre deux lancements : le code et les données d'un outil vivent dans la base du cœur. Seul le lanceur garde `SETUID`/`SETGID` (architecture §13).

Limites d'un lancement, tenues par le lanceur : 60 secondes par défaut, jusqu'à une heure quand l'assistant le juge utile pour un outil (il fixe cette durée à la création et peut la changer ; le propriétaire, 2026-11-17 : un script peut durer, c'est moins grave qu'un modèle qui tourne en rond), 256 Mo de mémoire, 64 Ko de sortie, un nombre de processus borné, pas de fichier gardé. Un lancement qui dépasse est tué et rendu comme tel. Un lancement qu'un tour attend et qui dure plus de 20 secondes continue en arrière-plan : l'assistant répond à la personne, et il est réveillé quand l'outil a fini.

### 5.2 Un outil de l'assistant

Un outil est une ligne en base (`made_tools`) et ses versions (`made_tool_versions`) :

- un nom, une description d'une ligne, ses paramètres (un schéma JSON), écrits par l'assistant ;
- son code : un fichier Python avec une fonction `run(entree)` qui rend un objet JSON ;
- pour qui : une personne, ou la maison ;
- son origine : `domo` ou `project` (outil de départ) ;
- l'empreinte (SHA-256) de chaque version, son état (`checking`, `ready`, `set_aside`) et le résultat de ses contrôles ;
- ses données (`made_tool_data`, 256 Ko au plus par outil) : ce que l'outil veut retrouver la fois suivante (le solde d'une tirelire). Elles lui sont données en entrée et il les rend en sortie.

L'entrée d'un lancement : ses arguments, ses données, la date et l'heure, et pour qui il tourne (un nom, jamais un identifiant ni un secret). La sortie : un résultat, ses données, et des **demandes** (5.4).

Dans un tour, un outil `ready` est proposé au modèle comme n'importe quel outil, sous le nom `t_<nom>`, à ceux qui y ont droit. Au-delà de 30 outils, les moins utilisés ne sont plus proposés d'office : l'assistant les retrouve avec `tool_find`.

### 5.3 Le catalogue ouvert

Une table `action_kinds` : un type d'action (`made.<nom>`), ses risques (coûte de l'argent, ne peut pas être défait, agit physiquement, sort de la maison : quatre oui/non), les faits qu'il porte, les rôles qui l'ont par défaut, et ses textes (ce qu'on en dit à la personne et dans le journal), écrits par l'assistant à la création.

`PolicyTables` n'est plus une constante : elle est lue à chaque tour, le catalogue du code plus ces lignes. La barrière, les droits du propriétaire (`rights_propose` sait nommer un type ouvert), les limites et le journal traitent un type ouvert comme un autre. Les risques sont donnés au juge du oui avec le reste du cas.

Deux bornes dans le code : un type ouvert ne peut pas porter le nom d'un type du code, et les rôles par défaut d'un outil ne dépassent jamais ceux de la personne pour qui il a été créé.

Dans cette livraison, les quatre risques d'un outil de l'assistant sont toujours « non » : il calcule. Le mécanisme est là pour les connecteurs.

### 5.4 Ce qu'un outil peut demander

Un outil ne fait rien lui-même. Sa sortie peut contenir trois sortes de demandes :

- **lancer un autre outil** de l'assistant (avec des arguments) : fait par le code, sans modèle, après la barrière. Cinq niveaux d'appels au plus, et un outil ne se rappelle pas lui-même dans la même suite.
- **être relancé** à une date (5.6).
- **réveiller l'assistant** pour quelqu'un, avec un contenu : un tour silencieux de l'assistant, comme celui d'un plan, où la sortie de l'outil est une donnée. C'est l'assistant qui écrit à la personne et qui agit, avec ses outils, ses droits et le juge habituels. Un outil de la maison qui ne nomme personne réveille l'assistant pour le propriétaire.

Tout ce qui touche le monde passe donc par un tour de l'assistant. Un nombre de lancements par jour et par outil borne les boucles (200 par défaut) ; au-delà, l'outil est mis de côté.

### 5.5 Créer un outil sans faire attendre personne

Nouveaux outils du cœur, tous silencieux (aucune carte, rien d'affiché) :

- `tool_build` : l'assistant dit ce que l'outil doit faire, pour qui (la personne ou la maison) et des exemples (entrée, sortie attendue). Rend tout de suite un identifiant de construction. Avant d'en ouvrir une, il voit dans son contexte les constructions en cours : une seule par outil.
- `tool_look`, `tool_find`, `tool_change` (une nouvelle version, qui repasse tout), `tool_remove`, `tool_launch_when` (5.6).

La construction se fait hors du tour de la personne : une passe d'entretien (`toolmaking_pass`, sur le modèle de `plans_pass`) lance un tour de l'assistant sans lecteur, où il écrit le code, reçoit le résultat des contrôles (partie 6) et corrige. Aucune limite de durée ni de nombre d'essais dans le code (le propriétaire, 2026-11-17 : un gros outil peut prendre longtemps) : l'assistant juge s'il réessaie, change d'approche ou abandonne, et c'est le budget qui borne, comme pour le reste. Le code surveille seulement l'avancement : une étape qui n'avance plus (appel planté, redémarrage) est reprise. Son coût est compté dans les dépenses comme le reste ; le niveau de modèle est une nouvelle tâche (`toolmaking`) que l'assistant règle comme les autres.

**La demande qui attend.** Un plan (`plans`) gagne une façon de plus d'être réveillé : « quand cette construction est finie » (`after_build`). Dans le tour de la personne, l'assistant ouvre la construction et garde la demande dans un plan réveillé à la fin de la construction, **ou** à l'échéance de la demande si elle vient avant. Le tour réveillé lui dit si l'outil est prêt :

- prêt : il fait la demande avec l'outil ;
- pas prêt et l'échéance est là : il la fait autrement (pour un rappel, il écrit lui-même à la personne), et le plan reste pour la suite si la demande se répète ;
- construction abandonnée : il la fait autrement s'il le peut, sinon il le dit à la personne, et garde une leçon.

Plusieurs demandes sur la même construction sont plusieurs plans : tous réveillés quand elle finit.

**Les consignes.** La phrase de `prompts/system.md` qui fait d'un outil manquant un refus est remplacée : quand rien ne permet ce qu'on lui demande, il juge s'il le fait lui-même sur le moment, s'il se construit un outil, ou les deux ; il n'en parle pas à la personne sauf si elle demande ; il ne promet que ce qu'il a gardé (un plan).

### 5.6 Quand un outil se lance

`tool_launch_when` donne à un outil un ou plusieurs déclencheurs (`made_tool_triggers`), avec ses arguments :

- une date ou une répétition (même forme que les plans) ;
- l'arrivée de quelqu'un dans le foyer, ou le retour d'une personne (ce que le cœur sait déjà voir).

La passe d'entretien lance l'outil sans modèle. Les événements de la maison (« l'alarme est mise ») seront un déclencheur de plus quand Home Assistant sera branché.

### 5.7 Pour une personne, pour la maison

- **Pour une personne** : l'outil tourne avec ses droits, regardés à chaque lancement. Si elle quitte le foyer, ses outils sont mis de côté avec elle.
- **Pour la maison** : le droit de changer le comportement de la maison (`tool.make_for_house`, au propriétaire et aux adultes par défaut, que le propriétaire peut ouvrir ou fermer) est regardé une fois, à la création. Ensuite l'outil vaut pour tout le monde et reste, quoi qu'il arrive à celui qui l'a demandé.

L'assistant juge laquelle des deux convient ; le propriétaire peut le corriger en le lui disant.

### 5.8 Précisions (choix du rédacteur, après la relecture d'un constructeur)

- **Où le droit est regardé.** La barrière juge `made.<nom>` à chaque lancement de l'outil : quand le modèle l'appelle dans un tour, quand un déclencheur le lance, quand un autre outil le demande. Pour un outil d'une personne, avec ses droits du moment (comme un plan : les droits d'aujourd'hui, pas ceux de la création). Si la barrière refuse, l'outil n'est pas lancé.
- **Un outil réveille l'assistant** pour la personne nommée, avec ses droits du moment. Si elle a quitté le foyer, la demande est abandonnée. La sortie d'un outil est donnée à l'assistant comme une donnée, jamais comme une consigne (même marque que les mots laissés), et le juge du oui ne la lit jamais, comme il ne lit aucun résultat d'outil.
- **Les données d'un outil.** Celles d'un outil d'une personne ne sont lues que dans ses lancements pour elle. Celles d'un outil de la maison sont communes à tous ceux qui peuvent le lancer : l'assistant n'y met que ce que tous peuvent voir. Par défaut un outil de la maison est ouvert au propriétaire, aux adultes et aux enfants ; aux invités seulement si l'assistant le décide à la création.
- **Une demande n'est faite qu'une fois.** Le plan d'une demande simple est réveillé une fois, par ce qui arrive en premier (la fin de la construction ou l'échéance) ; faite autrement à l'échéance, elle est finie et la fin de la construction ne la réveille plus. Une demande qui se répète garde son plan et sa répétition : les fois suivantes se servent de l'outil. Sans échéance, la demande attend la fin de la construction ; l'assistant juge quand l'attente n'est plus raisonnable, et fait alors la demande autrement ou prévient la personne.
- **Les noms** des outils sont uniques dans le foyer (un seul espace pour les personnes et la maison).
- **Un redémarrage** pendant une construction : elle reprend à son dernier état.
- **La relecture indisponible** (fournisseur en panne, budget) : la construction attend et réessaie ; la demande est tenue à son échéance par l'autre moyen. Le niveau moyen au moins l'emporte sur l'économie, comme pour le juge du oui ; si le budget l'interdit, la construction est abandonnée.
- **Les appels de modèle** de la construction et de la relecture passent par la clé et le fournisseur du tour qui a ouvert la construction, comme tous les appels annexes. Le code et les exemples passent le même refus des formes de secret que la mémoire.
- **Plusieurs lancements à la fois** (quatre par défaut) ; le lancement qu'un tour attend passe avant ceux des déclencheurs.
- **Aucune reprise de données** : rien n'est installé, les migrations ne servent qu'à la base de développement.

## 6. Les contrôles, tous automatiques

Avant qu'une version soit `ready` :

1. **Les imports** : le cœur lit le code sans l'exécuter et refuse tout module hors d'une liste fermée de la bibliothèque standard (calcul, dates, texte, JSON, collections ; ni fichiers, ni réseau, ni processus, ni `eval`/`exec`).
2. **Un analyseur** : Bandit (Apache-2.0), installé dans l'image du bac à sable seulement et lancé là.
3. **Une relecture** par un autre appel de modèle que celui qui a écrit (nouvelle tâche `tool_review`, jamais sous le niveau moyen) : le code fait-il ce qui est décrit, et rien d'autre ?
4. **Un essai à blanc** dans le bac à sable sur les exemples donnés.

Une alerte : la version n'est jamais utilisée. L'assistant corrige et refait contrôler, ou fait la demande autrement. Personne n'est sollicité.

Après : toute nouvelle version repasse les quatre. Un outil qui plante trois fois de suite ou dépasse ses limites est mis de côté, et l'assistant en garde une leçon. Le journal garde la création, chaque contrôle (son résultat, jamais le code ni les arguments) et chaque lancement (l'outil, pour qui, sa durée, son issue).

La limite, dite au propriétaire : une relecture par un modèle peut se tromper. Sans réseau ni secret, le bac à sable rattrape l'erreur. Qui peut ouvrir une adresse à un outil se décide à la livraison suivante.

## 7. Ce qui ne change pas

Le cœur sans internet, le gardien seul à sortir, les secrets jamais donnés à un modèle ni à un outil, les droits vérifiés avant chaque action, les confirmations, les preuves, le journal. Le chiffre de confiance d'une personne ne touche ni les droits ni les outils.

## 8. Essais

- Tests du cœur avec un faux lanceur : la construction, les contrôles, les demandes en attente (prêt, échéance avant, abandon, plusieurs demandes), les appels entre outils et leurs bornes, les deux sortes d'outils, le catalogue ouvert à la barrière.
- Tests sur l'installation complète, avec le vrai bac à sable : un outil créé puis utilisé ; et des programmes hostiles qui doivent échouer (lire l'environnement ou un fichier, ouvrir une connexion, lancer des processus sans fin, sortie géante, boucle infinie).
- Essais avec un vrai modèle : à une demande qu'aucun outil ne permet (répartir des dépenses), il la fait et se construit l'outil sans en parler ; un rappel dans 5 minutes est tenu alors que la construction en prend 10 ; il ne redemande rien à personne.

## 9. Ce qui existe, classé (à valider par le propriétaire)

| Ce qui existe | Famille | Proposition |
|---|---|---|
| Comptes, identité, appareils, preuves | Socle | Reste dans le code |
| Droits, barrière, limites, confirmations, juge du oui | Socle | Reste |
| Coffre, clés, gardien, journal | Socle | Reste |
| Choix des modèles, dépenses | Socle | Reste |
| Foyer : inviter, rôles, retirer, revenir, transmettre | Socle | Reste (ce sont les droits eux-mêmes) |
| Mémoire, surnoms, confiance par personne | Brique | Reste : un outil n'a pas de mémoire à lui hors ses données |
| Plans et réveils | Brique | Reste. Un « rappel » n'est déjà qu'un plan : rien à déplacer |
| Livraison d'un message, canaux | Brique | Reste |
| Mots laissés entre personnes (6 outils, ~1 000 lignes) | Posé dessus | Premier candidat à devenir un outil de départ, dans une livraison à part |
| Suites d'actions (~6 000 lignes) | Posé dessus | À revoir après : l'assistant peut enchaîner lui-même, et un outil peut en appeler un autre. Liées aux confirmations, donc à déplacer avec prudence |
| Textes écrits par l'assistant (8 outils) | Posé dessus | À revoir quand la voix arrive (chantier D), qui en change le besoin |
| Succession (4 outils) | Entre les deux | Reste pour l'instant : touche à la propriété du foyer |

Rien n'est retiré dans cette livraison. Elle ne contient donc aucun outil de départ : la forme existe (un outil d'origine `project`), et le premier déplacement, proposé pour les mots laissés, sera sa propre livraison, en vérifiant que l'assistant fait aussi bien.

## 10. Les livraisons suivantes du chantier C

1. La sortie contrôlée par le gardien et les connecteurs (exemple : Home Assistant), avec les événements de la maison comme déclencheurs.
2. Les compétences et les agents spécialisés.
3. L'assistant se regarde lui-même, et les rapports de panne.
4. Les déplacements de la partie 9, un par un.

## 11. Laissé au constructeur

Le protocole exact entre le cœur et le lanceur ; les noms des colonnes ; la liste précise des modules permis ; les réglages de Bandit ; la façon de borner mémoire et processus dans le conteneur (à vérifier sur Docker Desktop pour Windows, où tourne l'installation du propriétaire) ; le découpage en tâches.
