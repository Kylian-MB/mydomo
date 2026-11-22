# Actions enchaînées, départ du propriétaire et liste des successeurs

Conception du 1er novembre 2026, décidée avec le propriétaire en conversation, après la fusion du 1b (version 0.2.0). Elle s'appuie sur `docs/superpowers/specs/2026-10-28-domo-architecture-design.md` (l'architecture) et `docs/superpowers/specs/2026-10-31-droits-retour-mots-design.md` (droits, limites, barrière).

Ce qui vient du propriétaire est marqué *(propriétaire)*. Ce que j'ai proposé et qu'il a accepté est marqué *(proposé, accepté)*. Ce que je décide seul est marqué *(mon choix)* et repris au §12.

## 1. Intention

Le propriétaire veut pouvoir dire en une phrase « je pars, donne mon accès à Justine et retire-moi du système », et que l'assistant le fasse. Aujourd'hui, ça ne marche pas pour trois raisons :
- chaque action est jugée seule, au moment du oui, avec les droits de ce moment ;
- une fois Justine propriétaire, l'ancien propriétaire est adulte, et un adulte ne peut retirer personne ;
- aucune action ne transmet le rôle de propriétaire (le socle du 1b dit que ce rôle n'est jamais donné).

Le propriétaire veut aussi préparer sa succession : une liste de successeurs, dans l'ordre, que l'assistant tient à jour avec tact. La succession après un décès (réclamer le rôle, délai, avertissements, preuve) viendra plus tard, quand l'assistant pourra joindre le propriétaire ailleurs que sur sa page web.

Il veut enfin que l'assistant sache tout de suite ce que donne un oui ou un non donné par un bouton, et juge s'il doit en parler *(propriétaire)*.

Fil conducteur, comme dans tout l'assistant : le code garde le socle de sécurité, et l'assistant décide du reste (mémoire « autonomie plutôt que règles figées »).

## 2. Objectifs, non-objectifs, anti-objectifs

**Objectifs**
1. Une phrase à plusieurs actions qui dépendent les unes des autres devient une **suite** : une carte, un oui, des étapes faites dans l'ordre.
2. Une suite garde les droits que la personne avait quand elle a demandé, pour ce que la suite elle-même lui a retiré *(propriétaire)*.
3. Quand une étape échoue, l'assistant décide de ce qui est le mieux *(propriétaire)*.
4. Après tout oui ou non donné par un bouton, l'assistant est réveillé et juge s'il répond *(propriétaire)*.
5. Le propriétaire transmet son rôle à un adulte du foyer, sans l'accord de celui-ci *(propriétaire)*.
6. Le propriétaire tient une liste ordonnée de successeurs, du foyer ou extérieurs *(propriétaire)*.

**Non-objectifs (plus tard)**
- La succession après un décès ou une disparition : réclamer le rôle, délai avec avertissements, preuve jugée par l'assistant, code de secours sur papier, commande sur le serveur.
- Les accès des successeurs extérieurs (accès personnel « réclamer seulement », code papier). Ici, l'assistant propose seulement la façon de faire et la note *(proposé, accepté)*.
- Transmettre le rôle à quelqu'un qui n'est pas encore dans le foyer. Il faut d'abord l'inviter comme adulte, puis transmettre une fois qu'il est là *(proposé, accepté)*.
- Une étape qui attend un événement (« quand il arrive, donne-lui mon rôle »).
- L'assistant qui écrit en premier dans d'autres cas que le réveil après un bouton.
- La voix et la présence, qui diront plus tard à l'assistant si quelqu'un était dans la pièce.

**Anti-objectifs (ce qui serait un échec même si ça marche)**
- Une suite qui permet quelque chose que la personne ne pouvait pas faire quand elle a demandé.
- Deux propriétaires, ou aucun, même un instant.
- Le rôle de propriétaire qui passe par un autre chemin que l'action de transmission.
- La liste des successeurs vue par quelqu'un d'autre que le propriétaire (ou qu'une étape d'une suite qu'il a approuvée quand il l'était).
- Un réveil de l'assistant qui en déclenche un autre, en boucle.

## 3. Approches (pour les actions enchaînées)

- **A. L'assistant déclare la suite, le code la fait.** C'est l'approche choisie par le propriétaire. L'assistant reçoit un outil pour faire des étapes dans l'ordre. Le code montre une carte, demande un oui, fait les étapes en passant chacune par la barrière, applique la règle des droits de la suite, et réveille l'assistant à un échec. L'assistant juge seul quelles actions dépendent les unes des autres.
- **B. L'assistant enchaîne lui-même, sans suite déclarée.** L'ancien propriétaire garderait ses droits un court moment après le transfert. Écartée : des droits ouverts sans limite nette, une carte et un oui par étape, et un ordre non garanti.
- **C. Un outil spécial « je pars ».** Écartée : il ne résout que l'exemple, alors que le propriétaire veut que toute phrase à plusieurs actions marche.

## 4. Les suites d'actions

### 4.1 Ce que l'assistant fait

L'assistant a un nouvel outil (nom au constructeur, par exemple `do_in_order`). Il y met de 2 à 8 étapes, chacune étant un de ses outils et ses arguments, comme s'il l'appelait lui-même. Il ne s'en sert que quand les étapes dépendent les unes des autres. Pour des actions indépendantes (« invite Léa et laisse un mot à Alex »), il fait des appels séparés, comme aujourd'hui *(proposé, accepté)*.

L'outil n'offre que les outils que la personne voit déjà (`tools_for`). Il n'accepte ni une suite dans une suite, ni un outil de gestion des demandes (`confirmation_*`). 8 est le nombre d'appels par réponse du 1b (`MAX_CALLS_PER_ANSWER`).

### 4.2 Au moment de la demande

- Chaque étape est décrite par son outil (`describe`), pour la personne, avec les faits du moment.
  - **Exception :** une étape peut échouer à se décrire *seulement parce qu'une étape d'avant change ce qu'elle touche*. C'est le cas de « retire-moi » quand on est encore propriétaire : l'outil refuse toujours de retirer le propriétaire. Une telle étape entre dans la suite comme « vérifiée à son tour ». Comment le code reconnaît ce cas est laissé au constructeur (§11), avec ce garde-fou : l'étape doit agir sur une personne ou une chose qu'une étape d'avant touche aussi. Sinon, la suite entière est refusée et l'assistant est prévenu.
- Chaque étape passe par la barrière (`decide_now`). Une étape refusée refuse toute la suite, avec la raison de l'étape. L'assistant la reformule et peut proposer une autre suite.
- Si toutes les étapes sont « libres », la suite se fait tout de suite, dans le tour de l'assistant.
- Sinon, une seule demande (une ligne `confirmations` ou une table liée : au constructeur) porte toutes les étapes :
  - **son niveau** est le plus strict des étapes, et `always_confirm` dès qu'une étape est « vérifiée à son tour » *(mon choix)* ;
  - **son approbateur** est le plus exigeant des étapes : le propriétaire, puis un adulte, puis la personne elle-même ;
  - **le style du oui** est le plus strict : il suffit qu'une étape demande un oui explicite.
- **La carte** montre les étapes dans l'ordre, chacune en une phrase (`describe_for_people`). Une étape « vérifiée à son tour » le dit.

### 4.3 Après le oui

Les étapes se font une par une, dans l'ordre, chacune comme un appel permis aujourd'hui (`execute_allowed`), dans le contexte de la personne qui a demandé.

Avant chaque étape :
1. Elle est décrite à nouveau, avec les faits du moment (comme la décision R6 du 1b). Une étape qui ne décrit plus la même action, ou plus la même cible, est arrêtée : « plus possible ».
2. Elle passe par la barrière, pour la personne, avec **la règle des droits de la suite** :
   - si le rôle de la personne ou ses droits personnels ont changé depuis la demande **seulement à cause d'étapes de cette suite**, la barrière la juge avec le rôle et les droits qu'elle avait à la demande *(propriétaire)* ;
   - cela vaut pour **toutes** les étapes, même celles réservées au propriétaire (les règles, les droits, les réglages, les successeurs). Tout ce que le propriétaire a demandé en tant que propriétaire passe, même après la transmission *(propriétaire, choix (a) du 1er novembre)*. La seule exception est la transmission elle-même : seul le propriétaire *actuel* peut la faire (§6) ;
   - ce rôle d'avant ne sert qu'aux étapes fixées et approuvées dans la demande, tant que la suite vit (le délai de vie de sa demande). Une suite relancée après un arrêt refait ces mêmes étapes et rien d'autre *(mon choix)* ;
   - si **quelqu'un d'autre** les a changés entre-temps, ce sont ceux du moment qui comptent *(proposé, accepté)*. Par exemple, si la nouvelle propriétaire retire l'ancien entre-temps, la suite s'arrête.
3. Les faits (et donc les refus fixes des outils) sont toujours ceux du moment. C'est ce qui garde le socle : un ancien propriétaire ne peut pas retoucher au rôle du nouveau, puisque l'outil refuse toujours de toucher au rôle du propriétaire.
4. Le oui de la suite couvre chaque étape : on ne redemande pas de oui en route. Si la barrière demande un approbateur plus exigeant que celui qui a dit oui (un droit changé entre-temps), l'étape s'arrête et la suite attend le oui de la bonne personne, comme la décision R27 du 1b *(mon choix)*.

Les limites des droits lisent l'historique, et une suite compte chacune de ses étapes faites, comme une action faite.

### 4.4 Quand une étape échoue

La suite s'arrête, ce qui est fait reste fait, et l'assistant est réveillé dans la conversation de la personne qui a demandé (§5), avec le point exact : ce qui est fait, ce qui a échoué et pourquoi, ce qui reste. Il choisit ce qui est le mieux *(propriétaire)* :
- **faire quand même les étapes qui restent :** un outil les relance sous le même oui, chacune repassant par la barrière ;
- **les abandonner ;**
- **défaire ce qui est fait :** ce sont de nouvelles actions, qui passent par la barrière et demandent leur propre oui.

Le rôle de propriétaire ne se défait pas ainsi *(mon choix)* : l'ancien propriétaire, devenu adulte, ne peut pas le reprendre. L'assistant lui dit de demander au nouveau propriétaire, qui peut le lui rendre par la transmission ordinaire (§6). Il n'y a pas d'autre chemin pour le rôle (§8.1).

Une suite arrêtée garde le délai de vie de sa demande : passé ce délai, elle expire comme une demande.

## 5. Réveiller l'assistant après un bouton

- **Quand :** après chaque oui ou non donné par un bouton, sur n'importe quelle demande, suite ou pas *(propriétaire)*. Aussi quand une suite s'arrête sur un échec.
- **Un oui ou un non dit en mots.** Aujourd'hui, le code y répond par une phrase toute faite du catalogue, sans appeler le modèle (`_read_the_answer` dans `chat/routes.py`). Désormais, c'est l'assistant qui répond lui-même, après avoir vu ce que la décision a donné *(propriétaire)*. Comme la personne vient de lui parler, il répond, même brièvement. La phrase toute faite ne sert plus qu'en secours, si le modèle ne répond pas *(mon choix)*. Coût : un appel au modèle par réponse en mots.
- **Où :** dans la conversation de la personne qui a demandé, même si c'est quelqu'un d'autre qui a cliqué (un adulte pour un enfant) *(proposé, accepté)*.
- **Ce que voit l'assistant :** un bloc de données qui dit ce qui vient de se passer (la demande, la décision, qui l'a prise, le résultat). Ce sont des données, pas des consignes. Il a les outils et les droits de la personne qui a demandé, comme dans un tour normal.
- **Il juge s'il répond.** S'il n'a rien à dire, rien ne s'affiche *(propriétaire)*. Le moyen de se taire (une réponse vide, un outil…) est laissé au constructeur.
- **Tout de suite, page ouverte ou non.** Le tour se fait côté serveur dès la décision. Si la page de la personne est ouverte, la réponse y apparaît. Sinon, elle la trouve en ouvrant la conversation. Aujourd'hui, une réponse n'est écrite que quand une page ouvre son flux (`GET /chat/stream/{id}`). Il faut donc un tour qui ne dépend d'aucune page, et le flux ne fait plus que relayer. Le comment est laissé au constructeur.
- **Une à la fois.** La règle du 1b « une seule réponse à la fois par conversation » vaut aussi pour un réveil. Le constructeur décide si un réveil attend la fin d'une réponse en cours, ou s'il est pris dans le tour suivant.
- **Pas de boucle.** Seul un clic d'une personne réveille l'assistant. Ce que l'assistant fait pendant un réveil ne réveille rien, sauf une suite qu'il relance et qui s'arrête de nouveau : chaque réveil vient alors d'un échec réel.
- **Coût :** un court appel au modèle par clic, même quand l'assistant se tait. Plus tard, l'assistant pourra prendre un petit modèle pour ça.

