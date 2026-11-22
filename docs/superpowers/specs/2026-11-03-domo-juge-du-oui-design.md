# L'assistant juge du oui

Conception du 3 novembre 2026, après la fusion de la version 0.4.0. C'est le premier des six chantiers décidés le même jour, dans l'ordre accepté par le propriétaire *(propriétaire)* :
1. L'assistant juge du oui (ce document), avec trois corrections venues des essais de la 0.4.0 (§5.7) ;
2. L'assistant choisit son modèle selon le budget : le propriétaire ne donne que le fournisseur et la clé (ajouté après les essais de la 0.4.0) ;
3. la mémoire et la marche en permanence ;
4. les connecteurs et les initiatives ;
5. les serveurs et les applications ;
6. les micros et les caméras, avec la coupure.

Elle s'appuie sur :
- `docs/superpowers/specs/2026-10-28-domo-architecture-design.md` (l'architecture, surtout §7.4, §10.4 et §10.5) ;
- `docs/superpowers/specs/2026-10-31-droits-retour-mots-design.md` (droits, limites, barrière) ;
- `docs/superpowers/specs/2026-11-01-actions-enchainees-depart-design.md` (suites, réveil, transmission, successeurs) ;
- `docs/superpowers/specs/2026-11-02-sans-refus-de-commodite-design.md` (les 17 chemins).

Elle part d'un inventaire, fait sur la 0.4.0, des 163 endroits du code qui décident, imposent ou redemandent un oui. Chacun y est rangé : droit, secret, décidé par le propriétaire, intégrité du mécanisme, ou à juger. L'annexe A dit ce qui passe au juge.

Ce qui vient du propriétaire est marqué *(propriétaire)*. Ce que j'ai proposé et qu'il a accepté est marqué *(proposé, accepté)*. Ce que je décide seul est marqué *(mon choix)* et repris au §9.

## 1. Intention

En relisant les décisions de la 0.4.0, le propriétaire a relevé trop de « Coût : un oui de plus ». Il a dit : « c'est à sa réflexion et en fonction de ce qui est déjà arrivé de décider s'il faut redemander un oui ou pas ». L'assistant est « censé être une IA qui peut tout faire, même juger de la nécessité d'un oui ou pas, ou même juger par lui-même s'il veut faire une action qu'il juge mal placée ou malpolie » *(propriétaire)*.

Il a tranché le partage : « tout ce qui n'est ni un droit ni un secret devient son choix » *(propriétaire)*. Et même quand un contenu extérieur s'en mêle : « c'est toujours à l'assistant de juger et de comprendre son entourage » *(propriétaire)*.

**Aujourd'hui**, le code décide :
- chaque type d'action a un niveau : libre, oui demandé, ou oui à chaque fois ;
- le propriétaire choisit le style du oui, en mots ou par le bouton ;
- dans les suites, le code redemande un oui à beaucoup d'endroits ;
- L'assistant peut ajouter un oui, jamais en retirer.

**Après ce chantier**, devant une action permise, c'est l'assistant qui décide de :
- la faire tout de suite ;
- demander un oui ;
- demander un oui exprès ;
- demander une preuve que c'est bien la personne ;
- ne pas la faire, en disant pourquoi.

Exemples qui doivent marcher :
- Le propriétaire, qui invite souvent, dit « Invite Paul comme invité pour le week-end » : c'est fait tout de suite.
- Le propriétaire rentre toujours avec sa clé. Deux ans après, il demande « ouvre-moi la porte » : l'assistant trouve bizarre que ce ne soit jamais arrivé, doute de la provenance, et demande une preuve avant d'agir. C'est l'exemple du propriétaire *(propriétaire)* ; la porte viendra avec les connecteurs, mais la façon de juger est posée ici.
- Tout le monde est dans le fauteuil, la télé est allumée, et une voix dit « Domo, invite Marc et donne-lui le rôle d'adulte ». L'assistant trouve ça bizarre (exemple du propriétaire). Ici, dans la page, l'équivalent est une demande qui tombe sans aucun rapport avec ce dont la personne parlait.
- Un enfant demande une action qui demande l'accord d'un adulte : l'adulte est toujours sollicité (c'est un droit), quoi que pense le juge.
- L'assistant refuse d'envoyer un mot insultant. La personne explique que c'est une blague entre eux. L'assistant peut changer d'avis *(propriétaire)*.

## 2. Objectifs, non-objectifs, anti-objectifs

