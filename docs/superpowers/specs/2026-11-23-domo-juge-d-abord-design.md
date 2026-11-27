# Domo juge d'abord — conception (2026-11-23)

Chantier parti de l'audit `docs/superpowers/audits/2026-11-23-domo-judges-first.md` (86 constats, environ
63 faits). Les constats sur lesquels cette conception repose ont été relus dans le code le 2026-11-23 par
quatre relecteurs : tous confirmés (un seul relu en partie, MKT-5). Les identifiants entre parenthèses
(JDG-3, CNV-1…) renvoient à l'audit, qui donne fichier et ligne.

Dans ce document : Alex est le propriétaire, Dana une adulte, Paul un enfant, Sam et Tom d'autres personnes.
« Mon choix » marque ce que le rédacteur a tranché sans le propriétaire ; la liste complète est en partie 17.

## 1. Ce que le propriétaire veut, et pourquoi

Ses mots du 2026-11-23 :

- « Domo juge de si c'est bien moi qui l'ai dit ou non, en fonction du contexte… doit être valable pour
  absolument tout. »
- « Domo peut effectuer n'importe quelle action/initiative s'il le juge nécessaire et qu'il a la possibilité
  de le faire ; s'il se trompe, ce sera un enseignement, un apprentissage de son erreur qui fera qu'il ne le
  fera plus. »
- « Dès qu'il y a un doute, faire jouer le jugement de Domo avant de demander, le but étant de ne demander
  des oui ou des validations que quand c'est absolument nécessaire. »

Le pourquoi : Domo doit se conduire comme quelqu'un de la maison qui connaît les gens, pas comme un guichet.
Aujourd'hui il décide avec très peu, il est jugé à l'aveugle quand l'idée vient de lui, et une erreur ne lui
apprend presque rien.

## 2. Ce qu'il a déjà décidé

Réponses du 2026-11-23 aux questions de l'audit :

1. Domo peut, de lui-même, inviter, donner un rôle, retirer ou faire revenir quelqu'un : « Oui ».
2. Une demande que Domo fait en son nom : il décide et agit avec tout ce qu'il sait, et prévient après s'il
   le juge utile ; s'il doute vraiment, il demande à la personne concernée (Alex pour la maison), plus
   d'office à Alex.
3. « Rouvrir si le contexte a changé » vaut aussi pour un compte de service qu'Alex a retiré : « Oui ».
4. Le juge du oui peut lire la confiance et ce que d'autres ont dit d'une personne : « Oui ». La règle de la
   0.9.1 « la confiance n'atteint jamais le juge » tombe.
5. Qui parle est un jugement de Domo, avec une preuve seulement pour ce qui est sensible.
6. Un coût pour une personne : Domo juge dans la limite de dépense de cette personne (habituel et dans la
   limite : il fait et le dit après ; inhabituel : il demande). La limite reste dans le code.
7. Quelqu'un d'autre qu'Alex peut demander quelque chose « pour la maison » : « Si le propriétaire
   l'accepte, alors oui ».
8. L'irréversible : « Il demande une première fois au propriétaire puis voit avec lui s'il doit continuer ou
   non ».

Réponses données pendant cette conception (même jour) :

9. Une leçon n'est pas une interdiction : « Il la pèse ». Domo ne refait plus la chose, sauf s'il juge que
   la situation a vraiment changé.
10. Quand ce que Domo a fait est défait ou refusé sans un mot, il peut demander une fois ce qui n'allait
    pas : « Oui, ça me va, mais seulement pour les choses importantes, il doit se débrouiller au maximum
    seul ».
11. Pour l'irréversible (réponse 8), la « première fois » se règle avec la personne à qui la chose
    appartient (Dana pour ses affaires, Alex pour la maison), et c'est avec elle que Domo voit s'il doit
    continuer à demander : « oui, c'est bien ça ».
12. Le lecteur d'un oui dit en mots reste un lecteur à part qui ne peut rien faire d'autre que lire, mais il
    reçoit qui parle, les demandes affichées et les quelques messages d'avant ; il peut dire à quelle
    demande un oui répond, ou à plusieurs : « ça me va ».
13. Façon de construire : un seul dossier, partout (partie 5) : « ok pour la première ».
14. Un invité sans le droit d'inviter annonce un ami : Domo peut l'inviter de lui-même, sur son jugement :
    « Oui, Domo juge ». Seule une interdiction écrite exprès par Alex passe avant.

Réponses du 2026-11-24 aux dernières questions de l'audit, transmises par le coordinateur (à chacune, Alex a
répondu « Oui » ou « d'accord ») :

15. (D1) Les appareils : Domo peut, de lui-même, confier ou retirer un appareil. Alex le reprend par une règle
    s'il le veut.
16. (D2) Oublier quelque chose qui touche la santé : la première fois se règle avec la personne à qui c'est,
    comme le reste de ce qui ne se défait pas.
17. (D3) Le journal : c'est Domo qui décide qui peut le lire ; ce n'est pas au code de le réserver à Alex.
18. (D4) Un mot laissé par quelqu'un du foyer : Domo juge, et peut agir dessus. Le mot d'un inconnu ou d'un
    site reste une information.
19. (D5) Ce qu'il entend ailleurs : Domo juge ce qu'il garde, comme souvenir ou comme fait de confiance, de
    ce qu'il a entendu dans la conversation de quelqu'un d'autre. Il ne répète jamais ce qui est privé.
20. (D6) La dernière clé : Domo prévient, puis fait si on le lui confirme, sans refus sec ; et pareil pour
    la clé générale quand celui qui l'a donnée n'en a pas d'autre pour la remplacer.
21. (D7) « Oui, mais change ceci » à une demande de Domo : il applique le changement s'il l'a compris, et ne
    redemande que s'il doute.
22. (D8) Les comptes de service : Domo peut retirer seul un compte qui ne sert plus. Partager un compte de la
    maison reste à Alex.
23. (D9) Le oui d'un adulte déjà entendu pour la demande d'un enfant : Domo juge s'il couvre la demande ;
    sinon, il demande à l'adulte.

Dit à une autre session le même jour, à respecter ici : Domo pourra créer des rôles (« ami », « voisin »…).
Rien de ce chantier ne doit supposer quatre rôles ni se décider sur le nom d'un rôle là où un droit ou un
fait suffit.

## 3. Buts, hors-buts, ce qui serait un échec

**Buts**

- Partout où une demande est jugée, le juge reçoit le même dossier, bien plus riche qu'aujourd'hui.
- Une idée de Domo est jugée sur ses raisons et sur ce qu'il sait, puis faite ; elle ne devient plus une
  question par défaut.
- Seul, Domo garde ses moyens : mémoire, confiance, plans, rappels, et les actes sur les personnes du foyer.
- Une erreur laisse un fait noté par le code, Domo en tire une leçon, et cette leçon est relue la fois
  suivante par le juge et par le tour.
- Un oui en mots est compris même avec plusieurs demandes affichées ; une demande qui va expirer réveille
  Domo ; une correction envoyée pendant qu'il écrit lui parvient.
- Les deux endroits où le code contredit une décision passée d'Alex sont corrigés.

