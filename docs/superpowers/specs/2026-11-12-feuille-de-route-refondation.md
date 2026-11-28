# Feuille de route : adapter l'assistant à la règle du « possible »

Proposée le 2026-11-12, après l'essai de la 0.7.4. **L'ordre des chantiers (A, B, C, D, E, F, G) a été accepté par le propriétaire le 2026-11-12.** Ce document n'est ni une conception ni un plan de construction : chaque chantier aura sa propre conception, puis son plan, comme les précédents.

## 1. La règle

Dite par le propriétaire le 2026-11-12 :

- L'assistant sera relié à des micros et des haut-parleurs dans la maison, ou parlera à la voix sur un ordinateur ou un téléphone.
- Il doit pouvoir faire toute action qu'il juge possible. « Possible » veut dire deux choses seulement : il a les moyens physiques de la faire, et la personne qui demande est jugée comme ayant le droit de la faire.
- Les outils logiciels ne sont jamais censés être un frein.
- L'application est une base de vie, le modèle est la réflexion, et les outils créés par l'assistant finissent de faire son cerveau. Le tout forme un cerveau capable de s'adapter, de s'améliorer et d'apprendre.

## 2. Ce que l'analyse a trouvé (version 0.7.4)

L'assistant a 50 outils, tous tournés vers lui-même (foyer, droits, clés, modèles, textes). Aucun n'agit sur le monde. Environ 40 % du code du cœur sert aux confirmations et aux clés ; la mémoire, le temps, la maison, la voix et le code écrit par l'assistant n'en ont aucune ligne.

**À garder tel quel**

- L'isolation : cœur sans internet, gardien seul à sortir, coffre des secrets, journal.
- Le moteur d'un tour (`chat/agent.py`, `run_turn`), qui ne sait rien de la page ni de la base.
- La décision d'un droit : type d'action, faits, personne, moment (`policy/barrier.py`).
- Le choix des modèles et le suivi des dépenses.

**Fermé par construction**