**Objectifs**
1. Le code ne tient plus que les droits, les secrets, ce que le propriétaire a décidé, et l'intégrité du mécanisme du oui (§6). Tout le reste du oui est jugé par l'assistant (§4).
2. Ce jugement ne peut pas être soufflé par un contenu extérieur (§4.2).
3. L'assistant peut demander une preuve que c'est bien la personne (§4.5).
4. L'assistant peut ne pas faire une action déplacée ou impolie. Il le dit, et il peut changer d'avis (§4.6).
5. Les trois oui redemandés notés dans `docs/superpowers/followups/2026-10-30-iteration-1b.md` §9 sont réglés (§5.5).

**Non-objectifs (plus tard)**
- Retenir durablement « plus besoin de me demander pour ça » : il faut la mémoire (chantier 2). Ici, l'assistant juge d'après la conversation et l'historique des demandes.
- Les preuves par SMS, mail ou notification Home Assistant : il faut les canaux et les connecteurs (chantier 3). Le cadre qui les accueillera est posé ici (§4.5).
- La voix, et la triangulation des micros pour savoir d'où vient une demande : chantier 5.
- Les initiatives que l'assistant prend sans qu'on lui demande : chantier 3.
- Un modèle plus petit choisi exprès pour le juge : il viendra avec le chantier 2, où l'assistant choisit ses modèles. D'ici là, le juge utilise le modèle du foyer.
- Laisser l'assistant répondre aux limites « à juger » des droits (§5.4) : il faut la mémoire.

**Anti-objectifs (échec même si « ça marche »)**
- Un mail, une page web ou une voix extérieure qui décide à la place du foyer qu'un oui est inutile.
- Un droit contourné parce que le juge a dit « fais-le ».
- Un secret vu par le juge.
- Un assistant qui demande un oui pour tout « par prudence » : ce serait revenir aux règles figées.
- Un refus sec, sans raison et sans possibilité d'en parler.

## 3. Approches

**A. L'assistant décide dans la conversation.** Le modèle qui parle avec la personne dit lui-même, à chaque action, s'il faut demander. C'est le plus simple et le moins cher. Mais c'est ce même modèle qui lira les mails et les pages web (chantier 3) : un texte piégé pourrait lui souffler sa décision. Écartée.

**B. Un juge du oui à part.** Une partie séparée de l'assistant, qui ne voit jamais de contenu extérieur, décide pour chaque action qui change quelque chose. L'assistant qui parle peut demander plus que le juge, jamais moins. Elle reprend la façon dont l'assistant comprend déjà un oui aujourd'hui (un appel à part du modèle, qui ne voit que la réponse de la personne). Elle coûte un appel au modèle de plus par action qui change quelque chose. **Retenue** *(propriétaire)*.

**C. Garder des niveaux dans le code, que l'assistant assouplit en apprenant** (« après 5 oui, il ne demande plus », §10.4 de l'architecture). C'est encore une règle figée, contraire à ce que veut le propriétaire. Écartée.

## 4. Le juge du oui

### 4.1 Quand il est consulté

- Pour chaque action qui change quelque chose, **après la barrière**. Si la barrière refuse, rien ne se fait et le juge n'est pas consulté. Si elle permet, ou si elle demande l'accord de quelqu'un d'autre, le juge est consulté.
- Les actions qui ne changent rien passent sans juge *(mon choix)* :
  - lire : `household.list`, `note.list_sent`, `policy.show`, `successors.show` ;
  - préparer un brouillon de droits qui n'est encore appliqué à personne : `rights.propose` ;
  - les outils de l'assistant sur les demandes : `confirmation.remind`, `confirmation.expire`, `confirmation.require_explicit`.
  
  Une action changée par `confirmation.amend` est jugée comme une action.
- **Une suite est jugée en entier**, une fois, au moment où elle est demandée : une carte, un verdict. Elle est jugée de nouveau quand on la change ou quand on la reprend (§4.7).
- **Aux endroits où le code redemandait un oui** (un changement, une reprise, une étape qui a peut-être agi), le juge est consulté avec l'histoire de la demande (§4.7).

### 4.2 Ce qu'il voit, et ce qu'il ne voit jamais

**Il voit :**
- l'action exacte, décrite comme la barrière la voit (type, cible, faits) ; pour une suite, toutes ses étapes ; pour un mot, le texte du mot, puisque ce sont les mots de la personne ;
- qui demande (nom, rôle), et ce que disent les droits : permis, ou accord de qui ;
- les mots de la personne dans la conversation en cours : ses derniers messages seulement ;
- l'historique de la personne pour ce genre d'action :
  - combien de fois elle l'a fait, sur un jour, une semaine, un mois et depuis toujours ;
  - la dernière fois ;
  - combien de ses demandes de ce genre ont été refusées ou abandonnées ;
  - et, quand c'est le cas, que c'est la première fois ;
