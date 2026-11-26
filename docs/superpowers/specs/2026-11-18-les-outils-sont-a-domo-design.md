# Les outils sont à l'assistant

Date : 2026-11-18. Corrige et étend `2026-11-17-domo-se-cree-un-outil-design.md` (« la première conception ») avant la sortie de la 0.10.0. Branche : `feat/domo-makes-tools`, la même. Là où les deux textes se contredisent, celui-ci l'emporte.

**État : approuvée par le propriétaire le 2026-11-18**, en discussion (ses réponses sont en partie 2 ; les précisions de la partie 5.8 et les choix de la partie 11 lui ont été présentés). Plan de l’étape 1 : `docs/superpowers/plans/2026-11-18-les-outils-sont-a-domo-etape-1.md`.

## 1. Pourquoi

La première conception a été construite et vérifiée. En lisant la liste de ses garde-fous, le propriétaire a vu qu'elle reposait sur un contresens :

- un outil y est rangé « pour une personne » ou « pour la maison », comme s'il appartenait à quelqu'un ;
- un outil n'y fait que calculer : toute action passe par un tour de l'assistant, donc par un appel de modèle.

Or un outil sert à l'assistant. Il est là pour qu'il dépense moins, pour qu'une chose habituelle se fasse sans le réveiller, et pour qu'il atteigne ce que son cœur n'atteint pas.

## 2. Ce que le propriétaire a décidé

Dit le 2026-11-18.

- « Les outils sont un moyen pour l'assistant de grandir, c'est à l'assistant qu'ils sont utiles, pour consommer moins de tokens, effectuer des actions répétitives sans devoir se réveiller. »
- « Ces outils doivent pouvoir agir sur le monde, cependant une règle doit rester : lorsqu'il y a une interaction avec une personne, là c'est l'assistant qui la fait, jamais l'outil. »
- « Tout outil est bon à prendre et à améliorer, l'assistant doit pouvoir se créer un écosystème à travers ses outils, une vraie application s'il en a besoin avec des bases de données. »
- Deux exemples à réussir : commander tout seul les pizzas préférées quand on le lui demande souvent (un mot de passe est donc nécessaire), puis dire où en est la commande ; contrôler un appareil du réseau que le cœur n'atteint pas.
- « Les possibilités doivent être infinies mais sécurisées. »
- Les droits : ceux de la personne quand l'action est pour elle ; des droits propres à l'assistant quand il agit pour la maison de sa propre initiative.
- « Tout ce qu'un outil fait, l'assistant devrait techniquement aussi pouvoir le faire sans cet outil (mais en consommant plus). » La première fois, il le fait lui-même et répond tout de suite, puis se construit l'outil pour la fois suivante.
- L'assistant prend des initiatives (suivre une commande et prévenir quand elle part) ; « c'est ensuite aux personnes concernées de dire si ça leur va ou s'ils ne veulent plus que l'assistant fasse ça ».
- Approche A (partie 4), avec un navigateur ajouté ensuite comme un moyen de plus.

Ce qu'il avait décidé le 2026-11-17 et qui reste : l'assistant juge s'il fait un outil et n'en parle pas ; une demande n'est jamais perdue ; les contrôles sont automatiques ; le code tourne dans le bac à sable ; un outil peut en appeler un autre ; l'assistant décide quand un outil se lance ; aucune limite de durée à une construction.

## 3. Buts, non-buts, ce qui serait un échec

**Buts**

- Un outil agit sur le monde sans appel de modèle : aller sur un site, se servir d'un compte, parler à un appareil du réseau, écrire dans une base, faire une action du catalogue.
- L'assistant peut faire lui-même, dans la conversation, tout ce qu'un outil peut faire, avec les mêmes moyens et les mêmes vérifications.
- Les outils partagent une base de données : ce qu'un outil a fait, un autre le retrouve, et l'assistant aussi.
- Un outil n'appartient à personne : c'est l'action qui est faite pour quelqu'un, et jugée avec ses droits.

**Non-buts**

- Installer des paquets Python (une livraison à part ; un outil garde la bibliothèque standard et le module `domo`, 5.2).
- Les compétences, les agents spécialisés, l'assistant qui lit son propre code, les rapports de panne.
- Retirer une fonction existante du cœur.
- Une page pour les outils : tout se demande à l'assistant.

**Ce qui serait un échec, même si ça marche**

- Un outil écrit, parle ou demande quelque chose à une personne.
- Un mot de passe vu par un outil, par un modèle ou écrit dans le journal.
- Une action faite sans passer par les droits, les limites et le journal.
- Un outil qui atteint les services de l'assistant lui-même (le cœur, sa base, le gardien, le coffre).
- L'assistant se réveille pour une chose que l'outil pouvait faire seul.
- L'assistant annonce qu'il crée un outil, ou perd une demande.

## 4. Approches

- **A. Un seul endroit, deux usages** (retenue). Le bac à sable reçoit les moyens d'agir. L'assistant y lance un programme écrit sur le moment ; gardé, c'est un outil. Une seule chose à sécuriser, et ce qu'il fait à la main est ce que l'outil fera.
- **B. Des outils fixes dans le cœur pour le « à la main ».** Plus rapide pour les cas prévus, mais deux chemins à sécuriser, et l'assistant est bloqué hors de ces cas.
- **C. Un navigateur que l'assistant conduit.** Marche sur les sites compliqués, mais lourd, fragile, et inutile pour un appareil ou une base.

Le propriétaire a choisi A, et le navigateur de C plus tard comme un moyen de plus (étape 3).

## 5. Ce qui est construit