## 6. Le départ : transmettre le rôle de propriétaire

**L'action.** C'est une nouvelle action, `household.transfer_ownership` (outil au nom du constructeur), avec pour argument la personne qui reçoit le rôle.
- Elle est au propriétaire seul : elle va dans `OWNER_ONLY_ACTIONS`, aucun droit ne peut la donner.
- Son niveau est `always_confirm`, avec un **oui explicite toujours** (bouton ou « je confirme ») : un réglage de style ne peut pas l'assouplir *(proposé, accepté ; socle)*.

**Refus fixes, quels que soient les droits** (avec les faits du moment) :
- celui qui demande n'est pas le propriétaire *actuel*. C'est ce qui empêche une suite de transmettre deux fois, même avec la règle des droits de la suite ;
- la personne n'est pas un adulte présent dans le foyer : ni un enfant, ni un invité, ni quelqu'un de parti, ni soi-même *(proposé, accepté)*.

**Pas d'accord du nouveau propriétaire** *(propriétaire)*. Le rôle passe dès le oui de l'ancien.

**En une seule transaction :**
- l'ancien propriétaire devient adulte, puis le nouveau devient propriétaire. L'index `uq_persons_single_owner` n'admet qu'un propriétaire : l'ordre et la transaction unique font qu'il n'y en a jamais deux ni zéro ;
- l'entrée d'audit `household.ownership_transferred` (de qui à qui, par identifiants) est écrite.