- la situation que le code connaît :
  - l'heure et le jour (fuseau du foyer) ;
  - qui fait partie du foyer ;
  - les demandes de la personne qui attendent ;
- pour un changement ou une reprise :
  - quand et comment le oui a été donné, et par qui ;
  - les étapes faites ;
  - si l'étape qui a échoué a peut-être agi (c'est un fait du code, §6.4) ;
  - ce qui a changé.

**Il ne voit jamais :**
- les réponses de l'assistant ni les résultats des outils ;
- un contenu extérieur (aujourd'hui, il n'en entre pas encore ; demain : mails, pages web, télé) ;
- un secret ;
- les mots privés des autres personnes.

Comme le classifieur du oui (`confirmations/intent.py`), c'est un appel à part du modèle, avec sa propre consigne, qui traite tout ce qu'il lit comme des données et jamais comme des ordres. Ce qu'il voit est construit par le code à partir de la base, jamais copié de ce que le modèle qui parle a lu.

### 4.3 Ce qu'il répond

Un verdict parmi cinq, avec une raison courte :
- **faire** : l'action se fait tout de suite, sauf si les droits demandent l'accord de quelqu'un d'autre ;
- **oui** : la personne répond avec ses mots ou le bouton, comme aujourd'hui ;
- **oui exprès** : le bouton, ou « je confirme » ;
- **preuve** : la personne prouve que c'est bien elle (§4.5), puis l'action se fait ;
- **ne pas faire** : l'assistant le dit à la personne avec la raison (§4.6).

La raison est gardée dans le journal d'audit. Elle est montrée au modèle qui parle, pour qu'il l'explique avec ses mots.

Si le juge ne répond pas (erreur, délai dépassé, réponse illisible), le verdict est **oui** *(mon choix)*.

### 4.4 Le plus exigeant gagne

Les verdicts sont rangés : faire, puis oui, puis oui exprès, puis preuve. Ce qui est demandé à la fin est le plus exigeant de :
- le verdict du juge ;
- les règles du propriétaire, de la forme « demande-moi toujours avant de… » (§6.3) ;
- ce que demande l'assistant qui parle : il peut demander plus (un oui, un oui exprès, une preuve), jamais moins *(proposé, accepté)*.

Les droits ne se rangent pas dans cette échelle, ils passent avant tout. Quand ils demandent l'accord d'un adulte ou du propriétaire, la demande attend toujours cet accord, même si le juge dit « faire ». Le verdict dit alors comment cette personne-là doit dire oui : « faire » veut dire un oui simple.

**Quand un accord est nécessaire.** Les accords nécessaires restent exactement ceux d'aujourd'hui, écrits comme des droits et non plus déduits d'un niveau *(mon choix)* :
- pour un enfant, l'accord d'un adulte pour les mêmes types d'action qu'aujourd'hui, c'est-à-dire ceux qui n'étaient pas libres (aujourd'hui : partir du foyer, et les étapes de ce genre dans une suite) ; un mot ou un nouveau nom reste sans adulte ;
- un droit qui nomme la personne dont l'accord est nécessaire, quand ce droit demandait un oui ;
- une limite qui envoie au propriétaire.

Quand l'accord nécessaire est celui de la personne qui demande (le propriétaire sous une limite qui envoie au propriétaire), le fait de demander vaut accord, et le juge décide du reste *(mon choix)*.

**Quand un accord est nécessaire et que le juge dit « ne pas faire ».** C'est le jugement de l'assistant : il peut refuser tout de suite, sans déranger l'adulte, ou laisser la demande aller à l'adulte. Le propriétaire l'a déjà décidé ainsi pour la 0.4.0 : l'assistant juge, selon la réponse de l'adulte et la façon dont il a répondu, s'il faut redemander à l'adulte ou refuser directement la demande de l'enfant *(propriétaire, 2026-11-02)*. Si l'enfant insiste pour que l'adulte décide, le juge voit ses mots et peut laisser la demande partir.

**Ne pas faire** n'est pas sur l'échelle. Quand le juge le dit, l'action ne se fait pas, et l'assistant qui parle ne peut pas passer outre. La personne peut en parler (§4.6).

### 4.5 La preuve que c'est bien la personne

- **Les moyens de preuve ne sont pas figés** *(propriétaire)*. L'assistant choisit parmi ceux qu'il a, selon la personne et la situation, et il en met en place d'autres quand il en a les moyens. Le propriétaire cite :
  - un SMS ;
  - un mail ;
  - une notification envoyée par Home Assistant ;
  - une application faite exprès, peut-être un jour.