### 5.1 Un outil est à l'assistant ; une action est pour quelqu'un

Un outil (`made_tools`) n'a plus de « pour qui ». Tout outil prêt est à l'assistant et peut servir dans n'importe quel tour.

Chaque lancement, lui, est fait **pour quelqu'un**, et c'est ce « pour qui » que la barrière juge, à chaque action :

- **pour une personne** : elle l'a demandé dans son tour, ou l'assistant a programmé l'outil pour elle. Ses droits du moment s'appliquent, ses limites, ses comptes, son coin de la base.
- **pour la maison** : l'assistant agit de sa propre initiative, pour tout le foyer. Ce sont les **droits propres à l'assistant** qui s'appliquent.

**Les droits propres à l'assistant.** L'assistant devient quelqu'un pour les droits. Le propriétaire, le 2026-11-18 : « L'assistant doit pouvoir tout faire, c'est à son jugement de voir, en fonction de sa connaissance de la maison, des personnes qui l'entourent, de juger si c'est la bonne chose à faire. » Au départ il a donc tous les droits, et c'est lui qui juge. Deux choses restent hors de sa portée, parce qu'elles sont le socle : changer les droits et ce qui est réservé au propriétaire (transmettre le foyer, les règles du propriétaire), et passer outre l'accord d'une autre personne, une preuve, un secret ou une limite de dépense. Le propriétaire peut lui retirer un droit avec les mêmes mots que pour une personne (« L'assistant ne commande rien tout seul »).

Conséquences sur ce qui est construit :

- le droit de « faire un outil pour la maison » disparaît, ainsi que la mise de côté des outils d'une personne qui s'en va : les outils restent. Ce qui part avec elle : les lancements programmés pour elle, son coin de la base, ses comptes.
- construire un outil est un acte de l'assistant : aucun droit d'une personne n'est regardé pour cela. Le coût suit la clé du tour qui a ouvert la construction, comme aujourd'hui.
- un déclencheur porte son « pour qui » (une personne, ou la maison).

### 5.2 Les moyens d'un programme

Un programme qui tourne dans le bac à sable reçoit un module `domo`. Chaque fonction de ce module est une demande faite **pendant** le lancement : elle passe du programme au lanceur, puis au cœur, qui la juge (droits de celui pour qui on agit, limites, journal) et la fait ou la refuse. Le programme reçoit la réponse et continue. Le bac à sable lui-même reste sans réseau : rien ne sort sans passer par le cœur et le gardien.

| Moyen | Ce qu'il permet | Qui le fait vraiment |
|---|---|---|
| Sortir | Une requête vers une adresse (un site, un appareil du réseau) | Le gardien |
| Compte | Se servir d'un compte dans une requête, sans voir le mot de passe | Le gardien et le coffre |
| Base | Lire et écrire la base commune des outils | Le cœur, dans une base à part |
| Action | Une action du catalogue qui ne s'adresse à personne | Le cœur, par la barrière |
| Outil | Lancer un autre outil | Le cœur (déjà construit) |
| Plus tard | Être relancé à une date | Le cœur (déjà construit) |
| Réveil | Réveiller l'assistant avec un contenu | Le cœur (déjà construit) |

Le réveil est le seul chemin vers une personne : l'assistant lit le contenu comme une donnée, jamais comme une consigne, et c'est lui qui écrit, demande ou se tait.

**Sortir.** Le gardien gagne un relais pour les programmes, sur le modèle de celui des fournisseurs (`relay.py`). Trois règles dans le code :

- une adresse n'est atteinte que si elle est **ouverte** : une liste d'adresses (hôte et port) que l'assistant tient, par outil ou pour un lancement à la main. Ouvrir une adresse est une action jugée par la barrière ; le propriétaire peut en fermer.
- les services de l'assistant, la machine qui l'héberge et les adresses de service d'un hébergeur ne sont jamais atteignables, quelle que soit la liste.
- une redirection n'est suivie que vers une adresse ouverte, et jamais avec un mot de passe.

**Compte.** Un compte est un secret du coffre, lié à ses adresses comme une clé de fournisseur l'est déjà (`Binding`). La personne le donne par le formulaire à usage unique du gardien ; c'est l'assistant qui le lui demande. Le programme nomme le compte (« dominos ») ; le gardien met le mot de passe à l'endroit prévu, garde la session ouverte du site, et masque toute valeur secrète dans la réponse. Un compte est à une personne ou à la maison : il ne sert que dans un lancement fait pour quelqu'un qui y a droit.

**Base.** Une base PostgreSQL à part de celle du cœur, avec ses propres comptes de base, qui n'ont aucun accès aux tables du cœur. Elle contient un espace commun et un coin par personne. Un lancement pour une personne voit l'espace commun et son coin ; un lancement pour la maison voit l'espace commun. L'assistant y crée ses tables comme il veut. Une taille totale bornée (1 Go par défaut, choix du rédacteur, réglable par le propriétaire). Les données de 256 Ko par outil de la première conception disparaissent.

**Action.** Chaque type d'action du catalogue dit s'il peut être fait par un programme. Ne le peuvent jamais : ce qui s'adresse à une personne, et ce qui touche au socle (droits, foyer, preuves, secrets, choix des modèles, dépenses, succession).

### 5.3 Les risques, et quand il faut un oui