**Ce qui suit de soi-même :**
- les demandes qui attendaient le oui du propriétaire attendent celui du nouveau, puisque `may_decide` lit le rôle au moment de décider ;
- les demandes de l'ancien propriétaire suivent les règles du 1b (R27) ;

**Ce que la transmission fait en plus, dans la même transaction, avec son audit** *(mon choix)* :
- **les demandes du nouveau propriétaire qui attendaient le oui du propriétaire** sont renvoyées vers ce que la barrière décide maintenant, c'est-à-dire son propre oui. Sinon elles resteraient bloquées : le propriétaire ne dit pas oui à sa propre demande à la place de quelqu'un d'autre, et il n'y a plus d'autre propriétaire ;
- **les droits personnels du nouveau propriétaire** (posés quand il était adulte) sont mis en pause, comme au retour d'un ancien membre (`rights.paused`). Un propriétaire ne doit pas rester bridé par des limites qu'on lui avait posées. L'assistant le lui dit, et il peut les reprendre lui-même ;
- les réglages, les droits, la clé du modèle et le fuseau horaire restent à la maison ;
- la page de chacun change avec son rôle.

**Le rôle est pour une durée indéterminée** *(propriétaire)*. Il n'a ni date de fin ni expiration, comme celui du premier propriétaire. Il ne change que par une nouvelle transmission.