- **Ce chantier construit le cadre** : une preuve est un moyen parmi d'autres, et les moyens s'ajoutent sans toucher au juge. Il livre les deux moyens qui existent déjà sur la page :
  - la clé d'accès de l'appareil (empreinte, visage ou code de l'appareil), si la personne en a une ;
  - son mot de passe.
  
  Les autres viendront avec les canaux et les connecteurs (chantier 3), et la voix avec le chantier 5.
- **Ce que le code garde, quel que soit le moyen** :
  - la preuve est vérifiée par le code, jamais par le modèle ;
  - un code reçu par SMS, par mail ou par notification se tape dans un champ de la carte, ou se valide depuis la notification, jamais dans la conversation. L'assistant ne le voit pas, pas plus que le mot de passe (§6.2) ;
  - un moyen qui envoie quelque chose (SMS, mail, notification) n'envoie qu'à une adresse, un numéro ou un appareil que la personne a enregistré elle-même, jamais à une destination venue de la conversation ou d'un contenu extérieur. Sinon, quelqu'un pourrait se faire envoyer la preuve d'un autre.
- L'action se fait juste après la preuve. Une preuve vaut pour cette demande seulement.

### 4.6 Ne pas faire, et en parler

- L'assistant qui parle peut toujours ne pas faire ce qu'il trouve déplacé ou impoli : il n'appelle pas l'outil, et il le dit poliment, avec sa raison *(propriétaire)*.
- Quand c'est le juge qui dit « ne pas faire », l'assistant le dit aussi, avec la raison du juge dite à sa façon.
- La personne peut répondre et argumenter : pour le propriétaire, ça fait partie de la base d'une conversation fluide et naturelle *(propriétaire)*. Si l'assistant est convaincu, il refait la demande. Le juge voit alors les nouveaux mots de la personne et peut changer d'avis.
- Il n'y a pas de liste d'actions interdites par prudence.
- Un refus laisse une ligne dans l'historique de la personne (« pas fait : <raison> ») et une entrée dans le journal d'audit *(mon choix)*.

### 4.7 Les suites, les changements et les reprises

- **Une suite** reste une seule demande : une carte, un verdict pour toutes ses étapes.
- **Une étape vérifiée à son tour** n'est plus forcée à « oui à chaque fois » : le juge la voit décrite en mots, comme les autres étapes.
- **Une suite qui attend une arrivée** n'est plus forcée à demander un oui. Elle garde une demande enregistrée, même sans oui, pour que la passe de l'entretien puisse la reprendre.
- **Pendant le déroulement**, chaque étape repasse la barrière, comme aujourd'hui. Une suite partie sans oui qui rencontre une étape pour laquelle les droits demandent l'accord de quelqu'un d'autre s'arrête comme aujourd'hui. C'est un droit.
- **Changer une suite** (« oui, mais… ») : le juge est consulté sur la nouvelle version, avec ce qui a changé, et il décide si le oui déjà donné suffit. Une étape gardée garde le rôle retenu (droit, §6.1).
- **Reprendre une suite arrêtée** (`chain_continue`) : le juge décide, avec l'histoire de la demande, si on reprend sous le même oui ou s'il en faut un nouveau. Le code continue de dire, comme un fait, si l'étape qui a échoué a peut-être agi.
- **À une arrivée** :
  - la transmission du rôle et la liste des successeurs redemandent leur oui exprès, comme le propriétaire l'a décidé pour la 0.4.0, sauf s'il a levé ce oui (§6.3) ;
  - les autres étapes continuent sous le oui déjà donné, comme aujourd'hui.
- **« Annule »** n'a pas de mécanisme à part *(mon choix)*. L'assistant fait l'action inverse : retirer le mot, annuler l'invitation, rendre l'ancien rôle… Elle est jugée comme toute action.

### 4.8 L'assistant qui parle

- Il ne déduit plus d'un niveau qu'il faut un oui : les descriptions d'outils ne disent plus « attend un oui ». Il dit ce qui s'est passé : fait, attend un oui, attend une preuve, ou pas fait et pourquoi.
- Il garde `confirmation_require_explicit` (demander un oui exprès sur une demande qui attend). Il gagne le moyen de demander, sur une action qu'il appelle, un oui, un oui exprès ou une preuve, avec une raison.
- Il ne demande plus au propriétaire comment confirmer les actions importantes (le style) : c'est le juge.
- Le texte fixe (`prompts/system.md`) dit :
  - L'assistant converse de façon fluide et naturelle et juge en temps réel ;
  - un refus se discute ;
  - le juge décide du oui, et l'assistant peut demander plus, jamais moins ;
  - « défaire » une suite passe par de nouvelles actions, jugées comme les autres. Aujourd'hui, le texte dit que chacune demande son oui.

## 5. Ce qui change dans le code

### 5.1 Les niveaux et les styles ne décident plus

- **Le catalogue** (`policy/catalog.py`) garde :
  - qui peut faire quoi ;
  - les actions du propriétaire seul ;
  - les actions à oui exprès toujours (`ALWAYS_EXPLICIT_ACTIONS`) ;
  - les limites par défaut.
  
  Il gagne une marque « ne change rien » (§4.1), et, pour chaque type, si l'action d'un enfant demande l'accord d'un adulte (§4.4 : les mêmes types qu'aujourd'hui). Les niveaux par type ne décident plus du oui.
- **Les styles par défaut** (`policy/settings.py`) disparaissent, et `MUST_BE_DECIDED` avec eux : l'assistant ne demande plus au propriétaire de choisir un style.
- **Les anciens réglages de niveau et de style sont oubliés** : styles par niveau, exceptions par type, style des suites (`chain.run`), et niveau écrit dans un droit *(propriétaire)*. L'assistant n'est encore installé nulle part, il n'y a donc rien à reprendre *(propriétaire)*. Un droit qui demandait l'accord de quelqu'un garde cet accord (§4.4).
- **La barrière** répond seulement : refusé, permis, ou accord de quelqu'un d'autre (avec qui). Elle ne répond plus « il faut un oui de tel niveau ».

### 5.2 Une demande garde ce qui est demandé

- Une demande enregistre ce qui est demandé (oui, oui exprès, preuve), qui l'a demandé (le juge, une règle du propriétaire, l'assistant qui parle) et la raison.
- La colonne `confirmations.level`, qui n'accepte que `confirm` ou `always_confirm`, change avec une migration.
- La migration n'a pas à garder les demandes en attente ni les anciens réglages : l'assistant n'est encore installé nulle part *(propriétaire)*. Elle doit seulement marcher sur une base de développement.
- La version passe à 0.5.0.