1. Le catalogue des actions est écrit dans le code (`policy/catalog.py`) : une action inconnue est refusée, et en ajouter une demande 6 à 8 fichiers et une nouvelle version. Ni bac à sable, ni connecteur, ni outil générique.
2. L'assistant ne vit que quand une page est ouverte : pas de mémoire d'une conversation à l'autre, pas d'horloge, pas de réveil de lui-même.
3. L'identité est un cookie de navigateur, toujours tenu pour sûr (`Actor.trusted` n'est jamais faux). Les preuves (bouton, mot de passe, clé d'accès) n'existent que sur une carte.
4. Le tour de discussion est mêlé au HTML (`chat/routes.py`) : liens d'invitation, formulaires de clé, erreurs.

S'y ajoute la consigne « si aucun outil ne le permet, dis que ce n'est pas possible » (`prompts/system.md`), qui fait d'un outil manquant un refus.

**À vérifier avant le chantier 3** : le gardien accepte une adresse de fournisseur sans clé, sans contrôle de l'hôte (`domo_gatekeeper/providers.py`). Seul le cœur peut le lui demander aujourd'hui.

## 3. Verdict

Pas de reconstruction. Quatre ouvertures à faire dans la base de vie, puis les moyens physiques, posés dessus comme des adaptateurs. D'ici là : plus de nouvelle carte, de nouveau bouton ni de nouveau texte de page, et les trois gros blocs taillés pour la page (suites d'actions, succession, textes écrits par l'assistant) ne sont plus étendus.

## 4. Les chantiers

Chacun est livré d'un bloc : une branche, une conception, un plan, une fusion.

### Chantier A — Le canal et l'identité

*Nouveau. Aucune capacité visible, mais tout ce qui suit cesse d'être construit en forme de page.*

- Le tour rend des événements (mots, actions, demandes) ; la page devient un canal parmi d'autres, qui les met en HTML.
- L'identité porte un canal et un degré de confiance (« c'est Alex, connecté » / « c'est probablement Alex, à la voix »), que la barrière, le juge et les limites peuvent lire.
- Les preuves ne dépendent plus d'une carte : une preuve peut être demandée sur un autre appareil de la personne.
- Ce qui se donne aujourd'hui par un lien ou un formulaire (invitation, clé) a un chemin qui ne suppose pas la page de celui qui parle.
- L'assistant peut pousser une parole vers un canal, sans attendre qu'une page la demande.
- **Construit en 0.8.0** (2026-11-14) : un cœur de conversation sans page, la page et un programme de texte (`domo-talk`) branchés dessus, des appareils confiés à l'assistant, des moyens de preuve convenus. Ce qu'il laisse pour après est dans les suites de l'itération 1b, §14.

### Chantier B — La mémoire et le temps

*L'ancien chantier 4, élargi.*

- Se souvenir d'une conversation à l'autre, apprendre de ce qui lui arrive, oublier ce qu'il juge inutile.
- Une horloge à lui : programmer, rappeler à une heure, se réveiller seul, parler le premier.
- Le nom que chacun lui donne (« Brenda ») ; un prénom seul désigne la bonne personne.
- La consigne du « pas possible » devient : chercher un moyen, sinon dire ce qu'il faudrait construire.
- L'assistant peut regarder ce que chaque personne a vraiment reçu, sur chaque canal (la page, un message, la voix rejouée) : de quoi comprendre une plainte (« je n'ai rien vu ») et apprendre de ses erreurs (demandé le 2026-11-14, à commencer ici avec la mémoire).
- Le scénario des vacances avec un invité, à garder pour la conception (le propriétaire, 2026-11-14) : « je pars deux semaines, appelle-moi s'il y a un gros problème, sinon j'ai donné les clés à un ami, tu peux le contacter ». L'assistant retient l'absence et ses dates, juge lui-même ce qui est un gros problème, donne à l'ami un séjour d'invité de deux semaines, s'adresse à lui d'abord pour un souci ordinaire et appelle le propriétaire pour un gros, puis revient à la normale au retour. Piège : « les clés » sont celles de la maison, pas des clés de modèle ; il le comprend au contexte ou il demande. Seul le séjour d'invité existe aujourd'hui ; le reste demande la mémoire et le temps (ici), percevoir et appeler (E et G).
- **Construit en 0.9.0** (2026-11-16) : une mémoire que l'assistant tient lui-même (garder, changer, oublier, chercher par les mots et par le sens avec un petit modèle local, des secrets qu'il garde pour lui), repassée quand la discussion se calme ; l'heure, le foyer et la mémoire dans chaque tour ; des rendez-vous qu'il se donne et qui le réveillent ; le juge qui sait ce que l'assistant a déjà dit à la personne ; le nom que chacun lui donne et le prénom seul ; ce que chacun a vraiment reçu. Ce qu'il laisse pour après est dans les suites de l'itération 1b, §15.

### Chantier C — Les outils que l'assistant se crée

*L'ancien chantier 4 bis, élargi avec ce que l'architecture appelait 1e. C'est lui qui rend la règle vraie.*

- Un catalogue ouvert : une action nouvelle est décrite par ses risques (coût, réversible ou non, physique, vers l'extérieur) et jugée comme les autres, sans nouvelle version de l'application.
- Un bac à sable où tourne le code que l'assistant écrit, jamais dans le cœur, sans voir de secret, sans plus de droits que lui.
- Une sortie réseau contrôlée par le gardien, avec les secrets injectés par référence.
- Une façon unique de brancher un connecteur, qu'il vienne du projet ou de l'assistant.
- Des compétences (skills) que l'assistant se branche lui-même après les avoir fait contrôler, et des agents spécialisés qu'il crée quand il en a besoin : à « fais mes impôts », c'est lui qui décide comment (seul, ou avec un agent). Il tient à jour ses compétences et ses outils de contrôle. L'outil de contrôle n'est pas imposé : SkillSpector n'est ni obligatoire ni installé d'office (l'assistant peut l'installer lui-même), et le propriétaire peut en demander un autre, ou aucun contrôle. Un contrôle net : l'assistant ajoute et met à jour seul ; une alerte : il demande l'accord du propriétaire (le propriétaire, 2026-11-12).
- L'assistant se regarde lui-même : il lit son propre code et ses journaux, sans rien y changer, et écrit un rapport de panne que le développeur reprend. Il ne change jamais seul le code du cœur (le propriétaire, 2026-11-14).
- Un rapport de panne, n'importe qui peut le lui demander. Il peut l'envoyer au dépôt git du projet quand il y en aura un, et le faire de lui-même quand il voit un bogue, avec l'accord du propriétaire. Un rapport qui sort de la maison ne contient aucun nom, aucune discussion, aucun secret, et sort par le gardien (l'envoi demande aussi G).