**Prévenir le nouveau propriétaire.** Une note dans le contexte de l'assistant dit au nouveau propriétaire qu'il l'est devenu, quand et par qui. Cette note, et elle seule, reste 30 jours : c'est le temps pour l'assistant d'y penser *(mon choix)*. Ensuite, le journal d'audit garde la trace du transfert. L'assistant juge s'il faut le dire au nouveau propriétaire, et comment *(propriétaire)*. Aujourd'hui, il ne peut pas savoir si la personne était là au moment du transfert. Plus tard, la voix et la présence le lui diront.

**Après le départ :**
- l'ancien propriétaire, devenu adulte, garde son compte et sa conversation ;
- une suite peut le retirer ensuite (« retire-moi »), grâce à la règle des droits de la suite. Le retrait de soi est déjà permis par l'outil (décision R1/R2 du 1b) ;
- l'ancienne liste des successeurs (§7) devient celle du nouveau propriétaire *(mon choix)*. L'assistant lui en parle au moment qu'il juge bon, pour qu'il la garde ou la change.

## 7. La liste des successeurs

**Ce qu'elle contient :** une liste ordonnée, tenue par le propriétaire seul et vue par lui seul *(proposé, accepté)*. Chaque successeur est :
- **soit une personne du foyer** (par son identifiant) ;
- **soit quelqu'un d'extérieur** : un nom, et ce que le propriétaire veut dire de lui (un lien, comme « mon frère »), sans coordonnées pour l'instant *(propriétaire ; coordonnées : plus tard)*.