### 5.3 Le juge

- Un module à part, sur le modèle de `confirmations/intent.py`, avec sa consigne dans `prompts/`.
- C'est un appel isolé au modèle du foyer :
  - pas d'outils, pas d'historique de la conversation ;
  - une réponse courte, lue strictement : un des cinq verdicts et une raison ;
  - tout le reste vaut « oui ».
- Ce qu'il voit est construit par le code à partir de la base (§4.2). Les messages de la personne sont pris parmi ceux dont la provenance est `person`.
- L'historique gagne :
  - le nombre d'actions « depuis toujours » et la date de la dernière, à côté des comptes sur un jour, une semaine et un mois (`policy/facts.py`) ;
  - le nombre de demandes refusées ou abandonnées, lu dans `confirmations`.
  
  Il faut un index pour que ce soit rapide.

### 5.4 Les limites « à juger »

Une limite des droits peut répondre « à juger », avec une question (par exemple « seulement ses amies »). Faute de mémoire, cette question part aujourd'hui au propriétaire. **Ça ne change pas dans ce chantier** *(mon choix)*. La question porte sur un droit, et l'assistant ne connaît pas encore les liens entre les gens. Le propriétaire a déjà dit que, quand l'assistant saura (mémoire), il répondra seul, et qu'en cas de doute il demandera au propriétaire *(propriétaire, 2026-10-31)*.

### 5.5 Les trois oui redemandés

1. **Le reste d'une suite d'enfant** attend encore l'adulte, même quand tout ce qui reste est libre. L'accord nécessaire est redemandé à la barrière, au lieu d'être recopié de l'ancienne demande (droit). L'attente d'un oui, elle, est décidée par le juge.
2. **Une étape `model_rekey` dans une suite** ne fait que laisser un bouton : elle ne touche jamais au coffre. Elle est donc comptée comme « sûrement pas faite » quand elle échoue (fait du code).
3. **Annuler un décès qui n'a pas été noté** est vérifié avant de demander quoi que ce soit, au lieu d'échouer après le oui exprès.

### 5.6 Les textes

À réécrire :
- le texte fixe (§4.8) ;
- les descriptions des outils qui disent « attend un oui » (inviter, changer un rôle, retirer, partir, faire revenir, la clé du modèle), celle de `policy_update` sur les styles, celles de `do_in_order`, `chain_continue` et `confirmation_amend` ;
- la consigne du rédacteur des droits : il ne choisit plus de niveau de lui-même, seulement quand le propriétaire en dit un ;
- le contexte « le rédacteur des droits… ne le change jamais sans leur oui » ;
- les README (anglais et français).

### 5.7 Trois corrections venues des essais de la 0.4.0

Le propriétaire a essayé la 0.4.0 le 3 novembre. Trois de ses remarques entrent dans ce chantier, pour être livrées d'un bloc *(proposé, accepté)* :

