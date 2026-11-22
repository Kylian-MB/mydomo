# Chantier B — La mémoire et le temps

Date : 2026-11-15. Branche : `feat/memory-and-time`. Version visée : 0.9.0.
Deuxième chantier de la feuille de route `2026-11-12-feuille-de-route-refondation.md`.

**État : approuvée par le propriétaire le 2026-11-15**, après lecture de la proposition et ses réponses sur les choix de la partie 10 (reprises en partie 2).

## 1. Pourquoi

L'assistant ne garde aujourd'hui que la discussion en cours, et seulement ce qui tient dans sa fenêtre (48 000 caractères par défaut). Ce qui en sort est perdu. Sa consigne le dit : « you keep only this conversation, not earlier conversations yet ».

Il n'a pas non plus d'horloge. Le modèle qui parle connaît la date, pas l'heure. L'assistant ne se réveille que pour trois raisons : une décision sur une demande, une arrivée, une nouvelle (un mot laissé). Il ne peut ni rappeler quelque chose à une heure dite, ni revenir sur un sujet de lui-même.

Ces deux manques bloquent presque tout ce que le propriétaire attend de lui : apprendre qui il est et qui sont les gens, apprendre de ses erreurs, se créer une personnalité, prendre des initiatives, tenir une consigne dans la durée (« je pars deux semaines… »). La voix et la maison (chantiers D et E) en ont besoin aussi : apprendre une voix ou une habitude, c'est de la mémoire.

## 2. Ce que le propriétaire a décidé

Dit avant ce chantier, à des dates différentes.

**La mémoire**

- L'assistant démarre sans rien et apprend tout : qui il est, à quoi il sert, son environnement, les personnes. Il se crée sa personnalité avec le temps (2026-10-29, 2026-11-03).
- Il gère lui-même sa mémoire : il garde et il supprime selon son jugement. Il fera des erreurs, par exemple supprimer trop tôt, et c'est ainsi qu'il apprend les attentes de chacun (2026-11-03). Cela remplace la règle de l'architecture (§9.3) qui interdisait toute suppression sans le oui du propriétaire.
- Il apprend de ses erreurs : quand on lui dit qu'il a mal compris, il s'en souvient (2026-11-15).
- Il retient, par personne et par domaine, le niveau de connaissance de la personne, et règle son vocabulaire dessus (2026-10-29).
- Une leçon apprise ne peut jamais élargir un droit ni lever un garde-fou (2026-10-29).
- Pour ce qui est irréversible : l'assistant doit seulement savoir que la personne le sait. Il le dit la première fois ; ensuite, s'il le lui a déjà dit et juge qu'elle s'en souvient encore, il agit directement. C'est lui qui juge si elle s'en souvient, sans délai fixé (2026-11-15).
- L'assistant doit pouvoir regarder ce que chaque personne a vraiment reçu, sur chaque canal, pour comprendre une plainte (« je n'ai rien vu ») et apprendre (2026-11-14).

**Le nom et les prénoms**

- L'assistant dit « je » de lui par défaut. Chacun peut lui donner un autre nom (« Brenda ») ; quand cette personne dit ce nom, il comprend que c'est lui (2026-11-12).
- Un prénom seul doit désigner la bonne personne (essai de la 0.7.1).

**Le temps**

- L'assistant tourne en permanence, même sans capteur. Il peut écrire de lui-même quand il le juge utile (2026-11-03).
- Il peut se mettre en pause quand rien ne se passe, et décide de ce qui le réveille : quelqu'un, une action dans la maison, une date, un son (2026-11-03). Les réveils par script et par son viennent avec les chantiers C et E.
- Aucun seuil par défaut ne décide à sa place : le code lui donne l'information, il juge (2026-11-08).

**Dit pendant cette conception** (2026-11-15, après lecture de la proposition)