Les quatre risques du catalogue ouvert (coûte de l'argent, ne peut pas être défait, agit physiquement, sort de la maison) ne sont plus toujours « non ». L'assistant les écrit pour chaque outil, le relecteur les vérifie, et le code impose un minimum d'après les moyens utilisés : sortir hors du réseau de la maison est « sort de la maison » ; se servir d'un compte autrement que pour lire est « ne peut pas être défait ».

Un programme ne juge rien. Pour chaque demande, la barrière répond sans modèle :

- **permis** : c'est fait, et noté au journal ;
- **refusé** : le programme reçoit un refus ;
- **il faut un oui** : rien n'est fait. Le programme s'arrête là, et l'assistant est réveillé pour la personne avec ce qui devait être fait. Il demande, juge la réponse comme d'habitude, puis relance.

Pour qu'une chose habituelle se fasse sans réveil, une personne peut donner un **accord qui dure** : « commande sans me demander le vendredi, jusqu'à 40 € ». L'assistant le garde comme un droit, lié à l'outil, à la personne et à ses bornes ; la barrière le lit sans modèle. La personne le retire quand elle veut.

Les limites de dépense de chacun s'appliquent à ce qu'un outil dépense pour lui.

### 5.4 À la main d'abord, l'outil ensuite

Un nouvel outil du cœur, silencieux : l'assistant donne un programme et ses arguments, et il tourne tout de suite dans le bac à sable, avec les mêmes moyens, pour la personne du tour. Chaque demande passe la barrière comme celles d'un outil.

Avant de tourner, ce programme passe les deux contrôles rapides (les imports, l'analyseur). Il ne passe ni la relecture ni l'essai à blanc : l'assistant vient de l'écrire pour une seule fois, et la barrière juge chacune de ses actions. Un programme gardé comme outil passe les quatre.

Le déroulé de l'exemple : on demande où en est la commande ; aucun outil ne le sait. L'assistant écrit le programme, le lance, répond. Dans le même tour il ouvre la construction de l'outil, avec ce programme comme point de départ. La fois suivante, l'outil répond sans modèle.

Les consignes (`prompts/system.md`) le disent : quand rien ne permet la demande, il la fait à la main si la personne attend, et se construit l'outil si la demande reviendra ; en construisant un outil qui agit, il prévoit ce qui va avec (suivre, annuler, changer).

### 5.5 Les initiatives

L'assistant peut programmer un outil de lui-même, pour une personne (suivre sa commande) ou pour la maison. La première fois qu'une initiative touche quelqu'un, il le lui dit s'il juge qu'elle ne le sait pas. Quand une personne concernée dit qu'elle n'en veut plus, il arrête, annule ce qui était programmé pour elle et le retient.

### 5.6 Les bornes revues

- **Construction** : le plafond de 25 essais est retiré, comme le propriétaire l'avait demandé ; le budget borne. Une construction qui ne peut être ni écrite ni contrôlée pendant 24 heures reste abandonnée.
- **Lancements par jour** : l'assistant fixe le nombre par outil (200 par défaut) ; au-delà, l'outil est mis de côté.
- **Durée, mémoire, sortie, lancements à la fois, niveaux d'appels entre outils** : inchangés.
- **Requêtes vers l'extérieur** : un nombre par lancement et une taille de réponse bornés (au constructeur).

### 5.7 Le navigateur (étape 3)

Un moyen de plus du module `domo` : ouvrir une page dans un vrai navigateur, cliquer, remplir, lire. Le navigateur tourne dans un conteneur à lui, qui ne sort que par le gardien et ne reçoit un mot de passe que par lui. Sa conception détaillée est écrite au début de l'étape 3, avec ce que les étapes 1 et 2 auront appris.

### 5.8 Précisions (choix du rédacteur, après la relecture d'un constructeur)

- **Ce que la barrière juge.** Chaque demande d'un programme est une action d'un type à elle : sortir, se servir d'un compte, lire ou écrire la base, une action du catalogue. Le lancement d'un outil reste jugé lui aussi (`made.<nom>`), ouvert par défaut à tous les rôles, invités compris ; le propriétaire peut fermer un outil à quelqu'un. La borne « pas plus que le rôle de la personne pour qui il a été créé » disparaît avec le « pour qui ».
- **Les opérations d'un outil.** Un outil nomme ses opérations (« commander », « suivre ») et dit les risques de chacune ; chaque demande dit à quelle opération elle appartient. Un programme lancé à la main fait de même dans son appel. Les risques d'une demande sont ceux de son opération, jamais moins que le minimum du code (5.3). Suivre une commande ne demande donc pas ce que commander demande.
- **Quand il faut un oui, sans modèle.** Trois cas, lus dans le code : l'opération coûte de l'argent et aucun accord qui dure ne la couvre ; une règle du propriétaire dit de toujours demander pour ce type ; la barrière demande l'accord d'un adulte ou du propriétaire, comme aujourd'hui. Les trois autres risques ne demandent pas de oui par eux-mêmes : l'assistant en a jugé en lançant l'outil.
- **Pour la maison, c'est l'assistant qui juge.** Un outil ne juge toujours rien. Quand un lancement pour la maison arrive à une opération qui coûte et qu'aucun accord qui dure ne la couvre, il s'arrête et l'assistant est réveillé, seul. Il juge : il relance lui-même, il demande à quelqu'un, ou il ne fait rien. Pour une chose habituelle, il pose lui-même l'accord qui dure, avec ses bornes, au moment où il programme l'outil. Aucun oui d'une personne n'est imposé par le code.
- **Le oui et la relance.** Un oui vaut pour un lancement : cet outil, cette opération, cette personne, ces arguments. Il est utilisé une fois, par la relance, qui repart du début. Ce qui a été fait avant l'arrêt est donc refait : le relecteur vérifie qu'un outil demande ce qui exige un oui avant de changer quoi que ce soit, et qu'il retrouve dans la base ce qu'il a déjà fait.
- **Le montant.** C'est le programme qui dit le montant d'une opération qui coûte. Le code ne peut pas le vérifier sur le site. Un accord qui dure a donc deux bornes : un montant (déclaré) et un nombre de fois par période (compté par le code, sûr). Les dépenses déclarées sont tenues par personne, à part des dépenses de modèle, et les limites existantes (`policy/limits.py`) lisent ce montant comme un fait.
- **Les droits propres à l'assistant** valent pour tout ce qu'il fait sans personne : un lancement pour la maison, et un tour où il s'est réveillé seul. L'assistant est un sujet à part dans les droits, pas une personne du foyer. Les types du catalogue du code reçoivent les quatre risques, en même temps que la marque « faisable par un programme ».
- **Qui est réveillé.** Pour une personne : elle. Pour la maison : le propriétaire. L'accord d'un adulte ou du propriétaire suit le chemin qui existe.
- **Les comptes.** Celui qui donne un compte ouvre lui-même le formulaire. Un compte de la maison est donné par le propriétaire ou un adulte ; il sert aux lancements pour le propriétaire, les adultes, les enfants et la maison, pas pour un invité sauf si le propriétaire l'ouvre.
- **Ouvrir une adresse** est un acte de l'assistant, jugé avec ses droits propres. Le propriétaire peut fermer une adresse ou lui interdire d'en ouvrir.
- **Une personne qui s'en va** : ses lancements programmés, son coin et ses comptes sont gelés, comme ses droits le sont déjà, et repris si elle revient. Ils sont supprimés quand elle est supprimée.
- **Le réseau de la maison** : les adresses privées, moins les réseaux de l'assistant et sa machine. On suppose que le gardien les atteint depuis Docker Desktop ; c'est vérifié au début de l'étape 2, et si ce n'est pas le cas, la conception du réseau est faite à ce moment.
- **Les plans.** Le plan de l'étape 1 est écrit d'abord ; ceux des étapes 2 et 3 quand on y arrive.

## 6. Ce qui garde tout cela sûr

Ce qui ne change pas : le cœur sans internet, le gardien seul à sortir, les secrets jamais donnés à un modèle ni à un programme, les droits vérifiés avant chaque action, les confirmations, les preuves, le journal (jamais le code, les arguments ni une valeur). Le code d'un modèle ne tourne jamais dans le cœur. Le juge du oui ne lit jamais la sortie d'un programme.

Ce qui s'ajoute :

1. Le bac à sable reste sans réseau : chaque demande d'un programme traverse le cœur et sa barrière.
2. Le canal entre un programme et le lanceur est propre à ce lancement ; un programme ne peut ni parler pour un autre ni changer pour qui il tourne.
3. Les adresses ouvertes, et la liste fixe de ce qui n'est jamais atteignable.
4. Les comptes par référence, liés à leurs adresses, la réponse masquée.
5. La base des outils à part, sans accès aux tables du cœur, avec un coin par personne.
6. Les risques minimaux imposés par le code, et le oui demandé par l'assistant, jamais par l'outil.
7. Les quatre contrôles pour tout outil gardé ; le relecteur voit aussi les adresses, les comptes et les risques déclarés.
8. Les limites de dépense et l'accord qui dure, lus sans modèle.

La limite, dite au propriétaire : un programme qui agit peut se tromper sans mauvaise intention (commander deux fois). Les bornes d'un accord qui dure, les limites de dépense et le nombre de lancements par jour rattrapent l'erreur ; le journal dit ce qui a été fait.

## 7. Ce qui change dans ce qui est construit

| Construit dans la première conception | Devient |
|---|---|
| Le bac à sable, le lanceur, les limites d'un lancement | Reste ; le lanceur gagne le canal des demandes |
| Les quatre contrôles | Restent ; la liste des imports gagne le module `domo` |
| Le catalogue ouvert et ses quatre risques | Reste ; les risques sont enfin utilisés |
| La construction hors du tour, les demandes qui attendent, les réveils | Restent |
| Les déclencheurs, les appels entre outils | Restent ; un déclencheur porte son « pour qui » |
| Outil « pour une personne » ou « pour la maison » | Retiré : un lancement est fait pour quelqu'un |
| Le droit de faire un outil pour la maison, l'interdiction de construire | Retirés |
| Les outils d'une personne qui part mis de côté | Retiré : ses lancements programmés, son coin et ses comptes partent |
| Les demandes rendues à la fin d'un lancement | Remplacées par les demandes pendant le lancement |
| Les données de 256 Ko par outil | Remplacées par la base commune |
| Le plafond de 25 essais | Retiré |

Rien n'est installé : la migration `0053` est corrigée sur place.

## 8. Dans quel ordre

Une branche, une fusion, la version 0.10.0. Trois étapes, chacune avec son plan, chacune laissant un logiciel qui marche et qui passe les essais.

1. **Les outils sont à l'assistant** : le « pour qui » au lancement, les droits propres à l'assistant, le canal des demandes, la base commune, les actions du catalogue, les risques et l'accord qui dure, le programme lancé à la main.
2. **Sortir et les comptes** : le relais du gardien pour les programmes, les adresses ouvertes, les comptes et leurs sessions. Exemple à réussir : un appareil du réseau, et un site qui se pilote par des requêtes.
3. **Le navigateur.** Exemple à réussir : la commande de pizzas et son suivi.

## 9. Essais

- Tests du cœur avec un faux lanceur : chaque moyen permis, refusé, en attente d'un oui ; le « pour qui » (personne, maison) ; l'accord qui dure et ses bornes ; le coin d'une personne invisible aux autres ; le programme à la main puis l'outil.
- Sur l'installation complète : des programmes hostiles qui doivent échouer (atteindre le cœur, sa base, le gardien ; lire un mot de passe dans une réponse ; suivre une redirection vers une adresse fermée ; lire le coin d'un autre ; se faire passer pour un autre lancement), en plus de ceux qui existent.
- Avec un vrai modèle : il répond à la main puis construit l'outil sans en parler ; il demande le oui à la place de l'outil ; il arrête une initiative quand on le lui dit.

## 10. Laissé au constructeur

Le protocole du canal entre programme, lanceur et cœur ; les noms des fonctions du module `domo` ; la forme des comptes de la base des outils et de ses espaces ; la liste exacte des adresses jamais atteignables ; comment le gardien garde une session ; les bornes des requêtes ; le découpage en tâches.

## 11. Les choix du rédacteur, à valider

1. Décidé par le propriétaire le 2026-11-18 : l'assistant a tous les droits pour lui-même et juge ; le rédacteur en exclut le socle (5.1).
2. Construire un outil ne regarde plus le droit de personne.
3. Un programme lancé à la main ne passe que les deux contrôles rapides.
4. La base des outils est bornée à 1 Go.
5. Le plafond de 25 essais est retiré.
6. Les paquets Python attendent une livraison à part.
7. Les trois étapes sortent ensemble, en 0.10.0.
8. Sans accord qui dure, seul ce qui coûte de l'argent demande un oui de lui-même (5.8).
9. Le montant d'une dépense est celui que le programme déclare ; le nombre de fois est compté par le code (5.8).
10. Tout outil est ouvert à tous par défaut, invités compris ; ce sont ses actions qui sont jugées (5.8).
11. Ouvrir une adresse relève des droits propres à l'assistant (5.8).

## 12. Ajout du 2026-11-21 : les bornes sont à l'assistant

Après lecture des décisions prises pendant la construction de l'étape 1, le propriétaire a dit :

- « L'assistant peut laisser une note en son nom. »
- « Pourquoi est-il nécessaire de limiter le nombre d'outils, le but est de les utiliser le plus possible ; même en bornage, n'est-ce pas à l'assistant de juger ça, c'est à lui de les créer. »
- L'assistant doit avoir un maximum de capacités et être autonome, pour le maintien du code de ses outils comme pour le jugement. Il fabrique ses scripts comme il le souhaite ; d'après les résultats et les problèmes (lenteur, réponse différente de celle attendue), il analyse et modifie ses outils. Son but : un jeu d'outils qui lui fait consommer moins de tokens.

Cet ajout l'emporte sur 5.6, sur la ligne « Bornes, exactes » du plan de l'étape 1 et sur les décisions R13, R17, R18, R23 et R24 du constructeur.

### 12.1 Une borne est une valeur de départ, pas une règle du code

Tout ce qui borne ce que font les outils et les programmes de l'assistant devient un **réglage de l'assistant** : une valeur de départ (celle d'aujourd'hui), qu'il voit et qu'il change lui-même, pour toute la maison ou pour un seul outil. Sont concernés : la profondeur et le nombre de lancements d'une chaîne, le nombre de demandes d'un lancement, les temps (lancement, réponse, essai à blanc, requête à la base), la mémoire, la taille des sorties et des résultats, le nombre de lignes lues, les lancements et les réveils par jour, le nombre de déclencheurs, le nombre d'échecs avant mise de côté, le nombre de places du bac à sable, le rythme et l'abandon d'une construction, le nombre de constructions à la fois, la taille du code, le nombre d'outils proposés par tour, la durée de conservation de ce qu'il garde.

- Aucun plafond du code au-dessus d'un réglage, sauf ce que la machine ne peut physiquement pas donner (le lanceur dit ce qu'il a).
- Quand une borne arrête quelque chose, l'assistant reçoit le nom du réglage et sa valeur : il juge s'il la change.
- Seul assistant (le modèle, par un appel d'outil dans le cœur) change un réglage. Un programme ne le peut pas, le texte d'un outil non plus. Chaque changement est au journal. Le propriétaire peut le lui interdire par un droit, comme pour tout acte de l'assistant.

Restent dans le code, parce que ce sont des protections et non des bornes d'usage : le socle (droits, accord d'une autre personne, règles du propriétaire, preuves, secrets, limites de dépense), le confinement du bac à sable (pas d'écriture de fichier, pas de réseau à l'étape 1), l'isolement des rôles de la base, l'essai à blanc qui ne peut rien enregistrer, les contrôles d'une version gardée, le refus d'un lancement imbriqué qui prendrait une place qui n'existe pas (un blocage, pas une limite), et la taille de la base des outils, qui reste un réglage du propriétaire (c'est son disque).