1. **Entrée envoie le message, Maj+Entrée passe à la ligne** *(propriétaire)*. Une saisie en cours de composition (accents, clavier de langue asiatique) n'envoie rien *(mon choix)*.
2. **L'assistant finit sa réponse même si la personne quitte la page** *(propriétaire)*. Changer d'onglet, fermer la page ou perdre la connexion n'arrête plus la réponse. Aujourd'hui, c'est un choix de départ : quand la page se ferme, le modèle s'arrête (`chat/routes.py`, `chat/sse.py`). Désormais, la réponse tourne sans la page, comme un réveil de l'assistant après un bouton (`chat/wake.py`), et elle est gardée. Quand la personne revient, elle voit la réponse finie. Si la réponse n'est pas encore finie, la page montre que l'assistant écrit, et la réponse apparaît dès qu'elle est finie *(mon choix)*. La page la montre déjà ainsi pour un réveil.
3. **Les textes parlent à qui les lit** *(propriétaire)*. Au propriétaire, l'assistant dit « tu », jamais « le propriétaire » à la troisième personne. La phrase « Pour les actions les plus importantes, tant que le propriétaire n'a pas décidé autrement » n'était pas un de nos textes. C'est le modèle qui reprenait nos consignes et nos descriptions d'outils, écrites à la troisième personne. Le texte fixe dit donc au modèle de s'adresser à la personne qui lit quand c'est d'elle qu'il parle, et les descriptions d'outils réécrites au §5.6 sont relues dans ce sens. Les textes du code (cartes, lignes d'historique) gardent une version pour le lecteur.

## 6. Le socle (ce qui ne change pas)

### 6.1 Les droits

Restent dans le code :
- la barrière, les droits, les rôles ;
- les actions du propriétaire seul ;
- les limites, y compris celles qui envoient au propriétaire ;
- l'accord d'un adulte pour un enfant ;
- le rôle retenu par une suite pour les étapes demandées quand la personne l'avait ;
- les refus fixes : le rôle de propriétaire jamais donné par erreur, personne ne change son propre rôle, personne n'agit sur le propriétaire, le propriétaire ne part pas sans donner son rôle, les noms qui distinguent les gens.

**Le juge ne transforme jamais un refus ou un accord nécessaire en « faire ».**

### 6.2 Les secrets

- L'assistant ne voit jamais un secret.
- Le formulaire de la clé du modèle ne s'ouvre que pour la personne.
- Le mot de passe d'une preuve se tape hors de la conversation.
- Le juge ne reçoit aucun secret.

### 6.3 Ce que le propriétaire a décidé

Par défaut, ces oui sont demandés :
- **Donner son rôle, la liste des successeurs, annuler un décès** : un oui exprès à chaque fois, redemandé à une arrivée *(propriétaire)*.
- **Appliquer des droits** (`rights.apply`), y compris une règle « demande-moi toujours » : le oui du propriétaire, qui lit les exemples avant que quoi que ce soit change *(propriétaire, 2026-10-31)*.

**Tout cela s'adapte aux demandes du propriétaire** : « tout doit pouvoir être adapté par l'assistant en fonction des demandes du propriétaire » *(propriétaire, 2026-11-03)*. Pour chacun de ces oui, le propriétaire peut dire à l'assistant de ne plus le lui demander *(propriétaire)* :
- quand l'assistant juge avoir bien compris ;
- sous certaines conditions, qu'il dit avec ses mots (par exemple « seulement pour les invités », « sauf si ça donne plus de droits à un enfant ») ;
- ou plus du tout.

Le juge décide alors, avec ces conditions sous les yeux. Une condition est une consigne du propriétaire, pas un contenu extérieur. Avec « plus du tout », le juge ne demande plus de oui pour s'assurer que le propriétaire le veut bien. Il peut encore demander une preuve s'il doute que ce soit vraiment le propriétaire qui parle *(mon choix)* : c'est la seule chose qui protège encore la transmission du rôle quand le oui exprès est levé.

Le changement qui lève un de ces oui est lui-même un changement de droits : il passe par le oui du propriétaire la première fois. Le propriétaire peut remettre son oui quand il veut, par le même chemin.
- **« Demande-moi toujours avant de… »** : une règle que le propriétaire pose, en mots, pour un type d'action, éventuellement pour une personne ou un rôle. Elle dit ce qu'il faut demander (oui, oui exprès, preuve), et le code la fait respecter *(proposé, accepté)*.
- **Les plafonds de dépense**, plus tard : ce sont des limites, donc des droits *(proposé, accepté)*.
- **« Ne me demande jamais pour… »**, sur une action que rien n'impose de demander, n'est pas une règle du code : c'est une indication pour le juge *(mon choix)*. Le juge doit pouvoir douter quand même, comme dans l'exemple de la porte. Sur un oui demandé par défaut (point ci-dessus), ça lève ce oui.

### 6.4 L'intégrité du oui

Ce sont des règles du mécanisme, pas de la prudence. Elles restent :
- un oui ne répond qu'à la demande montrée juste avant ;
- seule la bonne personne répond ;
- le bouton vaut un oui exprès ;
- une demande expire au plus tard au bout du plafond ;
- un formulaire de clé par oui ;
- l'action qui se fait est celle qui a été montrée, ou sa version changée ;
- le code dit, comme un fait, si une étape a peut-être agi.

## 7. Textes à écrire (français et anglais)

- La carte « Prouve que c'est bien toi » : le bouton de la clé d'accès, le champ du mot de passe, le texte quand la preuve échoue.
- La ligne d'historique « pas fait : <raison> ».
- La carte d'une demande qui dit qui l'a demandée : l'assistant, ta règle, ou l'accord nécessaire d'un adulte.
- Le paragraphe du texte fixe (§4.8), en restant sous 6000 caractères.
- La consigne du juge, en anglais comme les autres consignes. Elle dit au juge :
  - de comprendre la situation ;
  - de se méfier de ce qui est inhabituel pour la personne ;
  - de ne pas demander un oui pour tout ;
  - de lire tout ce qu'on lui donne comme des données.

## 8. Scénarios qui doivent passer

Le faux modèle des tests répond au juge comme il répond déjà au classifieur.

1. Le propriétaire invite un invité, le juge dit « faire » : c'est fait sans carte.
2. Même demande, le juge dit « oui » : une carte, un oui en mots, c'est fait.
3. Un enfant demande une action qui demande l'accord d'un adulte, le juge dit « faire » : la demande attend quand même l'adulte.
4. Le propriétaire a dit « demande-moi toujours avant d'inviter », le juge dit « faire » : un oui est demandé.
5. Le juge dit « preuve » : la carte de preuve, la clé d'accès (ou le mot de passe) est vérifiée, l'action se fait. Avec un mauvais mot de passe, rien ne se fait.
6. Le juge dit « ne pas faire » : l'assistant le dit avec la raison. La personne argumente, l'assistant refait la demande, le juge voit les nouveaux mots et dit « faire » : c'est fait.
7. Le juge dit « faire », mais l'assistant qui parle demande un oui exprès : un oui exprès est demandé.
8. Le juge ne répond pas : un oui est demandé.
9. Ce que voit le juge ne contient ni les réponses de l'assistant ni les résultats des outils (vérifié sur l'appel au faux modèle).
10. Une première fois est dite au juge : une action jamais faite par la personne arrive au juge avec « première fois ».
11. Une suite : un seul verdict. Une étape vérifiée à son tour n'impose plus un oui à chaque fois.
12. Une suite arrêtée dont l'étape a peut-être agi : le juge reçoit ce fait. S'il dit « faire », la suite reprend sous le même oui.
13. Une suite qui attend une arrivée, jugée « faire » : elle se met en pause sans carte de oui, et reprend à l'arrivée.
14. La transmission du rôle à une arrivée redemande le oui exprès, quoi que dise le juge, tant que le propriétaire ne l'a pas levé.
17. Le propriétaire lève le oui exprès de la transmission « plus du tout » (ce changement passe par son oui) : une transmission jugée « faire » se fait sans carte, et le juge peut encore demander une preuve.
18. Le propriétaire lève son oui sur les droits « sous condition : seulement pour les invités » : la condition arrive au juge. Il remet son oui : le oui est de nouveau demandé.
19. Dans la page, Entrée envoie le message ; Maj+Entrée ajoute une ligne sans envoyer.
20. La personne envoie un message puis ferme la page avant la fin de la réponse : la réponse se finit quand même. Quand elle revient, elle la voit en entier.
21. Le texte fixe dit au modèle de s'adresser au propriétaire par « tu » quand c'est lui qui lit, et aucune description d'outil ne lui fait dire « le propriétaire » à la troisième personne quand il parle au propriétaire.
15. Les trois oui redemandés du §5.5 ne sont plus redemandés à tort.
16. Le propriétaire dit « demande-moi toujours un oui exprès avant de retirer quelqu'un » : la règle est enregistrée, et un retrait jugé « faire » demande quand même un oui exprès. Les anciens réglages de style ne s'appliquent plus.