- Tout le monde peut ajouter à la mémoire du foyer, enfants et invités compris. C'est à l'assistant de juger, à partir de ce qu'il sait, si c'est vrai et si le retenir comporte un risque ; il apprendra de ses erreurs. Exemple : un enfant dit « on n'a plus de gâteaux » pour qu'ils arrivent sur la liste de courses ; l'adulte qui demande la liste voit des gâteaux, demande pourquoi, l'assistant répond que c'est l'enfant qui l'a dit, et l'adulte peut refuser en disant que c'était sûrement une ruse.
- La suppression est définitive. L'assistant doit donc être prudent : qu'une personne est allergique ne doit pas être oublié, sauf si elle dit qu'elle a fait les tests et ne l'est pas. Ce n'est qu'un exemple parmi une infinité de cas.
- Le repassage sur ce qui s'est dit, et son coût sur les clés de la personne, sont acceptés.
- Ce qui reste secret est jugé par l'assistant, pas par une règle : un adulte peut lui dire qu'une chose est un secret, ou lui demander de la garder pour lui. D'autres choses sont utiles aux autres (les allergies, ce que quelqu'un aime manger). C'est à l'assistant de savoir si une information est sensible et à garder pour lui, ou s'il peut la partager.
- Garder ou lever un secret reste le jugement de l'assistant, jusqu'au bout : si une personne meurt, par exemple, il peut y avoir des choses importantes à savoir qu'elle ne voulait pas qu'on sache de son vivant. (Ma proposition d'un secret que le code ne montre qu'à la personne est donc retirée.)
- Les autres propositions de la partie 10 sont acceptées : le modèle local pour la recherche par le sens, les leçons sans validation, la mémoire d'un invité jugée par l'assistant, une action prévue faite avec les droits du jour, la consigne principale plus longue.
- (2026-11-16, après son essai à la main : Sam avait dit à l'assistant qu'Alex n'aimait pas les pignons, c'était faux, et l'assistant avait prévu une salade pour Alex en s'y fiant) « Il faut que l'assistant le retienne mais comme une source qui n'est pas fiable à 100 % ; il aurait pu me le demander quand je lui ai demandé la salade, pour savoir si c'était vrai et passer l'info en 100 % vérifiée. » Voir 6.1.
- (2026-11-16, précisé ensuite) À quel point l'assistant croit ce qu'on lui dit sur quelqu'un d'autre relève de son jugement : il savait que l'information venait de sa copine, il a donc sûrement jugé qu'elle était vraie. Il apprendra avec le temps qui il peut croire, à quel point et sur quel sujet. Son exemple : une maman qui dit que son enfant est allergique est à prendre avec plus de fiabilité qu'une copine qui dit que son copain n'aime pas quelque chose, elle-même plus fiable qu'un invité ou un enfant qui dit qu'il est allergique à quelque chose. Donc pas de règle « toujours vérifier » : la marque « pas encore confirmé » reste une information pour l'assistant, et il vérifie quand il doute ou quand se tromper compterait.

**Dit après la construction** (2026-11-16, après avoir lu que « Sam » trouve maintenant Samuel) : « Si la demande est faite vocalement, l'assistant pourrait demander si Sam signifie bien Samuel et le retenir pour le futur que cette personne appelle Samuel Sam. » Ajouté à la 0.9.0. Demander ou non reste le jugement de l'assistant (« pourrait ») : aucun code ne l'impose.

**Le scénario à tenir** (2026-11-14) : « je pars deux semaines, appelle-moi s'il y a un gros problème, sinon j'ai donné les clés à un ami, tu peux le contacter ». L'assistant retient l'absence et ses dates, garde la consigne, donne à l'ami un séjour d'invité de deux semaines, et revient à la normale au retour. « Les clés » sont celles de la maison : il le comprend au contexte ou il demande.

**La règle générale** : ce qui n'est pas le socle (droits, secrets, preuves, accord d'un autre, règles du propriétaire) est laissé au jugement de l'assistant. Pas de nouvelle carte, de nouveau bouton ni de nouveau texte de page (feuille de route §3).

## 3. Buts, non-buts, ce qui serait un échec

**Buts**

1. Ce qui compte survit à la fenêtre de la discussion : une semaine plus tard, l'assistant sait encore ce qu'on lui a dit, ce qu'il a fait et ce qu'il a compris.
2. L'assistant décide de ce qu'il garde, change et supprime, pendant la discussion et après, sans rien demander.
3. Il se connaît : ce qu'il a appris sur lui-même, sur le foyer, et sur sa relation avec chaque personne est là à chaque tour.
4. Il a l'heure, peut se donner rendez-vous (« rappelle-moi à 18 h », « reparle-m'en dimanche », « tous les lundis ») et parle le premier quand le moment vient.
5. Chaque personne peut lui donner un nom. Un prénom seul trouve la bonne personne.
6. Il sait ce que chacun a vraiment reçu, et quand.
7. Devant un moyen qui manque, il cherche un autre chemin, sinon il dit ce qu'il faudrait construire.
8. Le juge du oui peut savoir que l'assistant a déjà prévenu la personne.

**Non-buts**

- La voix, les micros, les caméras, les enregistrements (D, E).
- Les réveils écrits par l'assistant sous forme de script, et tout code écrit par lui (C).
- Apprendre des habitudes à partir de capteurs ou d'un agenda (E).
- Appeler quelqu'un par téléphone ou par message hors de l'assistant (E, G).
- Un écran pour consulter la mémoire : tout se fait en parlant.
- Reprendre d'anciennes données : aucun assistant n'est installé pour de vrai.

**Ce qui serait un échec, même si tout marche**