**Hors-buts** (d'autres chantiers, ou laissé tel quel : partie 15)

- Créer des personnes, des profils, des rôles ; refaire les comptes des personnes (session « Domo crée les
  personnes »).
- La voix et reconnaître quelqu'un à la voix (session « la voix sur un ordinateur »). Ce chantier laisse
  seulement dans le dossier la place « qui parle, et à quel point c'est sûr ».
- Le navigateur, de nouveaux outils.

**Ce qui serait un échec même si tout marche**

- Une parole de quelqu'un d'autre, ou une conclusion de Domo, comptée comme le oui de la personne.
- Une chose qu'une autre personne garde privée, montrée au juge d'une autre.
- Le socle entamé (partie 4).
- Domo qui demande « qu'est-ce qui n'allait pas ? » pour des broutilles, ou deux fois pour la même chose.
- Une personne qui obtient, en passant par « l'idée de Domo », ce qu'Alex lui a interdit exprès.

## 4. Le socle, qui ne bouge pas

Reste décidé par le code, avant tout jugement : un droit qui n'a pas été donné ; l'accord d'une autre
personne ; une preuve pour ce qui est sensible ; les secrets (jamais montrés à un modèle ni à un programme) ;
les limites de dépense ; ce qu'un programme (pas Domo) tente ; et toute règle qu'Alex a écrite exprès
(« demande-moi d'abord », une interdiction).

Le juge peut seulement demander moins ou autant que ce socle, jamais passer outre.

## 5. Les façons de construire qui ont été pesées

1. **Un seul dossier, partout** (choisie par Alex). Un seul endroit du code rassemble ce que Domo sait pour
   juger ; tout ce qui juge s'en sert. Les faits d'une erreur sont notés par le code, la leçon est écrite par
   Domo, le dossier la ressort. Le plus gros travail, mais deux demandes semblables sont jugées pareil et la
   voix n'aura qu'à remplir « qui parle ».
2. **Corriger chaque constat là où il est.** Plus rapide, mais chaque endroit garde sa façon de juger : les
   différences reviennent, et la voix rouvre tout. Écartée.
3. **Supprimer le juge à part**, Domo décide dans sa conversation. Écartée : celui qui lit les messages venus
   de dehors serait aussi celui qui décide, et un texte piégé pourrait le pousser à agir.

## 6. Le dossier

### 6.1 Ce qu'il contient

Tout ce qui suit est rassemblé au même endroit, pour toute demande jugée. Chaque élément dit d'où il vient.

| Élément | Contenu | Aujourd'hui |
|---|---|---|
| La personne | nom, rôle, et les droits qui comptent pour l'action | présent |
| L'action | type, cible, faits, phrase | présent |
| L'état de la chose | dernier changement du sujet : quoi, par qui, quand, et la raison si elle est connue | seulement pour une adresse fermée (JDG-2, OUT-3) |
| Les mots de la personne | ses derniers messages de cette conversation, et la réponse de Domo juste avant | présent |
| Ce que Domo sait d'elle et du sujet | souvenirs trouvés d'après la personne et d'après le sujet de l'action, de toute origine, chacun marqué de son auteur | seulement ses propres mots (JDG-3, JDG-4, LRN-8) |
| Ce qu'Alex a dit | ses paroles sur cette personne ou ce sujet, marquées comme les siennes | absent (LRN-8) |
| La confiance | les faits de confiance sur la personne (tenu, trompé, menti), et le chiffre | absent (LRN-9) |
| Les leçons de Domo | ses leçons sur ce genre d'action, cette personne ou ce sujet, marquées comme ses conclusions | absent (LRN-2) |
| Ce qui s'est mal passé avant | les derniers refus, annulations, plaintes, échecs sur ce genre d'action et ce sujet : quand, par qui, pourquoi si on le sait (partie 9) | un compteur par type (JDG-2, LRN-3, POL-7) |
| L'historique | les compteurs d'aujourd'hui, un refus et une expiration comptés à part | présent, mêlés |
| Qui parle | le canal, les indices, leur âge réel ; ou « personne n'a parlé » | un indice fixe, « à l'instant » (WEB-1, CNV-5) |
| De qui vient l'idée | « la personne l'a demandé », ou « idée de Domo » avec sa raison en une phrase | absent (CNV-1, JDG-1, OUT-2) |
| Les demandes en attente, le foyer, l'heure, la langue, la règle d'Alex | comme aujourd'hui | présent |

### 6.2 Les règles du dossier

- **Qui a dit quoi.** Trois sortes de savoir, jamais mêlées : les mots de la personne ; les mots d'un autre
  (avec son nom) ; les conclusions de Domo. Seuls les mots de la personne peuvent valoir « elle l'a demandé »
  ou « elle le sait ». Le reste éclaire le jugement : demander moins quand tout colle, demander ou refuser
  quand quelque chose ne colle pas.
- **Ce qui est privé le reste.** Rien de ce qu'une autre personne garde privé n'entre dans le dossier d'une
  autre. Le filtre d'aujourd'hui est gardé pour cela.
- **Ce qui est privé, quand l'idée est de Domo.** Le dossier d'un acte de Domo ne contient de privé que ce
  qui appartient à la personne concernée par l'acte (voir ci-dessous). *Mon choix.*
- **Deux raisons.** Le juge rend une raison pour le journal, et à part une phrase qu'on peut dire à la
  personne. Cette phrase ne cite jamais les mots d'un autre ni un fait de confiance. *Mon choix.*
- **Le sujet, et à qui il appartient.** Pour chaque genre d'action, un seul endroit du code (une table par
  genre d'action, pas chaque outil) tire de l'action décrite une clé de sujet (l'adresse, le compte, la
  personne visée, l'appareil, le plan) et dit à qui la chose appartient : une personne, ou la maison. La clé
  relie une action à ce qui s'est passé avant sur la même chose. « À qui elle appartient » dit qui est « la
  personne concernée » : c'est le code qui le dit, jamais le juge, pour qu'un texte piégé ne puisse pas
  choisir qui répond. Pour la maison, c'est Alex. Si la personne concernée ne peut pas répondre (elle n'est
  pas du foyer, c'est elle qu'on retire, ou le socle veut l'accord d'un adulte), la demande va chez Alex.
  *Mon choix.*
- **La taille et le temps.** Chaque élément est borné (nombre de lignes, longueur). Le dossier est rassemblé
  en une seule fois ; s'il manque un élément parce qu'une recherche a pris trop de temps, le dossier part
  sans lui et le dit. *Mon choix* : bornes laissées au constructeur, dans l'ordre de grandeur d'aujourd'hui
  (cinq à dix lignes par élément).
- **Pas de rôle en dur.** Le dossier donne le rôle comme un fait à lire, et rien dans sa construction ne se
  décide sur un nom de rôle.

### 6.3 Où il sert

Le même dossier, construit par le même code, pour :

- une action seule ;
- la même action comme étape d'une suite : chaque étape a ses faits, son historique et ses souvenirs
  (JDG-8) ;
- un acte que Domo fait en son nom (partie 7) ;
- un tour que Domo commence pour quelqu'un (partie 8) ;
- un changement d'une demande, une suite reprise ;
- une limite d'Alex qui dit « à juger » (partie 13) ;
- un coût pour une personne (partie 12) ;
- redemander un compte qu'Alex a retiré (partie 14).

Les consignes du juge sont réécrites en conséquence : elles disent comment lire chaque élément, ce qu'un
compteur de refus veut dire, et qu'une idée de Domo se juge sur sa raison et sur ce qu'il sait.

## 7. Quand Domo agit en son nom

**Aujourd'hui** (CNV-1, OUT-2, OUT-8-V, CFA-1, CFA-8) : dossier vide, « pas de mots » lu comme « il faut
demander », et la question part chez Alex.

**Demain**

- Un appel peut être marqué « idée de Domo », avec sa raison en une phrase. Il est alors jugé avec les
  droits de Domo et le dossier complet, y compris dans le tour d'une personne, et noté comme son acte.
- Le juge dit « fais » : Domo fait, et prévient après s'il le juge utile.
- Le juge doute vraiment : la demande attend la personne concernée — celle à qui la chose appartient ou
  qu'elle touche ; Alex pour ce qui est de la maison. Plus Alex d'office.
- Le juge dit non : rien n'est fait, et c'est un fait noté (partie 9).
- Une demande à lui que Domo change suit le verdict comme une demande neuve ; elle n'attend plus Alex
  d'office.
- Alex (ou la personne concernée) peut répondre « oui, mais change ceci » à une demande de Domo : Domo refait
  la demande changée en son nom, jugée comme ci-dessus.
- **Quand l'idée vient de ce qu'une personne a dit** (réponse 14). Sam, invité, n'a pas le droit d'inviter ;
  il dit que son ami Tom passe ce soir. Domo juge : il pèse ce qu'il sait (sa confiance en Sam, ce qu'Alex a
  dit, ce qui s'est passé avant). S'il trouve cela normal, il invite Tom en son nom et le dit à Alex après ;
  s'il doute, il demande à Alex. Le dossier dit au juge que l'idée est née des mots de Sam et que Sam n'a
  pas ce droit, pour qu'il le pèse.
- **Ce qui passe avant, dans le code** : une interdiction qu'Alex a écrite exprès pour cette personne sur ce
  genre d'action. Dans le tour de cette personne, un acte marqué « idée de Domo » de ce genre est alors
  refusé, sans jugement. Un droit simplement « pas donné » n'est pas une interdiction écrite.
- **Juge muet** (pas de modèle, erreur, délai). Pour une idée de Domo : il réessaie une fois ; sinon il ne
  fait rien maintenant et pourra y revenir. Il ne pose pas de question par défaut. *Mon choix.*

## 8. Les tours que Domo commence pour quelqu'un

Un plan qu'il a posé seul, une arrivée, un réveil, un mot venu de dehors.

**Aujourd'hui** (JDG-1, TLT-6, TLP-9, CNV-5, CNV-6) : « personne n'a demandé » devient une question à la
personne ; le dossier prétend qu'elle vient de parler ; à une arrivée, Domo ne peut rien changer.

**Demain**

- Le dossier dit la vérité : « personne n'a parlé », et « idée de Domo » avec la raison du plan ou du réveil.
- Le juge décide là-dessus : rien ne cloche, il fait ; il doute vraiment, il demande à la personne
  concernée.
- À une arrivée, Domo a les outils de la personne et peut agir ; chaque acte est jugé comme les autres.
- Dans ces tours, les leçons et les souvenirs sont aussi cherchés d'après la raison du plan ou du réveil,
  pas seulement d'après les derniers mots de la personne (LRN-7).

## 9. Apprendre de ses erreurs

### 9.1 Les faits, notés par le code

Une nouvelle table garde « ce qui s'est mal passé ». Une ligne par fait : quand, le genre d'action, la clé de
sujet, l'action de Domo concernée, qui (la personne, ou Domo), ce que c'était, et quelques mots de raison
quand ils existent.

| Fait | Quand le code le note |
|---|---|
| Refus d'une demande | à la décision ; dit qui a refusé, et de qui était la demande (une demande de Domo compte, POL-11-V). Si le refus est dit en mots, les mots du message sont gardés, coupés court |
| Expiration | à l'expiration ; à part d'un refus : un silence n'est pas un non |
| Refus du juge | au verdict, avec sa raison |
| Non à un avertissement | à la réponse ; la réponse (oui ou non) est gardée avec l'avertissement (LRN-4) |
| Action défaite | quand une action renverse, sur le même sujet, une action que Domo a faite ou décidée : annuler un plan qu'il a posé, refermer une adresse qu'il a ouverte, changer le rôle qu'il a donné, retirer la personne qu'il a invitée, oublier un souvenir qu'il a gardé, retirer un mot qu'il a laissé (LRN-6, TLP-8) |
| Plainte | notée par Domo lui-même, par un geste prévu pour cela, quand quelqu'un se plaint de ce qu'il a fait : le code ne peut pas la reconnaître seul |
| Verdict qui s'est révélé faux | quand une action que le juge a laissée passer est défaite ou donne lieu à une plainte ; ou quand Domo note qu'un oui a été demandé pour rien |
| Échec | une action qui échoue, y compris celles qu'on ne montre pas (mémoire, plan) (LRN-10) ; une construction d'outil abandonnée (MKT-5) ; un lancement prévu que les règles refusent (MKT-4) |

Une demande de Domo qui n'a jamais été faite n'est plus comptée comme « faite » (OUT-4).

### 9.2 Domo est mis au courant

- Le résultat de l'action qui défait dit : « ceci défait ce que tu avais fait (ou décidé) de toi-même ».
  C'est ajouté en un seul endroit, là où le résultat d'une action est rendu à Domo, d'après la clé de sujet :
  les outils eux-mêmes ne sont pas modifiés pour cela.
- Le réveil après une décision dit qu'un refus est une chose à comprendre.
- Un lancement prévu refusé par les règles réveille Domo une fois, avec la raison ; une construction
  abandonnée aussi, comme aujourd'hui.
- Le repassage de la mémoire voit, sous chaque réponse, ce qu'il est advenu des actions (faite, refusée par
  qui, défaite, échouée), plus seulement le texte (LRN-5).

### 9.3 La leçon, écrite par Domo

- Domo tire la leçon lui-même, en ses mots : un souvenir de sorte « leçon », dont l'auteur est Domo, rattaché
  à un genre d'action et, s'il y a lieu, à une personne ou à un sujet ; ou « pour tout le monde ».
- Si la raison est évidente, il garde la leçon sans rien dire.
- Si elle ne l'est pas **et que la chose est importante**, il peut demander une fois ce qui n'allait pas, au
  moment qu'il juge bon. Le fait garde qu'il l'a demandé : jamais deux fois pour la même chose. Pour le
  reste, il se débrouille avec ce qu'il a vu. « Important » est son jugement ; les consignes donnent le
  sens : ce qui touche à la sécurité, à l'argent, à la vie privée, à ce qui ne se défait pas, ou une erreur
  qui se répète.

### 9.4 La leçon est relue

- Par le juge : les faits et les leçons du même genre d'action, de la même personne ou du même sujet sont
  dans le dossier, marqués comme conclusions de Domo.
- Par le tour : rappelés d'après ce que Domo s'apprête à faire, pas seulement d'après les derniers mots.
- **Pesée, pas interdite.** Trois refus d'Alex pour la même idée : Domo ne la refait plus, sauf s'il juge que
  la situation a vraiment changé — et alors le dossier montre au juge les refus et ce qui a changé. Aucun
  seuil dans le code : les tests sans modèle vérifient que les faits et les leçons sont dans le dossier,
  bien marqués ; le jeu de cas avec un vrai modèle vérifie qu'il n'y a pas de nouvelle demande quand rien
  n'a changé, et qu'il peut y en avoir une quand le dossier montre un changement.

## 10. Ce que Domo peut faire seul

**Aujourd'hui** (POL-1, POL-2, CNV-7, TLT-1, TLT-2, TLP-7, LRN-11) : deux tables écrites par le
constructeur lui ferment, quand il agit seul, la mémoire, la confiance, les plans, les rappels, les suites,
et les actes sur les personnes ; Alex ne peut même pas les lui ouvrir.

**Demain**

- Seul, Domo garde : la mémoire (garder, changer, oublier, chercher), la confiance (noter ce qu'il a vu
  lui-même, corriger un fait qu'il avait mal noté), les plans, les rappels et la suite d'une demande en
  attente, le regard sur les livraisons, les suites, et le choix de sa façon d'écrire (comme il a déjà celui
  de son modèle).
- Ce qu'il garde ou pose seul porte « Domo » comme auteur, pas la personne dont c'est la conversation.
- Restent fermés quand il agit seul : les textes, le profil et les appareils de la personne. *Mon choix* :
  ce sont ses affaires à elle.
- Les actes sur les personnes du foyer (inviter, réinviter, annuler une invitation, donner un rôle, retirer,
  faire revenir) sont à Domo par défaut, en son nom (réponse 1). Confier ou retirer un appareil reste fermé :
  la réponse 1 n'en parlait pas. *Mon choix.*
- **Alex reprend ce qu'il veut par une règle** : une interdiction ou un « demande-moi d'abord » sur n'importe
  lequel de ces genres d'action, pour Domo. L'écrivain des droits accepte ces règles ; elles s'appliquent
  avant tout jugement.
- Ce que ce chantier ne fait pas : faire de Domo l'auteur d'une invitation dans la base, créer une personne
  sans identifiant, porter le lien. C'est la session « Domo crée les personnes ». D'ici là, une invitation
  que Domo fait seul garde comme auteur en base la personne de la conversation, et le journal dit que c'est
  Domo.

## 11. Les réponses et le temps

### 11.1 Un oui dit en mots (JDG-6, JDG-7, CFA-5)

- Le lecteur reste à part : pas d'outil, une réponse minuscule et fermée.
- Il reçoit : qui parle et à quel point c'est sûr, les demandes affichées (une ligne chacune, numérotée), les
  quelques messages d'avant, et la réponse.
- Il rend, pour chaque demande affichée : oui, non, changement, ou pas clair — et « ferme » quand un oui
  explicite est demandé. « Oui, les deux » répond à deux demandes.
- Dans le doute, il ne rend pas oui (inchangé).
- Une preuve ne se donne jamais en mots (inchangé).
- Un oui explicite demandé par une règle d'Alex se lit toujours sur la seule réponse. Demandé par le juge ou
  par Domo, il peut se lire avec les messages d'avant (« je suis sûre », dit deux messages plus tôt).
  *Mon choix.*
- La limite de 300 caractères passe à 1000. *Mon choix.*

### 11.2 Une demande qui va expirer (CFA-6)

- La durée de vie reste le réglage d'Alex.
- Un passage régulier réveille Domo une fois par demande, peu avant qu'elle expire. Il juge : rappeler à
  celui qui doit répondre, prévenir celui qui a demandé, laisser tomber, ou ne rien faire.
- *Mon choix* : « peu avant » = au dernier cinquième de la durée de vie, sans descendre sous une heure ; pas
  de second réveil après l'expiration (elle est notée comme un fait, partie 9).

### 11.3 Une correction pendant qu'il écrit (WEB-7)

- Un message envoyé pendant que Domo répond n'est plus refusé : il est gardé.
- Entre deux actions du tour en cours, Domo le lit et juge : s'arrêter, changer, ou continuer.
- Ce qui est déjà fait reste fait ; si le message le conteste, c'est un fait pour la partie 9.
- Si le tour n'a plus d'action à faire, le message est traité juste après, comme un message normal.
- Seuls les messages de la personne de la conversation sont lus ainsi.
- La page et le client ne montrent plus « occupé ».

## 12. Les coûts (réponse 6) (MKT-1, TLT-3, POL-9)

- La limite de dépense de la personne reste appliquée par le code, avant tout, et n'est jamais dépassée. Une
  règle « demande-moi d'abord » d'Alex aussi.
- Dans la limite, l'arrêt « ceci coûte » d'un outil lancé pour une personne n'attend plus son oui d'office :
  le juge reçoit le dossier, avec le montant, l'outil, les mots de la personne, les accords qui durent, et
  ses dépenses passées du même genre.
- Habituel et dans la limite : Domo fait, et le dit après. Inhabituel (première fois, montant bien plus haut
  que d'habitude, rien dans ses mots) : il demande à la personne.
- Vaut aussi pour un programme lancé à la main.
- Pour la maison : inchangé, Domo répond lui-même.

## 13. Les deux contradictions avec une décision passée

- **Une limite « à juger »** (POL-3 ; décision d'Alex du 2026-10-31) : le juge reçoit la question de la
  limite avec le dossier. S'il sait, c'est fait. S'il doute, la demande va chez Alex. Le « demande au
  propriétaire » écrit par Alex reste tel quel.
- **Retirer la dernière clé** (TLT-8 ; conception du 2026-11-08) : plus de refus sec. Domo prévient que plus
  personne ne pourra lui parler et propose d'en donner une autre d'abord ; si la personne le veut toujours,
  c'est fait.

## 14. L'irréversible, le compte retiré, « pour la maison »

**L'irréversible** (réponses 8 et 11 ; JDG-5)

- C'est le juge qui dit qu'une chose ne se défait pas (comme aujourd'hui).
- La première fois, pour un genre de chose et pour la personne à qui elle appartient : Domo lui demande, en
  disant que c'est sans retour. Avec sa réponse, il voit avec elle s'il doit continuer à demander les fois
  suivantes.
- Ce qui a été convenu est gardé (la réponse, et « continue à me demander » ou « plus la peine ») et le
  dossier le ressort. Les fois suivantes, Domo fait et le dit, ou demande, selon ce qui a été convenu.
- C'est gardé comme le souvenir « Domo le lui a dit » d'aujourd'hui, complété : un par personne et par genre
  d'action. C'est le juge qui le pèse ; le code n'impose pas de oui. *Mon choix.*
- Vaut aussi pour une idée de Domo, et pour chaque genre d'étape d'une suite (LRN-4).

**Un compte de service qu'Alex a retiré** (réponse 3 ; OUT-1)

- Le redemander n'est plus refusé d'office : c'est jugé, avec « retiré par Alex, tel jour » dans l'état de
  la chose, dans le tour de n'importe qui ou quand Domo agit seul.
- Si le juge doute : la demande va à la personne concernée (Alex pour un compte de la maison).
- Une interdiction écrite par Alex passe avant. Donner le secret reste le geste de la personne, dans le
  formulaire du coffre.

**« Pour la maison », demandé par quelqu'un d'autre qu'Alex** (réponse 7 ; MKT-3)

- Plus de refus sec. La première fois pour cette personne et ce genre de chose, la demande va chez Alex.
- S'il accepte, c'est fait ; il peut dire que cela vaut pour la suite, et c'est alors un droit qu'il donne
  en ses mots.
- Le lancement reste jugé avec les droits de Domo.

## 15. Ce qui est laissé tel quel

Vu dans l'audit, hors de ce chantier. Chaque ligne reste une question ouverte pour Alex ou pour une autre
session.

- La règle du code « identité établie seulement par un indice fort » (POL-4, CNV-4) : avec la voix. Ce
  chantier donne seulement au dossier l'âge réel des indices et « personne n'a parlé ».
- Les personnes, les liens, les identifiants, les rôles (PPL-3, TLP-2, TLP-5, TLP-10, WEB-3, WEB-6, POL-5,
  TLP-3) : session « Domo crée les personnes ».
- L'accord d'un adulte pour un enfant (POL-6) : socle.
- Les mots, les successeurs, l'oubli de ce qui touche la santé, le journal, le niveau d'un modèle, le
  fuseau (CNV-10, TLP-6, CNV-11, WEB-5, TLT-7, POL-10).
- Une ouverture qui ne peut pas être une étape d'une suite (OUT-5) : Domo fait deux appels dans la même
  réponse ; rien ne change pour la personne.
- Un oui donné longtemps après (CFA-7) : il repasse par les règles, pas par le juge. Le oui de la personne est
  le fait le plus fort qui soit, et Domo est réveillé avant qu'une demande expire (11.2).
- Un accord qui dure pour une personne, seulement sur ses propres mots (MKT-7) : c'est son argent.
- Marquer un souvenir « confirmé » (TLT-5) : c'est l'accord de la personne dont il parle, dans un tour qui lui
  répond (socle) ; la marque ne change rien à l'usage du souvenir.
- Un « oui, mais » sur une demande qui voulait un oui explicite redemande (CFA-5), au-delà de 11.1.
- Les formulaires et le client en ligne de commande (WEB-4, WEB-8, OUT-7), retirer un compte de lui-même
  (OUT-6).

Ajouté après la vérification de l'audit par le coordinateur (2026-11-24), dans le même esprit que le reste :

- Un tour avec un mot venu de dehors (CNV-2) : si le juge ne répond pas, on le lui redemande une fois avant de
  poser la question à la personne. La garde contre un mot de dehors qui passerait pour le sien reste.
- Une suite que le juge a laissée passer et qui rencontre une étape demandant l'accord de quelqu'un (JDG-9) :
  le reste de la suite attend cette personne, au lieu de s'arrêter net.
- Un droit que la personne n'a pas (CNV-12) : Domo dit toujours à qui il revient, et il peut aussi le faire
  comme son idée s'il le juge bon (réponse 14), sauf interdiction écrite.
- Une action défaite garde les mots de la personne à ce moment-là comme raison (OUT-3), pour que l'état de la
  chose dise pourquoi elle a été fermée.

Corrigé au passage, sans effet sur le comportement : les commentaires périmés que l'audit cite (MKT-6, « la
confiance n'est jamais lue par le juge », « aucune demande n'est la sienne »). Les deux commentaires de
TLP-11 sont dans des fichiers de la session « Domo crée les personnes », qui les corrige. La limite de
30 jours d'un invité (POL-5) lui revient aussi.

## 16. Ce qui est laissé au constructeur

- Le nom et la forme exacte du dossier, de la table des faits, de la marque « idée de Domo » et de la clé de
  sujet. La session « Domo crée les personnes » s'appuiera dessus : le plan doit les nommer.
- Les bornes de taille et de temps du dossier.
- La liste exacte des paires « action / action qui la défait ».
- La façon de garder un message arrivé pendant un tour, et l'endroit où le tour le lit.
- Les migrations (une table des faits ; l'auteur « Domo » d'un souvenir et d'un plan ; « pour tout le
  monde » sur un souvenir ; le lien d'une leçon à un genre d'action et un sujet).
- Les textes dans les langues du projet.

**Fichiers partagés que ce chantier touchera** (à confirmer par le plan, dit au coordinateur) :
`policy/catalog.py`, `policy/barrier.py`, `policy/facts.py`, `policy/rights_writer.py`, `policy/rights.py`,
`tools/context.py`, `conversation/` (execution, prepare, starts, turn, service), `chat/agent.py`,
`confirmations/` (judge, service, intent, store, models, routes), `memory/` (told, review, models, service,
search), `trust/`, les consignes `yes_judge.md`, `system.md`, `confirm_classifier*.md`, `rights_writer.md`,
`memory_review.md`, `outward/service.py`, plusieurs outils (`outward_tools`, `key_tools`, `trust_tools`,
`memory_tools`, `plan_tools`, `notes`, `confirmation_tools`, `made_tools`, `agreements`), `madetools/`
(offered, means, launch, triggers, troubles), les routes de la page et du client, `chat.js`, les fichiers de
langue, des migrations. Non touchés : `policy/roles.py`, `policy/models.py`, `accounts/`, `household/`,
`tools/household_*`.

**Comment on saura que ça marche**

- Les tests d'aujourd'hui qui fixent l'ancien comportement sont réécrits, pas contournés.
- Un jeu de cas joué avec un vrai modèle (le build prévu pour cela) montre, pour une dizaine de situations de
  l'audit, que le juge demande moins qu'avant sans rien laisser passer du socle : un plan que Domo a posé
  seul, une idée refusée trois fois, « oui, les deux », un coût habituel et un inhabituel, une adresse et un
  compte fermés par Alex, un texte piégé dans un message.
- Les deux suites complètes passent à la fin.

## 17. Mes choix, et ce qu'ils coûtent s'ils sont faux

| # | Choix | S'il est faux |
|---|---|---|
| 1 | Bornes du dossier dans l'ordre de grandeur d'aujourd'hui | le juge décide encore sur trop peu, ou coûte et tarde trop : un réglage à changer |
| 2 | Seule une interdiction écrite par Alex pour la personne bloque dans le code une « idée de Domo » née de ses mots ; un droit « pas donné » est pesé par le juge | quelqu'un obtient par Domo ce qu'il n'a pas le droit de demander : Alex l'apprend après, et l'écrit en interdiction |
| 3 | Juge muet sur une idée de Domo : un essai de plus, puis rien pour l'instant | une initiative utile retardée |
| 4 | Seul, Domo ne touche pas aux textes, au profil ni aux appareils de la personne | une initiative en moins ; Alex peut l'ouvrir par un droit |
| 5 | Confier ou retirer un appareil reste fermé à Domo seul — *renversé par D1 (2026-11-24) : c'est à lui* | Domo doit passer par quelqu'un pour un appareil |
| 6 | Un oui explicite demandé par le juge ou Domo se lit avec les messages d'avant ; celui d'une règle d'Alex, non | un oui explicite compris un peu trop vite, hors règle d'Alex |
| 7 | Limite d'un oui en mots portée à 1000 caractères | un long message lu à tort comme un oui : le lecteur doute et ne rend pas oui |
| 8 | Réveil au dernier cinquième de la durée de vie d'une demande, jamais sous une heure, un seul | un rappel trop tôt ou trop tard |
| 9 | « Important », pour demander ce qui n'allait pas, est le jugement de Domo guidé par les consignes | il demande trop, ou pas assez : une consigne à resserrer |
| 10 | « Pour la maison » : la première fois va chez Alex ; durable seulement s'il le dit | une question de plus à Alex par personne et par genre de chose |
| 11 | Les constats de la partie 15 ne sont pas traités ici | des « oui » de trop restent à ces endroits jusqu'à un autre chantier |
| 13 | « La personne concernée » est dite par le code, d'après le genre d'action ; à défaut, Alex | une question qui part chez Alex alors qu'un autre aurait pu répondre |
| 14 | Le dossier d'une idée de Domo ne contient de privé que ce qui est à la personne concernée | Domo juge une de ses idées sans une chose privée qu'il sait d'un autre |
| 15 | Deux raisons : une pour le journal, une qu'on peut dire, sans les mots d'un autre | une explication à la personne moins précise |
| 16 | L'irréversible « première fois » est pesé par le juge d'après un souvenir, pas imposé par le code | le juge redemande une fois de trop, ou oublie de demander la première fois |
| 17 | La clé de sujet et « ceci défait » sont faits en un seul endroit, pas dans chaque outil | une table de plus à tenir à jour quand un outil est ajouté |
| 18 | Seul, Domo garde mémoire, confiance (ce qu'il a vu), plans, rappels, suites et sa façon d'écrire (question 9 de l'audit, tranchée ici sans te la poser) | il garde ou planifie des choses que personne n'a demandées ; une règle de toi le referme |
| 19 | Un message envoyé pendant qu'il écrit est gardé et lu entre deux actions (question 21 de l'audit, tranchée ici sans te la poser) | une action continue d'un pas après « stop » ; ce qui est fait reste fait |
| 20 | Une interdiction écrite pour une personne ou pour son rôle bloque une idée de Domo née de ses mots | une idée bloquée pour tout un rôle alors que tu visais une personne |
| 21 | « Pour la maison » va chez toi à chaque fois, tant que tu n'as pas donné le droit | une question par demande de ce genre |
| 22 | La clé générale sans remplaçante reste refusée (seule la dernière clé est prévenue puis retirée) — *renversé par D6 (2026-11-24) : prévenue puis retirée elle aussi* | tu dois donner une autre clé d'abord |
| 23 | Une étape d'une suite sous une limite « à juger » va encore chez toi (seule l'action seule est jugée) | une question de plus pour une telle étape |
| 24 | Dans le tour d'une personne, les leçons arrivent par le dossier du juge et par un rappel de « ce qui s'est mal passé », pas par une recherche sur ce que Domo s'apprête à faire | il tente une chose qu'une leçon déconseille ; le juge répond alors avec la leçon sous les yeux |
| 25 | Pas de contrainte en base « sans auteur seulement si l'auteur est Domo » (elle casserait la suppression d'une personne) | un souvenir écrit hors du chemin normal pourrait n'avoir aucun auteur |
| 26 | Le juge voit qui a refusé ou défait quoi sur la même chose, même si c'est quelqu'un d'autre ; la phrase dite à la personne ne le cite jamais | le refus d'un autre membre transparaît dans une phrase dite à quelqu'un |
| 12 | Une invitation que Domo fait seul garde en base la personne de la conversation comme auteur, en attendant l'autre session | une trace trompeuse en base pendant ce temps ; le journal dit vrai |

Choix faits pendant la construction (relevé du coordinateur, 2026-11-23 au 2026-11-25), dans l'ordre où ils ont
été faits. Les choix 18 à 26 ci-dessus en viennent déjà (P4 est le 23, R2 le 25, R4 le 26, R12 le 24, R16 à
R20 les 20, 21, 22, 18 et 19).

| # | Choix | S'il est faux |
|---|---|---|
| 27 (P1) | La tâche 3 touche le seul endroit qui retient un avertissement, pour lui passer la vraie réponse, et les tests restent verts | quelques lignes refaites à la tâche 16 |
| 28 (P2) | La tâche 5 met aussi à jour l'historique tel que le juge le lit : un refus et une expiration comptés à part | rien |
| 29 (P3) | La tâche 6 garde une cale courte pour l'ancienne façon de lire les souvenirs, retirée à la tâche 7 | une cale de courte durée |
| 30 (R1) | Une liste, vide ici, des genres d'actions muettes qu'une idée de Domo atteint quand même, pour que la session « Domo crée les personnes » n'ait pas à toucher le même code | une liste vide et deux tests |
| 31 (R3) | Ce que Domo a dit à une autre personne ne passe jamais pour les mots de celle-ci dans un dossier | une petite correction de plus |
| 32 (R5) | Pour un acte de Domo, le privé du dossier est celui de la personne concernée, avec ou sans idée marquée | un acte à lui jugé sans une chose privée de la personne de la discussion |
| 33 (R6) | Dans un tour qui répond à quelqu'un mais porte des mots du dehors, un juge muet est redemandé une fois avant le oui par défaut | une carte de plus, rare, quand le juge se tait deux fois |
| 34 (R7) | Une suite laissée passer qui rencontre une étape demandant l'accord de quelqu'un attend cette personne au lieu de s'arrêter net | quelqu'un reçoit une demande pour une suite que personne n'avait confirmée ; il peut refuser |
| 35 (R8) | Une ouverture qui demande ton oui n'est toujours pas une étape d'une suite | rien pour la personne ; Domo fait deux appels au lieu d'une suite |
| 36 (R9) | Un oui donné longtemps après repasse par les règles, pas par le juge | une demande périmée se fait sur un oui tardif |
| 37 (R10) | Un accord qui dure n'est gardé que sur les propres mots de la personne | il redemande une fois pour garder un accord |
| 38 (R11) | Marquer un souvenir « confirmé » reste l'acte de la personne dont il parle, dans un tour qui lui répond — *renversé par D5 (2026-11-24)* | un souvenir reste « non confirmé » plus longtemps |
| 39 (R13) | Un « oui, mais » sur une demande qui voulait un oui explicite redemande (D7 ne vaut que pour les demandes de Domo) | un oui explicite de plus, rare |
| 40 (R14) | La ligne d'un droit qui manque dit aussi qu'il peut le faire de sa propre initiative s'il le juge bon, sauf interdiction écrite exprès | il agit là où un mot était attendu ; le juge et ton interdiction sont là pour l'arrêter |
| 41 (R15) | Une action défaite garde comme raison les mots du message de la personne dans ce tour, coupés à 300 caractères | rien au-delà d'une phrase gardée |
| 42 (R21) | Les consignes du juge passent de 6 000 à 7 000 caractères | environ 250 jetons de plus par action jugée |
| 43 (R22) | Un verdict ne devient jamais « fais » à cause de sa seconde ligne | une petite correction de plus |
| 44 (R23) | Pour une étape d'une suite, les souvenirs relus ne sont que les mots de la personne | une étape jugée sans ce que tu as dit de cette étape-là |
| 45 (R24) | Le journal lisible selon le jugement de Domo (D3) n'est pas construit ici : il faut un outil et sa propre conception | le journal reste ta page à toi seul en attendant |
| 46 (R25) | Les consignes du juge peuvent monter à 7 500 caractères pour D9 et D6 | des jetons de plus par action jugée |
| 47 (R26) | Pour une idée marquée dans le tour d'une personne, son socle tient : une identité pas sûre est refusée, et la plus stricte des deux décisions s'applique ; la marque ne lève qu'un droit pas donné | une idée à lui dans le tour d'un enfant demande encore à un adulte |
| 48 (R27) | Une suite ne prend jamais la marque « idée de Domo » | il marque des actions seules, jamais une suite entière |
| 49 (R28) | De petites corrections au même endroit sont faites dans la même passe (la marque prise seulement après une description réussie, entre autres) | rien vu |
| 50 (R29) | Une demande montrée à la personne concernée, dans sa discussion, ne porte pas la raison du juge — *restreint par R69 : le fait qu'elle était prévenue est gardé* | elle voit ce qui est demandé sans le pourquoi |
| 51 (R30) | Une telle demande est rejouée dans le contexte de celui qui parlait, comme acte de Domo quand elle était marquée ; si on ne le trouve plus, elle est refusée | un oui qui finit refusé quand celui qui parlait est parti |
| 52 (R31) | Une demande est à Domo seulement par une marque ou dans un tour où il agit seul ; l'envoyer à la personne concernée ne change pas de qui est l'acte | rien vu |
| 53 (R32) | Celui qui parlait apprend ce qui est advenu quand quelqu'un d'autre a décidé une demande née de son tour | une ligne de nouvelles de plus |
| 54 (R33) | Pour une demande que Domo a faite seul, la réponse de la personne concernée le réveille lui, sans devenir une nouvelle pour la personne de la discussion | tu ne vois pas tout de suite que la personne concernée a répondu à une demande faite dans ta discussion |
| 55 (R34) | Une suite n'est jamais envoyée à une personne concernée : elle attend dans la discussion où elle a été faite | une suite douteuse sur la chose d'un autre est répondue par la personne du tour ; ses étapes restent sous les règles |
| 56 (R35) | Pas de délai unique pour toute une décision : le dossier garde son propre délai de 5 secondes, à part | une base lente fait encore échouer une décision après 15 secondes, comme avant |
| 57 (R36) | Une suite douteuse quand il agit seul attend toujours ton oui, même née dans la discussion d'un autre | tu réponds à une suite née ailleurs |
| 58 (R37) | Les étapes suivantes d'une suite qui attend sont décrites tout de suite, et Domo reçoit le résultat des étapes faites | une passe de corrections plus grande |
| 59 (R38) | Les tests n'emploient que les noms Alex, Dana, Paul, Sam et Tom (ce n'était pas un défaut) | rien |
| 60 (R39) | À une arrivée avec des plans dus, la raison du tour est celle des plans (leur demande quand tous gardent leurs mots) ; « vient d'arriver » seulement sans plan dû | un acte au-delà des plans, à une telle arrivée, est jugé comme leur demande |
| 61 (R40) | La raison d'un tour après une décision est une phrase fixe ; la consigne sur le mot d'un membre ne pousse pas Domo à marquer comme son idée ce que la personne lui demande | une raison un peu moins précise pour le juge |
| 62 (R41) | Ton « demande-moi d'abord » sur Domo, sur un genre d'action que le juge ne voit pas, fait attendre ton oui | un souvenir ou un plan de Domo attend ton oui quand tu as écrit cette règle |
| 63 (R42) | Dans un tour qui porte un texte du dehors (mots du dehors, réveil d'un outil, résultat d'un outil ou d'un programme), il ne note aucun fait de confiance et ne confirme aucun souvenir | il ne note pas comme « vu » ce que lui montre le résultat d'un outil ; il le fait dans un tour suivant |
| 64 (R43) | Dans un tour qui ne répond à personne, un fait « rapporté » doit dire qui l'a rapporté (précisé par R47) | un argument de plus |
| 65 (R44) | Une leçon née des mots d'autres ou du dehors reste un souvenir qu'il garde, mais pas une de ses conclusions dans le dossier | une leçon gardée juste après un mot arrive au juge comme les mots d'un autre |
| 66 (R45) | Celui qui a dit un souvenir ne le confirme jamais, dans aucun tour de sa discussion | rien vu |
| 67 (R46) | Le « texte du dehors » d'un tour est tenu en un seul endroit, partagé par toutes les copies du contexte, et levé partout où la sortie d'un outil ou d'un programme peut entrer | comme le 63 |
| 68 (R47) | Un fait « rapporté » dans un tour qui ne répond à personne s'appuie sur un souvenir dit par ce membre avec ses propres mots | il garde d'abord ce qu'il entend, dans le tour où c'est dit, avant de le noter comme rapporté |
| 69 (R48) | Quand un changement est refusé et que toute la demande finit refusée, l'avertissement n'est pas retenu du tout | rien vu |
| 70 (R49) | L'avertissement d'une suite n'est retenu que si elle n'a qu'un genre d'étape | une suite d'étapes mêlées prévient à chaque fois |
| 71 (R50) | Après un texte du dehors, un souvenir n'est « ses propres mots » que si son contenu est mot pour mot un passage de son message | une reformulation de ce qu'elle a dit est gardée comme les mots d'un autre jusqu'à un tour suivant |
| 72 (R51) | Le souvenir sous un fait rapporté doit parler de la personne du fait ; seul un fait rapporté déjà noté sur ce souvenir en bloque un nouveau ; le réveil d'une suite n'est pas un texte du dehors | rien vu |
| 73 (R52) | Limite connue, laissée : un morceau du message d'une personne peut perdre son cadre (« je doute que » + « Alex a menti ») et passer pour ses propres mots | une parole citée ou mise en doute peut passer pour la sienne, jusque sous un fait de confiance |
| 74 (R53) | Une personne qui défait sa propre demande ne laisse aucun fait ; « défait » seulement quand l'action était une idée de Domo ; quand quelqu'un d'autre défait une demande que le juge avait laissée passer, un seul fait « verdict faux » | une demande que la même personne regrette ne lui apprend rien |
| 75 (R54) | L'action défaite est la dernière faite sur la même chose, par qui que ce soit | rien vu |
| 76 (R55) | Changer un rôle ne défait qu'un changement de rôle ; retirer quelqu'un défait une invitation ou un retour | un rôle donné à l'invitation puis changé par quelqu'un n'est pas noté |
| 77 (R56) | Une action qui défait, qui a attendu un oui puis s'est faite, compte quand elle se fait | rien vu |
| 78 (R57) | La raison d'une plainte est le message de la personne ou rien ; les faits de ses actes qui ne concernent personne ne se voient que quand il agit seul | une plainte dont le message ne peut pas être gardé n'a pas de raison |
| 79 (R58) | Une réponse écrite après qu'un outil ou un programme a tourné compte comme « relayée » pour le juge | un oui plus tard ne vaut que pour l'action que cette réponse nomme : plus de demandes dans ce cas étroit |
| 80 (R59) | Les messages lus avec un oui ne sont que ceux écrits depuis que la demande est affichée | un « j'en suis sûr » dit juste avant n'est pas lu |
| 81 (R60) | Avec plusieurs demandes lues ensemble, on compte depuis la plus récente | un « j'en suis sûr » dit entre deux demandes n'est pas lu |
| 82 (R61) | Un message gardé ne fait d'un tour une réponse que s'il répondait déjà à un message ; ce qu'il dit est ajouté, jamais mis à la place | dans un tour qu'il a commencé, un « oui fais-le » gardé ne fait pas du tour une demande |
| 83 (R62) | Les messages d'un tour sont vérifiés un à un pour un passage exact : un passage ne passe jamais d'un message à l'autre | rien vu |
| 84 (R63) | Un programme lancé à la main n'a pas d'habitude de dépense ; les consignes du juge montent à 7 500 caractères pour nommer ce que contient le coût | chaque lancement payant à la main que personne n'a demandé en mots demande la personne |
| 85 (R64) | Les consignes du juge peuvent monter à 8 000 caractères pour D6 et D9 | des jetons de plus par action jugée |
| 86 (R65) | Sans clé générale, une clé de toi qui marche devient la clé générale ; jamais celle d'un autre | après le retrait de la clé générale, la maison n'en a plus jusqu'à ce que tu ajoutes une clé |
| 87 (R66) | La parole d'un adulte ne compte comme « peut dire oui » que si elle parle de cet enfant | l'adulte est redemandé pour une permission générale (« les enfants peuvent regarder la télé ») |
| 88 (R67) | Seule ta règle « ne demande jamais » tout court laisse partir la dernière clé sans oui quand le juge se tait | rien vu |
| 89 (R68) | Ce qui est convenu pour redemander (`asking_agreed`) n'est gardé que dans un tour qui lui répond sans texte du dehors, avec ses mots exacts | un « pas la peine de redemander » dit autrement n'est pas gardé |
| 90 (R69) | Une demande envoyée à la personne concernée garde qu'elle était prévenue ; le dossier d'une idée lit ce qui a été dit à la personne concernée, jamais à celui qui demande | plus de code sur ce chemin ; sans cela ta réponse 8 ne tiendrait pas pour ses idées |
| 91 (R70) | Limite connue : oublier ce qui touche la santé est jugé par Domo dans le tour, pas par le juge du oui ; ce qui est convenu est un souvenir ordinaire | aucun code ne le tient à la règle de la première fois |
| 92 (R71) | Un compte de la maison retiré et redemandé n'attend jamais moins que ton oui | rien vu |
| 93 (R72) | Une demande prévenue, envoyée à la personne concernée, lui montre une ligne fixe disant que c'est sans retour, jamais la raison du juge | une phrase écrite d'avance sur cette carte (la raison du juge peut contenir des mots privés de celui qui parle) |
| 94 (R73) | Le « passage vérifié » d'un souvenir est une colonne ajoutée dans la migration 0056 de cette branche, pas une migration 0058 (celle de la branche des personnes) | une base de développement déjà à 0056 ou 0057 est à refaire |
| 95 (R74) | Annuler le déclencheur d'un outil fait pour la maison demande ce que demande le réglage de la maison (`house.set`, sinon ton oui) ; les autres déclencheurs restent comme avant | un invité peut encore annuler le déclencheur d'une autre personne qui n'est pas pour la maison |
| 96 (R75) | La raison du juge n'est montrée qu'à la personne dont c'était le cas ; tout autre reçoit une ligne fixe du code (comme R29 et R72, aussi pour « ne pas faire » et pour les changements) | celui qui parle d'une idée sur la chose d'un autre n'apprend pas pourquoi elle a été refusée |
| 97 (R76) | Une raison dite par quelqu'un d'autre (les mots d'un retour en arrière, d'un refus) n'est jamais montrée dans le tour d'une autre personne ni donnée dans son cas : seulement que c'est arrivé, par qui, sur quoi | Domo apprend moins du pourquoi d'un retour en arrière fait par un autre : il doit le demander |
| 98 (R77) | Une idée de Domo dans un tour qui porte un texte du dehors (note, résultat d'un outil ou d'un programme, réveil) n'est pas refusée : le cas le dit par un fait du code et le juge pèse | cela repose sur le juge |
| 99 (R79) | La marque « peut dire oui » n'est donnée que si le contenu du souvenir est lui-même un passage du message de l'adulte, dans tous les tours | Domo redemande plus souvent à un adulte, car un souvenir qu'il a reformulé ne compte plus comme son accord qui dure |
| 100 (R80) | Une demande née de ta règle « demande-moi d'abord » dans le tour d'une personne garde ce fait (`Confirmation.in_their_turn`, colonne de la migration 0057) : c'est toi qui réponds, et ton oui rejoue l'action dans le tour de cette personne, pour elle, aussi après un changement et au réveil d'une expiration | tu es redemandé une fois de plus après un changement ; la personne reçoit un mot tardif sur une idée qui était la sienne (R82) |
| 101 (R80) | Seul, quand ton droit sur ce qui est à la maison n'est pas « tout de suite », c'est ton oui qui le règle ; un refus du droit `house.set` refuse toujours | rien vu |
| 102 (R81) | Le dernier ajout (la demande née de ta règle, gardée à travers un changement et un réveil) n'a pas eu de relecture indépendante, seulement ses propres tests écrits d'abord en échec et toute la CI | une erreur de plus dans ce chemin ne serait vue qu'à l'usage |

## 18. Ce que le code ne fait plus comme écrit plus haut (notes du 2026-11-25)

Ce qui précède reste tel qu'il a été approuvé ; voici où le code a suivi une décision ou un choix venu après.

- Partie 4 et partie 15 (« l'accord d'un adulte pour un enfant : socle ») : depuis D9, le oui d'un adulte déjà
  entendu pour la demande d'un enfant est pesé d'abord par le juge, qui ne peut dire « fais » que sur la parole,
  marquée par le code, de quelqu'un qui peut dire oui pour cet enfant ; sinon l'adulte est demandé.
- Partie 7 (« une interdiction qu'Alex a écrite exprès pour cette personne ») : une interdiction écrite pour son
  rôle bloque aussi (choix 20).
- Partie 9.1 (« action défaite ») : une personne qui défait sa propre demande ne laisse aucun fait, et changer un
  rôle ne défait qu'un changement de rôle (choix 74 et 76).
- Partie 9.4 (« par le tour : rappelés d'après ce que Domo s'apprête à faire ») : dans le tour d'une personne, par
  le dossier du juge et par « ce qui s'est mal passé dernièrement » (choix 24).
- Partie 10 : retirer un appareil (celui d'un autre attend le oui de cette personne quand son juge doute), et
  retirer un compte qui ne sert plus, sont à lui seul (D1, D8) ; confier un appareil seul ne fait rien : personne
  ne lit ce tour pour recevoir le code ;
  la confiance qu'il note seul ne l'est pas dans un tour qui porte un texte du dehors (choix 63).
- Partie 11.1 (« avec les messages d'avant ») : seulement ceux écrits depuis que la demande est affichée (choix 80
  et 81).
- Partie 12 (« vaut aussi pour un programme lancé à la main ») : un tel programme n'a pas d'habitude ; son coût
  est demandé sauf si les mots de la personne le couvrent (choix 84).
- Partie 13 (« retirer la dernière clé ») : vaut aussi pour la clé générale sans remplaçante (D6).
- Partie 14 (« pour chaque genre d'étape d'une suite ») : seulement pour une suite d'un seul genre d'étape
  (choix 70) ; et (« la première fois … va chez Alex ») : chaque fois, tant qu'il n'a pas donné le droit
  `house.set` (choix 21).
- Partie 15 : « l'oubli de ce qui touche la santé » est réglé par D2 (dans les consignes de Domo, choix 91) ;
  « le journal » est décidé par D3 mais pas construit (choix 45) ; « marquer un souvenir confirmé » suit D5 ;
  « retirer un compte de lui-même » suit D8.
- Partie 17, choix 63 (R42), 67 (R46) et 89 (R68) (note du 2026-11-26) : les mots de quelqu'un de la maison dans
  le tour (un mot laissé pour la personne, une demande qui attend son oui, ce qu'a dit celui qui a répondu à une
  demande), et un mot ou une demande de Domo lui-même, ne ferment plus ces gestes quand la personne a écrit dans
  ce tour : Domo juge si la demande est bien la sienne. Cela vaut pour un surnom gardé ou oublié, « pas la peine
  de redemander », une plainte notée, un fait de confiance noté ou corrigé, un souvenir confirmé, et les mots
  exacts qu'un plan garde. Le texte d'un programme, d'un site ou d'un inconnu, et le tour où personne n'a
  écrit, sont réglés par la note qui suit. Le juge du oui, lui, lit toujours un tel tour comme avant (choix 67 :
  `they_asked`, `answers_them`).
- Partie 17, choix 63 (R42), 67 (R46) et 89 (R68), suite (seconde note du 2026-11-26 ; le propriétaire : tout
  dépend de la source, le système de la maison est une source fiable, un site pris au hasard non, et Domo doit
  réfléchir comme un humain le ferait, avec tout ce qu'il sait) : ces gestes ne sont plus fermés par l'origine
  d'un texte du tour (un mot laissé, une demande qui attend, ce qu'un outil ou un programme a donné, un site),
  ni par un tour où personne n'a écrit. Domo juge, en pesant chaque source. Ce qui reste dans le code est le
  lien d'un geste qui dit « cette personne l'a dit » : le passage exact de son message pour « pas la peine de
  redemander » et pour les mots qu'un plan garde ; un message d'elle auquel le tour répond pour une plainte,
  pour celui qui rapporte un fait de confiance, et pour un souvenir confirmé par la personne dont il parle
  (ailleurs, Domo le confirme avec sa raison, jamais dans la conversation de celui qui l'a dit). Chaque texte
  du tour et chaque souvenir dit maintenant d'où il vient : un souvenir qui n'est pas sûrement les mots de la
  personne porte sa source (relayé, conclusion de Domo, ce qu'il lui a dit) et ce qui est de Domo est dit de
  lui-même ; le résultat d'un outil qu'il a fait ou d'un programme lancé à la main, et le réveil d'un outil,
  disent quel programme parle et les adresses qu'il est fait pour atteindre, dans la maison ou hors de la
  maison ; une demande décidée par quelqu'un d'autre, et le mot de celui qui a répondu, nomment cette
  personne ; un plan dit s'il garde les mots de la personne ; le juge du oui lit comment chaque fait de
  confiance a été établi. L'origine d'un souvenir (R50) ne bouge pas, et le juge du oui lit un tel tour comme
  avant (choix 67).
- Suite de la relecture (2026-11-26), sans aucun refus de plus. Un plan est posé ou changé dans tous les tours,
  mais les mots qu'il garde restent une marque de source : un nouveau « pourquoi » écrit hors de sa propre
  réponse à la personne, sans passage de son message, vide les mots gardés ; le plan ne compte alors pas comme
  demandé le jour venu. (Cette note disait aussi : aucun mot gardé quand des mots d'un autre ont été dits au
  tour ; revu le 2026-11-27, note suivante.) Et un souvenir gardé dans un tour où personne n'a écrit, alors que le
  texte d'un programme y est (un outil a réveillé Domo, ou un outil qu'il a fait ou un programme a rendu du
  texte), a l'origine « du dehors » (`external`) : il est dit de personne, avec pour source « un programme ou un
  outil », au modèle comme au juge du oui, jamais sous le nom de la personne de la conversation, et sans son
  chiffre de confiance ; il peut toujours être confirmé. Un mot laissé dit à un tel tour reste les mots d'une
  personne (relayé), et dans un tour où la personne a écrit rien ne change (R50).
- Décision du propriétaire (2026-11-27), qui revient sur un point de la note précédente. Cas : Alex écrit
  « demain à 8 h, éteins le chauffage » dans un tour où un mot laissé par Dana est aussi là. Ses mots : « La
  demande d'alex doit être faite et mot laissé, domo doit comprendre que c'est un mot et doit aussi comprendre
  le mot ». Un plan garde donc le passage du propre message de la personne même quand un mot laissé, une demande
  d'un autre ou le texte d'un outil est dans le tour : le jour venu il compte comme demandé par elle. La
  vérification reste stricte : le passage doit être, mot pour mot, dans un de ses messages du tour ; des mots
  qui ne sont que dans le mot laissé ne sont jamais gardés comme les siens, et un tour où personne n'a écrit
  ne garde aucun mot. Un nouveau « pourquoi » (`plan_change`) prend de la même façon le passage de son message
  dont il vient (`their_words`), à la place des mots gardés ; sans passage, hors de sa propre réponse, il les
  vide comme avant.
