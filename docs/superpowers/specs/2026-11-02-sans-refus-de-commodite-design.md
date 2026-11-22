# Plus de refus de commodité : les 17 chemins pour l'assistant

Conception du 2 novembre 2026, après la fusion de la version 0.3.0 (suites d'actions, départ du propriétaire, successeurs). Elle s'appuie sur :
- `docs/superpowers/specs/2026-10-28-domo-architecture-design.md` (l'architecture) ;
- `docs/superpowers/specs/2026-10-31-droits-retour-mots-design.md` (droits, limites, barrière, mots) ;
- `docs/superpowers/specs/2026-11-01-actions-enchainees-depart-design.md` (suites, réveil, transmission, successeurs).

Elle part de l'inventaire des refus de la version 0.3.0, que le propriétaire a lu. Il a demandé de mettre en chantier les 17 refus de commodité, dans l'ordre proposé *(propriétaire)*. Chaque section ci-dessous reprend le numéro de l'inventaire (C1 à C17).

Ce qui vient du propriétaire est marqué *(propriétaire)*. Ce que j'ai proposé et qu'il a accepté est marqué *(proposé, accepté)*. Ce que je décide seul est marqué *(mon choix)* et repris au §9. Ce qui attend sa réponse est marqué *(à confirmer)*.

## 1. Intention

Le propriétaire l'a répété : « L'assistant devra être capable de s'adapter et effectuer tout ce que quiconque pourrait lui demander, tant que c'est une demande jugée raisonnable et dont il a le droit. Il faut prévoir même la plus improbable des possibilités » *(propriétaire)*.

Aujourd'hui, le code dit encore « non » à 17 demandes raisonnables, sans que ce refus protège quoi que ce soit du socle. Ce sont des refus de commodité : ils simplifiaient le code, pas la vie des gens. Il faut les remplacer par des chemins. L'assistant comprend ce qu'on veut, demande des précisions s'il le faut, et le code continue de tout vérifier.

Exemples de demandes qui doivent marcher après ce chantier :
- « Oui, mais donne-lui 5 jours au lieu de 2 », sur la carte d'une suite.
- « Pour les suites, un oui en mots me suffit. »
- « Invite mon frère Paul comme adulte et donne-lui mon rôle quand il sera là. »
- « Reprends avec Léa B. », après une suite arrêtée parce que le nom était pris.
- « Oui pour la demande de Léa, mais 3 jours », dit par le père dans sa propre conversation.
- « Laisse tomber ma demande », dit par un enfant dont la demande attend un adulte.
- « Léa reste finalement jusqu'à dimanche. »
- « Non pardon, Paul n'est pas mort. »
- « Laisse-moi un mot : appeler le plombier. »
- « Retire-moi du foyer », dit par un adulte.

## 2. Objectifs, non-objectifs, anti-objectifs

**Objectifs**
1. Les 17 refus de commodité deviennent des chemins (§4 et §5).
2. Pour chaque chemin, le code garde le socle : qui décide, l'action exacte, la barrière, les secrets jamais vus, le contenu externe traité comme une donnée.
3. Quand une demande est floue, l'assistant demande des précisions au lieu de deviner *(propriétaire)*.

**Non-objectifs (plus tard)**
- Les 37 limites techniques de l'inventaire (longueurs, délais, nombres). Seul le nombre d'étapes d'une suite change ici (C11).
- Les 15 chemins manquants : rappels à une heure, mémoire, changement de modèle, arrêt d'urgence, connecteurs, etc. Ils ont leurs propres itérations.
- Prévenir quelqu'un ailleurs que sur sa page (notification, voix). Une relance attend que la personne revienne parler à l'assistant.
- La succession après un décès.

**Anti-objectifs (ce qui serait un échec même si ça marche)**
- Un chemin qui permet ce que la personne n'avait pas le droit de faire.
- Le modèle principal qui dit oui à la place de quelqu'un. Un oui vient toujours des mots de la personne, lus par le code et le classifieur isolé, ou de son bouton.
- Un lien, un jeton ou un formulaire vu par le modèle.
- Un refus remplacé par un autre refus déguisé : l'assistant doit toujours savoir quoi faire ensuite.

## 3. Approches

- **A. Une conception, deux lots, deux fusions.** Le lot 1 aurait traité les suites, le lot 2 les demandes et le foyer, chacun fusionné à part. Écartée par le propriétaire.
- **B. Tout d'un bloc.** Une seule branche, un seul plan, une seule fusion (version 0.4.0). C'est le choix du propriétaire *(propriétaire)*. Le plan suit l'ordre de l'inventaire autant que les dépendances le permettent : les suites d'abord (C1 et C2 en premier), puis les demandes et le foyer.
- **C. Un chantier par refus.** Écartée : 17 fois la même préparation, et les refus des suites se touchent trop pour être séparés.

## 4. Lot 1 : les suites

### 4.1 Modifier une suite qui attend son oui (C1)

« Il doit être possible de faire n'importe quelle demande à l'assistant, même modifier une suite. C'est à l'assistant de comprendre la modification demandée et de demander des précisions si besoin » *(propriétaire)*.

- Une suite qui attend se change comme toute demande. La personne répond avec un changement (« oui, mais… »), et le classifieur le lit comme un changement. L'assistant reçoit alors le droit de la changer, dans ce tour.
- L'assistant donne la suite entière, telle qu'il a compris la modification. Il peut changer les arguments d'une étape, en ajouter, en retirer ou les remettre dans un autre ordre. S'il ne comprend pas, il demande des précisions, et la demande attend toujours.
- Le code refait le plan de la suite, comme au moment de la demande :
  - chaque étape est décrite et jugée ;
  - le niveau, l'approbateur et le style du oui sont recalculés ;
  - une étape gardée telle qu'elle était demandée est jugée avec le rôle retenu à la demande ; une étape changée ou ajoutée, avec le rôle de maintenant (dernier point ci-dessous).
- Ensuite, c'est la règle d'un changement ordinaire :
  - si le oui donné avec le changement suffit (c'est le oui de la personne, et le style en mots est accepté), la suite modifiée tourne tout de suite ;
  - sinon, elle devient une nouvelle demande, qui remplace l'ancienne : par exemple quand une étape demande « je confirme », ou quand le oui revient maintenant à un adulte ou au propriétaire.
- Une étape **ajoutée** (un appel que la personne n'a jamais vu sur une carte) donne toujours une nouvelle carte, même avec un « oui, mais ». Changer les arguments d'une étape, en retirer une ou changer l'ordre peut tourner tout de suite *(mon choix)*. Raison : la personne dit oui à ce qu'elle a vu ; le modèle ne doit pas pouvoir glisser une action nouvelle dans son oui.
- Une étape refusée par la barrière refuse la suite modifiée, avec le numéro de l'étape, comme à la demande.
- **Le rôle retenu par une suite ne vaut que pour ce qui a été demandé quand la personne l'avait** *(mon choix)*. Une étape restée telle qu'elle était demandée garde ce rôle. Une étape changée ou ajoutée plus tard est jugée avec le rôle du moment où elle est changée. Exemple : après avoir transmis son rôle, l'ancien propriétaire ne peut pas changer une étape qui reste en « change la liste des successeurs ». C'est la règle de la spec du 2026-11-01 (§8.3), appliquée étape par étape.

### 4.2 Régler le oui des suites (C2)

« Ok, tant que ça peut être géré par la suite. Si ça devient trop contraignant, la personne peut décider de changer cela » *(propriétaire)*.

- Le propriétaire peut régler le style du oui des suites, comme pour tout type d'action (`chain.run`) : « pour les suites, je veux toujours le bouton », ou « un oui en mots me suffit ».
- Le oui d'une suite est le plus strict de ces styles :
  - le style de chaque étape décrite, chacune à **son propre niveau**. Une étape « confirmer » ne devient pas plus stricte parce qu'une autre étape de la suite est « toujours confirmer » *(mon choix)* ;
  - le style de la suite elle-même, qui vaut aussi pour les étapes vérifiées à leur tour. Leur niveau « toujours confirmer » est une précaution de la suite, pas de l'étape : le réglage des suites peut l'adoucir *(mon choix)*.
- Pour une étape vérifiée à son tour, le style se choisit dans cet ordre *(mon choix)* :
  1. le réglage du propriétaire pour le type de l'étape, s'il en a mis un (« pour les retraits, toujours le bouton » gagne) ;
  2. sinon, le réglage des suites, s'il en a mis un ;
  3. sinon, comme aujourd'hui : le style du niveau « toujours confirmer » (le bouton ou « je confirme » tant que le propriétaire n'a pas choisi).
- Sans réglage des suites, rien ne change par rapport à la version 0.3.0.
- La transmission du rôle et la liste des successeurs demandent toujours le bouton ou « je confirme », quel que soit le réglage (socle).
- Quand une suite demande quand même « je confirme », l'assistant peut dire à la personne quelle étape l'exige, et proposer au propriétaire d'adoucir ce réglage.

### 4.3 Une étape qui attend une arrivée (C3)

**a. Une étape vérifiée à son tour peut en suivre une autre.**
- Exemple : « passe Paul adulte, donne-lui mon rôle, puis retire-moi ». La transmission est vérifiée à son tour, car Paul n'est pas encore adulte. Aujourd'hui, « retire-moi » ne peut pas la suivre : une étape non décrite ne compte comme touchant personne.
- Désormais, une étape vérifiée à son tour compte comme touchant les personnes que son outil dit qu'elle touchera. Pour la transmission : celui qui donne et celui qui reçoit.

**b. « Quand il sera là ».**
- Une étape peut attendre l'arrivée d'une personne invitée : « invite mon frère Paul comme adulte et donne-lui mon rôle quand il sera là ».
- La personne attendue est désignée par le nom de son invitation. Ce peut être l'invitation d'une étape d'avant de la même suite, ou une invitation qui attend déjà dans le foyer : « Paul est invité depuis hier ; quand il arrive, donne-lui mon rôle ». Les noms se distinguent dans le foyer, invitations comprises : le nom désigne une seule invitation.
- Les étapes après l'attente sont vérifiées à leur tour : elles peuvent nommer le nouveau venu, qui n'existe pas encore.
- Après le oui, la suite fait ses étapes jusqu'à l'attente, puis se met en pause :
  - sa carte dit qu'elle attend l'arrivée de Paul ;
  - L'assistant la voit dans le contexte de la personne qui l'a demandée ;
  - la personne peut l'abandonner (l'assistant le fait à sa demande, comme pour une demande qui attend) ;
  - pour la changer, l'assistant l'abandonne et propose une nouvelle suite, qui attend la même invitation. L'invitation déjà faite reste.
- Quand Paul accepte l'invitation, la suite reprend au plus une minute plus tard, même si l'application a redémarré entre-temps *(mon choix)*. Chaque étape est rejugée à son tour, avec les faits de ce moment.
- Si une étape qui reste est une transmission du rôle ou un changement de la liste des successeurs, le code redemande d'abord le oui explicite de celui qui a dit oui. La carte dit : « Paul a rejoint le foyer. Je lui donne ton rôle ? ». Raison : le lien a pu arriver chez quelqu'un d'autre, et le propriétaire doit voir qui est vraiment arrivé avant de donner son rôle *(proposé, accepté)*.
- Si l'invitation se termine sans arrivée (expirée ou annulée), la suite s'arrête à l'attente. L'assistant est réveillé dans la conversation de la personne et décide : nouveau lien, abandon…
- Une suite en pause vit tant que son invitation peut encore être acceptée. Le rôle et les droits retenus pour la suite valent pendant ce temps *(mon choix)*.
- À l'arrivée, le délai de vie de la suite repart de zéro *(mon choix)*. La carte « Paul a rejoint le foyer » n'est donc jamais déjà expirée, et une suite reprise qui s'arrête peut encore être relancée. Le rôle retenu vaut jusqu'à la fin de ce nouveau délai.
- Quand la suite reprend, puis finit ou s'arrête, l'assistant est réveillé dans la conversation de la personne qui l'a demandée, comme après un bouton, et juge s'il en parle.

### 4.4 Reprendre l'étape qui a échoué (C8)

- Exemple : « l'invitation a échoué parce que le nom était pris ; reprends avec Léa B. ». Aujourd'hui, `chain_continue` ne fait que les étapes d'après. L'étape qui a échoué est perdue.
- `chain_continue` peut maintenant reprendre l'étape qui a échoué, puis celles d'après :
  - **sans changement** : elle est rejouée en premier, sous le oui déjà donné, et rejugée à son tour. C'est possible seulement quand on sait que l'étape n'a rien fait : refusée par la barrière, devenue impossible, ou rejetée par son outil *(mon choix)* ;
  - **quand on ne sait pas si elle a agi** (le temps est écoulé pendant qu'elle tournait) : la rejouer pourrait faire deux invitations. Elle ne repart qu'avec un nouveau oui de la personne, sur une carte qui dit « elle a peut-être déjà été faite ». Avant, l'assistant peut vérifier ce qui s'est passé avec ses outils de lecture *(mon choix)* ;
  - **avec un changement** : l'assistant donne les étapes qui restent, telles qu'il a compris la personne. Le oui avait été donné pour les étapes d'origine, et le modèle principal ne dit jamais oui à la place de quelqu'un. Les étapes changées demandent donc un nouveau oui : une carte, un oui pour le reste de la suite. Seule exception : quand tout ce reste est libre, il tourne tout de suite.
- Ce qui a été fait reste fait. Les étapes restées telles qu'elles étaient demandées gardent le rôle retenu par la suite ; les étapes changées sont jugées avec le rôle de maintenant (§4.1).
- L'assistant peut toujours abandonner, ou défaire ce qui a été fait par de nouvelles actions (spec du 2026-11-01, §4.4).

### 4.5 Plusieurs liens dans une suite, et le formulaire de la clé (C9, C10)

- Une suite peut contenir plusieurs étapes qui font un lien : des invitations, des liens de retour. Exemple : « fais revenir Alex et invite sa femme Léa, puis laisse un mot à Alex ».
- Elle peut aussi contenir l'étape qui ouvre le formulaire de la clé du modèle, pour celui qui demande : « change le fournisseur du modèle, puis ouvre-moi le formulaire de la clé ».
- Le formulaire s'ouvre toujours pour celui qui demande, jamais sur la page d'un autre membre *(mon choix)*. Ouvrir un formulaire de secret chez quelqu'un d'autre est un nouveau chemin vers les secrets, avec ses propres vérifications : il viendra plus tard. En attendant, pour « donne à Alex le droit de changer la clé, puis ouvre-lui le formulaire », l'assistant donne le droit, puis dit à Alex (par un mot) qu'il peut demander le formulaire lui-même.
- Chaque étape qui fait un lien ou ouvre le formulaire a son propre bouton sur la carte de la suite (« Montrer le lien »). Ce bouton n'est montré qu'à celui qui a demandé.
- L'assistant n'a jamais le lien ni le formulaire. Ce qui est montré une fois ne l'est qu'à la personne (socle).
- Une suite qui attend à nouveau le oui de quelqu'un d'autre garde les boutons des étapes déjà faites.

### 4.6 Le nombre d'étapes (C11)

- Une suite a **de 1 à 20 étapes** *(mon choix)*. Ce nombre n'est plus lié au nombre d'appels d'outils d'une réponse : une suite compte pour un seul appel.
- Une suite d'une seule étape est acceptée : c'est une demande ordinaire, avec la carte d'une suite.
- Au-delà de 20 étapes, l'assistant fait plusieurs suites. Le résultat le lui dit, au lieu d'un refus sec.

### 4.7 Une suite dans une suite (C12)

- Une étape « faire dans l'ordre » placée dans une suite est mise à plat : ses étapes prennent sa place, dans le même ordre, et comptent dans les 20 *(mon choix)*.
- « Relance la suite qui a échoué, puis retire-moi » : l'assistant relance d'abord l'ancienne suite (`chain_continue`), puis demande la nouvelle. Les deux se font dans la même réponse. L'ancienne suite garde son propre oui, et les droits qu'elle avait retenus *(mon choix)*.
- Les outils qui gèrent les demandes (`confirmation_*`) restent hors des suites : une suite est elle-même une demande. L'assistant s'en sert avant ou après la suite. Le résultat le lui dit.

## 5. Lot 2 : les demandes et le foyer

### 5.1 Dire oui pour un autre, en mots et avec un changement (C5)

- Exemple : le père lit dans sa zone « À valider » que Léa demande d'inviter Tom pour 7 jours. Il dit à l'assistant : « oui pour la demande de Léa, mais 3 jours ».
- L'assistant voit, dans le contexte de chaque personne, les demandes qui attendent son oui (comme approbateur). Il peut lui en présenter une dans sa conversation (comme `confirmation_remind`).
- Les mots qui suivent sont lus par le même code et le même classifieur isolé que ceux du demandeur. Les mêmes règles s'appliquent :
  - les mots ne répondent qu'à la seule demande montrée juste avant ;
  - le style exigé s'applique (« je confirme » si besoin) ;
  - l'approbateur doit toujours avoir le droit de dire oui à cette demande.
- Un « oui, mais… » de l'approbateur est son oui à la version changée. Elle se fait tout de suite *(propriétaire)*, avec le même socle qu'un changement du demandeur : même outil, même type d'action, passage par la barrière.
- Comme pour le demandeur, l'assistant peut choisir de redemander : la version changée devient une nouvelle demande, qui attend encore le oui de l'approbateur.
- L'assistant peut prévenir le demandeur de ce qui a changé *(propriétaire)*. Il est réveillé dans la conversation du demandeur, comme après un bouton, et juge s'il en parle.
- Pour qu'il puisse juger ensuite, l'assistant note en quelques mots, avec le changement, ce que l'approbateur a dit et à quel point il était ferme : « 3 jours, pas plus, c'est non négociable » n'est pas « 3 jours, on verra ». Cette note vient des mots de l'approbateur, dans sa propre conversation. Elle est courte, ne contient jamais de secret, et arrive dans la conversation du demandeur comme une donnée, pas comme une consigne *(mon choix)*.
- Si le demandeur conteste (« non, je veux qu'il reste 5 jours »), c'est à l'assistant de juger *(propriétaire)*, d'après la réponse de l'approbateur et sa manière de répondre :
  - redemander à l'approbateur : c'est une nouvelle demande du demandeur, qui attend le oui de l'approbateur, comme toute demande ;
  - ou refuser tout de suite, parce que l'approbateur a été assez clair. L'assistant explique au demandeur pourquoi.
- Socle gardé : l'identité de l'approbateur est vérifiée ; personne ne dit oui à sa propre demande quand c'est à un adulte ou au propriétaire de le faire.

### 5.2 Ses demandes qui attendent quelqu'un d'autre (C6)

- L'assistant voit, dans le contexte du demandeur, ses demandes qui attendent un adulte ou le propriétaire, marquées « attend le oui d'un adulte » ou « du propriétaire ».
- Le demandeur peut l'abandonner : « finalement, laisse tomber ma demande ».
- Il peut la changer, et le changement devient une nouvelle demande pour le même approbateur.
- Il peut demander une relance : « rappelle papa qu'il doit dire oui ». La demande est alors mise en avant dans la zone « À valider » du père. L'assistant la lui présentera la prochaine fois qu'il lui parlera *(mon choix)*. Prévenir le père ailleurs que sur sa page vient plus tard.

### 5.3 Changer un brouillon de droits (C16)

- « Oui, mais 3 jours au lieu de 2 », sur la carte d'un changement de droits.
- Le fond de la spec 5.6 reste : le propriétaire voit les exemples avant son oui. Mais c'est l'assistant qui fait le tour à sa place.
- Le rédacteur de droits est relancé avec les mots d'origine et le changement. Le nouveau brouillon est présenté comme une nouvelle demande, qui remplace l'ancienne. Le propriétaire n'a qu'un oui à donner.

### 5.4 Un décès noté par erreur (C4)

- « Non pardon, Paul n'est pas mort, je me suis trompé de nom. »
- Le propriétaire peut défaire un décès noté. Paul reprend sa place sur la liste.
- Remettre quelqu'un sur la liste lui redonne une place dans la succession. C'est donc un changement de la liste, avec le oui explicite du propriétaire, comme tout changement de liste *(mon choix)*.
- C'est audité (« décès défait »).

### 5.5 Prolonger ou raccourcir le séjour d'un invité (C7)

- « Léa reste finalement jusqu'à dimanche », ou « Léa part demain ».
- Changer le séjour d'un invité qui reste invité est accepté : c'est une nouvelle fin de séjour, comptée en jours à partir de maintenant.
- C'est le même niveau et le même droit qu'un changement de rôle. La règle du foyer (30 jours au plus, sauf choix du propriétaire) s'applique au nouveau séjour.
- La carte dit la nouvelle date de fin.

### 5.6 Un mot pour soi-même (C13)

- « Laisse-moi un mot : appeler le plombier. »
- Un mot peut avoir pour destinataire celui qui l'écrit. Il s'affiche sur sa propre page, dans ses mots reçus.
- D4 tient comme pour tout mot : un mot reçu n'atteint jamais un modèle, et seul son auteur peut le relire avec l'assistant, dans sa propre conversation (exception déjà décidée le 2026-10-31). Un mot pour soi suit cette règle : « qu'est-ce que je m'étais noté ? » marche *(mon choix)*.
- « Rappelle-moi demain à 8 h » demande toujours le planificateur, qui viendra plus tard. L'assistant le dit, et propose le mot en attendant.

### 5.7 Les droits d'une personne partie (C14)

- « Paul est parti, retire-lui le droit d'inviter. »
- Le rédacteur de droits connaît aussi les anciens membres, marqués « parti le … ». Un droit peut donc être retiré, ou laissé en pause, pour quelqu'un qui n'est plus là.
- Quand quelqu'un quitte le foyer, ses droits personnels sont mis en pause, comme quand il revient. Le propriétaire décide de les lui rendre à son retour.

### 5.8 Refaire un lien de retour (C15)

- « Refais le lien pour Alex, il a perdu l'autre. »
- Le retour remplace lui-même le lien qui attend. L'ancien est annulé dans la même opération, et la carte le dit. Il n'y a qu'un oui à donner.

### 5.9 Se retirer soi-même (C17a)

- « Retire-moi du foyer », dit par un adulte, un enfant ou un invité. Un invité peut ainsi partir plus tôt.
- Se retirer n'est pas dans le socle (spec 5.2) : chaque membre peut le demander, pour lui seul.
- Le oui suit la règle de la barrière :
  - un adulte ou un invité dit oui lui-même ;
  - pour un enfant, c'est un adulte *(mon choix)*.
- Retirer quelqu'un d'autre reste réservé au propriétaire, ou à qui en a reçu le droit. Le propriétaire, lui, ne se retire jamais : il transmet d'abord son rôle (socle).
- Rien n'est effacé, et la personne peut revenir avec son historique.

### 5.10 Garder les mots d'un invité parti (C17b)

- Le propriétaire a décidé que les mots laissés à un invité s'effacent quand il part *(propriétaire)*. Cela reste la règle.
- Mais celui qui a écrit des mots à un invité peut demander de les garder jusqu'à une date : « garde mes mots pour Léa, elle revient dans un mois ».
- C'est à l'assistant de décider d'après les mots de la personne, et de demander des précisions s'il le faut *(propriétaire)* : la date (« dans un mois »), les mots concernés, ou s'il faut vraiment les garder. Le code ne fixe que qui peut le demander (l'auteur, pour ses propres mots) et la date la plus lointaine (un an, comme le plus long séjour) *(mon choix)*.
- Ces mots sont effacés à cette date si Léa n'est pas revenue, et elle les retrouve si elle revient avant.
- Prolonger le séjour (§5.5) suffit quand l'invité reste : rien n'est effacé tant qu'il est là.

## 6. Le socle (ce qui ne change pas)

1. Un oui vient des mots de la personne, lus par le code et le classifieur isolé, ou de son bouton. Jamais du modèle principal.
2. Chaque étape d'une suite passe par la barrière à son tour, avec les faits du moment. Une suite reprise, changée ou réveillée par une arrivée ne fait pas exception.
3. Le rôle de propriétaire ne passe que par la transmission, demandée par le propriétaire actuel, avec son oui explicite, à un adulte présent du foyer au moment où elle se fait.
4. La transmission et la liste des successeurs demandent toujours le bouton ou « je confirme », quel que soit le réglage.
5. Personne ne dit oui à sa propre demande quand c'est à un adulte ou au propriétaire de le faire.
6. Un lien, un jeton ou un formulaire n'est jamais stocké ni donné au modèle.
7. Un mot reçu n'atteint jamais un modèle ; seul son auteur le relit avec l'assistant, dans sa propre conversation (D4).
10. Le rôle retenu par une suite ne vaut que pour les étapes demandées quand la personne l'avait, telles qu'elle les avait demandées.
8. L'audit ne porte que des identifiants, des noms, des rôles et des nombres.
9. Un changement garde le même outil et le même type d'action, et repasse la barrière.

## 7. Textes à écrire

- Les descriptions des outils pour le modèle (anglais) :
  - la suite : de 1 à 20 étapes, l'attente d'une arrivée, la mise à plat ;
  - `chain_continue` : reprendre l'étape échouée, avec ou sans changement ;
  - `confirmation_amend` : changer une suite, et le oui de l'approbateur avec sa note pour le demandeur ;
  - le mot pour soi ;
  - le retrait de soi ;
  - le séjour d'un invité ;
  - défaire un décès ;
  - garder les mots d'un invité.
- `prompts/system.md` (moins de 6 000 caractères) : demander des précisions plutôt que deviner ; les demandes qui attendent d'autres personnes.
- Retirer de `prompts/rights_writer.md` la phrase qui interdit le mot pour soi.
- Les cartes en français et en anglais : une suite en pause, « Paul a rejoint le foyer », un bouton « Montrer le lien » par étape, le séjour prolongé, le décès défait, le lien de retour remplacé.
- Les README (français et anglais).

## 8. Scénarios qui doivent passer

1. **Changer une suite.** Une suite de deux étapes attend. La personne dit « oui, mais 5 jours ». L'assistant donne la suite changée, qui tourne tout de suite. Si une étape demande « je confirme », une nouvelle carte remplace l'ancienne.
2. **Le réglage des suites.** Le propriétaire choisit « un oui en mots me suffit » pour les suites. Une suite avec une étape vérifiée à son tour passe sur un « oui ». Une suite avec une transmission demande toujours « je confirme ».
3. **Quand il sera là.** « Invite Paul comme adulte et donne-lui mon rôle quand il sera là » :
   - une carte, « je confirme » ;
   - l'invitation est faite, et la suite se met en pause ;
   - Paul accepte, et une carte « Paul a rejoint le foyer. Je lui donne ton rôle ? » arrive au propriétaire ;
   - au « je confirme », Paul est propriétaire.
   - Variantes : le lien expire, et la suite s'arrête, l'assistant est réveillé ; l'application redémarre pendant l'attente, et la suite reprend quand même.
4. **Suivre une étape vérifiée à son tour.** « Passe Paul adulte, donne-lui mon rôle, puis retire-moi » est acceptée et se fait en entier.
5. **Reprendre l'étape échouée.** Une invitation échoue, car le nom est pris. « Reprends avec Léa B. » donne une carte pour le reste, puis Léa B. est invitée. Sans changement, une étape échouée pour une panne passagère est rejouée sous le même oui.
6. **Deux liens.** « Fais revenir Alex et invite Léa » : deux boutons « Montrer le lien » sur la carte, et le modèle n'a aucun des deux liens.
7. **Le oui du père.** Léa (enfant) demande d'inviter Tom pour 7 jours. Le père dit à l'assistant « oui, mais 3 jours, pas plus ». L'invitation est faite pour 3 jours, et l'assistant peut le dire à Léa.
   - Léa répond « non, je veux 5 jours ». L'assistant sait que son père a été ferme, et le lui explique sans redemander.
   - Avec un père qui avait dit « 3 jours, on verra », l'assistant peut faire une nouvelle demande de Léa, qui attend le oui du père.
8. **La demande d'un enfant.** L'enfant voit sa demande qui attend, et dit « laisse tomber » : elle est abandonnée.
9. **Séjour, décès, mot, retrait.**
   - « Léa reste jusqu'à dimanche » : la nouvelle fin est enregistrée.
   - « Paul n'est pas mort » : au « je confirme », Paul est remis sur la liste.
   - « Laisse-moi un mot » : le mot est sur sa page.
   - Un adulte dit « retire-moi » : au oui, il a quitté le foyer.
   - Un adulte dit « retire Paul » : c'est refusé.
   - « Garde mes mots pour Léa, elle revient dans un mois » : Léa part, ses mots restent ; elle revient avant la date et les retrouve. Sans retour à la date, ils sont effacés.

## 9. Mes choix, à vérifier par le propriétaire

- Une étape ajoutée à une suite par un « oui, mais » donne une nouvelle carte ; changer, retirer ou réordonner peut tourner tout de suite.
- Le rôle retenu par une suite ne vaut que pour les étapes demandées quand la personne l'avait ; une étape changée ou ajoutée est jugée avec le rôle du moment.
- Le style d'une suite compte chaque étape à son propre niveau. Pour une étape vérifiée à son tour : le réglage de son type, sinon celui des suites, sinon comme en 0.3.0.
- La reprise après une arrivée se fait au plus une minute après, même après un redémarrage, et le délai de vie de la suite repart de l'arrivée.
- Une suite en pause vit tant que son invitation peut être acceptée.
- Une étape échouée est rejouée sous le même oui seulement si on sait qu'elle n'a rien fait. Si on ne le sait pas, elle demande un nouveau oui.
- Une étape reprise avec un changement demande un nouveau oui pour le reste, sauf si tout ce reste est libre.
- Le formulaire de la clé ne s'ouvre que pour celui qui demande.
- Un mot pour soi peut être relu par l'assistant à son auteur, comme ses autres mots.
- De 1 à 20 étapes par suite.
- Une suite dans une suite est mise à plat ; une suite relancée garde son propre oui.
- Avec le « oui, mais… » d'un approbateur, l'assistant note en quelques mots ce qu'il a dit et à quel point il était ferme, pour juger si le demandeur conteste.
- Une relance vers l'approbateur l'attend sur sa page et dans sa prochaine conversation.
- Défaire un décès demande le oui explicite.
- Pour un enfant qui veut se retirer, c'est un adulte qui dit oui.
- Garder les mots d'un invité : l'auteur seul, pour ses propres mots, jusqu'à un an au plus.

## 10. Laissé au constructeur

- Les noms des outils, des actions, des colonnes, des statuts, et les numéros de migration.
- Comment une étape de suite garde son niveau, et comment une suite en pause est rangée.
- Comment la reprise après une arrivée est déclenchée (une passe régulière, comme l'effacement des mots, convient).
- La forme des boutons « Montrer le lien » d'une suite.
- Comment le rédacteur de droits est relancé pour un « oui, mais… ».
