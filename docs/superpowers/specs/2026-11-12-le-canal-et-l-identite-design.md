# Chantier A — Le canal et l'identité

Date : 2026-11-12. Branche : `feat/channel-and-identity`. Version visée : 0.8.0.
Premier chantier de la feuille de route `2026-11-12-feuille-de-route-refondation.md`.

## 1. Pourquoi

L'assistant sera relié à des micros et des haut-parleurs, ou parlera à la voix sur un ordinateur ou un téléphone. Il doit pouvoir faire toute action possible : il a les moyens physiques, et la personne qui demande en a le droit. Le logiciel n'est jamais censé être un frein.

Aujourd'hui, l'assistant est bâti autour d'une page web :

- le tour de discussion fabrique du HTML en cours de route (`chat/routes.py`, `prepare_turn` et `_Turn`) ;
- il sait qui parle par le cookie du navigateur, et le tient toujours pour sûr (`Actor.trusted` n'est jamais faux) ;
- une preuve ne se donne que sur une carte (`confirmations/proof.py`) ;
- une parole de l'assistant n'arrive à quelqu'un que si sa page vient la chercher (`/chat/since`, toutes les 5 secondes).

Ce chantier n'apprend rien de neuf à l'assistant. Il refait le cœur de la conversation pour qu'il ne connaisse aucun canal, afin que la mémoire, la voix et la maison se posent dessus sans rien reprendre.

## 2. Ce que le propriétaire a décidé

Dit pendant la conception, le 2026-11-12.

**Les preuves**

- Il n'y a pas de liste fermée de preuves : mot de passe vocal, notification sur le téléphone, caméra, geste devant la caméra, ou tout autre moyen.
- Un moyen de preuve naît d'un accord : l'assistant l'imagine et la personne l'accepte, ou la personne l'imagine et l'assistant l'accepte.

**Qui parle**

- L'assistant a le dernier mot sur l'identité. Un mot de passe juste ne suffit pas si la voix, la caméra, la façon de parler ou l'intonation ne correspondent pas. Exemple : le propriétaire retire à ses enfants le droit sur la lumière de sa chambre ; un enfant qui imite sa voix avec un logiciel ne doit pas l'obtenir. L'assistant demande une autre validation, ou refuse s'il est sûr.
- L'assistant ne doit pas être trop prudent : le propriétaire ne veut pas être embêté chaque fois qu'il demande d'éteindre la lumière de sa chambre.
- Devant quelqu'un qu'il ne reconnaît pas, l'assistant demande qui c'est et une autre vérification. Une fois sûr, il retient (une voix enrhumée) et valide la suite. Une voix qui change peu à peu, il s'y adapte seul.
- L'assistant peut être plus prudent que le minimum fixé par le propriétaire, jamais moins.
- Si le propriétaire l'autorise à écouter les micros en continu, l'assistant apprend les voix en vivant avec le foyer. Le propriétaire parle à Justine et elle répond : cette voix est celle de Justine (une fois ne suffit pas, la répétition fait la certitude). Enrhumée mais toujours celle à qui l'on parle : c'est encore elle, avec une voix un peu changée. Il n'a pas besoin d'entendre son nom : il sait comment le propriétaire lui parle, et en déduit que c'est bien à elle qu'il parle.
- Un indice peut jouer contre. L'assistant entend le propriétaire dans la cuisine et, au même moment, sa voix dans la chambre d'un enfant : il ne peut pas être à deux endroits. Comme il parlait à Justine dans la cuisine, elle est la preuve qu'il y était : ce n'est pas lui dans la chambre.

**Le contexte d'une demande**

- « Éteins la lumière », sans préciser : s'il en a les moyens, l'assistant comprend où est la personne. Il peut inventer ce moyen avec son propre code.

**La façon de construire**

- Le cœur de la conversation est réécrit à neuf, pensé d'emblée pour plusieurs canaux (la « façon 2 », §5).
- Le chantier livre un deuxième moyen très simple de parler à l'assistant, hors de la page, pour constater qu'il ne dépend plus d'elle.
- À rappeler au propriétaire : refaire ce même essai à la voix quand le chantier D sera en place.

## 3. Buts, non-buts, ce qui serait un échec

**Buts**

1. Un cœur de conversation qui ne connaît ni HTML, ni requête web, ni cookie, ni gabarit.
2. La page web devient un canal parmi d'autres, branché sur ce cœur, sans rien perdre de ce qu'elle fait aujourd'hui.
3. Un deuxième canal : un petit programme de texte, sans navigateur, sur un ordinateur que la personne a confié à l'assistant.
4. Une identité qui porte un canal, un appareil et des indices avec leur force, et plus seulement « connecté ou non ».
5. Des moyens de preuve convenus, propres à chaque personne, qu'on peut ajouter sans nouvelle version, et qui ne supposent pas une carte.
6. L'assistant peut pousser une parole vers quelqu'un sans attendre qu'une page la demande.
7. Chaque demande porte son contexte (canal, appareil, et une place pour le lieu).

**Non-buts** (d'autres chantiers)

- La voix, la reconnaissance de qui parle, les caméras, le lieu d'une personne (D, E).
- La mémoire : retenir une voix, s'adapter à une mue (B).
- De nouveaux moyens de preuve réels : mot de passe vocal, notification sur le téléphone (ils arrivent avec leur moyen physique).
- Le catalogue ouvert des actions, le bac à sable (C).
- Changer ce que la page montre ou la façon dont elle se présente.

**Ce qui serait un échec même si ça marche**

- La page perd un comportement que le propriétaire a fait corriger depuis la 0.4.0.
- Le deuxième canal passe par la page (il retire du HTML après coup) : l'assistant resterait bâti autour d'elle.
- Une preuve devient vérifiable par la lecture du modèle.
- Un secret (clé de modèle, lien d'invitation, mot de passe) passe par le modèle, la discussion gardée, le journal ou les logs.
- Le nouveau cœur contient un cas particulier « si c'est la page ».

## 4. Contraintes

- Le socle ne bouge pas : cœur sans internet, secrets au coffre jamais montrés à un modèle, droits vérifiés avant chaque action, journal.
- **Les tests existants sont le contrat.** La page rebranchée fait passer la suite rapide (5 895 tests) et les parcours de bout en bout (107), à l'exception des tests qui fixent la forme interne du code réécrit, lesquels sont réécrits contre le nouveau cœur en gardant ce qu'ils vérifient.
- Aucune installation réelle n'existe : pas de reprise de données, les migrations ne servent qu'à la base de développement.
- Le moteur d'un tour (`chat/agent.py`, `run_turn`), les outils, la barrière (`policy/barrier.py`), la décision d'une demande (`confirmations/service.py`), le choix des modèles et le gardien sont gardés tels quels, sauf là où ce document dit le contraire.
- Livré d'un bloc : une branche, un plan, une fusion.
- Code et commits en anglais ; tout texte lu par une personne suit les règles de la 0.7 (phrases par défaut en français et en anglais, « tu » au propriétaire).

## 5. Les façons de faire envisagées

**Façon 1 — Dégager la page pas à pas, dans le code actuel.** Sortir le HTML du tour morceau par morceau. Les tests restent verts à chaque étape ; mais le plus gros fichier (2 800 lignes) est démêlé plutôt que remplacé, et sa forme reste celle d'une page.

**Façon 2 — Réécrire le cœur de la conversation à neuf.** Un nouveau cœur qui ne connaît aucun canal, puis la page rebranchée dessus, puis l'ancien code retiré. Résultat plus net ; risque de reperdre des comportements réglés essai après essai.

**Écartée — Faire passer le deuxième canal par la page.** Rapide, mais c'est exactement ce que le chantier doit défaire.

**Choisie par le propriétaire : la façon 2**, parce que tout le code sera fait d'emblée pour les extensions à venir. Le risque est tenu par la contrainte des tests (§4).

## 6. La conception

### 6.1 Le cœur de conversation

Un nouveau paquet, `domo_core/conversation/`. Il ne dépend d'aucun module web.

**Ce qu'il reçoit : une demande.** Elle dit :

- qui parle (§6.3) ;
- ce qui est dit : un texte, ou rien quand c'est l'assistant qui prend la parole (une arrivée, une nouvelle, un réveil) ;
- le contexte : le canal, l'appareil, et une place libre pour le lieu ;
- ce que le canal sait faire (§6.2).

**Ce qu'il rend : des événements, en données.**

| Événement | Ce qu'il porte |
|---|---|
| des mots | un morceau de la réponse, en texte brut |
| une action | ce qu'a fait un outil : la carte en données (`key`, `params`, `dates`), comme `ToolOutcome.card` aujourd'hui |
| une demande qui attend | la demande, qui doit répondre, comment (un oui, des mots précis, une preuve) |
| à montrer une fois | ce qui ne doit être vu qu'une fois par la personne (un lien d'invitation) ; jamais gardé, jamais donné au modèle |
| à faire sur un écran sûr | une étape qui ne passe pas par la conversation (saisir une clé de modèle) : où la faire |
| une preuve à donner | la preuve demandée et les moyens convenus de la personne (§6.4) |
| une erreur | sa raison, en donnée (pas de clé, modèle en panne, limite atteinte), et ce que la personne peut faire |
| la fin | le statut du tour, le modèle, la dépense |

Le cœur prépare le tour (ce que fait `prepare_turn`), le fait tourner (`_Turn.run`), garde la réponse, marque les mots dits, écrit le journal. Il ne met rien en forme : pas de `html.escape`, pas de gabarit, pas d'adresse de page.

Le nettoyage des marques de mise en forme (`text.PlainStream`, 0.7.3) reste dans le cœur : les mots sont du texte brut pour tous les canaux.

**Ce qui est réécrit dans ce paquet** : ce que font aujourd'hui `chat/routes.py` (hors routes HTTP), `chat/wake.py`, `chat/arrival.py`, `chat/visits.py`, et la partie de `chat/actions.py` qui prépare ce qu'une carte montre.

**Ce qui reste du côté du canal page**, seulement rebranché : `chat/key_routes.py` et `confirmations/routes.py`. Les règles qu'ils contiennent et dont un autre canal a besoin (qui peut ouvrir l'écran sûr d'une clé, `may_open_a_form`) sont sorties dans un module que le cœur peut appeler.

**Ce qui est gardé et appelé par lui** : `chat/agent.py`, `chat/store.py`, `chat/context_window.py`, les outils, `policy/`, `confirmations/service.py`, `confirmations/judge.py`, `llm/`, `prompts/`.

### 6.2 Les canaux

Un canal est ce qui relie une personne au cœur. Il fait trois choses :

1. Il donne une demande au cœur.
2. Il reçoit les événements et les met dans sa forme : la page en HTML, le programme de texte en lignes, plus tard la voix en paroles.
3. Il dit ce qu'il sait faire, pour que l'assistant choisisse comment s'y prendre :
   - montrer une carte avec un bouton ;
   - montrer quelque chose une seule fois à la personne seule ;
   - ouvrir un écran sûr (un formulaire du gardien) ;
   - recevoir une parole de l'assistant sans l'avoir demandée ;
   - quels moyens de preuve il peut porter.

Quand le canal ne sait pas faire une étape, le cœur ne la force pas : l'événement dit où elle peut se faire (« sur une page », « sur un autre de tes appareils »), et l'assistant le dit avec ses mots.

**Le canal page** : ce que sont aujourd'hui les routes de la discussion, les fragments, le flux d'événements de la page et `chat.js`. Il traduit chaque événement en ce que la page montrait déjà.

**Le canal texte** : §6.7.

Aucun `if` sur le nom d'un canal dans le cœur : seulement ce qu'il sait faire.

### 6.3 Qui parle

`Actor` (une personne, un rôle, sûr ou non) est remplacé, à l'entrée du cœur, par « qui parle » :

- la **personne supposée**, ou personne ;
- le **canal** et l'**appareil** ;
- des **indices**, chacun avec sa nature, sa force et son moment. Dans ce chantier il en existe trois : une session ouverte sur la page (forte), un appareil connu (forte), une preuve réussie depuis peu (forte, avec son moyen). Les indices à venir (une voix qui correspond plus ou moins, une caméra) se brancheront ici sans changer la forme.

La forme d'un indice est faite pour ce qui viendra avec les micros et la mémoire (E, B), même si rien de tel n'est construit ici :

- un indice pèse **pour** ou **contre** une personne (« entendu ailleurs au même moment » pèse contre) ;
- il garde sa **source** : une vérification faite par le code (une session, un appareil, une preuve), ou ce que l'assistant a perçu et appris lui-même ;
- il peut s'appuyer sur **une autre personne** (parler avec quelqu'un que l'assistant reconnaît) et sur un **lieu** ;
- ce que l'assistant déduit de lui-même peut le rendre plus prudent ou plus à l'aise, mais ne remplace jamais une preuve que le propriétaire ou le code exige pour une action.

Dans ce chantier, tous les indices viennent du code et pèsent pour. Une personne est **établie** quand un indice fort venu du code au moins la désigne : c'est une règle du code, pas un jugement du modèle. Comment un faisceau d'indices appris établit quelqu'un se décidera avec la voix.

Une preuve réussie reste un indice pendant 15 minutes, tant que la personne continue de parler sur le même canal *(mon choix)*. Elle ne remplace rien : une preuve vaut pour la demande qui l'a demandée, comme aujourd'hui, et le minimum (la règle du propriétaire, le code) est redemandé à chaque fois. L'indice sert seulement au juge, pour ne pas en redemander une de lui-même coup sur coup.

Ce que le code en fait :

- **Sans personne établie**, la demande n'a les droits de personne du foyer. L'assistant parle normalement, demande qui c'est, et peut demander une vérification ; il ne fait rien qui demande d'être quelqu'un. C'est la règle actuelle de la barrière pour un acteur non sûr, gardée. *Mon choix pour ce chantier :* seules la forme et cette règle sont construites et testées. Les deux canaux du chantier connaissent toujours la personne ; le chemin complet d'une demande sans personne (quelle discussion, quelle clé paie) arrive avec la voix.
- **Avec une personne établie**, la barrière décide comme aujourd'hui (droits, limites, qui approuve).
- **Le juge du oui lit les indices** (aujourd'hui il n'en voit aucun) : il peut demander une preuve quand ils sont faibles pour l'enjeu, et ne rien demander quand ils sont forts. C'est là que se règle « ni trop prudent, ni pas assez ».
- Le minimum reste dans le code : la règle du propriétaire, le juge et l'assistant, le plus exigeant des trois l'emporte (`judge.what_to_ask`, inchangé).

Sur la page et sur un appareil connu, les indices sont forts : rien ne change pour la personne dans ce chantier.

### 6.4 Les moyens de preuve convenus

Aujourd'hui : deux preuves fixées dans le code (le mot de passe et la clé d'accès, `confirmations/proof.py`), données sur une carte, et la règle « une preuve ne se donne jamais en mots ». Le bouton n'est pas une preuve et ne le devient pas : c'est une façon de répondre oui, et un clic ne satisfait jamais une demande de preuve.

Demain : chaque personne a ses **moyens convenus**. Un moyen dit :

- comment il se demande et où (sur quel canal ou quel appareil il peut être donné) ;
- qui l'a proposé (l'assistant ou la personne) et quand l'autre l'a accepté ;
- comment le code le vérifie.