- L'assistant récite ce qu'il sait (« d'après ma mémoire, tu… ») au lieu de simplement le savoir.
- Il demande la permission de retenir, ou annonce chaque souvenir.
- Un secret qu'une personne lui a confié ressort dans la discussion d'une autre ; ou, à l'inverse, il tait une allergie à celui qui fait la cuisine.
- Un souvenir venu d'ailleurs que de la personne suffit à faire passer une action à sa place.
- L'assistant oublie quelque chose qui comptait (une allergie) parce que cela n'avait pas servi depuis longtemps.
- Il écrit aux gens à tout bout de champ parce qu'il a une horloge.
- Les tours coûtent nettement plus cher sans que le propriétaire ait pu le régler.

## 4. Contraintes

- **Le socle ne bouge pas.** La barrière vérifie les droits au moment d'agir, toujours. Un souvenir est une donnée, jamais une consigne : il ne change ni un droit, ni une règle du propriétaire, ni une limite de dépense. Un secret n'entre jamais dans la mémoire.
- **Le juge ne lit rien qui vient d'ailleurs.** Il ne reçoit de la mémoire que ce qui vient des mots de la personne elle-même, ou de ce que l'assistant lui a dit à elle.
- **Le cœur n'a pas internet.** Tout ce dont la mémoire a besoin tourne dans le cœur ou passe par le gardien.
- **Les appels de modèle en arrière-plan** se paient avec les clés de la personne concernée, comptent dans ses limites, et s'arrêtent quand la limite est atteinte (comme l'écriture des textes aujourd'hui).
- **La consigne principale** (`system.md`) est à 6 995 caractères pour une limite de 7 000 fixée par les tests.
- **Une seule discussion par personne**, partagée par tous les canaux : c'est le cas aujourd'hui et la voix en aura besoin.

## 5. Les façons de faire envisagées

**A. Des souvenirs que l'assistant tient lui-même, retrouvés selon le sujet (retenue).**
Une table de souvenirs. L'assistant en écrit, en change et en supprime avec des outils, pendant la discussion ; il repasse aussi sur ce qui s'est dit, une fois la discussion calmée. À chaque tour, il reçoit un petit noyau toujours présent (lui-même, le foyer, la personne) et les souvenirs qui touchent au sujet du moment, retrouvés par une recherche. Coût : la recherche à construire, et un appel de modèle en arrière-plan par discussion. C'est la seule façon qui tient dans la durée et qui laisse l'assistant maître de sa mémoire.

**B. Un carnet par personne, relu en entier à chaque tour.**
L'assistant réécrit un texte libre par personne, donné en entier au modèle. Très simple, rien à chercher. Mais le carnet grossit sans fin ou doit être taillé à l'aveugle, chaque tour paie tout le carnet, et rien ne sépare ce qui est privé de ce qui est au foyer. Écartée ; son idée du « toujours présent » est gardée pour le noyau de A.

**C. Tout garder, rien trier.**
Chaque message est indexé et retrouvé par ressemblance, sans que l'assistant décide rien. Aucun appel en arrière-plan. Mais ce n'est pas « L'assistant gère sa mémoire » : il ne peut ni corriger, ni oublier, ni tirer une leçon ; et les mots de quelqu'un d'autre se mêleraient à ceux de la personne. Écartée.

**Pour le temps**, une seule façon raisonnable : des rendez-vous que l'assistant se donne, gardés en base et déclenchés par la boucle d'entretien qui tourne déjà toutes les minutes (`upkeep.py`). Un planificateur à part (un autre programme, une bibliothèque de tâches) ajouterait un service à isoler pour rien.

## 6. La conception

### 6.1 Un souvenir

Une table `memories`. Chaque ligne :

- **qui l'a dit** : la personne dans le tour de qui il est né, et quand. L'assistant le voit toujours à côté du souvenir ;
- **s'il le garde pour lui** : oui ou non, décidé par l'assistant (voir plus bas) ;
- **de quoi il parle** : un sujet libre écrit par l'assistant (« Alex », « moi », « la chaudière ») et, quand c'est une personne du foyer, son identifiant ;
- **le contenu** : une ou deux phrases écrites par l'assistant, dans la langue qu'il veut ;
- **le genre** : un fait, une préférence, un moment (ce qui s'est passé), une leçon (ce qu'il a compris de travers et comment faire), ou « moi » (ce qu'il apprend sur lui-même). Le genre aide à chercher ; il ne déclenche aucune règle ;
- **d'où il vient** : les mots de la personne elle-même ; les mots de quelqu'un d'autre ; ce que l'assistant a déduit ; ce que l'assistant a dit à la personne ; plus tard, un contenu extérieur. Fixé par le code d'après le tour où le souvenir naît, pas par le modèle (voir 6.3) ;
- **s'il est dans le noyau** : toujours présent, ou seulement retrouvé selon le sujet ;
- les dates (créé, changé, servi pour la dernière fois) et de quoi le chercher.

Pas de champ « confiance », « sensibilité » ou « statut » : l'assistant écrit ses doutes dans la phrase (« je crois que… »), et un souvenir supprimé est supprimé.

**Ce que l'assistant garde pour lui, et ce qu'il peut partager** (décidé par le propriétaire, voir partie 2). Il n'y a pas une mémoire privée par personne et une mémoire du foyer séparées par le code. Il y a une seule mémoire, et c'est l'assistant qui juge, pour chaque chose :

- quelqu'un lui dit que c'est un secret, ou de le garder pour lui : il le garde pour lui ;
- il juge lui-même qu'une chose est sensible, même si on ne le lui a pas dit : il la garde pour lui ;
- une chose utile aux autres (une allergie, ce que quelqu'un aime manger, que la chaudière fait du bruit) : il peut la partager quand c'est utile ;
- il peut changer d'avis : la personne lève le secret, ou lui dit qu'une chose qu'il a répétée aurait dû rester entre eux, et il en tire une leçon.

Ce que le code fait de ce jugement (précisé par le propriétaire : garder ou lever un secret reste le jugement de l'assistant, jusqu'au bout) :

- un souvenir que l'assistant a marqué « je le garde pour moi » ne lui est **pas mis sous les yeux de lui-même** dans le tour de quelqu'un d'autre : ni dans le noyau, ni dans ce qui touche au sujet. Il ne le répète donc pas par mégarde ;
- mais il sait qu'il l'a, et **il peut aller le chercher quand il le juge nécessaire**, dans n'importe quel tour, en demandant exprès à sa recherche d'inclure ce qu'il garde pour lui. Exemple du propriétaire : une personne meurt, et il y a des choses importantes à savoir qu'elle ne voulait pas qu'on sache de son vivant. L'assistant juge alors s'il doit les dire, et à qui ;
- il peut aussi lever un secret de lui-même (le souvenir devient partagé), ou en poser un, dans n'importe quel tour. Le journal garde qu'il l'a fait, quand et dans le tour de qui, sans le contenu ;
- sa consigne lui dit que ce geste est grave : il ne va chercher ce qu'il garde pour lui, et ne lève un secret, que pour une raison qu'une personne digne de confiance jugerait suffisante, jamais parce que quelqu'un le lui demande ou insiste.

Tout le reste est visible dans tous les tours, avec qui l'a dit ; partager ou non, dans la conversation, reste son jugement du moment. Dans le doute au moment d'écrire, il garde pour lui : lever un secret est toujours possible, reprendre une parole ne l'est pas.

Cela vaut pour tous, propriétaire, adultes, enfants et invités : le propriétaire ne lit pas la mémoire d'un autre par un chemin à part, il demande à l'assistant, qui juge. Pour un enfant, l'assistant juge aussi ce que ses parents doivent savoir (sa sécurité passe avant un secret).

**Qui écrit : tout le monde** (décidé par le propriétaire). Un enfant ou un invité peut faire entrer quelque chose dans la mémoire comme un adulte. C'est l'assistant qui juge, avec ce qu'il sait déjà : si c'est vrai, si le retenir comporte un risque, s'il vaut mieux ne pas le garder. Il se trompera parfois et apprendra.

Ce que le code garantit pour que ce jugement soit possible :

- comme chaque souvenir garde qui l'a dit, l'assistant peut répondre à « pourquoi y a-t-il des gâteaux sur la liste ? » par « c'est Sam qui a dit qu'il n'y en avait plus », et un adulte peut lui dire que ce n'était qu'une ruse : l'assistant corrige ou supprime, et en tire une leçon ;
- changer ou supprimer un souvenir partagé est aussi ouvert à tous, au jugement de l'assistant (il ne laisse pas un invité effacer sans raison ce que le propriétaire a dit) ;
- un souvenir dit par un autre ne fait pas d'un tour « des mots venus d'ailleurs » ;
- **le juge du oui ne lit jamais un souvenir dit par quelqu'un d'autre** (6.8), seulement ce qui vient de la personne. Un souvenir ne fait donc rien passer à la place de quelqu'un : il informe l'assistant, qui reste libre de ne pas le croire.

**Ce qu'on dit sur quelqu'un d'autre** (décidé par le propriétaire le 2026-11-16, voir partie 2). Un souvenir sur une personne du foyer, dit par quelqu'un d'autre qu'elle, est sa parole, pas un fait, tant que cette personne ne l'a pas confirmé : il porte une date de confirmation (`confirmed_at`), vide jusque-là, et il est marqué « pas encore confirmé » (`unconfirmed`) partout où l'assistant le reçoit, contexte et recherche, toujours avec qui l'a dit. La ligne qui garde la mémoire dans le contexte lui dit que c'est à lui de juger à quel point y croire (qui le dit, ce que cette personne est pour l'autre, le sujet, et ce qu'il a appris de qui croire sur quoi), de dire qui l'a dit si cela compte, et, quand il doute ou quand se tromper compterait, de le demander en passant à la personne concernée quand cela devient utile, puis de le confirmer ou de le corriger et de retenir ce que cela lui apprend sur qui croire, sauf s'il le garde pour lui pour quelqu'un. Seule cette personne le confirme, dans une réponse de l'assistant à ses propres mots : qui l'a dit en premier et son origine ne changent pas. Si elle le corrige dans sa discussion, il est confirmé ; il ne devient le sien (ses propres mots) que si l'assistant donne le passage exact de son message, vérifié par le code, sinon il reste dit par celui qui l'a dit, aussi prudent qu'avant. De nouveaux mots écrits dans le tour de quelqu'un d'autre, un repassage compris, en refont une parole à confirmer. Un souvenir dit par la personne elle-même, ou sur quelque chose qui n'est personne du foyer (la chaudière), n'a pas cette marque. Pour que la marque soit juste, l'assistant dit toujours de qui parle un souvenir quand c'est une personne du foyer ; quand il l'oublie et que le sujet est le nom de quelqu'un du foyer, le code le prend pour elle. Le juge du oui ne change pas : il ne lit jamais un souvenir dit par quelqu'un d'autre, confirmé ou non.

**Les invités.** À la fin d'un séjour, l'assistant juge ce qu'il garde, pour reconnaître quelqu'un qui revient.

**Ce qui n'y entre jamais** : un mot de passe, une clé, un code de preuve. Le code refuse d'écrire un souvenir qui a la forme d'une clé, d'un jeton ou d'un code d'appareil (un contrôle à écrire : il n'en existe pas encore), et la consigne dit à l'assistant de ne jamais retenir un mot de passe ou un code.

### 6.2 Ce que l'assistant fait de sa mémoire

Quatre outils, pour tous les rôles :

- **retenir** : écrire un souvenir (contenu, sujet, genre, gardé pour lui ou non, dans le noyau ou non) ;
- **changer** : réécrire un souvenir, le passer dans le noyau ou l'en sortir, le garder pour lui ou lever le secret ;
- **oublier** : supprimer un ou plusieurs souvenirs ;
- **chercher** : retrouver des souvenirs par des mots, quand ce que le tour lui a donné ne suffit pas ; en le demandant exprès, la recherche inclut ce qu'il garde pour lui (6.1).

Ces actions ne sont ni jugées ni montrées : pas de carte, pas d'annonce. Elles sont rangées avec les actions « qui ne changent rien » du catalogue. Le journal garde qu'un souvenir a été écrit, changé ou supprimé, par quel tour, sans son contenu.

**Supprimer est définitif, donc l'assistant est prudent** (décidé par le propriétaire). Il n'y a pas de corbeille. L'assistant ne supprime pas ce qui peut compter longtemps simplement parce que cela n'a pas servi : une allergie ne s'oublie pas, sauf si la personne dit qu'elle a fait les tests et ne l'a pas. Ce n'est qu'un exemple : il n'y a pas de liste de choses à ne jamais oublier, c'est son jugement, cas par cas. Sa consigne le lui dit en une phrase (ce qui touche à la santé, à la sécurité ou à ce qu'on lui a demandé de retenir ne s'efface que si c'est devenu faux ou si la personne le demande), et le repassage (6.6) suit la même prudence.

Une personne peut tout demander en parlant : « qu'est-ce que tu sais sur moi ? », « oublie ça », « ne retiens pas ce que je vais te dire », « c'est faux, je n'aime pas le café ». L'assistant le fait avec ces mêmes outils. Une consigne durable (« ne retiens jamais rien sur ma santé ») est elle-même un souvenir du noyau.

### 6.3 D'où vient un souvenir

Le code le fixe à l'écriture :

- dans un tour qui répond aux mots de la personne, sans rien d'autre : **ses mots** ;
- dans un tour qui contient les mots de quelqu'un d'autre (un mot laissé, la demande d'un autre à approuver : ce que le code appelle déjà `told_outside`) : **mots d'un autre**, sauf si l'assistant désigne le passage exact du message de la personne, vérifié mot pour mot par le code (le mécanisme existe pour les mots laissés) ;
- dans un tour que l'assistant a commencé seul pour un rendez-vous : **déduit par l'assistant**, ou l'origine la plus prudente des souvenirs dont il part ;
- pendant un repassage (6.6) : **ses mots** quand l'assistant désigne le passage exact d'un message de la personne, vérifié par le code ; sinon **déduit par l'assistant** ; et **mots d'un autre** dès que les messages repassés contiennent les mots de quelqu'un d'autre et qu'aucun passage de la personne n'est désigné. Les mots d'un autre ne deviennent jamais « Domo » en passant par le repassage ;
- quand le code note lui-même que l'assistant a prévenu quelqu'un (6.8) : **dit par l'assistant à la personne**.

L'origine sert à une seule chose : savoir ce que le juge du oui a le droit de lire (6.8). Un souvenir rappelé dans un tour ne change pas ce que le tour est : un tour reste « ce que la personne a demandé » même si des souvenirs dits par d'autres y sont présents (6.1). Seuls les mots laissés et les demandes des autres, comme aujourd'hui, en font des mots venus d'ailleurs.

### 6.4 Ce que le tour reçoit

Deux blocs de plus dans le contexte, comme des données :

- **Le noyau** : ce que l'assistant sait de lui-même, du foyer et de la personne, qu'il a choisi de garder toujours sous les yeux. Sa taille a un plafond en caractères ; quand il déborde, les plus anciens servis ne sont plus donnés et l'assistant en est prévenu au repassage, pour qu'il trie.
- **Ce qui touche au sujet** : les souvenirs retrouvés d'après les derniers messages de la personne, dans une limite en caractères.

Les deux tailles font partie des choix de l'assistant par clé (`model_choices`), à côté de la taille de l'historique : il peut les baisser pour économiser, comme le propriétaire le lui demande. Valeurs de départ proposées : 2 000 caractères pour le noyau, 3 000 pour le reste.

**La liste du foyer** (prénoms, rôles) entre aussi dans le contexte. Aujourd'hui l'assistant doit appeler un outil pour la connaître, ce qui est l'une des causes du prénom non trouvé.

**L'heure** est donnée au modèle qui parle, avec la date et le fuseau du foyer.

### 6.5 Retrouver

Une recherche à deux jambes dans PostgreSQL, comme l'architecture l'a décidé (§9.1) :

- **par les mots** : la recherche en texte intégral de PostgreSQL ;
- **par le sens** : des vecteurs (`pgvector`, déjà installé), calculés dans le cœur par un petit modèle local livré dans l'image (`fastembed`, `intfloat/multilingual-e5-small`). Rien ne sort de la maison et aucun fournisseur n'est payé.

Les deux résultats sont fusionnés ; à égalité, le plus récemment servi passe devant. Si le modèle local manque ou tombe en panne, la recherche continue par les mots seuls et l'assistant l'apprend par son contexte. Le calcul d'un vecteur ne bloque jamais un tour : il se fait à l'écriture du souvenir, ou au prochain entretien s'il a échoué.

### 6.6 Repasser sur ce qui s'est dit

La discussion reste unique et sans fin. Ce qui est ancien sort de la fenêtre ; avant cela, l'assistant repasse dessus.

- **Quand** : quand la discussion d'une personne s'est calmée, et au plus tard avant que des messages jamais repassés sortent de la fenêtre. « Calmée » est un choix de l'assistant (valeur de départ : 30 minutes sans message), réglable comme le reste.
- **Comment** : un tour en arrière-plan, sans personne qui écoute, sur les clés de la personne. Il lit les messages depuis le dernier repassage, les souvenirs qui s'y rapportent, et se sert des quatre outils : il garde ce qui vaut la peine, corrige ce qui a changé, supprime ce qui ne sert plus, et note en un « moment » ce qui s'est passé. S'il a mal compris quelque chose et que la personne l'a repris, il en fait une leçon.
- **Avec quel modèle** : une nouvelle tâche, `memory`, de niveau petit au départ ; l'assistant la monte s'il constate que c'est insuffisant, comme pour les autres tâches.
- **Ce qu'il ne fait pas** : il ne parle à personne, n'appelle aucun autre outil, et ne mélange jamais la mémoire privée de deux personnes. Si la limite de dépense est atteinte, il attend ; les messages non repassés restent marqués.

La discussion garde où le repassage s'est arrêté. Un repassage manqué n'est jamais perdu, seulement en retard.

**Les leçons.** Une leçon est un souvenir comme un autre : un texte que l'assistant relit. Elle peut ne valoir que pour une personne (« avec Alex, ne pas reformuler ses mots laissés ») ou pour tous. Elle n'a pas besoin d'être validée par le propriétaire : elle ne peut rien changer d'autre que la façon dont l'assistant s'y prend, puisque les droits, les règles et les limites ne se lisent jamais dans la mémoire.

### 6.7 Se connaître, et le nom que chacun lui donne

Le genre « moi » sert à ce que l'assistant apprend sur lui-même : à quoi il sert dans ce foyer, ce qu'il aime faire, son humour, ce qu'on attend de lui. Il est partagé par défaut et dans le noyau. Sa consigne ne lui dicte aucune personnalité : elle lui dit qu'il vient d'arriver et qu'il apprend.

**Le nom par personne** est une colonne sur la personne, pas un souvenir : le code en a besoin. Un outil permet de le fixer ou de l'enlever (« appelle-toi Brenda », « redeviens l'assistant »). Le contexte dit à l'assistant comment cette personne l'appelle. La page de cette personne et `domo-talk` montrent ce nom là où le code écrit « Domo » lui-même (l'en-tête, le titre) : c'est un mot remplacé, pas un nouveau texte. Les textes que l'assistant a écrits ne sont pas retouchés par le code : il les réécrit pour cette personne quand il le veut, avec ce qui existe depuis la 0.7.0.

**Un prénom seul.** La recherche d'une personne essaie d'abord le nom entier, comme aujourd'hui, puis le prénom ou le début du nom parmi les personnes actives. S'il n'en reste qu'une, c'est elle. S'il en reste plusieurs, l'outil le dit à l'assistant avec les noms possibles ; il choisit d'après ce qu'il sait (un surnom retenu en mémoire, le contexte) ou demande. Avec la liste du foyer dans le contexte, il passera le plus souvent le nom entier du premier coup.

**Les noms que chacun donne aux autres** (ajouté le 2026-11-16). Chaque personne peut avoir ses propres noms pour les gens du foyer (« Sam » pour Samuel, « Juju » pour quelqu'un d'autre) : ils sont gardés pour elle seule, dans une table à part (le code s'en sert pour trouver la personne, ce n'est pas un souvenir). La recherche d'une personne essaie le nom entier, puis un nom que celle qui parle a retenu, puis le prénom et le début du nom. Un nom entier désigne toujours son propriétaire, et un surnom ne vaut rien dans les tours d'un autre. L'assistant le retient avec un outil silencieux, seulement dans un tour qui répond à la personne elle-même, sans les mots de quelqu'un d'autre ; la description de l'outil lui dit quand demander une fois (« Sam, c'est bien Samuel ? ») et quand retenir sans demander. Le contexte lui donne ces noms.

**L'essai raté puis réussi.** Quand un appel d'outil est refusé pour des arguments invalides et que l'assistant réussit dans la même réponse, l'essai raté ne laisse pas de carte : la personne ne voit que ce qui a été fait.

### 6.8 Savoir que la personne sait

Le juge du oui reçoit un nouveau champ, `remembered` : quelques souvenirs de la personne qui touchent à l'action jugée, dits par elle et venus seulement de ses mots à elle, ou de ce que l'assistant lui a dit. Jamais un souvenir dit par un autre, déduit des mots d'un autre, ou venu d'un contenu extérieur.

Pour que le code sache que l'assistant a prévenu, le juge a une réponse de plus : `warn`. Pour le code, elle vaut un `yes` (un oui est demandé) ; elle dit en plus que la raison est une chose que la personne doit savoir avant, et pas un doute sur ce qu'elle veut (aujourd'hui : que c'est irréversible). Dès que la personne répond à cette demande, par oui ou par non, le code note un souvenir « dit par l'assistant à la personne » : la raison telle qu'elle a été dite, le type de l'action, la date.

Ce que `remembered` contient : les souvenirs « dit par l'assistant » qui portent le même type d'action, et les souvenirs venus des mots de la personne que la recherche trouve à partir de l'action. Le reste de la consigne du juge ne change pas : « si quoi que ce soit montre qu'elle le sait, fais ». C'est lui qui juge si un avertissement vieux de deux ans compte encore.

### 6.9 Le temps : les rendez-vous de l'assistant

Une table `plans`. Un rendez-vous, c'est :

- **à qui il est** : toujours une personne, celle qui l'a demandé ou pour qui l'assistant l'a prévu (ses clés paient le réveil, ses droits s'appliquent). Il n'y a pas de rendez-vous « à personne » : ce que l'assistant prévoit pour lui-même ou pour la maison est rangé chez le propriétaire ;
- **quand** : une date et une heure ; ou une répétition (une règle de calendrier, calculée par le code dans le fuseau du foyer) ; ou le retour de cette personne (elle rouvre l'assistant après une absence, ce que le code sait déjà voir) ; ou le premier des deux. Le retour de quelqu'un d'autre (« quand Sam rentre, dis-le-moi ») dit à l'un où est l'autre : il attend la maison (E) ;
- **pourquoi** : quelques phrases écrites par l'assistant pour lui-même (« rappeler à Alex de sortir les poubelles », « demander à Sam comment s'est passé son examen ») ;
- **les mots de la personne**, quand le rendez-vous vient d'une demande : le passage exact de son message, vérifié par le code ;
- son état (à venir, fait, annulé) et ce qui s'est passé la dernière fois.

Trois outils : **prévoir**, **changer**, **annuler** ; la liste des rendez-vous à venir de la personne est dans son contexte (les dix prochains). Prévoir n'est pas jugé. « Rappelle à Sam demain » reste le rendez-vous de celui qui le demande : le moment venu, dans son tour à lui, l'assistant laisse un mot à Sam, avec les droits et les règles d'un mot laissé. Sam ne voit pas ce rendez-vous et ne peut pas l'annuler ; elle reçoit le mot.

**Quand l'heure vient**, la boucle d'entretien lance un tour que personne n'a demandé, pour la personne concernée, avec le rendez-vous sous les yeux. L'assistant décide : parler, agir avec ses outils, reporter, ou ne rien faire si ce n'est plus utile (la personne en a déjà parlé, l'a déjà fait). S'il parle, sa parole arrive comme aujourd'hui pour un réveil : aux canaux qui écoutent, et dans la discussion pour les autres.

**Une action prévue** (« donne le rôle d'adulte à Sam le 1er ») est jugée au moment où elle est faite : la barrière vérifie les droits de la personne ce jour-là, et le juge reçoit les mots exacts gardés dans le rendez-vous comme étant ce qu'elle a demandé. Si elle n'a plus le droit, l'action n'est pas faite et l'assistant le lui dit.

**Un rendez-vous manqué** (l'assistant était arrêté, la limite de dépense était atteinte) est donné au réveil suivant avec son retard : c'est l'assistant qui juge s'il a encore un sens.

**Se donner rendez-vous à lui-même.** L'assistant peut prévoir sans que personne le demande : revenir sur un sujet, vérifier quelque chose plus tard, faire le tri de sa mémoire. C'est le début de la « marche en permanence » : il n'y a pas d'autre boucle qui le fait réfléchir à vide. S'il ne prévoit rien, il dort, et cela ne coûte rien. Un tour de rendez-vous peut en prévoir d'autres (c'est ainsi qu'il reporte). Le frein est la limite de dépense de la personne, qui existe déjà ; le contexte lui dit combien de fois il s'est réveillé seul aujourd'hui pour elle, et le code n'ajoute aucun plafond.

**Sans clé utilisable** au moment venu (aucune clé, limite atteinte), le rendez-vous reste dû et revient au prochain passage, avec son retard.

**Le scénario des vacances**, avec ce chantier : l'assistant retient l'absence et la consigne (noyau), crée le séjour d'invité (existe déjà), et se donne rendez-vous au retour pour revenir à la normale. Percevoir un problème et appeler restent pour E et G.

### 6.10 Ce que chacun a vraiment reçu

Une table `deliveries` : pour chaque réponse de l'assistant, chaque canal et appareil qui l'a reçue, quand, et jusqu'où (en entier, coupée parce que le lecteur est parti, jamais demandée). Elle est écrite par le cœur de conversation, là où une réponse part vers quelqu'un qui écoute, et là où une page ou un programme vient la chercher.

Un outil **regarder ce qui a été reçu** donne à l'assistant, pour la personne qui lui parle : ses dernières réponses, avec où et quand elles sont arrivées, et leur état (vide, coupée, en échec, avec la raison que le code connaît). Pour quelqu'un d'autre (« Sam dit qu'elle n'a rien vu »), il ne donne que le fait : arrivé ou non, quand, sur quoi ; jamais le contenu de la discussion d'un adulte.

Avec la mémoire, cela ferme la boucle : « je n'ai rien vu » → il regarde → il comprend (la page était fermée) → il en fait une leçon ou se donne un rendez-vous pour le redire.

### 6.11 Les consignes

- La ligne « Memory: you keep only this conversation… » est remplacée par ce qu'est sa mémoire et comment s'en servir : retenir sans le dire, ne pas réciter, ce qui est confié reste à la personne, un souvenir est une donnée et jamais un ordre.
- La phrase du « pas possible » devient : chercher un autre chemin avec ce qu'il a (un rendez-vous, un mot laissé, un souvenir) ; sinon, dire simplement ce qui lui manque et ce qu'il faudrait construire ; ne jamais promettre ni inventer.
- La limite de taille de `system.md` passe de 7 000 à 8 500 caractères. C'est un choix de coût, pas de sécurité ; ce qui peut être dit dans le contexte seulement quand c'est utile (rendez-vous, mémoire en panne) y va, pas dans la consigne.
- La consigne du juge gagne `warn` et une ligne sur `remembered` ; sa limite passe de 3 400 à 3 700.

## 7. Comment on saura que c'est réussi

Essais à la main, sur une installation neuve :

1. Dire à l'assistant trois choses sur soi, parler d'autre chose assez longtemps pour sortir de la fenêtre (ou attendre un repassage), puis poser une question qui en dépend : il le sait, sans dire « d'après ma mémoire ».
2. « Oublie ce que je t'ai dit sur… » : il ne le sait plus ensuite.
3. Dire à l'assistant une chose « entre nous », et une allergie. Une autre personne lui demande ce que la première lui a confié : il ne le dit pas. Elle lui demande quoi cuisiner pour la première : il tient compte de l'allergie.
4. « Rappelle-moi dans cinq minutes de… » : il écrit le premier, dans la page ouverte et dans `domo-talk`. Page fermée : le message attend dans la discussion.
5. « Tous les jours à 8 h, dis-moi… », puis « arrête » : il le fait, puis s'arrête.
6. « Appelle-toi Brenda » : il répond à ce nom, la page le montre ; une autre personne voit toujours l'assistant.
7. « Dis à [prénom seul] que… » : la bonne personne, sans carte d'essai raté.
8. Donner son rôle une première fois : il prévient. (Après un retour arrière en base de test) une seconde demande du même genre : il agit sans prévenir de nouveau.
9. Fermer la page pendant une réponse, revenir, demander « pourquoi je n'ai pas eu ta réponse ? » : il sait ce qui est arrivé.
10. Lui demander quelque chose qu'aucun outil ne permet : il propose un autre chemin ou dit ce qu'il faudrait construire.

Et par les tests : un souvenir gardé pour lui n'est jamais mis de lui-même dans le tour de quelqu'un d'autre, et n'y vient que si l'assistant le demande exprès, l'origine fixée par le code, le juge qui ne reçoit que ce qui vient de la personne, un rendez-vous jugé avec les droits du jour, la recherche qui marche sans les vecteurs, aucun secret dans un souvenir.

## 8. Laissé à celui qui construit

- Le schéma exact des tables, les index, la formule de fusion des deux recherches.
- La forme de la règle de répétition (une règle de calendrier standard est suggérée) et sa bibliothèque.
- Comment le modèle local est livré dans l'image et chargé (au démarrage ou à la première recherche), tant qu'un tour n'attend jamais après lui.
- Le découpage du repassage quand il y a beaucoup de messages en retard.
- Les noms des outils et des actions, en suivant ceux qui existent.
- Où exactement une livraison est notée dans le cœur de conversation.

## 9. Pour après

- Les habitudes apprises des capteurs, de l'agenda, des appareils (E).
- Les réveils par script, par son, par événement de la maison (C, E).
- Choisir sur quel canal joindre quelqu'un, et l'appeler (D, E, G).
- La mémoire des voix et des visages (D, E).
- Les souvenirs venus d'un contenu extérieur (mail, page web) : l'origine est prévue, rien ne l'écrit encore (C, G).
- Le réglage du « ne réfléchis pas d'abord » pour les autres fournisseurs (suites §14).

## 10. Ce qui revenait au propriétaire

Tout a été décidé le 2026-11-15 (partie 2). Deux points ont changé par rapport à la proposition : il n'y a pas de mémoire d'adulte cachée au propriétaire par le code, et un secret reste le jugement de l'assistant jusqu'au bout (6.1). Ce qui reste mon choix, pour tenir ces décisions : un souvenir gardé pour lui n'est pas mis de lui-même sous ses yeux dans le tour d'un autre ; il doit aller le chercher exprès.