**La façon d'ouvrir la porte.** Pour chacun, l'assistant propose les façons de lui ouvrir la porte le jour venu et recommande la meilleure *(propriétaire)* :
- un accès personnel qui ne sert qu'à réclamer le rôle, s'il peut le savoir ;
- un code sur papier rangé avec le testament, s'il ne doit pas le savoir.

La façon choisie est notée avec le nom, et sera construite avec la succession après décès.

**Les actions** (au propriétaire seul, toutes dans `OWNER_ONLY_ACTIONS`) :
- **voir la liste** : libre ;
- **changer la liste** (ajouter, retirer, changer l'ordre, changer la façon) : `always_confirm`, oui explicite *(proposé, accepté)*. La carte montre la liste avant et après ;
- **dire qu'un successeur est mort :** libre *(mon choix)*. Le propriétaire le dit, et l'assistant le note. Ça ne fait que retirer quelqu'un de la course, sans donner de droit à personne. On ne demande pas « je confirme » à quelqu'un qui vient d'annoncer un deuil. La personne reste visible dans la liste, comme « décédée », et le suivant passe en premier *(propriétaire)*.

**Le contexte du propriétaire** montre la liste, avec pour chacun :
- son rang ;
- s'il est du foyer, et son rôle : un enfant le dit, car il devra être adulte le jour venu *(proposé, accepté)* ;
- s'il a quitté le foyer, et depuis quand ;
- s'il est décédé, et depuis quand.

L'assistant choisit le moment de redemander un remplaçant, selon le lien entre le propriétaire et la personne, sans brusquer *(propriétaire)*.

**Correction de ma part.** J'avais dit en conversation qu'un successeur qui quitte le foyer laisse sa place au suivant. Mais un successeur peut être extérieur. Quitter le foyer ne le retire donc pas de la liste : l'assistant le signale au propriétaire, qui décide s'il reste *(mon choix, à confirmer)*.

## 8. Le socle (ce que le code garantit, quoi que l'assistant décide)

1. Le rôle de propriétaire ne passe que par l'action de transmission, demandée par le propriétaire actuel, avec son oui explicite, à un adulte présent du foyer. Jamais par une invitation, un changement de rôle, un retour, un droit ou une suite.
2. Il y a toujours exactement un propriétaire (index unique et transaction unique). Son rôle est pour une durée indéterminée : il n'expire jamais.
3. Une suite ne donne rien de plus que ses étapes :
   - chaque étape passe par la barrière à son tour, avec les faits du moment et les refus fixes des outils ;
   - la seule exception est le rôle et les droits que la personne a perdus par une étape de la même suite. Ils valent pour toutes les étapes fixées dans la demande, même réservées au propriétaire (choix (a)), mais seulement pour celles-là et tant que la suite vit ;
   - ce que quelqu'un d'autre change entre-temps s'applique.
4. La liste des successeurs n'est vue et changée que par le propriétaire, ou par une étape d'une suite qu'il a approuvée quand il l'était. La changer demande son oui explicite, sauf pour noter un décès.
5. Un réveil de l'assistant ne lui donne que les outils et les droits de la personne de la conversation. Le bloc qui dit ce qui s'est passé est une donnée, pas une consigne.
6. L'audit ne porte que des identifiants, des noms, des rôles et des nombres, jamais le texte d'un mot (D4).

## 9. Textes à écrire

- La description des nouveaux outils pour le modèle (anglais) : la suite, la transmission, la liste des successeurs. Celle de la suite dit quand s'en servir (des étapes qui dépendent les unes des autres) et quand ne pas s'en servir.
- `prompts/system.md` (moins de 6000 caractères) :
  - ce qu'est un réveil après un bouton ;
  - que l'assistant peut se taire ;
  - qu'à l'arrêt d'une suite, c'est à lui de choisir.
- Les cartes en français et en anglais (`locales`) : la suite et ses étapes, la transmission, la liste avant et après.
- Les README (français et anglais) : une phrase à plusieurs actions, transmettre son rôle, la liste des successeurs.

## 10. Scénarios qui doivent passer

1. **Le départ en une phrase.** Le propriétaire dit « je pars, donne mon accès à Justine et retire-moi » :
   - une carte, deux étapes, « je confirme » ;
   - Justine est propriétaire, l'ancien propriétaire est retiré, et il y a deux entrées d'audit ;
   - le contexte de Justine dit qu'elle est propriétaire ;
   - ses droits personnels d'adulte sont en pause, et ses demandes qui attendaient le propriétaire attendent son propre oui ;
   - jamais deux propriétaires à aucun moment.
   - Variante : « donne mon accès à Justine, puis donne-le à Alex ». La 2ᵉ étape est refusée, car seul le propriétaire actuel transmet.
2. **Une suite arrêtée après un bouton.** Le oui est donné par le bouton, et l'étape 2 échoue :
   - L'assistant est réveillé dans la conversation et décide (avec le faux modèle des tests) ;
   - l'étape 1 reste faite.
3. **Quelqu'un d'autre change les droits.** Un adulte a reçu un droit personnel. Il demande une suite qui en a besoin. Le propriétaire retire ce droit avant le oui. Au oui, l'étape qui en avait besoin est refusée.
4. **La règle de la suite ne déborde pas.** La suite « donne mon rôle à Justine, puis repasse Justine adulte » : la 2ᵉ étape est refusée, car le rôle du propriétaire ne change jamais.
5. **Un bouton sur une demande simple.** L'assistant est réveillé, se tait, et aucune bulle n'apparaît. Sur une autre demande, il répond, et la réponse apparaît dans la page ouverte.
   - Après un oui dit en mots, c'est la réponse de l'assistant qui s'affiche, pas la phrase toute faite. Si le modèle ne répond pas, la phrase toute faite s'affiche.
6. **Les refus de la transmission.** La transmission à un enfant, un invité, soi-même ou quelqu'un de parti est refusée. Demandée par un adulte, elle est refusée par le socle.
7. **La liste des successeurs.**
   - Le propriétaire met [Justine, son frère (extérieur, code papier)] avec un oui explicite.
   - Un adulte ne voit pas la liste.
   - Le propriétaire dit « Justine est morte » : elle est notée décédée, le frère passe en premier, et le contexte le dit.

## 11. Laissé au constructeur

- Les noms des outils, des actions, des tables et des colonnes, et les numéros de migration.
- Comment une demande porte plusieurs étapes : une colonne dans `confirmations`, ou une table d'étapes.
- Comment le code reconnaît une étape « vérifiée à son tour » (§4.2), dans le respect de son garde-fou.
- Comment se fait un tour côté serveur sans page ouverte, et comment la page le montre (§5).
- Comment l'assistant se tait.
- La forme de la carte d'une suite.

## 12. Mes choix, à vérifier par le propriétaire

- Le rôle d'avant ne sert qu'aux étapes fixées dans la demande, tant que la suite vit (au plus le délai de vie d'une demande). Une suite relancée refait ces mêmes étapes, sans en ajouter.
- Le rôle de propriétaire ne se défait pas par une suite. L'ancien propriétaire demande au nouveau, qui peut le lui rendre par la transmission ordinaire.
- À la transmission, les demandes du nouveau propriétaire qui attendaient le propriétaire sont renvoyées vers son propre oui, et ses droits personnels d'adulte sont mis en pause.
- Après un oui ou un non dit en mots, la phrase toute faite ne sert plus qu'en secours, si le modèle ne répond pas.
- Une suite qui contient une étape « vérifiée à son tour » demande un oui `always_confirm`, le plus fort.
- Si un droit change en route et demande un approbateur plus exigeant, la suite attend le oui de la bonne personne (comme R27), au lieu d'échouer.
- La transmission demande toujours un oui explicite, quel que soit le réglage de style.
- La note qui dit le transfert au nouveau propriétaire reste 30 jours dans le contexte de l'assistant. Le rôle, lui, n'a pas de fin.
- La liste des successeurs passe au nouveau propriétaire avec le rôle, et l'assistant lui en parle.
- Noter un décès ne demande pas de « je confirme ».
- Quitter le foyer ne retire pas un successeur de la liste, et l'assistant le signale (c'est une correction de ce que j'avais dit en conversation).