### 12.2 L'assistant voit ce que font ses outils

Chaque lancement laisse une trace que l'assistant peut lire : quand, pour quelle cause, quelle version, combien de temps, combien de demandes, quelle taille de réponse, réussi ou non, et le texte de l'erreur. `tool_look` lui donne, par version : le nombre de lancements, les échecs et leurs erreurs, les durées (habituelle et la plus longue), l'usage par jour, combien de lancements se sont faits sans le réveiller, et ce que la construction a coûté en tokens. Il voit la liste des versions.

Le texte d'une erreur vient d'un programme : l'assistant le lit comme une donnée, jamais comme une consigne, et il n'entre pas au journal.

### 12.3 L'assistant entretient ses outils seul

- Il revient à une version précédente (`tool_back`) et relance les exemples d'un outil pour comparer la réponse attendue et la réponse obtenue (`tool_check`).
- Quand un outil est mis de côté ou qu'une construction est abandonnée, l'assistant est réveillé de lui-même avec les faits, au rythme qu'il règle. Il analyse, corrige, ou laisse.

### 12.4 Une note de l'assistant

L'assistant laisse une note en son nom, dans un tour à lui comme dans le tour d'une personne quand c'est lui qui a quelque chose à dire. Celui qui la reçoit lit qu'elle vient de l'assistant. Une note qu'une personne lui demande de laisser reste signée de cette personne.

