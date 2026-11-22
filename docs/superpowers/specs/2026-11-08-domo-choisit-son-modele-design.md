# L'assistant choisit son modèle

Conception du 8 novembre 2026, après la fusion de la version 0.5.0 (« L'assistant juge du oui »). C'est le deuxième des six chantiers décidés le 3 novembre :
1. L'assistant juge du oui (fait, 0.5.0) ;
2. L'assistant choisit son modèle selon le budget (ce document) ;
3. la mémoire et la marche en permanence ;
4. les connecteurs et les initiatives ;
5. les serveurs et les applications ;
6. les micros et les caméras, avec la coupure.

Elle s'appuie sur :
- `docs/superpowers/specs/2026-10-28-domo-architecture-design.md`, surtout §11 (le routeur de modèles), §12 (le coffre et le gardien) et §11.5 (l'appel des fournisseurs) ;
- `docs/superpowers/specs/2026-11-03-domo-juge-du-oui-design.md` (le juge du oui et le socle).

Elle remplace §11.1 à §11.3 de l'architecture là où elles diffèrent. §11.4 (garder des données en local) reste pour plus tard (§2).

Ce qui vient du propriétaire est marqué *(propriétaire)*. Ce que j'ai proposé et qu'il a accepté est marqué *(proposé, accepté)*. Ce que je décide seul est marqué *(mon choix)* et repris au §9.

## 1. Intention

« Le propriétaire ne choisit pas de modèle : il donne seulement le fournisseur et la clé, et c'est l'assistant qui choisit le meilleur modèle à utiliser » *(propriétaire, 2026-10-31)*. En testant la 0.4.0, il l'a redit : l'assistant doit choisir le meilleur modèle selon le budget qu'on lui donne et la puissance qu'il faut pour ce qu'on demande *(propriétaire, 2026-11-03)*.

**Aujourd'hui :**
- à l'installation, le propriétaire choisit un fournisseur, tape le nom d'un modèle, puis la clé ;
- il n'y a qu'un fournisseur et qu'un modèle (`llm.models.provider_in_use`), utilisé pour tout : la conversation, le réveil après un bouton, le juge du oui, la lecture d'un oui en mots, l'écriture des droits ;
- chaque réponse note ses tokens dans le journal, mais il n'y a ni prix, ni budget, ni compte.

**Après ce chantier :**
- on donne seulement le fournisseur et la clé, ou l'adresse d'un modèle local ;
- on peut donner plusieurs clés, de plusieurs fournisseurs, et un modèle local *(propriétaire)* ;
- à chaque appel, le code applique les choix de l'assistant : quel modèle, sur quelle clé ;
- le budget vient des demandes du propriétaire ; sans consigne, l'assistant juge, et il demande si nécessaire *(propriétaire)* ;
- L'assistant tient le compte de ce qu'il dépense, et lit la vraie dépense quand il en a le moyen *(propriétaire)* ;
- c'est l'assistant qui décide comment économiser *(propriétaire)*.

**Exemples qui doivent marcher :**
- À l'installation, le propriétaire choisit Anthropic et donne sa clé. Aucun nom de modèle n'est demandé. L'assistant vérifie la clé, regarde les modèles qu'elle ouvre et choisit.
- « Ajoute ma clé OpenAI. » Le formulaire sécurisé s'ouvre. Ensuite, l'assistant choisit parmi les modèles des deux clés.
- « J'ai un modèle sur mon PC à telle adresse. » l'assistant l'ajoute. Il coûte 0, et l'assistant s'en sert pour les petites tâches s'il convient.
- « Économise ce mois-ci. » l'assistant prend des modèles moins chers et dit ce qu'il change.
- « Ne dépasse jamais 20 € par mois. » C'est une limite ferme, tenue par le code. Arrivé au bout, l'assistant ne fait plus que prévenir et demander quoi faire.
- « Combien tu m'as coûté ce mois-ci ? » l'assistant répond par son estimation et dit que c'en est une. Avec une clé admin, il donne le vrai chiffre.
- « Trouve un moyen de savoir ce que je paie vraiment. » l'assistant explique ce qui existe chez le fournisseur et demande ce qu'il faut par le formulaire sécurisé.
- Justine, sur la clé du propriétaire, dit « prends le meilleur modèle ». L'assistant juge si c'est une bonne idée. Il peut le faire, dire non, ou proposer autre chose *(propriétaire)*.
- « Paul doit apporter sa propre clé. » Paul n'a pas de clé : l'assistant lui demande la sienne, sans lui parler au-delà.
- Une réponse du modèle est ratée (outil mal appelé). L'assistant réessaie un cran au-dessus et s'en souvient.

## 2. Objectifs, non-objectifs, anti-objectifs

**Objectifs**
1. L'installation ne demande que le fournisseur et la clé, ou l'adresse d'un serveur à soi (§4.1).
2. Plusieurs clés, de plusieurs fournisseurs, plus des modèles locaux, ajoutés en conversation (§4.2).
3. Les clés de chacun : à qui elles sont, qui peut s'en servir, les règles de la maison, les invités, la clé générale (§4.3).
4. Un catalogue des modèles, avec leurs prix et ce qu'ils savent faire (§4.4).
5. Le code applique les choix de l'assistant à chaque appel, dans le socle (§4.5, §4.6).
6. Les budgets : les limites fermes sont tenues par le code, le reste est au jugement de l'assistant (§4.7).
7. Le compte de ce qui est dépensé, et la vraie dépense quand un moyen existe (§4.8).
8. Les façons d'économiser de ce chantier, et la place pour celles des chantiers suivants (§4.9).

**Non-objectifs (plus tard)**
- Se mettre en pause pour économiser : il faut la marche en permanence (chantier 3).
- Écrire des scripts ou des programmes qui font économiser des tokens, et gérer son serveur : il faut le bac à sable et les serveurs (chantier 5) *(propriétaire, 2026-11-08 : l'assistant gère entièrement son serveur et d'autres si le propriétaire l'y autorise ou lui en donne les moyens)*.
- Reconnaître une voix avec un programme plutôt qu'un modèle : chantier 6.
- Lire la facture reçue par mail : il faut les connecteurs (chantier 4).
- Garder certaines données « en local seulement » (§11.4 de l'architecture) : ça n'a de sens qu'une fois que l'assistant lit des documents (chantier 4) *(proposé, accepté)*.
- Les sous-agents et leur budget (§11.3 de l'architecture).
- Une page pour suivre les coûts : on demande en conversation *(propriétaire)*.

**Anti-objectifs (échec même si « ça marche »)**
- Demander à quelqu'un de choisir un nom de modèle.
- Un juge du oui, une lecture d'un oui en mots ou une écriture des droits qui passe à un modèle trop faible pour économiser.
- Une clé utilisée par quelqu'un sans l'accord de celui qui l'a donnée.
- Une limite ferme dépassée.
- Une clé (normale ou admin) vue par l'assistant.
- Une estimation présentée comme la vraie facture.
- Un assistant qui demande un accord à chaque changement de modèle, ou qui refuse tout « pour économiser ».

## 3. Approches

**1. Des règles écrites dans le code.** Chaque appel dit son genre de tâche, et le code choisit : le modèle qui sait faire, que le budget permet, et le moins cher du niveau voulu (§11.2 de l'architecture). C'est simple, prévisible et sans appel en plus. Mais l'assistant ne juge pas : c'est une règle figée, l'inverse de ce que veut le propriétaire. Écartée.

**2. L'assistant choisit à chaque demande.** Avant chaque demande, un petit modèle la lit et choisit le modèle qui la traitera. C'est un vrai jugement, demande par demande. Mais il coûte un appel de plus à chaque fois, et il faut de toute façon protéger le juge du oui dans le code : une demande piégée pourrait pousser vers un juge faible. Écartée.

**3. Le socle dans le code, les choix à l'assistant.** Le code tient ce qui ne se discute pas : le catalogue et les prix, le compte, les limites fermes, qui peut utiliser quelle clé, et le niveau minimum des appels qui protègent (§6). L'assistant tient ses propres choix : quel modèle pour quel genre de tâche, quand économiser, ce qu'il a appris de ses erreurs. Il les change en conversation ou de lui-même, et le code les applique à chaque appel, sans appel en plus. Pendant une demande, l'assistant peut passer un cran au-dessus. C'est le même principe que la 0.5.0. **Retenue** *(propriétaire)*.

## 4. Ce que voit et fait l'assistant

### 4.1 L'installation

1. Le propriétaire choisit le genre de fournisseur : Anthropic, OpenAI, Google, ou un serveur à soi (une adresse, avec ou sans clé). Il n'y a plus de champ « nom du modèle ».
2. La clé est donnée dans le formulaire du coffre, comme aujourd'hui. Elle va du navigateur au coffre, jamais par le cœur.
3. L'assistant vérifie : il demande au fournisseur la liste des modèles que la clé ouvre (`GET /models`, par le relais du gardien), puis il fait un tout petit appel avec un modèle qu'il choisit, le moins cher qui sait appeler des outils *(mon choix)*.
4. Si la liste ou l'appel échoue, la page propose ce qu'elle propose aujourd'hui : redonner la clé, réessayer, ou recommencer avec un autre fournisseur.
5. La clé de l'installation est celle du propriétaire et devient la **clé générale** (§4.3).

Pour un serveur à soi, la liste des modèles vient du serveur (`/models` de l'API compatible OpenAI). Si le serveur n'en donne pas, la page d'installation (ou d'ajout de la clé) demande le nom du modèle qu'il sert : c'est le seul cas où un nom est demandé, parce que l'assistant n'a aucun autre moyen de le savoir. La page le demande elle-même, puisque sans modèle l'assistant ne peut pas encore parler *(mon choix)*.

### 4.2 Les clés et les modèles

Une **clé** a :
- la personne qui l'a donnée ;
- son fournisseur et son adresse ;
- sa référence dans le coffre (jamais sa valeur), ou aucune pour un serveur sans clé ;
- les modèles qu'elle ouvre, relus quand l'assistant le juge utile, et au moins une fois par jour quand il s'en sert *(mon choix)* ;
- qui peut s'en servir (§4.3) ;
- ses limites (§4.7).

On en ajoute une en conversation : « ajoute ma clé OpenAI », « j'ai un modèle à telle adresse ». Le formulaire sécurisé s'ouvre, comme pour redonner la clé aujourd'hui (`model.rekey`). Celui qui la donne en devient le propriétaire. L'assistant la vérifie comme à l'installation.

On redonne une clé qui ne marche plus de la même façon. Ce qui existe pour la clé du modèle (`tools/model_key.py`) vaut pour toute clé, et seul celui qui l'a donnée peut la redonner *(mon choix)*.

Celui qui a donné une clé peut la retirer. Si c'est la dernière clé que la maison peut utiliser, l'assistant prévient d'abord que plus personne ne pourra lui parler, et il demande d'en donner une autre avant *(mon choix)*.

### 4.3 Les clés de chacun

**Par défaut, l'assistant gère pour tout le monde** : tout le foyer se sert de la clé générale *(propriétaire)*.

**Tout doit être possible** *(propriétaire)* :
- seulement certaines personnes sur une clé, et les autres apportent la leur ;
- chacun qui donne son propre budget ou sa propre clé ;
- un invité qui se sert de la clé de celui qui l'invite, ou qui doit fournir la sienne.

**Une clé appartient à celui qui l'a donnée.** Lui seul décide qui peut s'en servir. Même le propriétaire ne se sert pas de la clé de quelqu'un d'autre sans son accord ; il peut par contre exiger que quelqu'un apporte la sienne *(proposé, accepté)*.

**Les règles de la maison** (qui doit apporter sa clé, qui est sur la clé générale) sont au propriétaire par défaut, et se donnent comme n'importe quel droit *(proposé, accepté)*.

**À l'invitation**, l'invité se sert de la clé de celui qui l'invite, ou doit apporter la sienne. Le choix par défaut vient du propriétaire ; sans consigne, l'assistant décide seul, ou le demande à celui qui invite s'il le juge nécessaire *(propriétaire)*. Il se sert alors aussi de la clé générale, tant que celui qui l'invite s'en sert (§11.1).

**Comment l'accord est gardé** *(mon choix)* :
- chaque clé a sa liste de qui peut s'en servir : des personnes, « tout le foyer », ou « mes invités » ;
- au départ, la clé générale est à « tout le foyer », puisque l'assistant gère pour tout le monde *(propriétaire)* ;
- au départ, une autre clé n'est qu'à celui qui l'a donnée ;
- une règle de la maison (« Paul apporte la sienne ») retire une personne de la clé générale ;
- à l'invitation, « l'invité se sert de ma clé » ajoute l'invité à la liste de la clé de celui qui invite.

**Quelle clé pour une demande** *(mon choix)* :
1. la clé de la personne, si elle en a donné une ;
2. sinon, une clé qu'elle a le droit d'utiliser : celle de qui l'a invitée, puis la clé générale ;
3. s'il y en a plusieurs, l'assistant choisit d'après ses choix (§4.6) et le budget qui reste.

Tous les appels faits pour la demande d'une personne passent sur cette clé, le juge du oui compris *(proposé, accepté)*. Le réveil de l'assistant après un bouton passe sur la clé de celui dont c'est la conversation.

**Quelqu'un sans clé utilisable.** C'est à l'assistant et au propriétaire de voir *(propriétaire)* :
- si le propriétaire l'accepte, la clé générale sert à « l'entretien » : demander à la personne de fournir sa clé, et plus tard reconnaître sa voix, qui peut demander un modèle intelligent sauf si l'assistant construit un programme pour ça *(propriétaire)*. Ça ne va jamais jusqu'à une vraie conversation sur la clé générale, sauf si le propriétaire le dit *(proposé, accepté)* ;
- sinon, la page lui montre un message fixe, sans modèle, avec le formulaire pour donner sa clé *(proposé, accepté)*.

Sans consigne du propriétaire, l'entretien sur la clé générale est permis *(mon choix)* : il coûte quelques petits appels et c'est le seul moyen d'expliquer à la personne ce qu'il lui faut.

### 4.4 Le catalogue

- **Les prix et ce que savent faire les modèles** viennent d'un instantané du catalogue ouvert **models.dev** (licence MIT, vérifié le 2026-11-08). Pour chaque modèle, il donne : les prix d'entrée, de sortie, de lecture et d'écriture du cache, en dollars par million de tokens ; les tailles de contexte et de sortie ; l'appel d'outils, les images, le raisonnement, la sortie structurée.
- L'instantané est livré avec l'assistant, réduit aux fournisseurs qu'il connaît (Anthropic, OpenAI, Google) et aux champs utiles *(mon choix)* : le fichier complet fait près de 6 Mo. L'assistant peut le rafraîchir par le gardien, quand il le juge utile.
- **Ce qu'ouvre une clé** vient du fournisseur (`GET /models`). Les trois fournisseurs donnent la liste sans les prix (vérifié le 2026-11-08). L'assistant rapproche chaque modèle de la liste de son entrée du catalogue.
- **Un modèle sans prix connu** n'est utilisé que si rien d'autre ne convient. Il est compté au prix le plus haut de son fournisseur, et l'assistant le dit quand il en parle *(mon choix)*.
- **Un modèle local** coûte 0. Ce qu'il sait faire, l'assistant l'apprend en l'essayant (§4.6).
- **Les niveaux.** Chaque modèle a un niveau : *petit*, *moyen* ou *fort*. Le projet livre une table des niveaux par famille de modèles (Haiku, Sonnet, Opus, Gemini Flash, Gemini Pro…) *(mon choix)*. Ensuite, l'assistant l'ajuste d'après l'expérience, par genre de tâche (§4.6). Un modèle absent de la table compte comme *petit* pour les niveaux minimums (§6), tant que le propriétaire n'a pas dit autre chose *(mon choix)*.

### 4.5 Les genres de tâches

Chaque appel au modèle dit son genre de tâche et ce qu'il lui faut. Dans ce chantier, il y en a six :

| Genre | Il lui faut | Niveau de départ (l'assistant le change) | Niveau minimum (socle, §6) |
|---|---|---|---|
| la conversation, et le réveil après un bouton | les outils, assez de contexte pour l'historique | *moyen* | aucun |
| le juge du oui | — | *moyen* | *moyen* |
| la lecture d'un oui en mots | — | *moyen* | *moyen* |
| l'écriture des droits | — | *moyen* | *moyen* |
| la vérification d'une clé | les outils | *petit* | aucun |
| l'entretien (§4.3) | — | *petit* | aucun |

Le niveau de départ est *(mon choix)* : la conversation part sur *moyen*, parce qu'un modèle *petit* appelle souvent mal les outils. L'assistant le change ensuite selon ce qu'il apprend (§4.6).

Les chantiers suivants en ajouteront : vision, long document, code, résumé, mémoire… Le catalogue des genres est une table du projet.

Le niveau minimum du juge du oui et de l'écriture des droits est une proposition acceptée *(proposé, accepté)*. Celui de la lecture d'un oui en mots est *(mon choix)* : elle décide si des mots valent un oui.

**Si la clé de la personne n'a aucun modèle du niveau minimum**, l'assistant ne baisse pas la barre *(proposé, accepté)* :
- le juge du oui fait comme quand il ne répond pas : il demande un oui (`judge.fallback`), et les règles du propriétaire et ses oui par défaut comptent toujours ;
- la lecture d'un oui en mots fait comme quand elle ne répond pas : « pas clair », la demande attend le bouton ;
- l'écriture des droits ne se fait pas, et l'assistant dit pourquoi.

### 4.6 Les choix de l'assistant

L'assistant tient ses choix, enregistrés :
- pour chaque genre de tâche : un modèle précis, ou « le moins cher qui convient », ou « le plus fort » ;
- s'il économise, et à quel point ;
- ce qu'il a appris : tel modèle a raté tel genre de tâche tant de fois ;
- pour chaque changement : qui l'a demandé, quand et pourquoi.

**Sans consigne**, l'assistant prend le modèle le moins cher qui fait bien le travail. Pour une demande difficile ou importante, il en prend un plus fort *(proposé, accepté)*.

Concrètement *(mon choix)* :
- le code prend le moins cher du niveau que l'assistant a fixé pour le genre de tâche (au départ, celui du §4.5), sur la clé de la demande ;
- le code ne devine jamais seul qu'une demande est difficile : c'est l'assistant qui le juge, pendant la demande, en passant un cran au-dessus (plus bas).

**Où vivent les choix** *(mon choix)* :
- ils valent pour toute la maison ;
- une clé peut avoir ses propres réglages, par exemple « économise sur ma clé » demandé par celui qui l'a donnée.

**Les changer.**
- En conversation : « économise », « utilise Claude pour le code », « prends le meilleur modèle ». L'assistant juge si c'est une bonne idée. Il peut le faire, dire non, voir avec la personne ce qui est mieux, ou proposer autre chose, qu'elle accepte ou non. C'est vrai même sur la propre clé de la personne *(propriétaire)*.
- De lui-même : après des échecs, quand une limite approche, quand un modèle disparaît de la liste. Il le dit quand il juge que c'est utile.
- Changer ses choix ne change rien dans le foyer : l'outil n'est pas soumis au juge du oui, mais il est inscrit au journal *(mon choix)*.

**Pendant une demande**, l'assistant peut décider que celle-là mérite plus fort et passer un cran au-dessus pour le reste de la demande : au plus deux crans par demande, et jamais au-delà d'une limite ferme *(mon choix)*.

**Quand une réponse est ratée**, l'assistant réessaie un cran au-dessus et s'en souvient *(proposé, accepté)*. Le partage *(mon choix)* :
- **le code voit lui-même** une réponse mal formée, ou un outil appelé avec de mauvais arguments ou inconnu. Il refait alors l'appel au modèle un cran au-dessus, au plus deux fois, avec le même historique et l'erreur rendue, pour que le modèle sache ce qui n'allait pas. Ces erreurs sont vues avant qu'un outil ne tourne, donc un nouvel essai ne refait jamais une action ;
- **L'assistant juge lui-même** un échec que le modèle déclare, ou une correction de la personne. Il le note dans ce qu'il a appris (avec l'outil de ses choix), sans nouvel essai automatique ;
- chaque échec est enregistré, et l'assistant en tient compte dans ses choix.

**Ce que l'assistant apprend ne fait jamais entrer un modèle dans un niveau minimum** *(proposé, accepté)*. L'apprentissage peut faire monter ces trois tâches vers un modèle plus fort, ou choisir parmi ceux qui ont déjà le niveau, jamais faire entrer un modèle plus faible : pour elles, les erreurs ne se voient pas (un juge trop faible qui laisse passer ne provoque aucune erreur). Pour le juge du oui, la lecture d'un oui en mots et l'écriture des droits, seuls comptent la table des niveaux du projet et ce que dit le propriétaire. Un modèle local qui se débrouille bien ne devient donc pas juge du oui sans son accord.

**Un contenu extérieur** (dans les chantiers suivants) peut influencer les choix de l'assistant, comme toute conversation. Mais il ne peut jamais baisser un niveau minimum, dépasser une limite ferme, ni ouvrir une clé : le code les tient (§6).

### 4.7 Les budgets

**Le budget vient des demandes** *(propriétaire)* : une somme (« 20 € par mois »), « économise », « fais attention ce mois-ci », ou rien. Sans consigne, l'assistant juge, et il demande si nécessaire.

**Une limite ferme** (« ne dépasse jamais 20 € par mois ») est tenue par le code *(proposé, accepté)*.
- Elle porte sur une clé, ou sur une personne sur une clé.
- Avant chaque appel, le code compare ce qui est déjà dépensé, plus le maximum que l'appel peut coûter, à la limite. Si ça dépasse, l'appel n'est pas fait.
- Le maximum d'un appel *(mon choix)* : l'entrée estimée d'après le texte vraiment envoyé, plus le plafond de sortie de l'appel, aux prix du catalogue.
- Ce maximum est réservé avant l'appel, puis remplacé par le coût réel après. Chaque appel est vérifié à part : un nouvel essai, un cran au-dessus, chaque tour d'outils. Des appels en même temps voient les réservations des autres.
- La limite tient sur le compte de l'assistant. La vraie facture peut s'en écarter de l'erreur de l'estimation, et l'assistant le dit quand il en parle.
- Arrivé au bout, l'assistant ne fait plus que prévenir et demander quoi faire *(proposé, accepté)*. Comme il ne peut plus appeler de modèle sur cette clé, c'est un message fixe. Il dit ce qui est atteint et ce qu'on peut faire : relever la limite (celui qui l'a posée), ou attendre la période suivante.
- Une clé déjà au bout de sa limite laisse la place à la clé permise suivante, dans l'ordre du §4.3 : la demande suivante passe dessus, et l'assistant le voit et le dit s'il le juge utile *(mon choix)*. C'est permis par celui qui a donné l'autre clé, et ses propres limites tiennent. Le message fixe ne vient que quand aucune clé permise n'a plus de marge, ou quand un appel ne tient plus dans la demande en cours.
- Avant le bout, c'est au propriétaire et/ou à l'assistant de juger s'il faut prévenir *(propriétaire, 2026-11-08)*. Le code ne prévient pas de lui-même, et il n'y a pas de seuil par défaut : ce que l'architecture prévoyait (une alerte à 80 %, §11.3) serait une règle figée.
  - Le code donne à l'assistant où en est chaque limite que la personne a posée, et l'assistant juge s'il en parle *(mon choix)*.
  - Si celui qui a posé la limite demande d'être prévenu à un seuil (« préviens-moi à 80 % »), le seuil est gardé avec la limite, et l'assistant le dit une fois arrivé là, dans la période.
  - L'assistant peut aussi se donner un seuil à lui, dans ses choix (§4.6).

**Qui pose les limites.** Sur sa clé, celui qui l'a donnée. Sur la clé générale, le propriétaire, pour la maison ou pour une personne. Ça se donne comme un droit *(mon choix)*.

**La période** est le mois du calendrier, à l'heure du foyer, sauf si la personne dit autre chose (« par semaine ») *(mon choix)*.

**La monnaie.** Les fournisseurs et le catalogue comptent en dollars. Une limite dite en euros est convertie avec un taux que l'assistant garde et rafraîchit avec le catalogue. Chaque vérification prend le dernier taux, pour que la limite garde son sens en euros. Il parle à chacun dans sa monnaie, en disant que c'est une estimation *(mon choix)*.

### 4.8 Le compte, et la vraie dépense

**Le compte de l'assistant.** Chaque appel est compté : tokens d'entrée, de sortie, de lecture et d'écriture du cache, multipliés par les prix du catalogue. Le compte garde la clé, la personne, le genre de tâche, le modèle et le moment. Quand le fournisseur ne donne pas ses tokens, l'assistant les estime d'après la longueur des textes *(mon choix)*.

**En conversation** *(propriétaire)* : « combien tu m'as coûté ce mois-ci ? », « et Justine ? ».
- Chacun voit sa propre dépense, et ce qu'ont coûté les clés qu'il a données, personne par personne. Jamais les conversations *(proposé, accepté)*.
- L'assistant dit toujours si c'est son estimation ou la vraie dépense.

**La vraie dépense.** L'assistant ne voit pas la facture. Le propriétaire peut lui demander de trouver un moyen, et l'assistant doit voir de lui-même s'il en a déjà un *(propriétaire)*. Ce qui existe, vérifié le 2026-11-08 :
- **Anthropic** : une API de dépense et d'usage (`/v1/organizations/cost_report`, `/v1/organizations/usage_report/messages`). Elle demande une clé admin, et elle n'existe pas pour les comptes individuels, seulement pour les organisations. La dépense y est donnée par jour, en dollars.
- **OpenAI** : une API de dépense avec une clé admin existerait (`/v1/organization/costs`). Elle n'a pas pu être vérifiée (page refusée) : le constructeur la vérifie. Si elle existe, même chemin qu'Anthropic ; sinon, estimation seulement.
- **Google** : pas de moyen avec la clé. La dépense se voit dans AI Studio ou la facturation Google Cloud. Estimation seulement, et l'assistant le dit.

Quand un moyen existe, l'assistant l'explique et demande la clé admin par le formulaire sécurisé. Elle va au coffre, et le gardien l'injecte comme les autres. Quand l'assistant a la vraie dépense, il corrige son estimation pour cette clé, et il retient l'écart pour la suite *(mon choix)*.

### 4.9 Les économies

C'est l'assistant qui décide comment économiser *(propriétaire)*. Dans ce chantier, ses moyens :
- choisir un modèle moins cher, ou local, pour ce qui le permet ;
- envoyer moins d'historique quand ça suffit ;
- répondre plus court (moins de tokens de sortie) ;
- profiter du cache des fournisseurs, où un texte déjà envoyé coûte beaucoup moins cher la fois suivante ;
- ne pas monter d'un cran quand ce n'est pas nécessaire.

Les chantiers suivants ajouteront leurs moyens : se mettre en pause (chantier 3), des scripts et des programmes qui évitent des appels (chantier 5), un programme pour la voix (chantier 6). Les choix de l'assistant (§4.6) sont faits pour les accueillir.

## 5. Ce qui change dans le code

### 5.1 Les fournisseurs deviennent des clés

`llm.models.Provider` (un fournisseur, un modèle) devient une clé (§4.2) :
- elle n'a plus de colonne de modèle ;
- elle a celui qui l'a donnée, et la liste de ses modèles.

`provider_in_use` disparaît : chaque appel passe par le choix (§5.3).

L'assistant n'est installé nulle part en vrai *(propriétaire, 2026-11-03)* : la migration sert à la base de développement. La clé existante devient la clé générale du propriétaire, et son ancien modèle est oublié.

### 5.2 L'installation

Dans `setup_flow/model_routes.py` et `fragments/model_form.html`, le champ du nom du modèle disparaît. La vérification se fait par la liste des modèles, puis un petit appel (§4.1).

### 5.3 Le choix à chaque appel

Une fonction reçoit le genre de tâche, la personne et ce qu'il faut (outils, taille), et rend une clé et un modèle, ou une raison de ne pas appeler : pas de clé, limite atteinte, pas de modèle du niveau minimum. Elle lit les choix de l'assistant, le catalogue, les clés permises et les limites.

Tous les appels passent par elle :
- la conversation (`chat/routes.py`, `chat/agent.py`) ;
- le réveil (`chat/wake.py`) ;
- le juge du oui (`confirmations/judge.py`) ;
- la lecture d'un oui en mots (`confirmations/intent.py`) ;
- l'écriture des droits (`policy/rights_writer.py`) ;
- la vérification d'une clé (`llm/probe.py`).

Le cran au-dessus et le nouvel essai après une réponse ratée (§4.6) passent par elle aussi.

### 5.4 Le compte

Chaque appel écrit son coût estimé (§4.8). Le journal d'audit de chaque tour garde, en plus d'aujourd'hui, la clé, le modèle choisi, le genre de tâche et l'estimation.

### 5.5 Les outils et les actions

Nouveaux genres d'actions au catalogue (`policy/catalog.py`), avec des noms laissés au constructeur :
- **ajouter une clé** (la sienne) ;
- **redonner une clé** (la sienne ; remplace `model.rekey`) ;
- **retirer une clé** (la sienne) ;
- **dire qui peut se servir de sa clé** ;
- **poser ou retirer une limite** (sur sa clé ; sur la clé générale, le propriétaire) ;
- **voir ce qui a été dépensé** (§4.8) ;
- **donner une clé admin** (la sienne, pour sa clé) ;
- **changer les choix de l'assistant** (§4.6 ; ne change rien dans le foyer) ;
- **passer un cran au-dessus** pour la demande en cours ;
- **les règles de la maison sur les clés** (au propriétaire par défaut).

Ce qui porte sur sa propre clé est à chaque membre. Les règles de la maison et les limites de la clé générale sont au propriétaire par défaut, et se donnent comme un droit. Le juge du oui juge ces actions comme les autres, sauf changer les choix de l'assistant et passer un cran au-dessus (§4.6).

### 5.6 Les invitations

Une invitation dit si l'invité se sert de la clé de celui qui invite ou apporte la sienne (§4.3).

### 5.7 Le gardien

Le relais laisse déjà passer tout chemin sous l'adresse du fournisseur : la liste des modèles et les API de dépense passent par lui. Une clé admin est un secret comme les autres, lié à l'adresse du fournisseur. Le rafraîchissement du catalogue et du taux de change passe aussi par le gardien, puisque le cœur ne sort pas sur Internet seul (§12 de l'architecture).

### 5.8 Les textes de l'assistant

`prompts/system.md` dit à l'assistant :
- qu'il choisit ses modèles ;
- quels sont ses moyens d'économiser ;
- qu'il juge les demandes de modèle ;
- qu'il parle de dépense en disant si c'est une estimation.

## 6. Le socle (ce que le code tient)

- **Les clés sont des secrets.** L'assistant n'en voit jamais la valeur, qu'elle soit normale ou admin. Elle va du navigateur au coffre, et le gardien l'injecte (§12 de l'architecture).
- **On ne se sert d'une clé qu'avec l'accord de celui qui l'a donnée**, propriétaire compris *(proposé, accepté)*.
- **Les limites fermes** ne sont jamais dépassées (§4.7).
- **Les niveaux minimums** du juge du oui, de la lecture d'un oui en mots et de l'écriture des droits (§4.5). Sans modèle de ce niveau, on fait comme quand ils ne répondent pas, jamais plus faible.
- **Tout ce que la 0.5.0 a posé reste.** Le juge ne voit jamais de contenu extérieur, et le plus exigeant gagne.
- **Le journal** garde chaque clé ajoutée, redonnée, retirée ou partagée, chaque limite posée, et chaque changement des choix de l'assistant.

Tout le reste du choix des modèles est au jugement de l'assistant.

## 7. Textes à écrire (français et anglais)

- l'installation sans nom de modèle ;
- la carte et le formulaire pour ajouter, redonner ou retirer une clé, et pour une clé admin ;
- le message fixe « limite atteinte » (§4.7) ;
- le message fixe « pas de clé utilisable » (§4.3) ;
- la section des modèles et du budget dans `prompts/system.md` (§5.8) ;
- les descriptions des nouveaux outils.

Chaque texte parle à celui qui le lit, et dit « tu » ou « vous » comme les textes de la 0.5.0.

## 8. Scénarios qui doivent passer

1. L'installation avec une clé Anthropic : pas de champ de modèle. L'assistant vérifie la liste, choisit, et la conversation marche.
2. L'installation avec un serveur à soi qui ne donne pas sa liste : la page demande le nom du modèle.
3. Le propriétaire ajoute une clé OpenAI en conversation. Les deux clés sont utilisables, et l'assistant choisit entre elles.
4. Une conversation ordinaire prend le modèle le moins cher qui convient. Une demande difficile monte d'un cran, au plus deux.
5. Un outil mal appelé : nouvel essai un cran au-dessus, et l'échec est enregistré.
6. Le juge du oui ne passe jamais sous *moyen*. Sur une clé sans modèle *moyen*, il demande un oui.
7. « Économise ce mois-ci » : les choix changent, sont inscrits au journal, et l'assistant dit ce qu'il change.
8. « Ne dépasse jamais 1 $ » : l'appel qui ferait dépasser n'est pas fait, et le message fixe s'affiche.
9. « Combien tu m'as coûté ? » : l'estimation, dite comme une estimation.
10. Une clé admin Anthropic donnée : la vraie dépense est lue, et l'estimation est corrigée.
11. Justine, sur la clé générale, demande le meilleur modèle : l'assistant juge, et peut proposer autre chose.
12. Le propriétaire dit que Paul doit apporter sa clé. Paul, qui n'en a pas, reçoit une demande de clé, par l'entretien ou par le message fixe, selon la règle du propriétaire.
13. Un invité d'un adulte qui a sa clé se sert de la clé de l'adulte, si l'adulte l'a permis à l'invitation.
14. Le propriétaire n'utilise pas la clé de Justine sans son accord.
15. L'assistant ne voit jamais une clé, ni dans un texte, ni dans un journal, ni dans une erreur.
16. Retirer la dernière clé utilisable : l'assistant prévient et demande une autre clé d'abord.

## 9. Mes choix, à vérifier par le propriétaire

1. La vérification d'une clé se fait avec le moins cher qui sait appeler des outils (§4.1).
2. Un serveur à soi sans liste de modèles : la page demande le nom, seul cas où on le demande (§4.1).
3. La liste des modèles d'une clé est relue au moins une fois par jour quand l'assistant s'en sert (§4.2).
4. Seul celui qui a donné une clé peut la redonner (§4.2).
5. Retirer la dernière clé : l'assistant prévient et demande une autre clé d'abord (§4.2).
6. L'ordre des clés pour une demande : la sienne, celle de qui l'a invitée, puis la clé générale (§4.3).
7. Sans consigne, l'entretien sur la clé générale est permis (§4.3).
8. Le catalogue livré est réduit aux trois fournisseurs et aux champs utiles (§4.4).
9. Un modèle sans prix connu est compté au prix le plus haut de son fournisseur (§4.4).
10. La table des niveaux est livrée par le projet, et un modèle absent compte comme *petit* pour les minimums (§4.4).
11. La lecture d'un oui en mots a aussi un niveau minimum (§4.5).
12. Changer ses choix n'est pas soumis au juge, mais est inscrit au journal (§4.6).
13. Au plus deux crans au-dessus par demande, et deux nouveaux essais après une réponse ratée (§4.6).
14. Le code donne à l'assistant où en est chaque limite que la personne a posée, pour qu'il juge s'il en parle (§4.7).
15. Les limites se posent sur sa clé, ou sur la clé générale pour le propriétaire, et se donnent comme un droit (§4.7).
16. La période est le mois du calendrier, à l'heure du foyer (§4.7).
17. Les comptes sont en dollars, et convertis dans la monnaie de chacun (§4.7).
18. Sans tokens du fournisseur, l'assistant les estime d'après la longueur (§4.8).
19. La vraie dépense corrige l'estimation, et l'assistant retient l'écart (§4.8).
20. L'accord d'une clé est une liste : des personnes, « tout le foyer » ou « mes invités ». Au départ, la clé générale est à tout le foyer, et une autre clé à celui qui l'a donnée seulement (§4.3).
21. La conversation part sur le niveau *moyen*, et la vérification d'une clé et l'entretien sur *petit* (§4.5).
22. Le code prend le moins cher du niveau fixé, et ne devine jamais seul qu'une demande est difficile : c'est l'assistant qui monte d'un cran (§4.6).
23. Les choix valent pour toute la maison, avec des réglages possibles par clé (§4.6).
24. Le code ne réessaie seul que sur une erreur qu'il voit. L'échec déclaré et la correction de la personne sont jugés par l'assistant (§4.6).
26. Une limite ferme réserve le coût maximum de chaque appel avant de le faire, et tient sur le compte de l'assistant (§4.7).
27. Une limite en euros se vérifie au dernier taux (§4.7).

## 10. Laissé au constructeur

- Le schéma des tables : clés, modèles d'une clé, qui peut s'en servir, limites, compte, choix de l'assistant.
- Les noms des outils et des genres d'actions.
- Comment rapprocher un modèle de la liste du fournisseur de son entrée de models.dev.
- Le contenu de la table des niveaux.
- La route du gardien pour rafraîchir le catalogue et le taux de change, et la source du taux.
- La vérification de l'API de dépense d'OpenAI (§4.8).
- Le cache de chaque fournisseur, dans chaque adaptateur.
- Comment le faux modèle des essais (`tests/fakes/fake_llm/app.py`) sert une liste de modèles et joue plusieurs modèles de niveaux différents.
- Le découpage en tâches.

## 11. Ajout du 9 novembre : les invités et les clés

Ajouté après la relecture finale, avant la fusion, à la demande du propriétaire. Il a relu le récap et a dit : « c'est normal, un invité peut utiliser la clé générale si la personne qui l'invite utilise cette clé aussi ». L'assistant peut l'en avertir, ou non, selon son choix. Le propriétaire peut changer cette règle pour les prochaines fois, ou mettre une limite de montant pour les invités. « Tous les cas de figure possibles doivent pouvoir être pris en compte et faits par l'assistant » *(propriétaire)*.

Cet ajout passe avant le §4.3 et le §4.7 là où ils diffèrent.

### 11.1 Un invité suit celui qui l'invite

- Un invité qui se sert des clés de celui qui l'invite se sert aussi de la clé générale, tant que celui qui l'invite s'en sert *(propriétaire)*.
- C'est vrai même quand la règle par défaut des invitations est « les invités apportent leur clé ». Cette règle ne vaut que quand l'invitation ne dit rien *(proposé, accepté)*. La correction de la relecture finale, qui l'en empêchait, est enlevée.
- L'invité suit son hôte dans le temps. Si l'hôte est retiré de la clé générale, l'invité n'y est plus non plus. S'il y revient, l'invité aussi *(proposé, accepté)*.
- Une règle de la maison sur l'invité lui-même passe avant : « Robin peut se servir de ma clé générale », ou « Robin apporte la sienne » *(mon choix)*.
- Par cette voie, l'invité n'a jamais plus que son hôte. Une règle qui retire la clé générale à tout le foyer, ou aux invités du propriétaire, vaut aussi pour lui *(mon choix)*.
- Si l'hôte quitte le foyer, l'invité suit toujours ce que les règles disent de son hôte, comme s'il était encore là. Sinon, celui qui reçoit ton rôle et tous ceux que tu as invités perdraient la clé générale le jour où tu pars. Pour retirer l'invité, il y a la règle sur lui *(mon choix)*.
- Celui qui doit apporter sa clé (son invitation le dit, ou elle ne dit rien et la règle de la maison le veut) n'a pas la clé générale, comme avant.
- Ces règles valent pour tous ceux qu'on invite, quel que soit leur rôle *(mon choix)*. Celui qui revient avec un lien de retour suit l'hôte qui l'avait invité la première fois, mais la règle des prochains invités (§11.2) ne s'applique pas de nouveau à son retour : c'est laissé pour plus tard (§11.6).

### 11.2 Changer la règle pour les prochaines fois

- Le propriétaire, ou celui à qui il a donné les règles de la maison sur les clés, peut dire : « à partir de maintenant, les invités ne se servent plus de ma clé générale, même si leur hôte s'en sert » *(propriétaire)*. C'est une nouvelle règle de la maison, avec deux valeurs : suivre l'hôte (par défaut) ou jamais.
- Elle vaut pour ceux qui arrivent ensuite, quoi que dise leur invitation *(propriétaire : « pour les prochaines fois »)*. Ceux qui sont déjà là gardent ce qu'ils ont. L'assistant peut les retirer de la clé générale un par un si on le lui demande, avec la règle sur une personne qui existe déjà *(mon choix)*.
- La règle sur une personne (« Robin peut », « Robin ne peut pas ») reste possible à tout moment.

### 11.3 Une limite pour les invités

- Une limite peut porter sur un rôle : les invités, les enfants ou les adultes *(proposé, accepté ; les rôles, mon choix)*.
- Elle se pose pour chacun (chaque invité a sa propre somme) ou pour tous ensemble (une somme partagée) *(proposé, accepté)*. Quand la personne ne le dit pas, l'assistant comprend d'après ses mots, ou lui demande *(mon choix)*.
- Elle vaut pour tous ceux qui ont ce rôle au moment de l'appel, y compris ceux qui arrivent ensuite. « Ensemble » compte ce qu'ont dépensé dans la période, sur cette clé, ceux qui ont ce rôle au moment de l'appel *(mon choix)*.
- Elle se pose comme les autres limites (§4.7). Sur la clé générale, c'est le propriétaire, par les règles de la maison. Sur sa propre clé, c'est celui qui l'a donnée *(mon choix)*.
- Toutes les limites qui s'appliquent à un appel tiennent à la fois : celle de la clé, celle du rôle, celle de la personne *(mon choix)*.
- Comme pour les autres limites, l'assistant sait où elle en est, et un seuil demandé est dit une fois. Pour « chacun », il sait la plus grosse part dépensée, et par qui *(mon choix)*.

### 11.4 L'assistant sait qui commence à se servir de ta clé

- Celui qui a donné une clé, la clé générale comprise, l'apprend à son prochain tour de conversation : qui a commencé à s'en servir depuis la dernière fois. L'assistant sait le nom de la personne, son rôle, qui l'a invitée, et ce qu'elle a dépensé sur cette clé dans le mois *(proposé, accepté)*.
- C'est dit à l'assistant une seule fois, et c'est lui qui juge s'il en parle *(propriétaire : l'assistant pourra en avertir le propriétaire, « ou non selon son choix »)*. Il peut toujours le retrouver dans ce qui a été dépensé (§4.8).
- Compte comme « commencer à s'en servir » le premier appel pour une demande de la personne. L'entretien (§4.3) et la vérification d'une clé ne comptent pas *(mon choix)*.
- Le code ne prévient toujours personne de lui-même (§4.7). Écrire le premier, sans qu'on lui ait parlé, viendra avec le chantier 3 (l'assistant écrit tout avec ses mots) ou le chantier 5 (les initiatives) *(proposé, accepté)*.
- Au plus dix personnes par tour, les plus anciennes d'abord ; les autres au tour suivant *(mon choix)*.

### 11.5 Scénarios qui doivent passer

17. La règle de la maison veut que les invités apportent leur clé. Alex, sur la clé générale, invite Robin en disant qu'il se sert de ses clés : Robin se sert de la clé générale.
18. Le propriétaire retire Alex de la clé générale : Robin n'y est plus non plus. Il l'y remet : Robin aussi.
19. « À partir de maintenant, les invités ne se servent plus de ma clé générale » : le prochain invité d'Alex n'y est pas, mais Robin y est toujours.
20. « Pas plus de 2 € par mois pour chaque invité » : un invité arrivé au bout ne passe plus sur la clé, un autre invité si.
21. « 5 € par mois pour tous les invités ensemble » : quand leur total y arrive, aucun invité ne passe plus sur la clé, mais un adulte si.
22. Robin fait sa première demande sur la clé générale. Au tour suivant du propriétaire, l'assistant sait que Robin a commencé à s'en servir, qui l'a invité et ce qu'il a dépensé, une seule fois.

### 11.6 Laissé pour plus tard

- Une règle pour les invités d'une seule personne (« les invités d'Alex apportent leur clé ») : aujourd'hui, l'assistant les retire un par un une fois arrivés.
- Celui qui revient avec un lien de retour garde ses anciennes lignes sur les clés et suit l'hôte qui l'avait invité la première fois : ni la règle des prochains invités (§11.2), ni ce que son invitation disait des clés ne s'appliquent de nouveau, car le lien de retour ne dit rien des clés.
- L'assistant qui écrit le premier à celui qui a donné la clé (chantier 3 ou 5).