**La règle qui remplace l'ancienne : une preuve est toujours vérifiée par le code, jamais par la lecture du modèle.** Ce que la personne donne pour prouver ne passe ni par le modèle, ni par la discussion gardée, ni par le journal. L'assistant apprend seulement « prouvé » ou « pas prouvé », et par quel moyen.

Dans ce chantier :

- le cadre existe : les moyens d'une personne sont des lignes de la base, pas une liste dans le code ; chaque ligne renvoie à un **genre** de moyen, et c'est le genre qui sait comment le code vérifie ;
- les deux preuves actuelles (le mot de passe, la clé d'accès) deviennent les deux premiers genres ; chacun a les moyens qu'il a déjà, sans rien avoir à faire ;
- une preuve peut être donnée sur un autre canal que celui où l'on parle : la demande dit quels moyens la personne a et où chacun peut se donner ; le canal texte porte le mot de passe (saisi à part, masqué, envoyé au code et jamais dans la conversation) ; la clé d'accès reste liée à la page ;
- *mon choix :* les actions « convenir d'un moyen » et « en retirer un » ne sont pas construites ici. Il n'existe encore aucun nouveau genre à convenir, et un genre a besoin d'un code qui le vérifie : ces actions arrivent avec le premier nouveau genre (le mot de passe vocal, chantier D), avec la garde qui empêche de se retrouver sans aucun moyen. Le cadre est prêt pour elles.

Après une preuve, l'assistant reste libre de douter (§2) : il peut en demander une autre.

### 6.5 Un appareil connu

Une personne peut confier un appareil à l'assistant, depuis sa page : l'assistant lui montre un code court, valable peu de temps, qu'elle donne au programme sur l'appareil. L'appareil reçoit alors un secret à lui.

- Un appareil connu est un indice fort de qui parle. *Mon choix :* il vaut autant qu'une session ouverte sur la page, y compris pour le propriétaire ; ce qui demande une preuve la demande toujours.
- Tout se fait en parlant à l'assistant, sur la page ou ailleurs : confier un appareil, voir les siens, en retirer un. Le code court est montré une fois, comme un lien d'invitation. **Aucune nouvelle section de page** ; les seuls textes nouveaux sont les phrases de ces actions.
- Le propriétaire peut retirer l'appareil de n'importe qui.
- *Mon choix :* par défaut, le propriétaire et les adultes peuvent confier un appareil ; un enfant ou un invité seulement si le propriétaire lui en donne le droit.
- *Mon choix :* un appareil reste connu jusqu'à ce qu'on le retire. Il cesse de l'être quand la personne quitte le foyer, en est retirée, ou quand le séjour d'un invité finit. Changer de mot de passe ne le retire pas.
- Confier et retirer un appareil sont des actions jugées et écrites au journal.
- C'est la première brique de ce qui servira pour la voix sur ordinateur (D) et les ordinateurs de la maison (F).

### 6.6 L'assistant parle sans qu'on le lui demande

Aujourd'hui, un réveil, une arrivée ou une nouvelle ne tournent que parce qu'une page a demandé, et leur réponse attend que la page revienne la chercher.

Demain :

- c'est le cœur qui décide qu'un tour est dû (un mot laissé pour quelqu'un, une demande décidée, une suite qui reprend), sans attendre la demande d'une page ;
- ce que l'assistant dit est gardé comme aujourd'hui, puis **poussé** vers les canaux ouverts de la personne qui savent le recevoir ;
- s'il n'y en a aucun, la parole attend, et elle est donnée au prochain canal qui s'ouvre : c'est ce que fait aujourd'hui l'arrivée ;
- *mon choix :* un tour que l'assistant commence lui-même n'a pas de canal qui demande. Il est écrit sans supposer de canal (la ligne du contexte dit « personne ne t'écoute encore : dis tout en mots »), et ses mots vont à tous les canaux ouverts de la personne. Ce qui ne se montre qu'une fois, ou une demande qui attend, n'est pas envoyé au hasard : il attend que la personne le redemande, sur un canal qui sait le montrer. C'est la règle d'aujourd'hui pour un tour que personne ne lit (`ToolContext.no_reader`), dite par ce que le canal sait faire ;
- les garde-fous actuels sont gardés : un tour à la fois par personne, pas de tour qui en déclenche un autre sans fin, les délais entre deux nouvelles.

La page continue de demander toutes les 5 secondes pour s'afficher : c'est sa façon à elle de recevoir. La décision de parler ne dépend plus de cette demande.

### 6.7 Le deuxième canal : un programme de texte

Un petit programme lancé dans un terminal, sur un ordinateur confié à l'assistant (§6.5).

- On y tape, l'assistant répond, mot après mot.
- C'est la même discussion que sur la page : ce qui est dit d'un côté se retrouve de l'autre.
- Une carte y est dite en mots, avec la phrase de son catalogue.
- Une demande qui attend un oui se répond en mots. Une demande qui attend le bouton ou une preuve dit où elle peut se donner ; le mot de passe peut se donner ici (§6.4).
- Un lien d'invitation y est montré une fois. La saisie d'une clé de modèle renvoie vers l'écran sûr du gardien, à ouvrir dans un navigateur : une clé ne passe jamais par la conversation.
- Il reçoit ce que l'assistant dit de lui-même tant qu'il est ouvert.

Il parle au cœur par une interface faite pour les canaux, à travers la bordure, en HTTPS, avec le secret de l'appareil. La page n'emprunte pas cette interface dans ce chantier : elle reste servie par le cœur.

Où vit ce programme : un petit paquet à part dans le dépôt, que la personne installe sur son ordinateur. Il ne contient aucune logique de l'assistant.

### 6.8 Les consignes

`prompts/system.md` parle aujourd'hui de pages, de cartes et de boutons. Le texte fixe ne nomme plus aucun canal ; une ligne du contexte dit ce que le canal de cette demande sait faire (« tu peux montrer une carte », « ici tout se dit »). La limite de 7 000 caractères du texte fixe reste.

## 7. Comment on saura que c'est réussi

1. La suite rapide et les parcours de bout en bout passent sur la page rebranchée.
2. Le cœur a ses propres tests, sans rien de web : une demande entre, des événements sortent.
3. Un test vérifie qu'aucun module du cœur n'importe un module web.
4. Les mêmes scénarios tournent sur les deux canaux et donnent la même discussion gardée.
5. Le propriétaire confie son ordinateur à l'assistant, lance le programme de texte, et y retrouve sa discussion ; un mot laissé pour lui y arrive sans qu'il ait rien tapé.
6. Une preuve demandée pendant qu'il parle dans le programme de texte se donne par son mot de passe, sans ouvrir la page, et rien de ce mot de passe n'est dans la discussion, le journal ou les logs.

## 8. Laissé à celui qui construit

- Le découpage du paquet `conversation/` en modules, et les noms.
- La forme exacte des événements et de l'interface des canaux (flux d'événements ou autre).
- La technique du programme de texte, tant qu'il reste petit et sans logique de l'assistant.
- L'ordre de la réécriture, tant que le contrat des tests est tenu à la fin.
- Les migrations (moyens de preuve, appareils) : base de développement seulement.

## 9. Pour après

- **À rappeler au propriétaire au chantier D** : refaire à la voix l'essai du point 5 du §7.
- Les textes écrits par l'assistant pour les pages, les suites d'actions et la succession ne sont pas revus ici ; ils restent branchés sur le canal page.
- La barrière ne lit pas encore la force des indices dans ses limites : seul le juge la lit. À voir quand la voix apportera des indices faibles.