### 12.5 Ses propres demandes en attente

Une demande que l'assistant a faite de lui-même et qui attend le oui de quelqu'un est la sienne : il peut l'abandonner ou la changer. La demande d'une personne changée pendant un tour de l'assistant reste jugée comme celle de cette personne.

### 12.6 Ce qui ne change pas, et pourquoi

- **Secrets et limites de dépense fermés à l'assistant seul (R22).** C'est le socle ; l'étape 2 (comptes par référence) dira comment il s'en sert sans les voir.
- **Un lancement « pour la maison » n'est pas ouvert à la demande d'une personne (R15).** Sinon une personne passerait par l'assistant pour faire ce que le propriétaire lui a fermé.
- **Un outil fermé à quelqu'un par le propriétaire le reste (R11).** C'est une règle du propriétaire.

### 12.7 Choix du rédacteur, à valider

- Les réglages sont lus et changés par deux appels, l'un pour la maison, l'autre pour un outil ; le nombre de places du bac à sable se change sans redémarrer.
- « Moins de tokens » se mesure par ce qui est enregistré : le coût de construction d'un outil et le nombre de ses lancements faits sans tour de modèle. Le cœur ne calcule pas une économie ; l'assistant en juge.
- La trace des lancements est gardée 90 jours au départ (réglage de l'assistant).

## 13. Ajout du 2026-11-22 : l'étape 2, « Sortir et les comptes »

**État : à valider par le propriétaire.**

L'étape 1 est fusionnée (0.10.0). Le propriétaire a dit le 2026-11-22 :

- « Je ferai les essais une fois que les outils comme HA etc. pourront être ajoutés à l'assistant, je le mettrai en prod chez moi pour le tester. »
- À la question « tu lui donnes l'adresse et le jeton et il construit ses outils lui-même, ou tu veux des outils déjà prêts ? » : « L'assistant construit tout. »

Cet ajout précise l'étape 2 de la partie 8 ; les parties 5.2, 5.3 et 5.8 restent vraies. Il l'emporte sur la ligne « Les trois étapes sortent ensemble, en 0.10.0 » (parties 8 et 11) et sur la première ligne de 12.6 (R22).

### 13.1 Ce qui doit marcher à la fin

Dans une maison où l'assistant vient d'être installé et ne connaît rien :

1. Le propriétaire dit, avec ses mots, qu'il a Home Assistant à telle adresse.
2. L'assistant ouvre cette adresse et lui demande un jeton : le propriétaire le donne dans le formulaire à usage unique du gardien. L'assistant ne le voit jamais.
3. L'assistant essaie à la main (un programme lancé une fois) : il lit ce que Home Assistant sait faire.
4. Il se construit ses outils (allumer, éteindre, lire un état) et s'en sert ensuite sans tour de modèle : pour une personne, pour la maison, à une heure dite.
5. Le propriétaire ferme une adresse ou retire un compte : plus rien ne sort vers elle, tout de suite.

Le même scénario doit marcher que Home Assistant tourne sur une autre machine de la maison ou sur celle qui héberge l'assistant (13.10).

Rien dans le code ne nomme Home Assistant. Le même chemin sert à un autre appareil du réseau et à un site qui se pilote par des requêtes.

### 13.2 Approches

- **A. Un relais général.** Le programme écrit sa requête (méthode, adresse, en-têtes, corps) ; le gardien la fait si l'adresse est ouverte, et y met le compte nommé. Aucun code par service. **Choisie** : c'est la seule où « L'assistant construit tout » est vrai, et c'est celle de la partie 5.2.
- **B. Un connecteur déclaré par service**, sur le modèle des fournisseurs de modèles (une adresse de base et des chemins fixés à l'avance). Plus étroit, donc plus simple à garder sûr ; mais chaque service nouveau demande une déclaration, et un site qui change casse le connecteur. Écartée : l'assistant ne peut pas s'en servir pour ce qu'on n'a pas prévu.
- **C. Des outils Home Assistant livrés avec l'application.** Marche le premier jour sans rien construire ; mais c'est du code écrit pour un seul service, que l'assistant n'a pas fait et qui vieillit avec Home Assistant. Écartée par le propriétaire (« L'assistant construit tout »).

### 13.3 Sortir

Le module `domo` gagne une fonction : une requête HTTP ou HTTPS vers une adresse, avec sa méthode, ses en-têtes, son corps, et au besoin le nom d'un compte. Le programme reçoit le code de retour, les en-têtes et le corps. C'est le gardien qui fait la requête ; le bac à sable reste sans réseau.

- **Adresse ouverte.** Un hôte et un port, pour un outil ou pour un programme lancé à la main. Ouvrir et fermer sont deux actions de l'assistant, jugées avec ses droits propres et écrites au journal. Le propriétaire ferme une adresse ou interdit à l'assistant d'en ouvrir. Une adresse ouverte pour un outil ne l'est pas pour un autre.
- **Jamais atteignables**, quelle que soit la liste : les services de l'assistant, la machine qui l'héberge, les adresses de service d'un hébergeur. Le gardien résout le nom une fois, juge l'adresse obtenue, et se connecte à cette adresse-là : un nom qui change de cible entre le contrôle et la connexion n'ouvre rien.
- **Redirection** : suivie seulement vers une adresse ouverte, et jamais avec un compte.
- **Sans chiffrement (http)** : un compte ne part en clair que vers une adresse privée de la maison. La règle se lit sur l'adresse obtenue après résolution du nom, pas sur la forme du nom : `http://192.168.x.x:8123` et `http://homeassistant.local:8123` sont donc tous deux utilisables si le nom mène dans la maison. Vers internet, un compte demande https. (Aujourd'hui `cleartext_allowed` juge la forme du nom et laisse passer la machine et les services de l'assistant : pour les programmes, c'est la règle « jamais atteignables » qui passe d'abord.)
- **Bornes** : le temps d'une requête, la taille de la réponse, le nombre de requêtes d'un lancement sont des réglages de l'assistant (partie 12), avec un plafond du code pour la taille, comme pour une réponse d'outil (8 Mo).
- **Ce qui revient** est du contenu extérieur : jamais lu comme une consigne, par le programme comme par l'assistant.
- **Au journal** : l'hôte, le port, la méthode, le code de retour, la taille, l'outil, l'opération, pour qui. Jamais le chemin, les en-têtes, le corps.

Le réseau de la maison est atteint depuis Docker Desktop : vérifié le 2026-11-22 (un conteneur joint la box). Un nom en `.local` peut ne pas se résoudre depuis un conteneur ; l'assistant se sert alors de l'adresse en chiffres.

### 13.4 Les comptes

Un compte est un secret du coffre, lié à ses adresses et à l'endroit où il se pose dans la requête. Le lien ne change jamais après coup, comme aujourd'hui.

- **Trois façons de le poser** : dans un en-tête (un jeton, « Bearer … »), en identifiant et mot de passe d'en-tête (Basic), ou dans un champ du corps d'une requête de connexion. Dans ce dernier cas le gardien garde les cookies que le site rend, et les remet aux requêtes suivantes faites avec ce compte : c'est la session.
- **Qui le donne** : la personne, dans le formulaire à usage unique, quand l'assistant le lui demande. L'assistant décrit le compte (son nom, ses adresses, où il se pose) ; la personne voit cette description avant de taper.
- **À qui il est** : à une personne ou à la maison (5.8). Il ne sert que dans un lancement fait pour quelqu'un qui y a droit.
- **Jamais vu** : le programme nomme le compte, rien de plus. Le gardien masque la valeur du secret et les cookies de session dans tout ce qui revient, erreurs comprises.
- **Retirer** : celui qui l'a donné, ou le propriétaire. Ce qui s'en servait reçoit un refus. Le coffre ne sait pas effacer aujourd'hui : c'est le cœur qui refuse dès le retrait, comme pour une clé de fournisseur retirée. Un compte donné de nouveau avec d'autres adresses est un autre compte.

### 13.5 L'assistant seul et les comptes (R22 revu)

L'assistant ne voit toujours aucun secret, et rien ne lui ouvre le coffre. Ce qui change : **un compte de la maison sert aussi aux lancements que l'assistant fait pour la maison**, seul, sans qu'une personne l'ait demandé. Sans cela il ne pourrait ni allumer une lumière de lui-même ni suivre l'état de la maison.

- C'est ouvert au départ ; le propriétaire peut le fermer, pour un compte ou pour tous.
- Seul, l'assistant ne se sert jamais du compte d'une personne.
- Seul, il peut demander un compte à quelqu'un (ouvrir le formulaire pour lui) ; il ne peut ni en créer, ni en remplacer, ni en retirer un.
- Les limites de dépense restent du socle. Une opération qui coûte, pour la maison, suit 5.8 : un accord qui dure, ou l'assistant est réveillé et juge.

### 13.6 Les risques que le code impose

Comme en 5.3 : sortir hors du réseau de la maison est « sort de la maison » ; se servir d'un compte dans une requête qui n'est pas une lecture (autre que GET ou HEAD) est « ne peut pas être défait ». L'assistant écrit les autres pour chaque opération, et le relecteur les vérifie.

Dans un essai à blanc, rien ne sort : la demande de sortie est vérifiée dans sa forme et rend une réponse vide, comme une action du catalogue à l'étape 1 (et non comme une écriture en base, qui est faite puis défaite). Un outil qui lit la réponse d'un service prévoit donc le cas de l'essai (`domo.dry_run`) ; le relecteur le vérifie.

### 13.7 Ce qui n'est pas dans cette étape

- **Écouter.** Un outil demande et reçoit une réponse ; il ne reste pas à l'écoute d'un flux d'événements, et rien de l'extérieur ne l'appelle. Pour savoir ce qui a changé dans la maison, l'assistant regarde à intervalles (un outil relancé à une heure dite). Ce que ça coûte : il apprend un changement au prochain passage, pas à la seconde. Écouter viendra avec le chantier E (la maison).
- Un jeton rendu dans le corps d'une réponse de connexion (et non dans un cookie) : pas gardé par le gardien dans cette étape.
- Le navigateur (étape 3), les paquets Python, la recherche d'appareils sur le réseau, des outils livrés tout faits.

### 13.8 Essais et version

Une branche, une fusion, la version 0.11.0. Sur la vraie pile, deux faux services : un appareil du réseau à jeton, en http ; un site à connexion par formulaire et cookie, en https. Le scénario de 13.1 est joué en entier, une fois vers un autre hôte et une fois vers un port de la machine qui héberge, et aussi : un port de la machine que le propriétaire n'a pas ouvert, un service de l'assistant lui-même, une adresse fermée, un compte retiré, une redirection vers une adresse fermée, un nom qui change de cible, un secret qu'un service renvoie dans sa réponse.

### 13.9 Choix du rédacteur, à valider

1. Un compte de la maison sert à l'assistant seul, ouvert au départ (13.5).
2. Pas d'écoute d'événements dans cette étape : l'assistant regarde à intervalles (13.7).
3. La session d'un site est gardée par cookies seulement (13.4, 13.7).
4. Une adresse est ouverte pour un outil, pas pour tous (13.3).
5. Le journal ne garde ni le chemin ni le corps d'une requête (13.3).
6. Cette étape sort seule, en 0.11.0 ; le navigateur suivra en 0.12.0.

### 13.10 Précisions après la relecture d'un constructeur (choix du rédacteur)

Ce que la relecture a montré du code d'aujourd'hui, et ce qui en est décidé :

- **Le coffre ne sait poser un secret que dans un en-tête**, et un secret est une seule valeur, donnée dans un formulaire à un seul champ. L'identifiant avec mot de passe et le champ d'un corps de connexion demandent une nouvelle forme de secret (plusieurs valeurs), un formulaire à deux champs et ses textes, et le masquage de chaque valeur séparément. C'est du travail de cette étape.
- **Suivre une redirection et se connecter à l'adresse résolue** n'existent pas : le relais des fournisseurs ne suit aucune redirection et laisse la bibliothèque résoudre. Les deux sont à écrire, pour le relais des programmes seulement.
- **Sortir et se servir d'un compte sont jugés par les droits**, à la différence des autres demandes d'un programme (ouvertes à tous à l'étape 1) : un compte de la maison est fermé à un invité sauf si le propriétaire l'ouvre (5.8). Donner et retirer un compte restent fermés à l'assistant seul ; s'en servir et demander un formulaire ne le sont pas.
- **La liste des adresses ouvertes est tenue par le cœur**, qui l'envoie au gardien avec chaque requête : fermer une adresse vaut donc dès la requête suivante. Une adresse est liée au nom de l'outil et survit à ses versions ; pour un programme lancé à la main, elle est ouverte sans outil.
- **Fermer une adresse ou retirer un compte, quand le propriétaire le demande, est une règle du propriétaire** : le code l'applique, l'assistant n'en juge pas.
  - *Changé le 2026-11-23 pour la moitié des comptes (conception « Domo juge d'abord », partie 14, réponse 3 du propriétaire ; et sa décision D8 du 2026-11-24)* : un compte retiré par le propriétaire peut être redemandé quand l'assistant juge que les choses ont changé (« retiré par le propriétaire, tel jour » est dans ce qu'il pèse ; pour un compte de la maison, le oui demandé est celui du propriétaire), et l'assistant peut retirer seul un compte qui ne sert plus ; une interdiction écrite exprès passe avant, et partager un compte de la maison reste au propriétaire.
- **Ouvrir une adresse vers internet est le jugement de l'assistant**, comme le reste. C'est le seul chemin par lequel une donnée de la maison peut sortir : le propriétaire peut poser la règle « demande-moi d'abord » sur ce type d'action, et l'ouverture est toujours écrite au journal.
- **Ce qu'un programme met lui-même dans ses en-têtes** : les en-têtes d'identification (Authorization, Cookie, clé d'API) sont retirés ; un secret ne passe que par un compte.
- **Les cookies d'une session** sont gardés par le gardien en mémoire, par compte, et oubliés après un temps sans usage ou au redémarrage ; l'outil se reconnecte quand le site le lui refuse. Ils ne reviennent jamais au programme.
- **Au journal** : une ligne avant la requête (ce que la barrière a jugé), une ligne après (code de retour, taille, durée).
- **Les outils de l'assistant pour tout cela** (ouvrir et fermer une adresse, demander un compte, voir les comptes et les adresses) sont à ajouter ; leur forme est laissée au constructeur.
- **Un service sur la machine qui héberge l'assistant.** Le propriétaire a dit le 2026-11-22 : « Les deux doivent être possibles, on ne connaît pas comment les utilisateurs feront leurs installations. » La machine reste fermée au départ, mais un port précis peut en être ouvert :
  - Ouvrir un port de la machine qui l'héberge est un droit à part de celui d'ouvrir les autres adresses. Au départ, c'est le propriétaire qui dit oui, port par port, quand l'assistant le demande. Le propriétaire peut donner ce droit à l'assistant (« Un propriétaire pourrait accepter que l'assistant touche aux ports de lui-même », 2026-11-22) : l'assistant ouvre alors seul les ports de la machine, selon son jugement, et chaque ouverture est au journal. Le propriétaire le reprend quand il veut.
  - Les services de l'assistant lui-même (le cœur, sa base, le gardien, le bac à sable) ne sont jamais ouvrables, même par le propriétaire : un outil ne parle pas au cœur par le réseau.
  - Un port ouvert de la machine vaut comme une adresse de la maison (un compte peut y partir en http).
  - Le code ne suppose pas Docker : il sait atteindre « la machine qui m'héberge » que l'assistant tourne dans des conteneurs ou comme une application sur un serveur. Dans des conteneurs, c'est l'adresse de l'hôte vue du gardien ; comme application, c'est la machine elle-même (`localhost`).
