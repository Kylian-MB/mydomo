# Ajouts au 1b : retirer un mot, faire revenir quelqu'un, droits et limites décidés par le propriétaire

- Date : 2026-10-31
- Branche : `feat/iteration-1b`, avant sa fusion dans `main`
- Spec principale : `docs/superpowers/specs/2026-10-28-domo-architecture-design.md` (§6.1, §7.2, §10)
- Suites du 1b : `docs/superpowers/followups/2026-10-30-iteration-1b.md` §5
- Langue : ce document est en français ; le code, les commentaires et les commits restent en anglais.

## 1. Intention

Le propriétaire veut un assistant qui s'adapte à 100 % à ce qu'il décide, sans liste de règles prévue d'avance. Trois ajouts vont dans ce sens avant la fusion du 1b :

1. **Retirer un mot.** L'assistant voit les mots qu'une personne a laissés et que le destinataire n'a pas encore fermés. Il en retire un quand il comprend que c'est ce que l'auteur veut, y compris quand le mot ne sert plus à rien (l'auteur est rentré avant le destinataire, par exemple). C'est l'assistant qui décide quand, d'après l'intention et le contexte. Le code fixe seulement qui en a le droit : l'auteur, et lui seul.
2. **Faire revenir quelqu'un.** Un ancien invité, ou une personne retirée, revient avec son historique et son identifiant (exemple du propriétaire : un invité de deux jours qui revient un an plus tard).
3. **Droits et limites.** Le propriétaire dit à l'assistant, avec ses mots, qui peut faire quoi : pour un rôle ou pour une personne, avec ou sans limite. Il décide aussi quand un oui est demandé, et à qui. Exemples du propriétaire : « Donne le droit à Justine d'inviter des invités », « les mots de Sam passent par un adulte ». Les limites sont **illimitées** : elles suivent les outils disponibles, et le code n'en prévoit aucune liste.

S'y ajoute un changement de texte : l'assistant laisse un mot quand c'est ce que la personne veut, sans attendre une demande mot pour mot.

Pourquoi : le contenu des règles appartient au propriétaire, pas au code. Le code garde les valeurs par défaut et un socle de sécurité, et il applique toujours ce que le propriétaire a décidé. L'assistant (le modèle qui parle avec la personne) ne peut jamais passer outre.

## 2. Objectifs, non-objectifs, anti-objectifs

**Objectifs**

- Le propriétaire change les droits en conversation, et tout ce qui n'est pas dans le socle (§5.2) peut être donné, retiré ou limité.
- Une limite est appliquée par le code à chaque action. Ce que le code ne peut pas vérifier passe par un jugement ; en cas de doute, le propriétaire est consulté.
- Avant d'activer un droit, le propriétaire voit ce qu'il permet et ce qu'il refuse, sur des exemples, et dit oui.
- Chacun ne voit que les outils qu'il a le droit d'utiliser.
- Un auteur peut retirer son mot ; un ancien membre peut revenir.

**Non-objectifs (plus tard)**

- La mémoire de l'assistant, et donc le jugement fondé sur ce qu'il sait (§5.7) : en 1b, une limite qui demande de comprendre est soumise au propriétaire.
- Le choix automatique du modèle (proposé comme tâche à part, « L'assistant choisit lui-même son modèle »).
- La voix, la présence, la remise d'un mot au bon moment, les automatisations Home Assistant.
- L'« autonomie évolutive » de la spec §10.4 (proposer de passer une action en « Libre » après N oui).
- Du code Python écrit par un modèle et exécuté dans le bac à sable (approche B, §3) : il pourra s'ajouter quand le bac à sable existera.

**Anti-objectifs (échec même si « ça marche »)**

- Une personne limitée qui convainc l'assistant de dépasser sa limite.
- Un droit activé sans que le propriétaire ait vu ce qu'il permet.
- Le cœur qui exécute un programme écrit par un modèle, capable d'autre chose que répondre à une question.
- Quelqu'un qui revient sans avoir reçu le lien de retour.
- L'assistant qui lit les mots reçus par quelqu'un d'autre (décision D4 du 1b).

## 3. Approches (droits et limites)

Trois façons ont été comparées avec le propriétaire le 2026-10-31.

- **A. Une limite écrite dans un langage de règles fait exprès (CEL), appliquée par le cœur.** Un modèle à part écrit la limite ; le cœur l'évalue. CEL ne sait que calculer une réponse à partir de faits : pas de fichiers, pas de réseau, pas de boucle sans fin. Ce qui demande de comprendre passe par un jugement à part. Pour : faisable tout de suite, rapide, même réponse à chaque fois, vérifiable sur des exemples. Contre : une limite ne lit que les faits que le code lui donne ; le reste passe par le jugement.
- **B. Du code Python écrit par un modèle, exécuté dans le bac à sable.** Rien n'y est impossible. Contre : le bac à sable n'existe pas encore (prévu avec les connecteurs) ; chaque action l'attendrait (lent, et bloquée s'il tombe) ; un programme est plus dur à vérifier qu'une expression courte.
- **C. Seulement le jugement d'un modèle, à chaque action.** Le plus simple. Contre : un appel au modèle par action limitée, une réponse qui peut varier, rien de vérifiable d'avance, et le contenu de l'action (un nom, un message) peut chercher à tromper le juge.