### Chantier « Domo crée les personnes » — L'assistant tient lui-même les personnes du foyer

*Demandé le 2026-11-23, conception `2026-11-23-domo-cree-les-personnes-design.md`. Sa place dans l'ordre et son numéro de version sont fixés à la fusion.*

- L'assistant tient lui-même les personnes du foyer : une fiche par personne, avec ou sans compte ; un accès n'est pas un compte. Il ouvre, complète, réunit, sépare (90 jours), efface et oublie les fiches, note les liens entre les gens, crée des rôles sous un plafond tenu par le code, donne, reprend et promet des accès.
- Le propriétaire a ses consignes, appliquées avant tout jugement (ne plus créer de nouvelles personnes, ne plus les retenir, retirer les accès de quelqu'un, refuser quelqu'un), et deux règles à lui : être prévenu de chaque nouvel accès donné par l'assistant, et « les invités viennent avec leur propre clé ».
- **Construit sur la branche `claude/feat-domo-makes-people-1ebc54`** (relectures en cours, pas encore fusionné).
- Ce qu'elle remplace dans les conceptions passées (sa partie 6.14) : « le propriétaire relie chaque identité de canal à une personne du foyer » (architecture, 10.3) ; inviter et retirer comme droits d'administration donnés par le propriétaire, prolonger un séjour comme changement de rôle, « un adulte ne touche jamais à ton invitation » (droits 5.2, sans refus 5.5 et 5.9, suites du 2026-11-17) : l'assistant juge ; la liste fermée des quatre rôles ; « un invité n'a pas d'accès hors des pièces communes », devenu le point de départ d'un rôle de passage. La limite de 30 jours d'un séjour n'est plus qu'une fin par défaut.
- Ce qu'elle laisse à la voix (chantier D) : voir ce chantier.

### Chantier D — Parler à l'assistant à la voix sur un ordinateur

*Demandé le 2026-11-12. Le premier canal vocal.*

- Sur un ordinateur, on parle à l'assistant et il répond à voix haute, sans rien taper ni cliquer.
- C'est le cas le plus simple pour commencer : le micro et les haut-parleurs sont déjà là, et la personne est déjà connectée, donc son identité est sûre sans reconnaissance de la voix.
- Tout ce qui se fait aujourd'hui par une carte doit pouvoir se dire et s'entendre.
- Demande le chantier A. La reconnaissance et la synthèse de la parole passent par le gardien ou par des modèles locaux : à décider en conception.
- Là où la voix se branche est prêt (chantier « Domo crée les personnes », partie 6.12 de sa conception), sans rien qui écoute : une personne sans compte peut être celle qui parle (un canal la présente avec ses indices) ; un inconnu peut devenir une fiche, à laquelle le canal rattache sa propre référence pour le reconnaître ; réunir, séparer, effacer et oublier une fiche préviennent le canal. Le seul interrupteur de « qui l'assistant peut atteindre » s'élargit le jour où une voix peut répondre. Reste à décider ici, avec le propriétaire, avant de brancher la règle de ce qu'une personne mal reconnue peut faire (écrite, appelée nulle part) : un outil de l'assistant qui ne déclare qu'un geste physique (une porte) ou lit un secret, et ce que vaut une voix reconnue pour réutiliser une preuve donnée plus tôt. Avant la voix, quelqu'un sans compte passe par une personne du foyer.
- À rappeler au propriétaire (demandé le 2026-11-12) : refaire à la voix l'essai du deuxième canal du chantier A (même assistant, même discussion, ce qu'une carte montre est dit en mots). Les actions pour convenir d'un moyen de preuve ou en retirer un arrivent ici, avec le mot de passe vocal.