## 9. Mes choix, à vérifier par le propriétaire

1. Les actions qui ne changent rien passent sans juge : lire, préparer un brouillon de droits, les outils de l'assistant sur les demandes (§4.1).
2. Si le juge ne répond pas, un oui est demandé (§4.3).
3. Les moyens de preuve sont ouverts (le propriétaire l'a voulu). Mes choix : ce chantier livre la clé d'accès et le mot de passe ; un code reçu se tape dans la carte, jamais dans la conversation ; un moyen qui envoie quelque chose n'envoie qu'à une destination enregistrée par la personne (§4.5).
4. Un refus laisse une ligne dans l'historique de la personne et une entrée dans le journal d'audit (§4.6).
5. « Annule » n'a pas de mécanisme à part : l'assistant fait l'action inverse, jugée comme les autres (§4.7).
6. Les limites « à juger » vont encore au propriétaire jusqu'à la mémoire (§5.4).
7. « Ne me demande jamais pour… » est une indication pour le juge, pas une règle du code (§6.3).
8. Le juge utilise le modèle du foyer tant que le routeur de modèles n'existe pas (§2).
9. Les accords nécessaires restent exactement ceux d'aujourd'hui, écrits comme des droits. Le propriétaire qui demande sous une limite qui l'envoie à lui-même a déjà donné son accord en demandant (§4.4).
10. Quand le propriétaire lève un oui « plus du tout », le juge peut encore demander une preuve s'il doute que ce soit vraiment le propriétaire qui parle (§6.3).
11. Entrée n'envoie rien pendant une saisie en cours de composition (accents, clavier de langue asiatique) (§5.7).

## 10. Laissé au constructeur

- La consigne exacte du juge et sa forme de réponse.
- Le nombre de messages de la personne que voit le juge.
- Le délai du juge, et la façon de garder son appel court.
- Le schéma exact des demandes et la migration.
- La façon dont l'assistant qui parle demande plus sur une action : un argument commun aux outils, ou un outil à part.
- Où ranger les règles « demande-moi toujours avant de… » : elles peuvent valoir pour tout le foyer, pour un rôle ou pour une personne, alors qu'un droit porte sur une personne ou un rôle. Ce sont les réglages du propriétaire, ou un rangement à part.
- La façon dont la carte de preuve parle à la clé d'accès (WebAuthn, avec vérification de la personne) et au mot de passe.
- Les endroits exacts du code où le juge remplace les niveaux (annexe A).
- L'index pour l'historique.
- La façon dont le faux modèle répond au juge dans les tests.

## Annexe A. Ce que le juge reprend

D'après l'inventaire de la 0.4.0. Les chemins sont relatifs à `packages/domo-core/src/domo_core/`.

| Où | Ce que le code décide aujourd'hui | Après |
|---|---|---|
| `policy/catalog.py` (niveaux des types) | libre, oui demandé, oui à chaque fois | le juge (sauf transmission, successeurs, annuler un décès, appliquer des droits : §6.3) |
| `policy/barrier.py` (choix du niveau) | « il faut un oui de tel niveau » | la barrière dit seulement permis, refusé, ou accord de qui |
| `policy/settings.py` (styles, `MUST_BE_DECIDED`, `chain.run`, `style_for_step`) | style par défaut, style des suites, style d'une étape | le juge, et les règles du propriétaire (§5.1) |
| `chat/actions.py` (création d'une demande) | une demande dès que le niveau n'est pas libre | une demande dès que le verdict n'est pas « faire » ou qu'un accord est nécessaire |
| `confirmations/intent.py`, `chat/routes.py` (un oui simple qui ne suffit pas) | le style exige le bouton | ce qui est demandé dit s'il faut un oui exprès |
| `confirmations/service.py` (rejeu, niveau plus haut, `required_style`, « oui, mais » qui devient une nouvelle demande) | le code redemande | le juge, avec ce qui a changé |
| `tools/chain.py` (une suite qui attend une arrivée, une étape vérifiée à son tour, le niveau le plus haut des étapes) | oui forcé, oui à chaque fois, le plus strict des niveaux | un verdict pour la suite |
| `confirmations/chain.py` (une suite partie sans oui rencontre une étape qui demande un oui) | arrêt | arrêt seulement si un accord est nécessaire (droit) |
| `confirmations/service.py`, `confirmations/replan.py` (suite changée : étape ajoutée, rendue sans changement, tout libre redemandé) | nouvelle demande, ancien niveau, ancien approbateur | le juge ; l'accord nécessaire redemandé à la barrière |
| `tools/chain.py` (`chain_continue` : même oui si sûrement pas fait, nouveau oui sinon) | la règle du code | le juge, avec le fait « a peut-être agi » |
| `confirmations/store.py` (un approbateur répond dans le style exigé) | le style | ce qui est demandé |
| `tools/confirmation_tools.py`, `prompts/system.md`, descriptions d'outils | « attend un oui », « chaque action demande son oui », demander le style au propriétaire | §4.8 et §5.6 |