**Choix du propriétaire : A**, avec le jugement de C seulement pour ce qui demande de comprendre. B reste possible plus tard pour ce que CEL ne sait pas dire.

Lecture de la spec §6.1 (« le cœur n'exécute pas de code écrit par le modèle ») : une expression CEL est une donnée évaluée par un interprète sans effet de bord, qui ne peut que rendre une valeur. Ce n'est pas du code au sens de cette règle, dont le but est d'empêcher le cœur de faire ce qu'un programme écrit par un modèle déciderait. La spec §6.1 et §10 sont mises à jour pour le dire.

## 4. Partie 1 : retirer un mot

**Ce qui existe.** `notes` (`notes/models.py`) : `author_id`, `recipient_id`, `text`, `created_at`, `dismissed_at`. Le destinataire voit ses mots dans une zone de sa page (rafraîchie toutes les 15 secondes) et les ferme (`dismissed_at`). L'assistant ne lit jamais les mots reçus (D4) ; la lecture ratifiée permet à l'auteur de retrouver ses propres mots dans sa propre conversation.

**Ce qui change.**

- Deux colonnes : `seen_at` (la première fois que la page du destinataire a affiché le mot) et `withdrawn_at` (retiré par l'auteur). Un mot retiré disparaît de la zone du destinataire au rafraîchissement suivant.
- Outil `note_list_sent` (type `note.list_sent`, `free`, tous les membres) : les mots de la personne qui parle, ni fermés ni retirés : destinataire, texte, date, vu ou non. Seulement ses propres mots (D4).
- Outil `note_withdraw` (type `note.withdraw`, `free`, tous les membres) : retire un mot de la personne qui parle, ni fermé ni déjà retiré. La ligne est verrouillée pour trancher une course avec le destinataire qui le ferme. Entrée d'audit `note.withdrawn` (identifiants, jamais le texte).
- Description de `note_withdraw` : l'assistant s'en sert quand c'est ce que l'auteur veut, y compris quand il comprend que le mot ne sert plus (« quand elle rentre » alors que l'auteur est rentré avant). Il le dit à l'auteur, ou lui demande s'il hésite : c'est son choix. Il peut dire si le mot a déjà été vu.
- Description de `note_leave` : « quand c'est ce que la personne veut » remplace « when the person you are talking with asks you to ».

Le destinataire, lui, ferme déjà ses mots ; il ne les retire pas (ce ne sont pas les siens).

« Pas de mot pour un invité » devient une limite par défaut que le propriétaire peut lever (§5.11). (Cette limite a été levée le 1er novembre 2026 : §12.1.)

## 5. Partie 3 : droits et limites

(La partie 2 vient au §6 ; l'ordre de construction est au §8.)

### 5.1 Ce que le propriétaire peut décider

Un **droit** porte sur un type d'action du catalogue et sur un **sujet** : un rôle (`adult`, `child`, `guest`) ou une personne. Il peut :

- donner l'accès (`allow`) ou le retirer (`deny`), y compris un accès donné par défaut (« les invités ne peuvent plus se renommer ») ;
- porter une **limite** (§5.5) ;
- changer le **niveau** (`free`, `confirm`, `always_confirm`) ;
- changer **qui dit oui** (`self`, `adult`, `owner`).

Résolution, champ par champ : le droit de la personne, sinon celui de son rôle, sinon le catalogue (`policy/catalog.py`). Un nouveau droit sur le même sujet et le même type remplace l'ancien. Les droits sont relus à chaque décision, comme les réglages de confirmation : un changement vaut aussi pour les demandes qui attendent.

### 5.2 Le socle : ce que personne ne peut donner

Vérifié par le code avant les droits, et jamais modifiable par un droit :

- l'identité non fiable reste traitée comme aujourd'hui (`barrier.decide`, règle 1) ;
- changer les droits (`rights.propose`, `rights.apply`) et les réglages (`policy.update`) reste au propriétaire seul. Un changement de droits est toujours montré avec ses exemples et demande son oui (§5.6) ;
- personne n'agit sur le propriétaire (le retirer, changer son rôle) ;
- personne ne change son propre rôle (ce serait se donner plus de pouvoir). Se retirer soi-même n'est pas dans le socle : ça ne donne aucun pouvoir, et la personne peut revenir (§6) ; c'est au propriétaire (limites) et au jugement de l'assistant ;
- celui qui demande ne dit jamais oui à sa propre demande quand le oui revient à un adulte ou au propriétaire (déjà le cas : `confirmations/service.py`, `_approves`) ;
- une action inconnue du catalogue est refusée (comme aujourd'hui en 1b).

Tout le reste est au choix du propriétaire, y compris donner à un adulte un droit d'administration (inviter, retirer, changer la clé du modèle).

### 5.3 Stockage

Une table des droits (nom au choix du constructeur, par exemple `policy_rights`) :

- sujet : `person_id` ou `role` (exactement un des deux) ;
- `action_type` ;
- `access` (`allow` / `deny` / non précisé), `level` et `approver` (optionnels) ;
- `limit` (optionnel, §5.5) ;
- `summary` : la phrase qui dit le droit en mots simples, écrite par le modèle rédacteur et approuvée par le propriétaire ;
- `owner_words` : les mots du propriétaire tels que l'assistant les a transmis ;
- `examples` : les exemples montrés au propriétaire (§5.6) ;
- `created_by`, `created_at`, et `revoked_at` (un droit remplacé ou retiré n'est pas effacé : l'audit et l'historique y renvoient).

Les valeurs par défaut restent dans le code (`policy/catalog.py`) : aucune ligne = le défaut.

### 5.4 L'action décrite porte ses faits

Pour qu'une limite puisse tout lire, chaque action décrite gagne un sixième champ, `facts` : un objet de valeurs simples (texte court, nombre, booléen, `null`), écrit par le code de l'outil, jamais par le modèle. Exemples :

- `household.invite` : `role`, `days`, `display_name` ;
- `household.change_role` : `person`, `current_role`, `role`, `days` ;
- `household.remove` : `person`, `current_role` ;
- `household.bring_back` : `person`, `role`, `days` ;
- `note.leave` : `recipient`, `recipient_role`, `length` (jamais le texte) ;
- `note.withdraw` : `recipient`, `seen`.

Règles : un fait n'est jamais un contenu libre long (pas le texte d'un mot) ; les personnes sont des identifiants `person:<uuid>` ; les faits vont dans la confirmation et dans l'audit, comme le reste de l'action. Chaque outil déclare la liste de ses faits et leur type : c'est ce que le modèle rédacteur reçoit pour savoir sur quoi une limite peut porter. Un connecteur (1e) fera de même : les limites possibles grandissent avec les outils.

`DescribedAction.from_json` accepte aussi une action stockée sans `facts` (demandes créées avant ce changement) : elle se lit avec des faits vides.

### 5.5 Les limites

Une limite est une expression **CEL** qui rend un des quatre résultats :

- `"allow"` : la limite laisse passer ; le niveau et le « qui dit oui » s'appliquent ensuite normalement ;
- `"refuse"` : refusé ;
- `"ask_owner"` : la demande attend le oui du propriétaire ;
- `"judge"` : il faut comprendre, pas seulement vérifier (§5.7) ; le droit porte alors une `judge_question` en mots (« X est-elle une amie de Justine ? »).

Ce que l'expression peut lire (fixé par le code, en lecture seule, calculé au moment de la décision) :

- `actor` : `id`, `role`, `name` ;
- `action` : `type`, `target`, `facts` (§5.4) ;
- `now` : l'heure, le jour de la semaine et la date **dans le fuseau horaire du foyer** ;
- `household` : le nombre de personnes par rôle, d'invitations ouvertes par rôle ;
- `history` : combien de fois la personne a fait ce type d'action (`done`) sur les dernières 24 heures, 7 jours et 30 jours.

Exemple : « Justine peut inviter des invités, trois jours au plus » donne, pour la personne Justine et `household.invite` :
`action.facts.role == "guest" && action.facts.days <= 3 ? "allow" : "refuse"`.

**Échec fermé.** Une expression qui ne compile pas n'est jamais enregistrée (§5.6). À la décision, toute erreur (fait absent, mauvais type, résultat qui n'est pas un des quatre, dépassement du temps ou de la taille permis) donne `refuse` avec la raison `limit_error`, écrite au journal et dans l'audit. L'assistant dit à la personne que la vérification n'a pas pu se faire et qu'elle peut demander au propriétaire.

**Fuseau horaire.** Un réglage du propriétaire, `household.time_zone` (nom IANA, par exemple `Europe/Paris`). S'il n'est pas défini et qu'une limite parle d'heure ou de jour, l'assistant le demande au propriétaire avant d'écrire la limite ; il ne le devine jamais en silence.

### 5.6 Écrire un droit : le modèle rédacteur, les exemples, le oui

1. Le propriétaire le dit avec ses mots. L'assistant appelle `rights_propose` (type `rights.propose`, `free`, propriétaire seul, socle) avec ces mots.
2. L'outil appelle un **modèle rédacteur** : un appel à part, avec un texte fixe (`prompts/rights_writer.md`), qui ne voit **que** : les mots du propriétaire, le catalogue des types d'action avec leurs faits, la liste du foyer (noms, rôles, identifiants), les droits en place et ce que CEL peut lire. Jamais la conversation. Aujourd'hui c'est le même fournisseur que la conversation ; quand le routeur de modèles existera, c'est lui qui choisira le modèle.
3. Le rédacteur rend un objet JSON : les droits à créer, changer ou retirer ; pour chacun son `summary` ; au moins trois **exemples** (des faits d'action et le résultat attendu), dont au moins un qui passe et un qui ne passe pas quand il y a une limite.
4. Le code vérifie tout : la forme, les types d'action connus, les sujets qui existent, le socle (§5.2), que chaque expression compile et ne lit que des faits connus, puis il **évalue chaque exemple** et compare au résultat attendu. En cas d'écart, il redonne une fois les erreurs au rédacteur ; si ça échoue encore, l'outil échoue et l'assistant dit simplement au propriétaire qu'il n'a pas su écrire ce droit et lui demande de le dire autrement.
5. Le brouillon valide est enregistré tel quel (il ne change plus). L'assistant le présente et appelle `rights_apply` (type `rights.apply`, `always_confirm`, propriétaire seul, socle), dont l'action décrite vise ce brouillon. La carte de confirmation montre le `summary` et les exemples. **La phrase de chaque exemple est produite par le code à partir de ses faits** (un modèle de phrase par type d'action, dans le catalogue des textes), et son résultat est celui que le code a calculé, pas celui que le rédacteur annonce : ce que le propriétaire lit est exactement ce qui sera appliqué.
6. Le oui du propriétaire active le brouillon : les droits remplacés sont marqués `revoked_at`, les nouveaux écrits, une entrée d'audit `rights.changed` (avant et après) dans la même transaction.

Retirer ou modifier un droit suit le même chemin (« retire à Justine le droit d'inviter »). Le propriétaire voit les droits en place avec `policy_show`, qui les liste avec leur `summary`.

### 5.7 Les limites qui demandent de comprendre

Décision du propriétaire (2026-10-31) : si l'assistant sait déjà (il a entendu des conversations qui montrent que X est l'amie de Justine), pas besoin de validation ; sinon, ou en cas de doute, il demande au propriétaire. Si la seule source est la personne limitée elle-même (« Justine me l'a dit »), c'est un doute.

En 1b, l'assistant n'a pas de mémoire : il ne sait rien de ce genre. Le résultat `"judge"` se comporte donc comme `"ask_owner"`, et la demande montrée au propriétaire dit ce que l'assistant n'a pas pu savoir (la `judge_question`). Avec la mémoire viendra un **juge** : un appel à part qui voit la question, les faits de l'action et ce que l'assistant sait, avec la source de chaque chose apprise, mais jamais la conversation en cours. Il répond oui, non ou doute ; un doute va au propriétaire.

### 5.8 La barrière

`decide` reste une fonction pure. Avant de l'appeler, l'appelant charge (async) les droits et les faits du moment ; `decide` reçoit tout en paramètres. Ordre :

1. identité (inchangé) ;
2. socle (§5.2) ;
3. accès : droit de la personne, sinon du rôle, sinon catalogue ; refus `not_allowed` si non permis ;
4. limite, s'il y en a une : `refuse` → refus `limit` (avec le `summary` du droit) ; `ask_owner` ou `judge` → `NeedsConfirmation(level, "owner")` ; `allow` → suite ;
5. niveau (personne, rôle, catalogue) : `free` → `Allow` ;
6. qui dit oui : le choix du droit s'il y en a un ; sinon, comme aujourd'hui, un adulte pour un enfant et la personne elle-même pour les autres.

La même décision est rejouée quand la demande reçoit son oui (`confirmations/service.py`), avec les droits et les faits de ce moment-là. Les faits de l'action viennent de l'action stockée (ce à quoi la personne a dit oui) ; les faits du contexte (`now`, `household`, `history`) sont recalculés.

**Qui dit oui : `owner`.** Nouvelle valeur de `approver_kind` (contrainte de la table `confirmations` à migrer). Seul le propriétaire peut répondre, et jamais à sa propre demande (s'il est le demandeur, c'est `self`). Ses demandes à valider apparaissent dans la zone « À valider » de sa page.

**Ce que la personne entend.** Un refus pour limite donne au modèle `{"status": "refused", "reason": "limit", "limit": <summary>}`. L'assistant peut dire la limite telle que le propriétaire l'a approuvée.

### 5.9 Les outils montrés à chacun

`tools_for` prend la personne (pas seulement son rôle) et les droits chargés : un outil est montré si son type d'action est accessible à cette personne, avec ou sans limite. La barrière reste la règle, ce qui est montré reste une politesse (`tools/registry.py`).

### 5.10 Les outils du foyer

Les outils `household_*` ne sont plus « les outils du propriétaire » : ce sont ceux de quiconque en a le droit (le propriétaire par défaut). Leurs descriptions disent « quand c'est ce que veut la personne avec qui tu parles » au lieu de « when the owner asks ». Les vérifications du socle (§5.2) sont ajoutées là où elles manquent (changer son propre rôle, se retirer soi-même). (Depuis le 2 novembre 2026, se retirer soi-même est une action à part, `household.leave`, ouverte à chaque membre sauf le propriétaire : spec `2026-11-02-sans-refus-de-commodite-design.md`, §5.9.)

**Le lien montré une fois va à celui qui a demandé.** Aujourd'hui, ce qui n'est montré qu'une fois (`card_once` : le lien d'une invitation) part dans la réponse à celui qui a dit oui, qui est toujours le propriétaire répondant à sa propre demande. Quand quelqu'un d'autre dit oui (le propriétaire pour une invitation de Justine), c'est Justine qui veut envoyer le lien. Donc : le oui crée l'invitation **sans jeton** ; la carte de la demande, chez celui qui l'a faite, porte un bouton « Montrer le lien » qui crée le jeton et le montre une seule fois, à lui seul. Le jeton n'est toujours stocké nulle part (seulement son empreinte, comme aujourd'hui). Quand le demandeur est aussi celui qui dit oui, le lien peut être montré tout de suite, comme aujourd'hui. Même chose pour le lien de retour (§6). Si `model.rekey` est donné à un adulte, le formulaire du coffre est demandé pour celui qui a fait la demande, et les routes de la clé (`/chat/key/status`, « saisir la clé à nouveau ») s'ouvrent à lui, plus seulement au propriétaire.

### 5.11 Les règles des outils deviennent des limites par défaut

Certaines vérifications sont aujourd'hui figées dans les outils alors que ce sont des choix de produit : pas de mot pour un invité (`tools/notes.py` ; levé le 1er novembre 2026, §12.1), un séjour d'invité de 1 à 30 jours (`household/service.py`, et la contrainte `guest_days_range` de la table `invitations`). Le contenu des règles appartient au propriétaire : ces choix deviennent des **limites par défaut du catalogue**, écrites en CEL dans le code, au niveau du rôle (ou de tous). Un droit du propriétaire peut les remplacer comme n'importe quelle limite. La contrainte de la base est élargie à un plafond technique (au choix du constructeur, par exemple 365 jours).

Restent dans les outils les vérifications techniques ou d'intégrité : longueurs, noms qui se distinguent, le rôle de propriétaire jamais donné, formats valides. (Un mot pour soi-même était dans cette liste : depuis le 2 novembre 2026, ce n'est plus une vérification fixe, et un mot peut avoir pour destinataire celui qui l'écrit. Voir la spec `2026-11-02-sans-refus-de-commodite-design.md`, §5.6.) Le modèle rédacteur reçoit les limites par défaut (pour pouvoir les remplacer) et la liste des vérifications qui restent fixes : si le propriétaire demande d'en lever une, l'assistant lui dit simplement que ce n'est pas possible.

### 5.12 Les droits d'une personne quand sa situation change

Décision du propriétaire (2026-10-31), pour les droits donnés à une personne (pas à un rôle) :

- **Elle quitte le foyer** (retirée, ou fin de son séjour) : ses droits personnels sont **mis en pause**, pas effacés. Une personne hors du foyer ne peut de toute façon rien faire.
- **Elle revient** (§6) : au moment où elle utilise le lien de retour, ses droits personnels encore actifs sont marqués en pause (par exemple une colonne `paused_at`) et ne comptent plus. L'assistant rappelle au propriétaire les droits qu'elle avait, et le propriétaire dit lesquels lui rendre. Rendre un droit est un changement de droits comme un autre (§5.6, propriétaire seul) ; les autres restent en pause, visibles dans `policy_show`.
- **Elle change de rôle** : elle garde ses droits personnels. Quand c'est le propriétaire qui fait le changement, le résultat de l'outil liste ces droits (leur `summary`) pour que l'assistant lui dise lesquels elle a encore.

## 6. Partie 2 : faire revenir quelqu'un

**Ce qui existe.** Une personne retirée a `disabled_at` ; un invité dont le séjour est fini a `expires_at` passé. Ni l'un ni l'autre ne peut revenir : il faut une nouvelle invitation, et l'ancien identifiant reste pris (`_username_exists` regarde tous les comptes).

**Ce qui change.**

- Outil `household_bring_back` (type `household.bring_back`, `always_confirm`, propriétaire par défaut, et donnable comme les autres) : le nom d'un ancien membre (retiré, ou invité dont le séjour est fini), le rôle à son retour, les jours s'il revient comme invité (1 à 30, 2 par défaut), et un nouveau nom d'affichage si l'ancien est pris entre-temps. Le nom est cherché parmi les anciens membres seulement ; un nom qui en désigne plusieurs est `ambiguous`, et la réponse décrit chacun par le rôle qu'il avait et le jour où il est parti (jamais son identifiant de connexion ni son id). L'assistant demande lequel, puis rappelle l'outil avec un argument optionnel `left_on` (cette date) qui les départage. Une personne encore dans le foyer n'est pas un ancien membre (`not_found`, avec un détail qui le dit).
- Il fait un **lien de retour** : une invitation liée à la personne existante (nouvelle colonne, par exemple `invitations.person_id`), avec le même délai et la même règle « montré une fois » que les invitations. Annuler un lien de retour passe par `household_cancel_invitation`, comme une invitation.
- La page du lien dit « Bon retour » avec le nom de la personne, montre son identifiant (qui ne change pas) et demande un nouveau mot de passe. Au oui : la ligne est verrouillée et revérifiée ; `disabled_at` est vidé, le rôle et la fin de séjour sont ceux du lien, le nom d'affichage change s'il le faut, le mot de passe est remplacé, les anciennes passkeys sont supprimées (elle pourra en ajouter), et le lien est marqué accepté. Son historique (conversations, mots) est là.
- Rien de l'ancien accès ne fonctionne avant que la personne ait utilisé le lien.

**Retirer quelqu'un qu'on ne voit plus.** Décision du propriétaire (2026-10-31) : l'assistant peut décider de retirer une personne qu'il n'a plus vue depuis trop longtemps, ou demander au propriétaire s'il a un doute. Retirer n'efface rien : l'historique reste, et la personne peut revenir par un lien de retour.

- **Dans ce travail :** `household_list` donne, pour chaque personne, le jour où elle est venue pour la dernière fois (le plus récent `last_seen_at` de ses sessions), à ceux qui ont le droit de retirer quelqu'un (le propriétaire par défaut). L'assistant peut ainsi le remarquer en parlant avec le propriétaire et le lui proposer ; le retrait suit le niveau de `household.remove` (`always_confirm` par défaut), donc le propriétaire dit oui.
- **Plus tard**, quand l'assistant pourra agir de lui-même en dehors d'une conversation (la « réflexion périodique » de la spec) : il retire sans demander quand il n'a pas de doute, le dit ensuite au propriétaire, et lui demande quand il doute. C'est noté dans la spec principale.
- Audit : `household.bring_back_offered` à la création du lien, `household.brought_back` à son usage (identifiants, rôles, jamais le lien).

## 7. Textes à changer

- `tools/notes.py` : description de `note_leave` (§4).
- `tools/household_admin.py` : en-tête et descriptions (§5.10).
- `prompts/system.md` : seulement si nécessaire, en restant sous 6000 caractères (test `test_the_fixed_text_is_short_and_plain`). Les descriptions d'outils portent l'essentiel.
- `README.md` et `README.fr.md` : rôles et droits (le propriétaire les change en conversation), retrait d'un mot, retour d'un ancien membre.
- Spec principale : §6.1 (lecture de CEL, §3), §10.1 (les faits d'une action), §10.2 (ordre de la barrière), §10.3 (rôles modifiables, droits par personne, limites), §11 (le rédacteur passera par le routeur), et la liste « Plus tard » (l'assistant retire de lui-même qui n'est plus venu depuis trop longtemps, §6).
- Suites du 1b (§5) : les trois points passent de « à décider » à « fait ».

## 8. Ordre de construction

Du plus petit au plus gros, chaque étape testable seule :

1. Retirer un mot (§4) et la nouvelle description de `note_leave`.
2. Faire revenir quelqu'un (§6).
3. Les faits dans l'action décrite (§5.4) et le socle explicite (§5.2).
4. Les droits en base, la barrière qui les lit, les outils montrés par personne, `approver = owner` (§5.1, §5.3, §5.8, §5.9), avec des droits écrits directement par les tests.
5. Les limites CEL, le fuseau horaire, l'échec fermé, les limites par défaut tirées des outils (§5.5, §5.7, §5.11), et le lien montré au demandeur (§5.10).
6. Le modèle rédacteur, `rights_propose`, `rights_apply`, la carte avec exemples, `policy_show` (§5.6).
7. Textes, README, spec (§7) ; scénarios de bout en bout (§9).

## 9. Scénarios qui doivent passer

- Un auteur demande ce qu'il a laissé, puis retire un mot pas encore vu : le destinataire ne le voit plus. Un mot fermé ou retiré ne peut pas l'être une deuxième fois. Personne d'autre que l'auteur ne peut le retirer.
- Un invité dont le séjour est fini revient un an plus tard : il garde son identifiant et son historique, choisit un nouveau mot de passe, et rien ne marche avant le lien. Deux anciens membres du même nom : l'assistant demande lequel.
- Justine avait le droit d'inviter, part, puis revient : ce droit est en pause, l'assistant le rappelle au propriétaire, et il ne compte de nouveau qu'une fois rendu. Justine devient enfant : elle garde le droit, et le propriétaire en est informé.
- Le propriétaire donne à Justine le droit d'inviter des invités, trois jours au plus : il voit les exemples, dit oui. Justine invite un invité pour deux jours : ça passe (avec son propre oui, l'action restant `always_confirm`). Elle invite un adulte : refusé, et l'assistant lui dit la limite. Elle tente de convaincre l'assistant : toujours refusé.
- Le propriétaire dit « les mots de Sam passent par un adulte » : le mot de Sam attend le oui d'un adulte.
- « Justine peut inviter seulement ses amies » : en 1b, chaque invitation de Justine attend le oui du propriétaire, avec la question sur la carte.
- Une limite en erreur refuse, et l'erreur est écrite au journal et dans l'audit.
- Personne d'autre que le propriétaire ne peut changer les droits, même s'il a reçu tous les autres. Personne ne change son propre rôle.
- Le propriétaire demande qui n'est pas venu depuis longtemps : l'assistant le sait et peut lui proposer de retirer quelqu'un. Un adulte sans le droit de retirer ne voit pas ces dates.
- Un outil non accessible n'est pas montré ; accessible avec limite, il l'est.

## 10. Laissé au constructeur

- La bibliothèque CEL. Deux candidates sous Apache-2.0 : `common-expression-language` (enveloppe d'un interprète Rust) et `cel-python` (Python pur ; ses métadonnées de paquet ont un classement de licence contradictoire, à vérifier). Critères : licence permissive confirmée par le contrôle de licences de la CI, entretien récent, limites de coût ou de temps d'évaluation.
- Les limites de taille d'une expression et de temps d'évaluation.
- Le texte de `prompts/rights_writer.md` et la forme exacte du JSON du rédacteur.
- Les noms des tables et colonnes, les numéros de migration.
- La liste exacte des faits de chaque type d'action, dans l'esprit du §5.4.
- Comment `history` est calculé (depuis l'audit `policy.decision`, ou une requête dédiée).
- Les phrases d'exemple par type d'action (catalogue des textes, français et anglais).

## 11. Mes choix, à vérifier par le propriétaire

- Changer les droits reste au propriétaire seul et demande toujours son oui sur les exemples (socle, §5.2). C'est la garantie que rien ne s'active sans qu'il l'ait vu.
- Personne ne change son propre rôle (socle).
- Le jour de dernière venue n'est montré qu'à ceux qui peuvent retirer quelqu'un (§6), et l'assistant dit au propriétaire ce qu'il a retiré de lui-même (plus tard).
- En 1b, sans mémoire, une limite qui demande de comprendre va toujours au propriétaire (§5.7).
- Une limite en erreur refuse (échec fermé, §5.5).
- L'assistant peut dire à la personne limitée la limite telle que le propriétaire l'a approuvée (§5.8).
- Le retour passe par un lien et un nouveau mot de passe ; les anciennes passkeys sont supprimées (§6).
- Le fuseau horaire du foyer devient un réglage, demandé quand une limite en a besoin (§5.5).
- Le lien d'une invitation ou d'un retour va à celui qui a demandé, par un bouton « Montrer le lien » une fois le oui donné (§5.10).
- Les choix de produit figés dans les outils (pas de mot pour un invité, séjour de 1 à 30 jours) deviennent des limites par défaut que le propriétaire peut remplacer (§5.11). (Celle des mots aux invités a été levée le 1er novembre 2026, §12.1.)
- Deux anciens membres du même nom se distinguent par leur ancien rôle et leur date de départ (§6).

## 12. Ajouts du 1er novembre 2026, avant la fusion

Décidés avec le propriétaire en conversation, après la relecture finale du 1b.

### 12.1 Les mots aux invités

- La limite par défaut « pas de mot à un invité » disparaît : `note.leave` n'a plus de limite par défaut. Un invité présent peut recevoir des mots. Le propriétaire peut toujours poser une limite à lui (le fait `recipient_role` reste).
- On ne laisse un mot qu'à quelqu'un de présent (déjà le cas : un nom ne désigne qu'une personne présente).
- Quand un invité part (son séjour est fini, ou il a été retiré), les mots qu'il a reçus sont effacés pour de bon, lus ou non, dans la minute. S'il revient, il revient sans eux.
- Mes choix : les mots que l'invité a laissés aux autres restent ; un adulte ou un enfant retiré garde les mots reçus, et les retrouve s'il revient.
- L'effacement est fait par une petite tâche du cœur, au démarrage puis toutes les minutes. C'est le premier morceau du planificateur prévu par l'architecture (§ composants). Chaque effacement est noté au journal d'audit (`notes.erased`, par le système) : l'identifiant de l'invité et le nombre de mots, jamais leur texte. Une passe qui échoue est notée dans les journaux, et la suivante recommence.

### 12.2 Le temps du rédacteur

- Nouveau réglage `rights_writer.seconds` : le temps de chaque appel au rédacteur, un nombre entier de secondes de 30 à 240, 60 par défaut. Le maximum reste sous la coupure du gardien (300 s par appel au modèle). Comme les autres réglages, le propriétaire le change en parlant (`policy_update`, avec son oui).
- Quand le rédacteur manque de temps, rien n'est stocké (comme avant). Le résultat de `rights_propose` le dit à l'assistant avec le temps qu'il avait, et le journal d'audit le note (`rights.writer_timeout`, avec les secondes).
- Le contexte du propriétaire (et de lui seul) dit combien de fois le rédacteur a manqué de temps ces 30 derniers jours et le temps qu'il a, quand c'est arrivé au moins une fois. L'assistant juge s'il propose d'en laisser plus. Il propose, le propriétaire dit oui. L'assistant ne change jamais ce réglage tout seul.