### Chantier D bis — L'application Android

*Demandé le 2026-11-22. Placé juste après D : l'application a besoin que l'assistant sache déjà entendre et parler. Android seulement pour l'instant.*

- Un mot de réveil au nom que chacun a donné à l'assistant (« ok domo », « ok friday »), détecté sur le téléphone, application fermée et écran éteint. Rien ne part vers le serveur avant le mot. Quand quelqu'un lui donne un nom, l'assistant prépare lui-même ce qu'il faut pour le détecter et l'envoie au téléphone de cette personne ; le serveur vérifie le mot et reconnaît la voix.
- Une écoute en continu pour apprendre à connaître la personne, hors de la maison aussi. Le téléphone ne garde que les moments où quelqu'un parle, chiffre le son et l'envoie par paquets ; seul le serveur a la clé. Tout le monde est enregistré, pas seulement la voix connue : sans l'autre moitié d'une conversation, l'assistant ne comprend pas un « oui ».
- Personne d'autre que l'assistant n'écoute ces enregistrements. La transcription se fait sur le serveur, pas chez un fournisseur extérieur. L'assistant retient ce qui concerne la personne et jette le son.
- Réglage de départ, modifiable : une conversation où la voix du propriétaire du téléphone n'apparaît jamais est jetée. Le propriétaire est belge : en droit belge, enregistrer une conversation à laquelle on participe n'est pas un délit, capter celle des autres l'est (à vérifier dans le nouveau Code pénal, en vigueur depuis avril 2026).
- Le propriétaire du téléphone peut toujours couper : un bouton dans la notification, appliqué sur le téléphone, sans l'assistant ni réseau, qui ferme tout le micro. Ce n'est pas la coupure des micros de la maison (E), que l'assistant juge : le propriétaire d'un téléphone n'est pas forcément celui de l'assistant.
- Des coupures automatiques selon le lieu (zones, nom du Wi-Fi) ou entre certaines heures. Les règles vivent sur le téléphone ; on les dit en langage naturel (« ne m'écoute pas au travail »), l'assistant les écrit et peut en proposer. Par défaut elles arrêtent l'écoute pour apprendre et laissent le mot de réveil. Le téléphone garde les dernières minutes avant de les envoyer et les efface s'il se découvre dans un lieu interdit.
- La notification dit toujours l'état du moment et sa raison.
- Les appels ne sont pas écoutés par l'application : Android ne le permet pas à une application ordinaire. Un propriétaire qui le veut peut faire passer ses appels par l'assistant (téléphonie par internet reliée au serveur) ; ce n'est pas construit ici.
- À mesurer en conception : ce que l'écoute coûte en batterie sur un vrai téléphone.
- Demande les chantiers A (un canal de plus, un appareil identifié), B (la mémoire, le nom que chacun lui donne) et D (entendre et parler). La reconnaissance de qui parle est commune avec E.

### Chantier E — La maison

*Les anciens chantiers 4 ter, 5 et 7.*

- Micros et haut-parleurs des pièces, présence, lumières, télé, son, agenda (Home Assistant en premier).
- Reconnaître qui parle, avec un degré de confiance, et une preuve de plus pour ce qui est sensible.
- Initiatives : agir sans qu'on le lui demande, dans l'intérêt et les limites de chacun.
- La coupure sur demande.
- Plusieurs personnes qui parlent en même temps de sujets différents : des tours en parallèle, chacun avec ses droits, et une seule voix à la fois par haut-parleur (l'assistant répond dans la bonne pièce, ou l'un après l'autre).
- Une discussion à plusieurs : une discussion partagée, où chaque parole est attribuée à qui l'a dite, chaque demande jugée avec les droits de celui qui la fait, et où l'assistant ne dit rien de privé d'une personne devant les autres. Aujourd'hui, une discussion appartient à une seule personne.
- Le oui d'un adulte dit à voix haute pour la demande d'un enfant vaut son accord, tout de suite, si l'assistant est sûr que c'est bien l'adulte ; un « c'est bon, fais-le » du propriétaire après un refus la fait faire sous ses droits à lui, pour cette fois seulement, sauf s'il dit que l'enfant peut désormais. Ce qui exige un oui ferme ou une preuve l'exige toujours.
- Le scénario des vacances avec un invité (voir B) : percevoir un gros problème dans la maison, et appeler.

### Chantier F — Les ordinateurs de la maison

*Demandé le 2026-11-12, pour plus tard.*

- Un programme installé sur chaque ordinateur que le propriétaire confie à l'assistant.
- L'assistant peut voir ce qu'il y a dessus et le contrôler, avec les droits de la personne qui demande.
- Il y affiche des graphiques, des images, des fichiers ou des tableaux de bord créés pour la personne à qui c'est destiné : une interface à lui, différente pour chacun.
- Demande les chantiers A (un canal de plus, un appareil identifié) et C (ce qu'il affiche est un outil qu'il se crée) ; la mémoire (B) lui dit ce que chacun aime voir.

### Chantier G — Agir sur internet

*Demandé le 2026-11-12, pour plus tard : tout ce qu'un être humain peut faire sur internet.*

- Commander un article, commander une pizza, réserver, chercher : par un connecteur quand il en existe un, sinon en se servant du site comme une personne, dans un navigateur qu'il pilote.
- Trouver des codes de réduction et les saisir pour la personne, sur l'ordinateur où elle est occupée (c'est le chantier F qui lui donne cette main).
- Les identifiants et les moyens de paiement restent au coffre : le gardien s'en sert pour lui, sans jamais les lui montrer.
- Un achat est une action comme une autre du catalogue ouvert, avec son coût parmi ses risques : le droit de la personne, les plafonds de dépense et le oui demandé se décident là, pas dans une règle à part.
- Le contenu des sites est du contenu extérieur : jamais suivi comme une consigne.
- Quand un site demande de prouver qu'un humain est là (CAPTCHA, détection de robots), l'assistant porte l'étape à la personne, là où elle est : sur l'ordinateur qu'elle désigne (« affiche-le sur tel PC »), ou sur son téléphone si elle n'a pas d'ordinateur à proximité. Elle la fait, et il reprend. Le propriétaire aurait voulu pouvoir aussi lui demander de la passer lui-même (2026-11-12) ; ce n'est pas prévu ici : la résolution automatique d'un CAPTCHA ne sera pas construite.
- Demande le chantier C (bac à sable, sortie réseau, catalogue ouvert) ; la saisie sur l'ordinateur de la personne demande F.

### Ensuite

Les serveurs et les applications (ancien chantier 6), l'iPhone, les appels qui passent par l'assistant, les autres connecteurs : des adaptateurs de plus sur A, B et C.

## 5. L'ordre accepté

A, B, C, D, E, F, G. G peut passer avant E et F pour tout ce qui ne touche pas l'ordinateur de la personne : il ne dépend que de C.

- A d'abord : sans lui, la mémoire et les initiatives seraient encore bâties autour de la page, et à refaire pour la voix.
- D peut passer juste après A si le propriétaire veut entendre l'assistant plus tôt : il ne dépend ni de B ni de C.
- D bis (l'application Android) a été ajouté le 2026-11-22, juste après D, avec l'accord du propriétaire : à commencer dès que D est fusionné.
- C avant E et F : les connecteurs de la maison et l'interface de chacun sont des outils, écrits par le projet ou par l'assistant.
- « Domo crée les personnes » a été ajouté le 2026-11-23 ; sa place dans l'ordre est fixée à la fusion. Il prépare l'endroit où la voix (D, E) se branche.

## 6. Ce qui ne change pas

Le socle de sécurité reste dans le code : l'isolation du cœur, les secrets jamais montrés à un modèle, le code d'un modèle jamais exécuté dans le cœur, les droits vérifiés avant chaque action, le journal. La règle du « possible » ne les lève pas : le droit de la personne est l'une de ses deux conditions.
