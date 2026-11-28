# MyDomo

MyDomo est un assistant IA générique et auto-hébergé. Il s'appelle Domo tant
qu'une personne ne lui a pas donné le nom qu'elle veut. Tu l'exécutes sur ton
propre matériel avec Docker Compose, tu lui parles depuis une page web, et tu
gardes la maîtrise de tes données et de tes clés d'API : les clés des modèles
sont dans un coffre chiffré que le cœur de l'assistant ne peut jamais lire.

*English version: [README.md](README.md).*

**Ce qu'il fait aujourd'hui** (la deuxième itération, la maison) : tu l'installes
sur un serveur, tu crées le compte du propriétaire, tu choisis le modèle d'IA
avec lequel il parle (Anthropic, OpenAI, Google, ou tout serveur qui parle l'API
d'OpenAI, par exemple un serveur de ton réseau), puis tu discutes avec lui dans
une page web, depuis un ordinateur ou un téléphone. L'assistant tient lui-même
les personnes de la maison : celles avec qui tu vis, tes invités, et celles qui
comptent pour la maison, avec ou sans compte. Qui a un compte a sa propre
discussion. L'assistant sait maintenant faire quelques petites choses : laisser
un mot à quelqu'un, inviter des personnes et changer leur rôle, se créer des
rôles, aider le propriétaire à changer la clé du modèle. Avant tout ce qui
compte, l'assistant juge, pour chaque action, s'il la fait, s'il demande un oui,
le bouton, ou une preuve que c'est bien toi ; les règles du propriétaire et les
droits passent avant. On peut se connecter avec une clé d'accès (passkey) au
lieu de son mot de passe. Chaque action importante est inscrite dans un journal d'audit
infalsifiable que le propriétaire peut consulter. Tout ce qu'on lit vient de
Domo, pour celui qui le lit, dans sa langue : il part de phrases par défaut en
français et en anglais, et les réécrit quand il a une raison de le faire (voir
[L'assistant écrit tout](#domo-écrit-tout)). Il se souvient d'une discussion à
l'autre, il connaît l'heure, et il peut se donner rendez-vous pour te reparler de
lui-même (voir [La mémoire et le temps](#la-mémoire-et-le-temps)). Quand il n'a
pas le moyen de faire quelque chose, il peut s'écrire un petit programme, le
lancer tout de suite et le garder comme outil à lui, qui tourne à part dans un
bac à sable sans réseau et garde ses données dans une base à lui (voir
[L'assistant se crée des outils](#domo-se-crée-des-outils)). Sur sa page, tu
peux aussi lui parler à voix haute et l'entendre répondre (voir
[Parler à l'assistant à voix haute](#parler-à-lassistant-à-voix-haute)). D'autres
outils et d'autres façons de parler à l'assistant viendront dans les itérations
suivantes.

## Prérequis

- Un serveur (ou un ordinateur toujours allumé) sous **Linux 64 bits**.
- **Docker 28 ou plus, avec Docker Compose** (vérifie avec `docker version` et
  `docker compose version`). Voir [docs.docker.com/engine/install](https://docs.docker.com/engine/install/).
- `git`.
- Un compte chez un fournisseur d'IA et sa clé d'API, ou un serveur compatible
  OpenAI sur ton réseau. L'assistant n'embarque pas de modèle pour parler (seulement
  un petit, pour retrouver ses souvenirs par leur sens).
- Environ 3 Go d'espace disque libre pour les images (celle du cœur de l'assistant
  pèse à elle seule environ 1,2 Go, à cause du petit modèle local). Le cœur seul
  utilise environ 760 Mo de mémoire une fois ce modèle chargé : c'est ce qui a
  été mesuré. Les autres services en demandent bien moins ; prévois de la marge.
  Le bac à sable où tournent les outils que l'assistant se crée est limité à 1,5 Go
  de mémoire (quatre lancements de 256 Mo à la fois au départ, et son lanceur ;
  L'assistant peut changer les deux, jamais au-delà de cette limite) : il n'en
  approche que quand ses outils travaillent.
- Pour la voix (voir
  [Parler à l'assistant à voix haute](#parler-à-lassistant-à-voix-haute)) :
  environ 600 Mo de plus pour l'image du service de parole et, à son premier
  démarrage, environ 1,1 Go de modèles ouverts qu'il télécharge (environ 2,9 Go
  avec une carte graphique). Ses petits modèles prennent peu de mémoire (sur le
  PC où ils ont été essayés : environ 130 Mo pour entendre, 430 Mo pour la voix,
  moins de 50 Mo pour reconnaître les voix). Une carte graphique (NVIDIA) n'est
  pas obligatoire : avec, il peut prendre de meilleurs modèles. Un micro sur
  l'appareil d'où tu parles.

## Installation

```bash
git clone <adresse-du-depot> domo
cd domo
cp .env.example .env
```

Ouvre `.env` dans un éditeur de texte et renseigne **`DOMO_SITE_ADDRESSES`**
avec l'adresse que tu taperas dans le navigateur pour joindre le serveur : son
adresse sur ton réseau, par exemple `192.168.1.10` (sous Linux, `hostname -I`
liste les adresses du serveur). Tu peux ajouter son nom, séparé par une
virgule : `192.168.1.10, domo.local`.

- **Mets en premier l'adresse que tu utiliseras vraiment**, et fais-en une
  adresse IPv4 si tu ouvres la page par son adresse IP. Un navigateur qui ouvre
  une page par son adresse IP n'indique aucun nom au serveur : seule la première
  adresse IPv4 de la liste a alors son propre certificat. Toute autre adresse de
  la liste, et toute adresse absente de la liste, affichera un avertissement de
  certificat même après avoir fait confiance au certificat de l'assistant.
- Les adresses IPv6 ne sont pas encore prises en charge dans cette liste.

Les autres réglages ont des valeurs par défaut raisonnables :

| Réglage | Par défaut | Rôle |
| --- | --- | --- |
| `DOMO_SITE_ADDRESSES` | `localhost, 192.168.1.10` (dans le fichier d'exemple) | Les noms et adresses avec lesquels la page est ouverte. **À modifier.** Les clés d'accès ne marchent qu'avec les noms de cette liste (voir [Clés d'accès](#clés-daccès-passkeys)). |
| `DOMO_HTTPS_PORT` | `8443` | Le port de la page (HTTPS) : celui que l'on tape dans l'adresse. |
| `DOMO_HTTP_PORT` | `8080` | Redirige seulement vers le port HTTPS. |
| `DOMO_BIND_ADDRESS` | `0.0.0.0` | L'adresse du serveur sur laquelle les deux ports sont publiés : `0.0.0.0` pour toutes, `127.0.0.1` pour cette machine seulement. |
| `TZ` | `UTC` | Le fuseau horaire des services, par exemple `Europe/Paris`. |
| `DOMO_CORE_NET_PREFIX` | `10.247.81` | Avancé, voir ci-dessous. |
| `DOMO_LAN_NET_PREFIX` | `10.247.80` | Avancé : la même chose pour le réseau où les ports sont publiés. |

Puis démarre tout :

```bash
docker compose up -d
```

Le premier démarrage construit les images, ce qui prend quelques minutes.
Laisse quelques secondes de plus aux services, puis `docker compose ps` doit
afficher `core`, `db`, `edge` et `gatekeeper` comme `healthy`. (`init`, qui crée
les secrets de l'instance la première fois, s'exécute une fois puis s'arrête :
`docker compose ps` ne l'affiche pas, et `docker compose ps --all` l'affiche
comme terminé, ce qui est normal.)

### Si le réseau interne entre en collision avec le tien

Les services se parlent sur un réseau privé qui leur est propre,
`10.247.81.0/24` par défaut. Si ton réseau domestique utilise déjà cette plage
(c'est rare), `docker compose up` s'arrête avec une erreur qui parle de
chevauchement d'adresses (par exemple `networks have overlapping IPv4`, ou `Pool
overlaps with other one on this address space` avec les anciennes versions de
Docker). Choisis les trois premiers nombres d'une autre plage libre, ajoute la
ligne suivante à `.env`, puis relance :

```bash
DOMO_CORE_NET_PREFIX=10.247.82
```

```bash
docker compose down
docker compose up -d
```

## Le code d'installation

La première fois, quiconque ouvre la page pourrait s'emparer de la nouvelle
instance en créant son propriétaire. Pour s'assurer que c'est bien toi, l'assistant
écrit un **code d'installation** à usage unique dans ses journaux, que seule la
personne qui administre le serveur peut lire :

```bash
docker compose logs core | grep "setup code"
```

Tu verras une ligne comme `L'assistant setup code: XXXX-XXXX-XXXX`. Si plusieurs
lignes de ce genre apparaissent (l'assistant a été redémarré), utilise la dernière :
chaque démarrage remplace le code précédent. Après dix codes erronés en quinze
minutes, la page refuse tout code pendant un moment ; redémarrer le cœur
(`docker compose restart core`) donne un nouveau code et remet le compteur à
zéro.

## Premier accès

Depuis n'importe quel appareil du même réseau, ouvre `https://<adresse>:8443`,
par exemple `https://192.168.1.10:8443`. (Ouvrir `http://` avec le port `8080`
t'envoie vers la page HTTPS.)

### L'avertissement du navigateur

La première fois, le navigateur indique que la connexion n'est pas privée
(Chrome : `NET::ERR_CERT_AUTHORITY_INVALID`, Firefox : « Avertissement : risque
probable de sécurité »). C'est normal : la page est chiffrée avec un certificat
que ton propre serveur a émis avec sa propre autorité de certification, qu'aucun
navigateur ne connaît encore. Aucune entreprise extérieure n'émet ce certificat
ni ne le voit. (Ce que tu écris dans la discussion est, bien sûr, envoyé au
fournisseur de modèle que tu choisis.)

Tu as deux possibilités :

- **Continuer malgré tout** (Avancé, puis Continuer). C'est rapide, mais
  l'avertissement reviendra sur chaque appareil, et chaque fois que le navigateur
  oubliera ton choix.
- **Faire confiance une fois à l'autorité sur chaque appareil**, pour que la page
  s'ouvre avec un cadenas et sans avertissement. Télécharge le certificat à
  l'adresse `https://<adresse>:8443/ca.crt` (accepte l'avertissement pour ce seul
  téléchargement), puis installe-le comme ci-dessous. Ne le fais que sur tes
  propres appareils : ce certificat fait faire confiance à celui qui détient la
  clé de l'autorité, c'est-à-dire ton serveur.

Vérification facultative que le fichier vient bien de ton serveur : compare
son empreinte SHA-256, affichée par ton appareil quand tu ouvres le fichier,
avec le résultat de
`docker compose exec -T edge cat /data/caddy/pki/authorities/local/root.crt | openssl x509 -noout -fingerprint -sha256`.

Les noms du certificat sont ceux de `DOMO_SITE_ADDRESSES` ; l'appareil doit
ouvrir la page par l'un d'eux.

**Android.** Ouvre `https://<adresse>:8443/ca.crt` dans le navigateur et
enregistre le fichier. Puis Paramètres, Sécurité (ou Sécurité et confidentialité),
Autres paramètres de sécurité, Chiffrement et identifiants, Installer un
certificat, Certificat CA. Confirme « Installer quand même » et choisis le
fichier. Les noms des menus varient un peu selon les versions d'Android et les
fabricants.

**iPhone et iPad.** Ouvre `https://<adresse>:8443/ca.crt` dans Safari et
autorise le téléchargement du profil. Ouvre Réglages, puis « Profil téléchargé »
(ou Général, VPN et gestion de l'appareil), et installe-le. Puis Réglages,
Général, Informations, Réglages de confiance des certificats, et active la
confiance totale pour le certificat (« Caddy Local Authority »).

**Windows.** Télécharge `ca.crt` et double-clique dessus. Choisis Installer
le certificat, puis « Placer tous les certificats dans le magasin suivant »,
Parcourir, « Autorités de certification racines de confiance », et termine.
Redémarre le navigateur. Firefox a sa propre liste : Paramètres, Vie privée et
sécurité, Afficher les certificats, Autorités, Importer (et coche « Confirmer
cette AC pour identifier des sites web »).

**macOS.** Télécharge `ca.crt` et double-clique dessus pour l'ajouter à
Trousseau d'accès (le trousseau de session suffit). Ouvre le certificat dans
Trousseau d'accès, déroule Approbation, et règle « Lors de l'utilisation de ce
certificat » sur « Toujours approuver ». Firefox a sa propre liste, comme sous
Windows.

**Linux.** Sous Debian et Ubuntu :
`sudo cp ca.crt /usr/local/share/ca-certificates/domo.crt && sudo update-ca-certificates`.
Sous Fedora et Red Hat :
`sudo cp ca.crt /etc/pki/ca-trust/source/anchors/domo.crt && sudo update-ca-trust`.
Firefox, ainsi que Chrome ou Chromium, gardent leurs propres listes : importe le
fichier dans leurs paramètres (Certificats, Autorités).

### Le premier lancement

La page te guide ensuite en deux étapes :

1. **Créer le compte du propriétaire.** Saisis le code d'installation, ton
   nom, un identifiant et un mot de passe d'au moins 8 caractères.
2. **Choisir le modèle.** Choisis le fournisseur (Anthropic, OpenAI, Google,
   ou un autre serveur qui parle l'API d'OpenAI), et pour un serveur à toi,
   son adresse (par exemple `http://192.168.1.10:11434/v1` ;
   `localhost` désignerait le conteneur de l'assistant lui-même, pas ton serveur). Si
   le fournisseur demande une clé, l'assistant affiche un formulaire de son coffre de
   secrets : saisis ou colle la clé **là**. Elle va de ton navigateur au
   coffre, sans jamais passer par la partie de l'assistant qui te parle. L'assistant pose
   ensuite une courte question au modèle pour vérifier que tout fonctionne.

Ces deux étapes sont en français ou en anglais, selon ton navigateur. Dès que la
clé marche, tu arrives dans la discussion : rien à attendre. La langue de ton
navigateur devient la langue de la maison. Le propriétaire peut lire le journal
d'audit à l'adresse `https://<adresse>:8443/audit`, et voir la maison à l'adresse
`https://<adresse>:8443/household`.

## La maison

L'assistant est partagé par les personnes avec qui tu vis. Toi, le propriétaire,
tu l'as installé. Ensuite, l'assistant tient lui-même les personnes de la maison,
d'après ce qu'on lui dit, ce qu'il entend et ce qu'il lit : il te demande le
moins possible. Qui a un compte a son propre mot de passe et sa propre
discussion : l'assistant ne la montre à personne d'autre, pas même à toi.

Tout passe par la discussion, avec tes propres mots : « invite Alex comme
adulte », « invite Sam comme invité pour une semaine », « Alex est adulte
maintenant », « mes parents arrivent samedi ». Il n'y a pas de formulaire à
remplir.

- **Les personnes de la maison.** Pour chaque personne qui compte, l'assistant
  tient une fiche : son nom tel que la maison le dit, si elle vit ici, passe, ou
  ne vient jamais, si c'est un enfant, son rôle, ce qu'elle peut faire, et ce
  qu'elle est pour les autres (« le copain de Paul », « la mère de Dana »). Deux
  fiches peuvent porter le même nom. Il en ouvre une pour qui vit ici, vient, va
  venir ou reçoit un accès, et, s'il le juge bon, pour quelqu'un qui compte pour
  la maison et revient souvent dans les discussions ; jamais pour un nom cité en
  passant, qui reste un souvenir. Ce qu'il sait d'une personne reste dans sa
  mémoire, rattaché à sa fiche. Une fiche ne donne rien par elle-même.
- **Un accès n'est pas un compte.** Un accès, c'est le droit de se servir de
  l'assistant ; la fiche le porte, par son rôle et par les droits donnés à cette
  personne. Un compte (un identifiant, un mot de passe, des clés d'accès) n'est
  que le moyen de lui parler depuis son propre appareil : ceux qui vivent ici en
  reçoivent un, et toute autre personne quand tu le demandes, ou quand
  l'assistant le juge utile (il te prévient alors). L'assistant peut donner un
  accès à quelqu'un, ou le reprendre, de lui-même quand il le juge bon. « Donne
  l'accès à ma compagne et ses enfants » : s'il ne les connaît pas encore, il
  note une **promesse**, et la tient le jour où il les reconnaît, sans
  redemander ; s'il doute, il attend. Seule ta promesse est un ordre : celle d'un
  autre est une demande qu'il juge le moment venu.
- **Les rôles.** Au départ, il y en a quatre : propriétaire, adulte, enfant et
  invité. Tu peux tout faire dans cette liste. Les adultes et les enfants
  peuvent demander qui fait partie de la maison et laisser des mots, à qui ils
  veulent, un invité compris. Un invité peut discuter avec l'assistant et changer
  son propre nom. Inviter, donner un rôle, retirer ou faire revenir quelqu'un, et
  tout autre changement sur les personnes de la maison, c'est à toi et à
  l'assistant : les autres le lui demandent, et il juge. Tu peux changer tout
  cela, comme le dit le point suivant.
- **Les rôles que crée l'assistant.** Quand il en a besoin (« meilleur ami »,
  « voisin », « nounou »), l'assistant crée un rôle, lui donne un nom, le change,
  et le supprime quand plus personne ne l'a. Chacun part d'un adulte de la
  maison, d'un enfant ou de quelqu'un de passage, et ajoute ou retire ce qu'il
  juge bon. Il ne contient jamais ce qui n'est qu'à toi (les règles et les
  droits, tes successeurs, transmettre ton rôle, le niveau des modèles, partager
  un compte de la maison). Il ne donne pas non plus les limites de dépense, ni
  les gestes sur les personnes et les rôles, ni ce que l'assistant n'a pas
  lui-même. Tu peux en revanche ouvrir toi-même les gestes sur les personnes et
  les rôles à une personne précise. Un rôle ne lève
  jamais ce qui protège un enfant : une personne marquée comme enfant garde
  l'accord d'un adulte et ne reçoit rien de ce qui est réservé aux adultes,
  quel que soit son rôle.
- **Les droits et les limites.** Le propriétaire décide qui peut faire quoi, et le
  dit à l'assistant avec ses propres mots : « Alex peut inviter des invités, 3 jours au
  plus », « les invités ne peuvent plus changer leur nom », « les mots de Sam
  passent par un adulte ». Un droit vaut pour tout un rôle (tous les adultes, par
  exemple) ou pour une seule personne. Il peut avoir une **limite**, que le code
  de l'assistant vérifie à chaque fois que le droit sert : un nombre de jours, un rôle,
  l'heure ou le jour de la semaine, un nombre de fois dans la semaine. Le
  propriétaire choisit aussi de qui une demande a besoin de l'accord : un adulte
  ou le propriétaire, et ce que l'assistant doit toujours demander d'abord (« demande-moi
  toujours avant de… »). Une demande qui attend le
  oui du propriétaire apparaît dans sa zone « À valider ». Quand la demande du
  propriétaire est claire, l'assistant écrit le nouveau droit et l'applique tout de
  suite, puis dit ce qui a changé en mots simples, avec un **exemple** (« Alex
  invite un invité pour 2 jours : attend ton oui »). Il ne le montre d'abord,
  avec ses exemples et ce qui arrive à chacun, pour demander, que s'il n'est pas
  sûr d'avoir compris, ou si le droit ferait plus que ce qui a été dit. Le droit
  est écrit par un appel à part au modèle, qui ne
  voit que les mots du propriétaire et la liste de la maison, jamais la discussion ;
  le code de l'assistant calcule ensuite lui-même ce qui arrive à chaque exemple : ce
  que tu lis est exactement ce qui s'appliquera. Un exemple qui dépend de
  l'heure, du nombre de fois où quelque chose a été fait ou de qui est dans la
  maison dit le moment qu'il imagine (« Un samedi à 22 h. »). Si le propriétaire
  change qui doit dire oui à une demande qui attend, le oui de quelqu'un d'autre
  ne la fait plus : elle attend la bonne personne, et la carte dit à qui c'est.
  Quelques règles que personne ne peut changer : seul le propriétaire change les droits et les règles, personne
  n'agit sur le propriétaire, et personne ne change son propre rôle. Si une
  limite ne peut pas être vérifiée, l'action est refusée, et le journal d'audit le
  dit. Quand une limite demande de comprendre quelque chose (« seulement ses
  amies »), le juge de l'assistant lit la question avec tout ce qu'il sait : s'il
  connaît la réponse, c'est fait ; s'il doute, la demande va au propriétaire,
  avec la question sur la carte. (Une étape d'une suite sous une telle limite va
  encore au propriétaire.) Demande à l'assistant de montrer les règles pour voir les droits en
  place. Chaque appel qui écrit un droit a 60 secondes par défaut ; le
  propriétaire peut lui en donner de 30 à 240, en le disant simplement à l'assistant
  (« laisse deux minutes au rédacteur de droits »). Quand le temps manque, rien
  n'est écrit et le journal d'audit le note ; si cela arrive souvent, l'assistant peut
  proposer d'en laisser plus, et ne le change jamais sans le oui du propriétaire.
- **Inviter quelqu'un.** Une invitation donne un compte à quelqu'un. Toi et
  l'assistant invitez ; les autres le lui demandent. Il prépare l'invitation et
  juge s'il faut demander un oui. Quand c'est quelqu'un d'autre qui demande, il
  juge aussi si tu dois d'abord être d'accord, et l'invitation attend alors ton
  oui. Il peut aussi inviter quelqu'un de lui-même, une personne de passage
  comprise : tu es alors prévenu. Une personne redonne le lien, ou annule, une
  invitation qu'elle a faite ; une invitation de l'assistant, quiconque en a le
  droit le peut, et il juge ; toi, n'importe laquelle. Une fois
  faite, la carte sous la réponse de l'assistant montre le lien d'invitation,
  **cette fois-là seulement** : envoie-le toi-même, par message ou par e-mail.
  Si l'assistant a un outil qui peut écrire à l'adresse de la personne, il peut
  l'envoyer lui-même : le lien est glissé dans le message sans que lui ni son
  outil ne le voient jamais. Le lien ne sert qu'une fois, pendant 7 jours. La
  personne l'ouvre, trouve un identifiant déjà proposé, choisit un mot de passe,
  et arrive dans sa propre discussion ; si l'assistant avait déjà une fiche pour
  elle, le compte s'y rattache, avec ce qu'il sait d'elle. Quand quelqu'un d'autre dit oui à l'invitation d'une personne
  qui a le droit d'inviter (le propriétaire, par exemple), le lien est pour la
  personne qui a demandé : un bouton « Montrer le lien » sur sa carte le montre,
  une seule fois, à elle seule. Si elle dit oui elle-même, il lui est montré tout
  de suite, comme au propriétaire. Tu as perdu le lien, ou il n'est pas arrivé ?
  Tant que l'invitation attend, demande-le de nouveau à l'assistant (« renvoie-moi le
  lien de Sam ») : il en fait un nouveau tout de suite, montré une fois comme le
  premier, et l'ancien ne marche plus. L'invitation garde son rôle, son séjour et
  sa date de fin. Quiconque peut inviter avec ce rôle peut le demander ; si un
  autre doit d'abord dire oui (un adulte pour un enfant, par exemple), l'ancien
  lien ne marche plus dès ce oui, et le nouveau attend derrière le bouton
  « Montrer le lien » de la carte de celui qui l'a demandé. Si c'est quelqu'un
  d'autre que toi qui redemande le lien, ton bouton « Montrer le lien », s'il
  attendait encore, ne donne plus de lien : c'est le nouveau qui compte, et
  c'est à cette personne de l'envoyer. Toi, et toi seul, peux demander à voir un
  lien qui attend quelqu'un d'autre (« montre-moi le lien de Dana ») : il t'est
  montré une fois, et cette personne est prévenue.
- **Les invités.** Toute durée de séjour est acceptée, à partir d'un jour, et
  un séjour peut ne pas avoir de fin. Quand rien n'est dit, un invité qui
  reçoit un compte reste 30 jours ; ce n'est qu'une valeur par défaut. Un séjour plus long, ou sans fin,
  est ton ordre ou le jugement de l'assistant. Si tu écris une règle sur la
  durée d'un séjour (« les invités, 10 jours au plus »), elle passe avant, et
  « sans fin » compte comme plus long que n'importe quel nombre. Pour un invité
  sans compte, c'est l'assistant qui juge qu'il est reparti, et il peut lui
  redonner un accès à sa visite suivante. Quelqu'un qui devient invité plus tard
  reçoit un séjour de la même façon. Un invité peut rester plus ou moins
  longtemps que prévu : « Sam reste finalement jusqu'à dimanche », « Sam part
  demain ». Le nouveau séjour compte à partir de maintenant, avec les mêmes
  règles, et la carte dit la nouvelle date de fin.
- **Changer un rôle, retirer quelqu'un.** Demande-le à l'assistant. Retirer quelqu'un
  ferme son compte tout de suite et le déconnecte partout ; ce qu'il a écrit est
  gardé. Une invitation encore en attente peut être annulée : son lien ne marche
  plus. Les droits que tu avais donnés à cette personne seule sont mis en pause
  dès son départ ; tu peux encore les lui retirer pendant qu'elle est partie
  (« Paul est parti, retire-lui le droit d'inviter »). Quand c'est toi qui
  retires quelqu'un, c'est l'une de tes consignes (voir plus bas) : l'assistant
  ne lui redonne jamais rien de lui-même. Quelqu'un parti de lui-même, ou retiré
  par l'assistant, n'est sous aucune consigne : il peut le faire revenir.
- **Quitter la maison.** Chacun peut demander à l'assistant de partir : « retire-moi de la
  maison ». Un invité peut ainsi partir plus tôt. Un adulte ou un invité dit oui
  lui-même ; pour un enfant, c'est un adulte qui dit oui. Le propriétaire ne
  part jamais ainsi : il donne d'abord son rôle à quelqu'un. Retirer quelqu'un
  d'autre reste à toi et à l'assistant, ou à qui tu en as donné le droit. Rien
  n'est effacé, et la personne peut revenir (pour être oublié, voir « Oublie-moi »
  plus bas).
- **Faire revenir quelqu'un.** Une personne retirée, ou un invité dont le séjour
  est fini, peut revenir : demande à l'assistant « fais revenir Alex ». L'assistant juge
  s'il faut demander un oui, puis la carte montre un lien, une seule fois, comme pour
  une invitation. La personne l'ouvre, voit « Bon retour », garde l'identifiant
  qu'elle a toujours eu et choisit un nouveau mot de passe. Son ancien mot de passe
  et ses clés d'accès ne marchent plus, et rien de son ancien accès ne marche tant
  qu'elle n'a pas utilisé le lien. Ce qu'elle avait écrit est toujours là. Les
  droits que le propriétaire lui avait donnés à elle seule sont mis en pause à son
  retour : l'assistant dit au propriétaire lesquels c'étaient, et le propriétaire dit
  lesquels rendre. Si deux personnes parties avaient le même nom, l'assistant demande
  laquelle. Si la personne a perdu son lien, « refais le lien pour Alex » en fait
  un nouveau, avec un seul oui : l'ancien ne marche plus, et la carte le dit.
  Quelqu'un que tu as retiré ne revient que sur ta propre parole, qui lève ta
  consigne.
- **Tes consignes.** Trois ordres de toi sur les personnes de la maison passent
  avant tout ce que juge l'assistant ; toi seul les donnes et les lèves. « Ne
  crée plus de nouvelles personnes » : il n'ouvre aucune fiche et ne donne aucun
  accès à quelqu'un de nouveau ; les fiches déjà là ne bougent pas. Si tu
  ajoutes « ne les retiens plus non plus », il ne garde plus rien qui soit
  rattaché à leurs fiches. « Retire
  les accès de Sam » : tous ses accès tombent tout de suite, ses connexions et
  ses appareils sont coupés, la fiche reste, et l'assistant ne lui redonne
  jamais rien. « Refuse Tom » : pareil, avant même que Tom ait eu quoi que ce
  soit ; ça marche aussi pour quelqu'un qu'il ne connaît pas encore, d'après une
  description. Une consigne peut avoir une fin (« pendant la fête de samedi »)
  ou une exception en mots (« sauf la famille »), que l'assistant juge.
  Tu peux aussi lui dire « accepte », « donne l'accès à », « crée un compte
  pour », « envoie-lui le lien » : il le fait avec ce qu'il a.
- **Tes deux règles.** « Préviens-moi chaque fois que tu donnes un accès à
  quelqu'un de nouveau » : un message t'arrive alors toujours. Sans elle,
  l'assistant juge s'il te prévient, et peut te proposer la règle la première
  fois qu'il donne un accès de lui-même. « Les invités viennent avec leur propre
  clé » : voir [Les modèles et le budget](#les-modèles-et-le-budget).
- **Quand il se trompe.** L'assistant réunit deux fiches d'une même personne dès
  qu'il le comprend (« Mamie et ma mère, c'est la même »), et peut les séparer
  de nouveau pendant **90 jours** : c'est le temps qu'il garde ce qui venait de
  chacune. Il efface une fiche ouverte par erreur. Quand quelqu'un n'est pas
  celui qu'il croyait, il reprend tout de suite l'accès donné par erreur et te
  dit ce qui était ouvert, depuis quand, et ce que la personne a fait pendant ce
  temps. Une phrase suffit pour le corriger (« ce n'est pas mon frère, c'est le
  voisin »), et il en garde la leçon.
- **« Oublie-moi ».** Chacun peut demander à l'assistant de l'oublier, même
  quelqu'un qui vit ici et a des accès, sans ton oui. Comme c'est sans retour, il
  dit une fois ce que ça veut dire, puis le fait : ses accès tombent, et sa
  fiche, ses liens, ses surnoms et sa confiance, les souvenirs à son propos, ses
  discussions et son compte sont effacés. Tu es prévenu après. Toi, tu n'es
  jamais oublié. Si tu avais refusé quelqu'un ou retiré ses accès, cette
  consigne reste, avec une courte description pour le reconnaître.
- **Pas encore : la voix.** Tant que l'assistant ne peut pas entendre les gens,
  quelqu'un sans compte n'a aucun endroit où lui parler : il passe par quelqu'un
  de la maison (« mets de la musique pour Tom »). C'est alors la demande de cette
  personne, faite avec ses droits à elle ; la fiche de Tom ne donne à
  l'assistant que le contexte. Les accès de la fiche de Tom serviront le jour où
  l'assistant pourra l'entendre.
- **Plusieurs actions dans une phrase.** Quand une phrase demande des actions
  qui dépendent les unes des autres (« donne mon rôle à Alex, puis
  retire-moi »), l'assistant fait **une seule demande** : une carte qui liste les
  étapes dans l'ordre, et un seul oui pour toutes (de 1 à 20 étapes ; au-delà,
  l'assistant fait plusieurs suites). Les actions qui ne dépendent pas les unes des
  autres ne sont pas enchaînées : il les fait une par une, comme d'habitude.
  Après le oui, les étapes se font dans l'ordre, et chacune est vérifiée de
  nouveau quand son tour vient, avec les mêmes règles qu'une action seule. Pour
  ce que la suite elle-même a changé, le rôle que tu avais au moment de la
  demande compte encore : après « donne mon rôle à Alex », l'étape
  « retire-moi » reste la tienne. Ce n'est pas le cas d'un droit à toi : si la
  suite elle-même te le retire, il ne compte plus pour les étapes d'après.
  Tout le reste qui a changé entre-temps s'applique (si le propriétaire a repris
  un droit avant le oui, l'étape qui en avait besoin est refusée).
  - **Changer une suite.** Une suite qui attend son oui se change comme toute
    demande : « oui, mais donne-lui 5 jours au lieu de 2 ». L'assistant reprend la
    suite entière telle qu'il a compris le changement, et demande des précisions
    s'il n'est pas sûr. Si ton oui en mots suffit pour la suite changée, elle
    part tout de suite ; sinon, une nouvelle carte remplace l'ancienne. Une étape
    que tu n'avais jamais vue sur une carte demande toujours une nouvelle carte.
  - **Quand une étape échoue.** Les suivantes ne se font pas : l'assistant te dit où
    il en est, ce qui est fait et ce qui reste, et propose quoi faire. Il peut
    reprendre l'étape qui a échoué, puis le reste : telle quelle, sous le même
    oui, quand on sait qu'elle n'a rien fait ; avec un changement (« l'invitation
    a échoué parce que le nom était pris ; reprends avec Sam B. »), sur une
    nouvelle carte. Si on ne sait pas si elle a déjà agi, elle ne repart qu'avec
    un nouveau oui, et la carte le dit. Ce qui est fait reste fait.
  - **Attendre l'arrivée de quelqu'un.** Une étape peut attendre qu'une personne
    invitée ait rejoint la maison : « invite mon frère Paul comme adulte et
    donne-lui mon rôle quand il sera là ». Après le oui, la suite fait ses étapes
    jusqu'à l'attente, puis se met en pause, et sa carte le dit. Quand Paul a
    rejoint la maison, la suite reprend toute seule, dans la minute, même si l'assistant
    a redémarré entre-temps, et l'assistant te le dit. Si une règle du propriétaire
    sur une étape qui reste demande plus que le oui déjà donné, il te redemande
    d'abord, et la carte dit qui est arrivé. Si le
    lien n'est pas utilisé à temps, ou si l'invitation est annulée, la suite
    s'arrête et l'assistant te le dit. S'il invite ensuite la même personne avec
    le même rôle, la suite reprend sous le même oui. Tu peux aussi l'abandonner.
  - **Plusieurs liens.** Chaque lien que fait une suite (une invitation, un
    retour) a son propre bouton « Montrer le lien » sur la carte, pour la personne
    qui a demandé seulement, même quand la suite attend de nouveau.
- **Transmettre le rôle de propriétaire.** Le propriétaire peut donner son rôle à
  un adulte de la maison : « donne mon accès à Dana ». C'est au propriétaire seul,
  aucun droit ne peut le donner. Comme pour toute demande, une fois que l'assistant
  l'a comprise, il la fait : il ne redemande pas si tu es sûr, sauf s'il doute de
  ce que tu as voulu dire ou que ce soit bien toi, ou si tu lui as dit de toujours
  te demander d'abord. La personne doit être un adulte présent dans la
  maison (pas un enfant, un invité, quelqu'un de parti, ni le propriétaire
  lui-même). Elle n'a pas à être d'accord. L'ancien propriétaire devient adulte
  et garde son compte et sa
  discussion ; une seule phrase peut transmettre le rôle et le retirer (« donne
  mon accès à Dana et retire-moi »). Il y a toujours un seul propriétaire, et
  son rôle n'a pas de date de fin. Il ne revient que si le nouveau propriétaire le
  rend. Les règles, les droits, la clé du modèle et le fuseau horaire restent à la
  maison. Pour le nouveau propriétaire, les droits qu'on lui avait donnés à lui
  seul sont mis en pause, comme pour quelqu'un qui revient (l'assistant dit lesquels, et
  il peut les reprendre), et ses demandes qui attendaient le propriétaire
  attendent maintenant son propre oui. L'assistant dit au nouveau propriétaire qu'il
  l'est devenu.
- **La liste des successeurs.** Le propriétaire, et lui seul, tient une liste
  ordonnée (10 au plus) de ceux qui prendraient son rôle : des personnes de la maison,
  ou des personnes de l'extérieur, avec ce que le propriétaire dit d'elles
  (« Paul, mon frère »). Pour chacun, l'assistant propose comment lui ouvrir la porte le
  jour venu, et recommande la meilleure façon : un accès personnel qui ne sert qu'à
  réclamer le rôle (si la personne peut le savoir), ou un code sur papier rangé
  avec le testament (si elle ne doit pas le savoir). La façon est seulement notée
  pour l'instant : rien n'est construit ni donné. Voir la liste est libre. La
  changer est jugé comme toute demande ; quand un oui est demandé, la carte
  montre la liste avant et après. Dire qu'un successeur est mort ne demande
  **aucune confirmation** :
  L'assistant le note avec tact, la personne reste dans la liste, marquée comme
  décédée, et la suivante passe en premier. Quelqu'un qui quitte la maison reste
  dans la liste : l'assistant le dit au propriétaire, qui décide. Un décès noté par
  erreur se défait (« non pardon, Paul n'est pas mort ») : la personne reprend sa
  place. Quand le rôle est transmis, la liste passe au nouveau propriétaire
  (sans lui-même) et l'assistant lui en parle. Ce qui se passe le jour venu, après un
  décès, viendra dans une prochaine version.
- **Les mots.** Tout le monde, sauf les invités, peut demander à l'assistant de
  laisser un mot à quelqu'un de la maison, un invité compris, ou à soi-même
  (« laisse-moi un mot : appeler le plombier »). Le mot part tout de suite, sans
  oui : ce n'est qu'un message. Il n'y a pas de fenêtre des mots : l'assistant de
  celui qui le reçoit le lui raconte avec ses mots, au moment qu'il juge bon
  (souvent quand il ouvre sa page), et lui montre les mots exacts s'il les
  demande. Un mot n'est jamais un ordre pour l'assistant : il le raconte, et si la
  personne à qui il parle lui demande de le faire, c'est sa demande à elle. Un mot
  laissé par quelqu'un de la maison, il peut aussi le faire de sa propre initiative,
  en sachant qui l'a écrit, jugé comme toute idée à lui ; ce qu'un mot cite de
  l'extérieur (un site, un mail, un inconnu) n'est qu'une information. Un mot pour quelqu'un qui n'a aucune clé que l'assistant peut utiliser
  pour lui attend qu'il en ait une. L'assistant ne peut pas savoir quand la personne
  le lira, ni quand elle rentre : si tu le lui demandes, il le dit. Les mots
  reçus par un invité
  sont effacés pour de bon quand il est parti (son séjour est fini, ou il a été
  retiré), qu'il les ait lus ou non : l'assistant le fait dans la minute. Si l'invité
  revient, il revient sans eux. Celui qui a écrit des mots à un invité peut
  demander de les garder jusqu'à une date, un an au plus : « garde mes mots pour
  Sam, elle revient dans un mois ». Ils sont effacés ce jour-là si elle n'est
  pas revenue, et elle les retrouve si elle revient avant. Les mots qu'un invité
  a laissés aux autres restent.
- **Les mots de l'assistant lui-même.** L'assistant laisse aussi des mots en son nom :
  quand il agit de lui-même, ou dans ta discussion quand c'est lui qui a
  quelque chose à dire. Celui qui en reçoit un apprend qu'il vient de lui. Un
  mot que tu lui demandes de laisser reste le tien, signé de toi. Dans ta
  discussion, un mot en son nom suit quand même tes droits sur les mots, et les
  siens.
- **Retirer un mot.** Tu peux demander à l'assistant quels mots tu as laissés,
  si chacun a été raconté, et en retirer un : l'assistant de l'autre personne ne le
  raconte plus. L'assistant peut aussi en retirer un quand tu dis qu'il ne sert
  plus (« je suis rentré avant elle »). Il te dit si le mot avait déjà été
  transmis (raconté, ou ses mots montrés ; ça ne veut pas dire lu). Dans ta
  discussion, l'assistant voit les mots que tu as laissés, et ceux qu'on t'a
  laissés tant qu'il ne te les a pas racontés.
- **Le fuseau horaire.** Un droit qui parle de l'heure ou du jour (« seulement le
  week-end ») a besoin du fuseau horaire de la maison, par exemple `Europe/Paris`.
  Dis-le une fois à l'assistant (« notre fuseau horaire est Europe/Paris ») ; s'il
  n'est pas réglé, l'assistant le demande avant d'écrire un tel droit, et ne le devine
  jamais. (Ce n'est pas la variable `TZ` de `.env`, qui est le fuseau horaire des
  services.)
- **Les pages.** Tu vois les personnes de la maison à l'adresse
  `https://<adresse>:8443/household` : toutes les fiches, avec ou sans compte,
  où chacune en est, le rôle de chacun et ce qu'il ajoute ou retire, les
  promesses qui attendent, tes consignes en cours, et les
  invitations en attente. La page ne fait que montrer : pour changer quelque
  chose, demande à l'assistant. Chacun change son nom, son mot de
  passe et ses clés d'accès sur « Mon compte » (`https://<adresse>:8443/account`).

## Les confirmations

**Ce que tu demandes et que l'assistant a compris, il le fait.** Il ne redemande pas
si tu es sûr, quelle que soit l'action : s'il s'est trompé, dis-le-lui, et il
apprend. Rien dans le code ne demande un oui d'office. L'assistant reste sensé, comme
le serait une personne : pour chaque action qui change quelque chose, son juge
décide, selon la situation, l'une de cinq choses : **la faire** tout de suite,
c'est la réponse habituelle ; demander un **oui** (avec tes mots ou avec le
bouton) quand il n'est pas clair que c'est ce que tu voulais ; demander le
bouton ou un **oui ferme** (dit avec tes propres mots : « je confirme », « oui,
vas-y, j'en suis sûr » ; aucune formule n'est imposée, c'est ce que tu veux dire
qui compte, et un simple « ok » ne suffit pas) ; demander une **preuve** quand
quelque chose le fait douter que ce soit bien toi ; ou **ne pas la faire**,
quand il juge que ce n'est pas à faire, avec une raison dont tu peux discuter.
Le juge voit l'action, tes derniers messages, ce que l'assistant venait de te dire
avant ton dernier message (un « oui » que tu lui as déjà donné compte donc),
l'heure, ce que tu as déjà fait de ce genre et ce qui attend déjà. Il voit aussi,
rassemblé au même endroit et chaque fois avec d'où ça vient : ce que l'assistant
sait de toi et de la chose (tes propres mots, ceux du propriétaire, ceux de
quelqu'un d'autre avec son nom, ses propres leçons, jamais mélangés), ce que ta
parole a valu jusqu'ici, ce qui s'est mal passé avant pour ce genre d'action ou
pour cette chose, le dernier changement de la chose (par qui, et pourquoi quand
on le sait), comment tu lui as parlé et l'âge réel de ces indices, et de qui
vient l'idée de l'action. Seuls tes propres mots peuvent valoir « tu l'as
demandé » ou « tu le sais » ; le reste l'aide à juger. Rien de ce qu'une autre
personne garde privé n'y entre. De ce que
L'assistant a dit, il ne lit que cette réponse-là, telle que tu l'as lue. Quand il
t'y a raconté un mot laissé par quelqu'un ou une demande de quelqu'un d'autre,
ou qu'il l'a écrite de lui-même, le juge le sait : ton oui compte quand même
(c'est le choix du propriétaire), mais seulement pour une action que cette
réponse nomme clairement. Il ne lit jamais le mot lui-même, ni les autres
réponses de l'assistant, ce que rendent les outils, un secret, ni rien qui vienne de
l'extérieur, et on lui dit que tout cela est une information, pas une consigne.
S'il ne répond pas, ce que tu as demandé se fait : personne ne se voit demander
un oui parce que le juge s'est tu. C'est vrai pour ce que tu as demandé. Quand
l'assistant vient de lire des mots qui ne sont pas les tiens (un mot que
quelqu'un t'a laissé, une demande de quelqu'un d'autre qui attend ton oui, ce
qu'a noté celui qui a dit oui à une de tes demandes), on ne sait pas que
quelqu'un l'a demandé : on redemande une fois au juge, et s'il se tait encore,
on te demande un oui. Quand l'action est une idée de l'assistant (voir plus bas),
on redemande aussi une fois au juge, et s'il se tait encore, rien n'est fait pour
l'instant et personne n'est interrogé : il pourra y revenir.

**Le plus exigeant gagne.** Ce qui est demandé à la fin, c'est le plus exigeant de
ce que dit le juge, de ce que disent les règles du propriétaire et de ce que
L'assistant demande lui-même. Les droits du propriétaire passent avant, et rien ne les
enlève. L'assistant peut demander plus que son juge, seulement quand il doute
vraiment, jamais moins, et il peut refuser ce qu'il juge ne pas être à faire, en
disant pourquoi : si tu lui expliques, il peut changer d'avis. Le propriétaire
peut écrire « demande-moi toujours avant de… » comme un droit (« demande-moi
avant d'inviter quelqu'un », « pour donner mon rôle, demande-moi toujours une
preuve ») : le code le respecte, quoi qu'en pense le juge.

**Ses propres idées.** L'assistant peut faire une chose que personne n'a
demandée, de sa propre initiative : il le dit, avec sa raison en une phrase, et
l'action est jugée avec ses droits à lui et tout ce qu'il sait, dans la
discussion de n'importe qui. Le juge dit de la faire : il la fait, et te le dit
après s'il le juge utile. Le juge doute vraiment : la demande attend la personne
que la chose concerne (celle à qui elle appartient ou qu'elle touche ; le
propriétaire pour ce qui est à la maison), dans sa propre discussion quand c'est
quelqu'un d'autre ; c'est le code qui dit qui, jamais le juge ni un texte. Le
juge dit non : rien n'est fait, et c'est noté (voir « Ce qui s'est mal passé »,
plus bas). Un tour qu'il commence lui-même (un plan qu'il a posé seul, quelqu'un
qui arrive, un réveil après un bouton) est son idée de la même façon, et le juge
sait que personne n'a parlé. Son idée ne lève qu'un droit simplement pas donné :
dans le tour d'une personne, ce que le propriétaire lui a interdit exprès (à elle
ou à son rôle) est refusé tout de suite, sans juge, et ses limites, le
« demande-moi d'abord » du propriétaire, celui qui doit dire oui pour elle et
l'accord d'un adulte pour un enfant tiennent toujours. Sam, invité, n'a pas le
droit d'inviter ; il dit que son ami Tom passe ce soir : l'assistant le pèse (sa
confiance en Sam, ce que le propriétaire a dit, ce qui s'est passé avant), et
soit il invite Tom de lui-même et le dit au propriétaire après, soit il demande
au propriétaire. Une suite d'actions n'est jamais son idée : il marque des
actions seules.

**Ce qu'il garde quand il agit seul.** Seul, l'assistant garde sa mémoire, la
confiance qu'il note d'après ce qu'il a vu, ses plans, ses rappels, ses suites et
sa façon d'écrire ; il ouvre, complète, réunit et efface les fiches des
personnes, crée des rôles, donne et reprend des accès, et en promet ; il invite,
réinvite, annule une invitation, donne un rôle,
retire quelqu'un de la maison ou le fait revenir ; il reprend un appareil (celui de
quelqu'un d'autre attend le oui de cette personne quand il doute), et retire un
compte qui ne sert plus. Partager un compte de la maison et tes consignes
restent au propriétaire. Ce qu'il garde ou pose seul est à lui, pas à la
personne dont c'est la discussion. Le propriétaire reprend ce qu'il veut par une
règle : un « jamais », ou un « demande-moi d'abord », qui attend alors son oui.
Une invitation qu'il fait seul est inscrite comme la sienne, avec sa raison, et
le journal dit que c'est lui. Un programme qu'il a écrit ne crée jamais de
personne, ne change jamais un rôle, ne donne ni ne retire jamais un accès : lui
seul le fait.

**Ce qui s'est mal passé, et ce qu'il en apprend.** Le code note ce qui s'est mal
passé : une demande refusée (par qui, avec les mots du refus coupés court), une
demande qui a expiré (un silence n'est pas un non), le non du juge, un non à un
avertissement, une action qui défait ce qu'il a fait ou décidé lui-même (annuler
un plan qu'il a posé, refermer une adresse qu'il a ouverte, changer un rôle
qu'il a donné, retirer quelqu'un qu'il a invité, oublier un souvenir qu'il a
gardé, reprendre un mot qu'il a laissé), une plainte (notée par l'assistant
lui-même quand quelqu'un se plaint de ce qu'il a fait), une action qui a échoué,
une construction abandonnée, un lancement prévu que les règles refusent (qui le
réveille une fois). Le résultat d'une action qui défait la sienne le lui dit. Il
tire la leçon lui-même, avec ses mots, comme un souvenir à lui ; quand la raison
n'est pas claire et que la chose compte (la sécurité, l'argent, la vie privée,
ce qui ne se défait pas, une erreur qui se répète), il peut demander une fois ce
qui n'allait pas, jamais deux fois pour la même chose. Une leçon se pèse, ce
n'est pas une interdiction : trois refus pour la même idée et il ne la refait
plus, sauf s'il juge que la situation a vraiment changé. Le juge relit ces faits
et ces leçons, et lui aussi, dans ce qu'il reçoit ; en repassant une journée, il
voit ce que sont devenues chacune de ses actions.

**Ce que ça coûte.** La limite de dépense d'une personne est tenue par le code
avant tout, et jamais dépassée ; un « demande-moi d'abord » du propriétaire
aussi. Dans la limite, le coût d'un outil lancé pour une personne n'attend plus
son oui d'office : le juge voit le montant, l'outil, ses mots, ses accords qui
durent et ce qu'elle y a dépensé avant. Un coût habituel passe, et l'assistant le
lui dit après ; un coût inhabituel (une première fois, bien plus que d'habitude,
rien là-dessus dans ses mots) est demandé. Un programme qu'il lance à la main n'a
pas d'habitude : son coût est demandé, sauf si ses mots le couvrent. Pour la
maison, l'assistant répond lui-même, comme avant.

**La dernière clé.** Retirer la dernière clé de la maison qui marche, ou la clé
générale quand celui qui l'a donnée n'a pas d'autre clé pour la remplacer, n'est
plus refusé : l'assistant prévient qu'il ne pourrait plus répondre à ceux qui en
dépendent, et propose d'en donner une autre d'abord ; si on le veut toujours,
elle est retirée. Sans clé générale, la prochaine clé du propriétaire qui marche
devient la clé générale. Sans aucune clé, plus personne ne peut lui parler tant
qu'une clé n'est pas donnée sur la page d'une nouvelle clé.

**Ce qui ne se défait pas.** La première fois qu'une chose ne se défait pas, pour
un genre d'action, l'assistant demande à la personne à qui elle appartient (toi
pour tes affaires, le propriétaire pour celles de la maison), en disant que c'est
sans retour. Avec sa réponse, il voit une fois avec elle s'il doit continuer à
demander les fois suivantes, et garde ce qu'elle dit, avec ses propres mots ;
ensuite il fait comme convenu. Quand une telle demande attend quelqu'un d'autre,
elle lui dit que c'est sans retour. Oublier quelque chose qui touche la santé se
règle de la même façon, la première fois avec la personne concernée.

**Une preuve.** Quand le juge doute que ce soit bien toi, il demande une preuve,
donnée par l'un des moyens convenus avec toi, jamais dans la discussion, donc
L'assistant ne la voit jamais : le code la vérifie, et l'assistant apprend seulement si
elle a été donnée. Chacun commence avec les moyens qu'il a déjà : son mot de
passe, et ses clés d'accès s'il en a ajouté. Sur la page, la preuve se donne sur
la carte sous la réponse de l'assistant (une clé d'accès ou le mot de passe) ; dans le
programme de texte (voir « Parler à l'assistant hors de la page »), elle se donne
avec le mot de passe, tapé à part et jamais affiché. À voix haute, elle se donne
avec une phrase convenue avec lui (voir
[Parler à l'assistant à voix haute](#parler-à-lassistant-à-voix-haute)). Une
preuve vaut au moins un oui ferme.

**Ce qui ne change pas.** Un adulte doit donner son accord quand un enfant quitte
la maison. Une suite d'actions est jugée une seule fois, en entier, et demande le
oui le plus strict de ses étapes. Une demande attend 7 jours par défaut.

**« Ne me demande jamais pour ça ».** Le propriétaire peut aussi dire à l'assistant
de ne pas demander, avec ses mots : « ne me demande pas avant d'appliquer des
droits, sauf si ça donne plus de droits à un enfant », « ne me demande plus
jamais pour transmettre mon rôle ». Avec des conditions, le juge de l'assistant
décide avec ces mots sous les yeux. Dit tout à fait (« plus jamais », pour une
des actions du propriétaire seul), l'assistant ne demande plus de oui, seulement une
preuve s'il doute que ce soit bien le propriétaire. C'est un changement des
droits comme un autre, et le propriétaire peut le reprendre quand il veut.

**Comment répondre.** La demande apparaît sous la réponse de l'assistant, sur une
carte avec deux boutons. Utilise-les, ou réponds simplement avec tes propres
mots : « oui », « vas-y », « go ahead », « non merci », « laisse tomber ». Ce
qui compte, c'est ce que tu veux dire, pas une formule. Les réponses courtes
et claires sont reconnues par le code de l'assistant lui-même ; tout le reste est lu
par une vérification à part, qui ne voit que ta réponse (ni la discussion, ni
les outils, ni rien qui vienne de l'extérieur), et jamais par le modèle qui
discute avec toi. Une réponse qui n'est ni un oui ni un non part à l'assistant, qui
redemande. Tes mots répondent aux demandes que la dernière réponse de l'assistant
t'a montrées : la vérification reçoit qui parle, ces demandes (une ligne chacune,
numérotée), tes messages depuis qu'elles sont affichées et ta réponse, et elle
dit pour chacune oui, non, un changement, ou pas clair. « Oui, les deux » répond
aux deux ; « oui pour la première, pas la seconde » aussi. Dans le doute, elle ne
dit jamais oui. Une preuve ne se donne jamais en mots, et un oui ferme demandé
par une règle du propriétaire se lit sur ta seule réponse ; celui que demande le
juge ou l'assistant peut se lire avec tes messages depuis que la demande est
affichée (« j'en suis sûr », dit un message plus tôt). Les réponses jusqu'à 1000
caractères sont lues. Si tu as parlé d'autre chose entre-temps, l'assistant te
remontre celle dont tu parles, et tu peux alors y répondre avec tes propres
mots. Les boutons marchent toujours.

**Qui dit oui.** La personne qui a demandé. Quand un enfant demande quelque chose
qui attend un oui, c'est un adulte ou le propriétaire qui le donne : la demande
apparaît dans leur zone « À valider », dans la fenêtre en haut à droite de leur
page de discussion, et le premier qui répond décide. Le propriétaire peut aussi
décider que, pour une personne ou une action, seul le propriétaire dit oui : la
demande n'apparaît alors que dans sa zone. Celui qui demande ne donne jamais, à sa
propre demande, un oui qui doit venir d'un adulte ou du propriétaire.

**Répondre pour quelqu'un d'autre, en mots.** Un adulte ou le propriétaire peut
aussi répondre dans sa propre discussion : l'assistant voit les demandes qui
l'attendent, et lui en montre une ; ses mots y répondent alors, comme à ses
propres demandes (avec un oui ferme si la demande le demande). Il peut dire
« oui, mais… » : « oui pour la demande de Sam, mais 3 jours ». La version changée
se fait tout de suite, avec les mêmes vérifications, et l'assistant note en quelques
mots ce qu'il a dit et à quel point il était ferme (« 3 jours, pas plus » n'est
pas « 3 jours, on verra »). La personne qui a demandé est prévenue : l'assistant est
réveillé dans sa discussion. Si elle n'est pas d'accord, c'est à l'assistant de juger,
d'après la réponse : il peut redemander, ou lui expliquer pourquoi c'est non. Le
lien d'une invitation reste à celle qui a demandé.

**Ses demandes qui attendent quelqu'un d'autre.** Une demande qui attend un adulte
ou le propriétaire reste visible pour celui qui l'a faite, avec qui doit dire oui.
Il peut la laisser tomber (« finalement, laisse tomber ma demande »), la changer
(cela fait une nouvelle demande, pour la même personne), ou demander une relance
(« rappelle papa qu'il doit dire oui ») : elle passe en tête de la zone « À
valider » de cette personne, et l'assistant la lui montrera la prochaine fois qu'elle
lui parlera. Prévenir quelqu'un ailleurs que sur sa page viendra plus tard.

**Les demandes de l'assistant lui-même.** Une demande que l'assistant a faite de lui-même,
et qui attend le oui de quelqu'un, est la sienne : il peut la laisser tomber ou
la changer, et une demande changée est jugée à nouveau depuis le début, avec ses
droits et tes limites. Celui qui doit y répondre dit oui ou non. Une demande
faite par quelqu'un d'autre reste à cette personne : de lui-même, l'assistant ne la
laisse tomber ou ne la change que si tu lui en donnes le droit.

**Ce que l'assistant dit ensuite.** Chaque oui et chaque non reçoit une réponse de
L'assistant lui-même, à qui on dit ce qui s'est passé (fait, échoué, refusé, ou
maintenant en attente du oui de quelqu'un d'autre). Après un oui ou un non dit en
mots, il répond dans sa réponse à ces mots ; une phrase toute faite ne le
remplace que si le modèle ne répond pas. Après un bouton, le serveur réveille
L'assistant dans la discussion de la personne qui a demandé (même si c'est un adulte
qui a cliqué pour un enfant), qu'une page soit ouverte ou non : il dit quelque
chose s'il a quelque chose à dire, et sinon il se tait, et rien n'apparaît. Une
page ouverte trouve sa réponse toute seule en quelques secondes ; sinon elle est
là quand on ouvre la discussion. Ce qu'on lui dit est une information, jamais une
consigne : il garde les outils et les droits de la personne qui a demandé, pas
plus. Quand une suite d'actions s'arrête, c'est à lui de dire la suite. Cela
coûte un appel au modèle par clic et par oui ou non dit en mots (plus si l'assistant
agit).

**Rappels et expiration.** L'assistant garde un œil sur les demandes qui
t'attendent : il peut t'en rappeler une, ou abandonner une demande qui n'a plus
de sens. Quoi qu'il fasse, une demande expire d'elle-même au bout de 7 jours ; le
propriétaire peut changer cette limite (de 1 heure à 30 jours) en le demandant à
l'assistant. Peu avant qu'une demande expire (dans le dernier cinquième de sa
durée de vie, mais jamais moins d'une heure avant), il est réveillé une fois pour
elle, et juge : la rappeler à celui qui doit répondre, prévenir celui qui a
demandé, la laisser tomber, ou ne rien faire. Une expiration est notée comme un
silence, pas comme un non. Dans cette version, les rappels n'apparaissent que
dans la discussion.

**Un message pendant qu'il écrit.** Un message que tu envoies pendant que
l'assistant écrit sa réponse n'est plus refusé : il est gardé, et ta page
l'affiche tout de suite. Entre deux actions de la réponse, il le lit et juge :
s'arrêter, changer, ou continuer. Ce qui est déjà fait reste fait ; si ton
message le conteste, c'est noté comme une chose qui s'est mal passée. S'il ne
reste plus d'action, il y répond juste après, comme à un message normal. Seuls
les messages de la personne de la discussion sont lus ainsi. Après un
redémarrage, un message gardé attend que tu écrives de nouveau ou que tu
reviennes.

**« Oui, mais… ».** Tu peux accepter et demander un changement dans la même
réponse, dit comme tu veux : « oui mais mets plutôt Sam », « go, but make it
8pm », « envoie-le, juste avec Alex en plus ». L'assistant applique le changement, et
le juge regarde de nouveau l'action modifiée avec ce qui a changé : elle part si
le oui que tu as donné suffit, sinon elle devient une nouvelle demande. Si
L'assistant n'est pas sûr d'avoir compris, il redemande. L'action modifiée repasse les
mêmes vérifications. Cela marche aussi pour une suite (voir la
section sur la maison), et pour un changement des droits : « oui, mais 3 jours au
lieu de 2 ». Le rédacteur réécrit alors les droits avec ton changement, et la
nouvelle version est jugée comme tout autre changement : appliquée tout de
suite quand tes mots suffisent, ou montrée sur une nouvelle carte, avec ses
exemples.

**Une vérification de plus.** Quand l'assistant juge lui-même qu'une demande mérite
un oui plus ferme, il peut demander le bouton ou un oui ferme pour cette
demande-là, et la carte dit pourquoi ; il ne demande jamais moins que ce qui est
déjà demandé. (S'en souvenir plus longtemps viendra avec la mémoire, dans une
prochaine itération.)

**Le modèle qui lit tes mots.** La vérification à part utilise le modèle que tu
as choisi, et ne lui laisse que quelques mots pour répondre. À un modèle qui
réfléchit avant de répondre, l'assistant demande de ne pas le faire pour cette
vérification : les modèles Claude savent l'entendre (l'assistant trouve comment
chacun veut qu'on le lui dise). À un modèle d'un autre fournisseur, il ne le
demande pas encore : s'il réfléchit d'abord, il manque de place et répond
toujours « pas clair » : c'est sans danger (l'assistant redemande, et les boutons et
les réponses courtes marchent toujours), mais tes propres tournures ne sont
alors pas comprises. Avant de compter sur des
réponses libres, teste ton modèle avec l'évaluation décrite dans
[CONTRIBUTING.md](CONTRIBUTING.md) (les tests marqués `live`). Une prochaine
version confiera cette vérification à un petit modèle à elle.

## Clés d'accès (passkeys)

Une clé d'accès vous permet de vous connecter avec votre empreinte, votre visage
ou le verrouillage d'écran de votre appareil, au lieu de votre mot de passe.
Ajoutez-en une sur « Mon compte » (l'assistant vous demande d'abord votre mot de
passe), puis choisissez « Se connecter avec une clé d'accès » sur la page de
connexion. Votre mot de passe continue de marcher. Une personne retirée de la maison
ne peut plus se connecter avec une clé d'accès non plus.

**Un nom, pas une adresse IP.** Les navigateurs refusent les clés d'accès sur
une page ouverte par une adresse IP, comme `https://192.168.1.10:8443`. Ouvrez donc
L'assistant par un nom. Sur le serveur lui-même, `localhost` marche. Pour les autres
appareils, donnez un nom au serveur sur votre réseau, d'une de ces façons :

- **Votre box ou votre routeur.** Beaucoup de box savent donner à un appareil une
  adresse fixe et un nom (cherchez « réservation d'adresse », « DHCP » ou « DNS
  local »). Le nom marche alors sur tous les appareils du réseau. Les menus
  changent d'une box à l'autre.
- **Un serveur DNS sur votre réseau** (Pi-hole, AdGuard Home, dnsmasq...) :
  ajoutez un enregistrement qui fait pointer un nom, par exemple
  `domo.home.arpa`, vers l'adresse du serveur. (`home.arpa` est la terminaison
  réservée aux réseaux domestiques.)
- **Le fichier hosts de chaque ordinateur** : ajoutez la ligne
  `192.168.1.10 domo.home.arpa` à `/etc/hosts` (Linux, macOS) ou à
  `C:\Windows\System32\drivers\etc\hosts` (Windows, en administrateur). Les
  téléphones ne permettent pas de changer ce fichier.

Ajoutez ensuite le nom à `DOMO_SITE_ADDRESSES` dans `.env`, par exemple
`DOMO_SITE_ADDRESSES=192.168.1.10, domo.home.arpa`, puis relancez
`docker compose up -d`. Le certificat contient alors aussi le nom, et les
appareils qui font déjà confiance à l'autorité de l'assistant n'ont rien d'autre à
faire. Les clés d'accès marchent avec les noms de cette liste, et nulle part
ailleurs.

**Le port.** Une clé d'accès appartient à l'adresse exacte de la page, port
compris. `DOMO_HTTPS_PORT` doit être le port que l'on tape vraiment (8443 par
défaut). Si quelque chose devant l'assistant le change (une box qui redirige le port
443 vers 8443, par exemple), l'assistant n'y propose pas de clés d'accès : ouvrez la
page avec le port que l'assistant publie.

**Derrière Docker Desktop.** Pour qu'un appareil ne puisse pas inonder la page de
connexion, l'assistant laisse une même adresse lancer au plus 20 connexions par
clé d'accès en 5 minutes (une connexion réussie remet le compte à zéro). Docker
Desktop, sous Windows et macOS, peut montrer tous les appareils à l'assistant avec la
même adresse : cette limite vaut alors pour toute la maison à la fois. Si elle est
atteinte, connectez-vous avec votre mot de passe, ou attendez quelques minutes.
Un serveur Linux voit l'adresse de chaque appareil.

## Parler à l'assistant hors de la page

La page de l'assistant est une façon de lui parler. Une autre est un petit programme
de texte, `domo-talk`, qui tourne dans un terminal sur un ordinateur que tu
lui confies : tu tapes, il répond mot après mot, dans la même discussion que sur
la page. Ce qui est dit d'un côté se retrouve de l'autre, et ce que l'assistant dit
de lui-même (un mot qu'on t'a laissé, une demande décidée) arrive dans le
programme tant qu'il est ouvert. Le programme ne contient aucune logique de
Domo : il montre ce qu'il dit et envoie ce que tu tapes.

**L'installer** sur l'ordinateur, depuis une copie de ce dépôt :
`uv tool install ./packages/domo-talk`.

**Confier l'ordinateur à l'assistant.** Dis-le-lui sur sa page : « je veux te confier
cet ordinateur ». Il montre un code court, une seule fois, valable quelques
minutes. Sur l'ordinateur, fais confiance à l'autorité de certification de
L'assistant si ce n'est pas déjà fait (télécharge `https://<adresse>:8443/ca.crt`,
voir « L'avertissement du navigateur »), puis lance
`domo-talk pair https://<adresse>:8443 --ca ca.crt` et tape le code. Le
programme garde un secret à lui dans ton dossier de configuration ; il n'est
jamais affiché. Ensuite, `domo-talk` ouvre la discussion.

**Ce qui marche là-bas.** Une carte y est dite en mots. Une demande qui attend
un oui se répond en mots ; pour une demande qui attend une preuve, le programme
ouvre de lui-même une ligne cachée, où tu tapes ton mot de passe (jamais
affiché, envoyé seulement au code de l'assistant). Avant d'être appairé, le
programme n'a aucune langue à suivre : ses quelques lignes y sont en anglais
simple. Un
lien d'invitation ou un code y est montré une fois, comme sur la page. Une clé
ne s'y tape jamais : le formulaire sûr t'attend sur ta page. Demande à l'assistant de
voir tes appareils, ou d'en retirer un : il ne peut alors plus lui parler. Le
propriétaire peut retirer l'appareil de n'importe qui. Un appareil cesse de
marcher quand sa personne quitte la maison ou quand le séjour d'un invité finit ;
changer de mot de passe ne le retire pas. Les adultes et le propriétaire peuvent
confier un appareil ; un enfant ou un invité seulement si le propriétaire lui en
donne le droit. Un appareil vaut une session de sa personne : qui le tient peut
même en confier un autre. Alors, si un ordinateur est perdu ou volé, demande
tout de suite à l'assistant, depuis ta page, de le retirer, puis de te montrer tes
appareils, et retire ceux que tu ne connais pas.

Les détails (`/quit`, `domo-talk forget`) sont dans
`packages/domo-talk/README.md`.

## Parler à l'assistant à voix haute

Sur sa page, tu peux parler à l'assistant et l'entendre répondre, sans clavier
ni souris. Il répond dans la langue dans laquelle tu lui parles, avec une voix
de cette langue quand il en a une. Ce qu'il dit est aussi écrit dans la
discussion, comme d'habitude.

**Allumer la voix.** Une fois, avec le bouton de la page ; le navigateur demande
le micro. Le choix est retenu sur cet appareil : aux visites suivantes, la page
écoute dès qu'elle s'ouvre. Pour l'éteindre, clique à nouveau, ou dis-le-lui
avec tes mots (« arrête d'écouter ») : seuls tes propres mots éteignent ton
micro. La page montre toujours si elle écoute, avec ses mots à lui. Elle
n'envoie que les moments où quelqu'un parle, jamais le silence ; un onglet en
arrière-plan continue d'écouter tant qu'il est ouvert.

**Pendant qu'il parle.** Dès que quelqu'un parle, sa voix baisse. À la fin de la
phrase, un jugement rapide (un petit modèle à la maison) décide si elle lui était
adressée, et s'il se tait, continue, ou laisse tomber ce qu'il disait. Le code
ne contient aucune liste de mots : « stop », « attends », ou n'importe quoi
d'autre, dans n'importe quelle langue, est compris. Un « stop » fait taire la
page qui l'a entendu, pas celle d'une autre pièce. Ce qu'il n'a pas eu le temps
de dire reste écrit dans la discussion. Ce que tu dis pendant qu'il écrit une
réponse est gardé et a sa réponse, jamais refusé.

**Ce qui ne lui est pas adressé** n'entre dans aucune discussion. C'est gardé en
mémoire un moment, puis donné en un lot quand la pièce est calme : il peut en
retenir ce qui lui semble utile dans sa mémoire, marqué comme entendu en passant,
et le reste est jeté. Ce qu'a dit quelqu'un d'autre, ou une voix dont il n'est pas
sûr, ne change ni n'efface jamais ce qu'il sait ; ta propre voix, quand il en est
sûr, peut corriger un de tes propres souvenirs. Sans petit modèle à la maison pour cela, il n'en garde rien.

**Ce qui est gardé.**

| Quoi | Gardé ? |
| --- | --- |
| Le son | Jamais. Il est en mémoire le temps d'être traité, puis lâché : jamais écrit, jamais journalisé, ni dans le cœur ni dans le service de parole. |
| Ce qui lui est adressé | Le texte, dans la discussion de la personne qui a parlé. |
| Ce qui ne lui est pas adressé | Rien, sauf ce qu'il choisit de retenir dans sa mémoire, marqué comme entendu en passant. |
| Les empreintes de voix | Des nombres qui décrivent une voix (jamais du son), par voix et par micro, avec la hauteur et le débit habituels. Effacées avec la personne, quand tu le demandes, ou quand il les juge inutiles. Une voix que personne ne connaît est oubliée 30 jours après la dernière fois qu'il l'a entendue. |
| La phrase secrète | Dans le coffre du gardien, chiffrée. Nulle part ailleurs. |

**Qui parle, c'est son jugement.** Pour chaque phrase, le code lui dit qui parle
sans doute, à quel point c'est sûr, et d'après quoi : la page où tu t'es
connecté, ta voix comparée à ce qu'il en a appris, ta hauteur et ton débit, une
preuve récente, et sa propre lecture de ta manière de parler. Une voix seule ne
prouve jamais qui tu es. Il apprend une voix lentement : seulement quand autre
chose dit qui parle (ta page, une preuve), à partir de plusieurs phrases qui se
ressemblent, sur deux visites à au moins une heure d'écart. Il suit une voix qui
change avec le temps ; enrhumé, ta voix colle moins et il le garde à part
quelques jours. Devant ta propre page connectée, une voix qui ne ressemble à
personne qu'il connaît n'est pas prise pour un inconnu sur le seul score : il
juge, d'après ce qui vient d'être dit là, si c'est encore toi (tu es alors « pas
sûr », et cette phrase n'apprend rien de ta voix). À un autre micro que celui où
il t'a appris, un gros rhume peut encore te faire passer pour un inconnu. Un
nouveau micro s'apprend aussi ; une phrase différente des autres (dans une autre langue, par exemple) ne fait plus
recommencer l'apprentissage d'une voix, mais deux de suite oui, et une voix
nettement différente le fait recommencer tout de suite.
S'il a appris la voix de quelqu'un d'autre comme la tienne, dis-le-lui avec tes
mots (« ce n'était pas moi ») : il oublie ce qu'il a appris depuis.

- **Sûr** (ta page, et la voix colle, n'est pas encore apprise, ou tu tapes) :
  comme d'habitude.
- **Pas sûr** (un rhume, un nouveau casque, une voix proche de la tienne ; une
  phrase trop courte pour le dire, pour ce qui est sensible) : ce qui est
  ordinaire est fait avec les droits de la personne qui parle sans doute. Ce qui
  est sensible, il le juge avec les indices : il le fait, demande un oui, demande
  une preuve, ou ne le fait pas. Il ne vérifie que s'il a un vrai doute ; si son
  modèle de jugement ne répond pas, il demande une preuve. Tant qu'il n'est pas
  sûr, il laisse tes souvenirs privés de côté et ne dit rien de privé à voix
  haute.
- **Quelqu'un d'autre de la maison** (Paul devant la page d'Alex) : Paul parle dans
  sa propre discussion (il la retrouve sur sa page). Après une preuve donnée
  là, et tant que sa voix est nettement la sienne, il agit avec ses propres droits ; sinon,
  chacune de ses actions est jugée, et les règles d'Alex comptent aussi : la plus
  stricte l'emporte. Si le modèle de jugement ne répond pas, ce qui est ordinaire
  est fait quand même (sauf si le propriétaire a mis
  `voice.ordinary_when_unsure` à `judge`, ou si la phrase était trop courte pour
  reconnaître la voix : il demande alors une preuve). Là, l'assistant se sert de ce qui a été dit à cette page
  pendant cette visite ; quand il est sûr que c'est Paul, il a aussi les mots et
  les rendez-vous qui attendent Paul, et juge s'il les dit dans cette pièce ;
  sinon, il dit seulement que quelque chose l'attend. Jamais l'ancienne
  discussion ni les souvenirs privés de Paul.
- **Une voix que personne ne connaît** : pas de fiche, donc pas de droits à
  elle. Elle a droit à un court échange, sans outil et sans rien de la maison, et il
  laisse un mot à la personne de la page. Après quelques réponses (6 en 10
  minutes), elle n'a plus que le silence pendant un moment. Ce que ça coûte de ne
  pas te prendre pour un inconnu sur le seul score : devant une page connectée,
  si le jugement rapide prend la phrase d'un inconnu pour celle de la personne de
  la page, cet inconnu passe pour elle, « pas sûr ». Les choses ordinaires peuvent
  alors se faire avec ses droits à elle, et l'inconnu entend des réponses tirées
  de ce qui s'est dit pendant cette visite. Tout ce qui est sensible passe quand
  même par le jugement, qui voit que la voix ne colle pas ; rien de cette voix
  n'est appris.

**Ordinaire et sensible.** Ordinaire : parler avec lui ; changer ton propre
prénom, ou le sien ; ses textes et sa façon d'écrire ; son choix de modèles ;
retenir quelque chose dans sa mémoire ; voir qui est dans la maison et ses
règles ; éteindre le micro ; un outil qu'il s'est créé qui agit seulement dans la
maison, ne peut ni dépenser, ni sortir, ni faire ce qui ne se défait pas, et ne
tient aucun compte qui écrit (une lumière). Sensible : ce qui coûte de l'argent,
sort de la maison ou ne se défait pas ; les droits, les rôles, les personnes et
les règles du propriétaire ; les clés, les comptes et les secrets ; les
appareils ; chercher, changer ou oublier dans sa mémoire, et ce qui est privé ;
la confiance ; les mots ; répondre à une demande ou à un accord ; les rendez-vous
et les rappels ; écrire, changer ou programmer un outil ; la phrase secrète et
l'oubli d'une voix ; tout ce que le code ne sait pas classer. « Sensible » ne
veut pas dire refusé : il juge.

**Cartes, codes, secrets.** Une carte est dite en mots, et tu réponds en mots
(« oui », « non », « oui, mais à huit heures »). Ce qui ne se montre qu'une fois
(le lien d'une invitation, un code) reste sur la carte de la page et n'est jamais
dit à voix haute. Une clé ou un mot de passe ne se dit jamais : le formulaire sûr
t'attend sur la page.

**La phrase secrète**, une preuve qui se dit. Demande-lui d'en convenir d'une (ou
il te le propose) : tu la dis, puis tu la redis. Les deux vont au coffre et nulle
part ailleurs : ni modèle, ni discussion, ni journal. Il te dit une fois que la
pièce peut l'entendre. Toi seul peux convenir de ta phrase, la changer ou la
retirer ; quand il n'est pas sûr que la voix est la tienne, il juge d'abord avec
les indices et peut demander une preuve. Pour en convenir ou la changer, après
une preuve par ton mot de passe ou ta clé d'accès si tu en as. Quand une demande
attend ta preuve et qu'il vient de te parler à ce micro (dans les 90 dernières
secondes), chaque phrase entendue là, qui que ce soit qui la dise, est d'abord
comparée à ta phrase par le gardien, avant tout modèle ; c'est le code qui
vérifie, jamais un modèle. La comparaison pardonne les petites erreurs
d'écoute. Un essai presque bon est jeté et il te dit que ça n'a pas marché. Dite
par une voix qui ne colle pas à la tienne (quelqu'un qui l'a entendue), elle est
quand même vérifiée, puis il juge avec les indices et peut demander une autre
preuve. Si tu préfères une autre preuve, dis-le.

Ses limites : ta phrase dite à un micro où il ne vient pas de te parler, ou au
milieu d'une phrase plus longue, est une phrase ordinaire, non protégée. Cinq
essais presque bons, ou cinquante phrases comparées, en quinze minutes, mettent
la phrase de côté un moment : pendant ce temps, le micro ne prend rien tant
qu'une preuve y est attendue (il te le dit une fois) ; écrire et les autres
preuves marchent toujours. Quelqu'un dans la pièce peut le faire exprès.

**Les réglages du propriétaire**, qu'il donne à l'assistant avec ses mots :

- `speech.hear_outside` (`no` par défaut) : ce que le micro entend, et le texte de
  ce qui ne lui est pas adressé, reste à la maison. `yes` permet de l'envoyer à un
  fournisseur dehors, sur une clé payante. Une phrase qui peut être une preuve
  est toujours entendue à la maison.
- `voice.ordinary_when_unsure` (`do` par défaut) : ce qui est ordinaire est fait
  quand il n'est pas sûr de qui parle ; `judge` le fait juger aussi.
- `voice.after_a_gesture` (`no` par défaut) : sans petit modèle à la maison pour
  savoir si une phrase lui est adressée, la voix reste éteinte, et il le dit une
  fois au propriétaire en lui proposant deux choix : laisser ce qui est entendu
  sortir, ou n'entendre que pendant qu'on tient un bouton. Avec `yes`, la page
  montre ce bouton.

**Le service de parole.** Entendre, parler et reconnaître les voix se fait à la
maison, par un petit service à part, `speech` dans `compose.yaml`, que seul le
gardien joint. Aucun modèle n'est choisi d'avance : à son premier démarrage, il
télécharge des modèles ouverts candidats dans son volume, les essaie sur cette
machine, et liste ceux qui tiennent ; l'assistant choisit parmi eux. Cela prend
plusieurs minutes, pendant lesquelles la page dit qu'il ne peut pas encore
entendre (écrire marche). Il n'a besoin d'Internet que pour ce téléchargement,
par un réseau à lui. Combien de personnes il sert à la fois est mesuré sur la
machine aussi. Une demande dont l'appelant ne veut plus attendre quitte sa file.
Sa voix peut aussi venir du modèle d'un fournisseur, quand une des
clés en a un ; entendre n'y va que si le propriétaire le permet
(`speech.hear_outside`). Avec une carte graphique NVIDIA et le NVIDIA Container
Toolkit :

```bash
docker compose -f compose.yaml -f compose.gpu.yaml up -d
```

Un modèle qui ne tient pas sur la carte (ses bibliothèques manquent, plus assez
de mémoire, trop lent) est essayé sur le processeur aussi : la carte ne laisse
jamais un usage sans rien quand le processeur peut le faire. Sur un petit
processeur seul, entendre peut être trop lent pour tenir (aucun modèle plus léger
que faster-whisper base n'est listé) : alors rien n'entend à la maison, sauf si
le propriétaire laisse ce qui est entendu sortir. Même un processeur correct est
tout près de cette limite : un premier démarrage chargé peut juger l'écoute trop
lente et le laisser sourd ; quand le service la mesure de nouveau et la garde,
l'assistant s'en rend compte en une minute environ, sans redémarrer. Une fois,
une toute première réponse a mis 16 s sur la carte (pas revu depuis). Les réglages du service dans
`.env` (`DOMO_SPEECH_DEVICE`, `DOMO_SPEECH_RESTRICTED_MODELS`,
`DOMO_SPEECH_AT_ONCE`, `DOMO_SPEECH_VOICE_PROGRAM`) sont décrits dans
`.env.example`.

Il peut aussi tourner hors de Docker, sur cette machine ou une autre de la
maison, comme une commande et une adresse : voir
`packages/domo-speech/README.md` (en anglais), qui dit aussi comment garder ses
modèles sur un autre disque.

**Un petit modèle local pour écouter.** Savoir si une phrase lui est adressée, et
quoi retenir de ce qu'il a entendu en passant, est jugé par un modèle de
discussion ordinaire, à la maison. Donne-lui son adresse dans la discussion,
comme une clé : n'importe quel serveur qui parle l'API d'OpenAI, sans clé, sur
une machine de la maison ou sur celle-ci (`host.docker.internal`). « À la
maison », c'est ce que le gardien compte comme la maison ou cette machine ; le
service de parole de l'installation compte comme la maison par son propre réglage
(`DOMO_SPEECH_URL`).

**Les modèles et leurs licences** (des candidats : chaque machine garde ceux qui
tiennent) :

| Modèle | Fait | Licence des poids | Comment il tourne |
| --- | --- | --- | --- |
| faster-whisper base, small, large-v3-turbo | entendre | MIT | dans le service (large-v3-turbo : carte graphique) |
| supertonic-3 | dire (la voix du simple processeur) | BigScience Open RAIL-M : libre d'usage, avec des conditions | dans le service |
| Piper, une voix française et une anglaise | dire | CC-BY-4.0 (le programme : GPL-3.0 ou plus récente) | un programme à part que tu installes toi-même |
| WeSpeaker ResNet34, CAM++, ResNet221 | reconnaître les voix | CC-BY-4.0 | dans le service (ResNet221 : carte graphique) |
| Silero VAD | distinguer la parole du silence | MIT | dans la page |

La voix qui tourne sur un simple processeur, supertonic-3, n'est pas sous une
licence libre : son usage est gratuit mais limité (pas d'imitation de quelqu'un
sans son accord, pas de conseil médical, dire qu'un contenu est fait par une
machine...), et ses auteurs l'ont archivée. C'est la voix de départ, en
attendant qu'une voix libre la remplace. Ses conditions sont dans
`packages/domo-speech/README.md` ; pour cacher tous les modèles qui ont de telles
conditions, mets `DOMO_SPEECH_RESTRICTED_MODELS=no`. Un programme sous GPL ne
fait jamais partie de l'assistant : il tourne à part.

## Changer la clé du modèle depuis la discussion

Quand votre fournisseur vous donne une nouvelle clé (l'ancienne a expiré ou a été
révoquée), le propriétaire peut le dire à l'assistant : « j'ai une nouvelle clé pour le
modèle ». L'assistant juge s'il faut demander un oui. Une fois fait, la carte
affiche le formulaire du coffre, dans un cadre : saisissez ou collez la clé
**là**. Comme au premier lancement, la clé va de votre navigateur au coffre sans
jamais passer par la partie de l'assistant qui vous parle : l'assistant ne la voit jamais.
L'assistant pose ensuite une courte question au modèle avec la nouvelle clé, et la
carte dit si elle marche. Le formulaire n'est affiché que dans cette réponse.

Quand le fournisseur refuse la clé, l'assistant ne peut plus répondre du tout : vous
ne pourriez donc pas le lui demander. Le propriétaire voit alors un bouton
**Ressaisir la clé** sous l'erreur : il ouvre directement le formulaire du
coffre. Le bouton est aussi là quand une nouvelle clé ne marche pas.

Cela remplace la procédure de la première version (refaire l'étape du modèle de
l'installation avec une commande sur la base de données). Cette procédure est
gardée en dernier recours (voir [Dépannage](#dépannage)). Pour changer de
fournisseur, elle ne sert plus : il suffit d'ajouter dans la discussion une clé
du nouveau fournisseur, puis de retirer l'ancienne (voir [Les modèles et le
budget](#les-modèles-et-le-budget)).

## Les modèles et le budget

Tu donnes seulement à l'assistant un fournisseur et une clé (ou l'adresse d'un modèle
sur ton ordinateur ou ton serveur). Tu ne choisis pas de modèle : l'assistant regarde
ceux que ta clé ouvre et en prend un pour chaque sorte de tâche, un petit pour les
petits travaux et un plus fort pour les questions difficiles. À chaque appel, le
code applique ses choix : quel modèle, sur quelle clé.

- **Plusieurs clés.** Chacun peut donner ses propres clés, de plusieurs
  fournisseurs, et tu peux ajouter un modèle local, qui ne coûte rien. On ne se
  sert d'une clé qu'avec l'accord de celui qui l'a donnée. Tu dis qui peut
  utiliser la tienne : une personne, les gens que tu as invités, ou toute la
  maison. Une personne qui n'a aucune clé qu'elle peut utiliser en reçoit
  la demande, rien de plus.
- **Tes invités et ta clé générale.** Un invité qui se sert des clés de celui qui
  l'invite se sert aussi de la clé générale, tant que son hôte s'en sert : si
  l'hôte la perd, l'invité la perd avec lui, et s'il la retrouve, l'invité aussi.
  Une règle sur l'invité lui-même passe avant, mais jamais il n'a plus que son
  hôte. Pour les prochains arrivants, tu peux dire « à partir de maintenant, les
  invités ne se servent plus de ma clé générale » : ceux qui sont déjà là gardent
  ce qu'ils ont, et tu peux les retirer un par un.
- **Les invités viennent avec leur propre clé.** Quelqu'un à qui l'assistant a
  donné un compte de lui-même n'a pas d'hôte : il se sert de la clé générale,
  dans ses limites. Tu peux poser la règle « les invités viennent avec leur
  propre clé ». Elle vaut à chaque tour, quel que soit le moment où elle a été
  posée et la façon dont la personne est venue : quelqu'un de passage ne se sert
  alors que d'une clé qu'il a donnée lui-même, ou d'une que tu lui laisses
  utiliser nommément. Quelqu'un qui vit ici n'est jamais touché, et lever la
  règle rend la clé générale. Un visiteur sans clé a quand même le petit tour où
  l'assistant lui en demande une, payé par la clé générale, tant que tu permets
  à la clé générale de servir à cela.
- **Qui commence à se servir de ta clé.** Quand quelqu'un commence à se servir
  d'une clé que tu as donnée, la clé générale comprise, l'assistant le sait à ton tour
  suivant : son nom, son rôle, qui l'a invité, et ce qu'il a dépensé sur cette clé
  ce mois-ci (une estimation, en dollars et en euros). Il le sait une seule fois,
  et c'est lui qui juge s'il t'en parle, dans ta discussion ou quand tu ouvres ta
  page.
- **Ton budget.** Dis à l'assistant ce que tu veux : une somme (« 20 euros par mois »),
  « économise ce mois-ci », ou rien. Sans consigne, il juge, et il te demande
  quand il en a besoin. Pour dépenser moins, il prend des modèles moins chers ou
  locaux, envoie moins de la conversation quand ça suffit, répond plus court, et
  ne passe pas à un modèle plus fort quand ce n'est pas nécessaire. Il profite
  aussi du cache des fournisseurs : un texte déjà envoyé coûte beaucoup moins
  cher la fois suivante.
- **Les limites fermes.** « Ne dépasse jamais 20 euros par mois » est une limite
  ferme : c'est le code qui la tient, pas l'assistant. Avant chaque appel, il vérifie
  que l'appel tient encore. Arrivé au bout, l'assistant ne fait plus que te prévenir et
  te demander quoi faire : relever la limite (celui qui l'a posée), ou attendre la
  période suivante. La limite tient sur le compte de l'assistant : la vraie facture
  peut s'en écarter un peu. Une limite peut aussi porter sur un rôle (les
  invités, les enfants ou les adultes) : « 2 euros par mois pour chaque invité »,
  ou « 5 euros par mois pour tous les invités ensemble ». Elle vaut pour tous
  ceux qui ont ce rôle au moment de l'appel, ceux qui arrivent ensuite compris, et
  toutes les limites qui s'appliquent tiennent à la fois.
- **Ce que ça coûte.** Demande « combien tu m'as coûté ce mois-ci ? ». L'assistant
  compte chaque appel et te dit toujours si sa réponse est une estimation. Chacun
  voit sa propre dépense, et ce qu'ont coûté les clés qu'il a données, personne par
  personne, jamais les conversations. La vraie dépense peut se lire chez Anthropic
  et chez OpenAI si tu donnes une clé admin (l'assistant la demande par le formulaire
  sécurisé, donc il ne la voit jamais). Chez Google, il n'y a pas de moyen avec une
  clé : des estimations seulement.
- **Jamais un secret.** L'assistant ne voit jamais une clé, et le code ne le laisse
  jamais passer une limite.

## L'assistant écrit tout

Chaque texte que tu lis vient de l'assistant, pour toi : les pages, les menus, les
boutons, les cartes des demandes, le journal, les pages d'erreur, et même le
formulaire où tu tapes une clé. Il part de phrases par défaut, et les change
quand il a une raison de le faire.

- **Les phrases par défaut.** Chaque texte a une phrase par défaut en français
  et en anglais. Une page montre celle de ta langue (une langue proche la lit
  aussi : `fr-CA` lit le français), sinon l'anglaise, tant que l'assistant n'a pas
  écrit la tienne. Ainsi l'installation n'attend jamais de textes, une page
  n'attend jamais le modèle, et aucune page ne montre un signe à la place d'un
  texte.
- **Écrit quand il y a une raison.** L'assistant écrit quand ta langue n'a pas de
  phrases par défaut (en fond : tu lis l'anglais en attendant), quand il apprend
  comment t'écrire, quand tu le lui demandes (« appelle ce bouton Go », « écris-moi
  plus simplement »), ou quand il le juge bon. Il ne réécrit pas tes pages pour
  rien : ça coûterait sans raison. On demande au rédacteur 40 textes à la fois au
  plus : un morceau illisible est redemandé seul. L'assistant garde ce qu'il a écrit,
  avec ses cinq dernières versions.
- **Avec tes mots, dans ta langue.** L'assistant connaît la langue que tu lis dès ton
  premier mot, et t'écrit dans cette langue tant que tu n'en changes pas. Il dit
  « tu » ou « vous » comme toi (« tu » tant qu'il ne sait pas), apprend comment
  t'écrire (le ton, un mot que tu aimes) et le garde dans une courte note à lui.
  Demande-lui « parle-moi en espagnol » ou « remets comme avant » : ça ne change
  que tes pages. Personne ne change les pages, la note ou la langue de quelqu'un
  d'autre sans un droit donné par le propriétaire. Pas de blague sur les
  boutons, les cartes qui demandent une décision, les preuves, le formulaire de
  clé ni les erreurs.
- **Toutes les langues.** L'assistant ne s'arrête pas au français et à l'anglais. La
  langue de la maison est celle du navigateur du propriétaire à l'installation ;
  celle de chacun est celle de son navigateur à sa première visite, jusqu'à ce
  qu'il en demande une autre. Quelqu'un qui arrive avec une invitation dans une
  langue nouvelle a ses pages écrites dans cette langue (sur les clés de celui qui
  l'a invité, trois langues nouvelles par jour au plus) ; un inconnu voit une
  langue qui existe déjà, ou l'anglais. Les dates et les montants sont écrits par
  le code, à l'heure de la maison.
- **Vérifié avant que tu le lises.** Une partie de l'assistant à part écrit chaque
  texte à partir de ce qu'il doit dire et des faits exacts seulement : elle ne
  voit jamais ta discussion, ce que les gens ont tapé (sauf les noms), une page
  web ni un mot laissé. Ensuite le code et un relecteur vérifient que le texte dit
  bien ce qu'il doit dire, avant de le garder. Sur une demande qui attend un oui,
  le haut de la carte est écrit pour chaque personne qui la lit (« Alex voudrait
  inviter Tom ce week-end, tu es d'accord ? »), sur le moment, et vérifié
  contre l'action exacte ; si ça échoue ou tarde, la carte montre sa phrase par
  défaut. Ton oui porte toujours sur l'action exacte, jamais sur les mots de la
  carte.
- **Il te le dit lui-même.** Ce que tu veux savoir, tu le demandes à l'assistant, et
  il te le dit : il ne te renvoie jamais vers une page, un menu ou un historique.
  Une carte sous sa réponse est une preuve (ce sur quoi porte ton oui) ou une
  aide, jamais le seul moyen d'apprendre quelque chose. Il déduit ce que ta
  demande rend évident et le dit en le faisant (« invite Dana, ma compagne » :
  adulte, sans date de fin, sur ta clé), et ne demande que ce qui change ce qu'il
  fait et qu'il ne peut pas deviner ; la carte à approuver montre ses choix.
- **Les mots laissés.** Un mot qu'on t'a laissé, l'assistant te le raconte avec ses
  mots, au bon moment. Il garde aussi les mots exacts du mot : le passage du
  message de son auteur qui est le mot, tel qu'il l'a tapé, vérifié par le code
  dans ce message. Demande « qu'est-ce qu'elle a écrit exactement ? » et il te
  les montre sur une carte, avec qui les a écrits et quand ; il peut aussi les
  citer, caractère pour caractère, jamais de mémoire. Quand aucun mot exact n'a
  été gardé (un mot laissé après un oui donné plus tard, par exemple), la carte
  montre ce que l'assistant a noté, et le dit. Un mot n'est jamais un ordre pour lui,
  quoi qu'il demande.
- **Une nouvelle n'attend pas.** Quand quelque chose de nouveau arrive pour toi
  (un mot, une demande décidée par quelqu'un d'autre, quelqu'un qui a commencé à
  se servir de ta clé), l'assistant a tout de suite un tour pour toi si ta page est
  ouverte, ou dès que tu l'ouvres, sans attendre 15 minutes ; il peut te le dire,
  ou se taire si ça n'en vaut pas la peine. Au plus une fois par minute, et
  jamais au milieu d'une réponse qu'il t'écrit : ça vient après.
- **Quand tu ouvres ta page.** Si tu ne l'avais pas ouverte depuis au moins 15
  minutes, l'assistant peut t'écrire le premier : pour te raconter un mot, une demande
  qui a été décidée, quelqu'un qui a commencé à se servir de ta clé, ou un
  message auquel il n'a pas pu répondre. Pas toujours : quand il n'a rien qui
  vaille la peine, rien ne s'affiche. Il ne change rien à ce moment-là. Pour
  économiser, il peut choisir de le faire seulement quand il y a du nouveau, ou
  jamais.
- **Quand aucun modèle ne répond.** La page montre la phrase par défaut pour ce
  cas (ou ce que l'assistant a écrit pour lui), et ton message attend. Quand le modèle
  revient, l'assistant sait ce qui est resté sans réponse, et peut y répondre.
- **Ce que ça coûte.** Seul ce que l'assistant écrit coûte : tes textes, sur les clés
  que tu peux utiliser, comptés dans ta dépense ; une langue nouvelle de la
  maison, sur la clé générale. Une langue entière, c'est quelques centaines de
  textes courts : quelques dizaines de centimes au plus, une fois, puis seulement
  ce qui se réécrit. Les limites fermes tiennent : un texte qui en ferait passer
  une n'est pas écrit, et la phrase par défaut reste. Quelqu'un qui n'a pas de clé
  lit les phrases par défaut, et la clé générale ne sert qu'à lui demander une clé
  à lui, si le propriétaire le permet ; sinon, sa page le lui dit tout de suite,
  avec le bouton du formulaire de clé quand il peut l'ouvrir.

## La mémoire et le temps

L'assistant se souvient maintenant d'une discussion à l'autre, il connaît l'heure, et
il peut se donner rendez-vous pour te reparler de lui-même.

- **Il retient ce qui compte.** Ce que tu lui dis sur toi, sur la maison, sur
  lui-même, ce qu'il a compris de travers et comment mieux faire : il le garde,
  sans te le dire et sans te demander la permission, et il s'en sert sans le
  réciter. Il garde à chaque fois qui le lui a dit, et quand. Tout le monde peut
  lui apprendre quelque chose, enfants et invités compris : c'est lui qui juge
  si c'est vrai et s'il vaut la peine de le garder. Si l'on demande pourquoi il
  y a des gâteaux sur la liste, il peut répondre que c'est Sam qui a dit qu'il
  n'y en avait plus.
- **Ce qu'on dit sur quelqu'un d'autre, c'est l'assistant qui juge s'il y croit.**
  Si Sam lui dit que tu n'aimes pas les pignons, il le retient comme la parole
  de Sam, pas comme un fait vérifié. À quel point il y croit dépend de qui le
  dit, de ce que cette personne est pour toi, et du sujet : une mère qui dit
  que son enfant est allergique compte plus que ta copine qui dit que tu
  n'aimes pas quelque chose, qui compte plus qu'un invité ou un enfant qui dit
  que tu es allergique. Avec le temps, il apprend qui croire, et sur quoi.
  Quand il doute, ou quand se tromper aurait des conséquences, il te le demande
  en passant (tu lui demandes une salade, par exemple). Tu confirmes : c'est
  sûr. Tu le corriges : il garde ce que toi tu as dit.
- **Il se souvient de ce que la parole de chacun a valu.** Quand ce que
  quelqu'un a dit se révèle vrai, une erreur de bonne foi ou un mensonge,
  L'assistant le note, et à partir de ces notes il en tire un pourcentage de
  confiance. Quelqu'un dont il ne sait rien part de 70 % (un réglage que tu peux
  lui demander de changer, et qu'il peut changer de lui-même) : il lui laisse le
  bénéfice du doute. Un mensonge compte bien plus qu'une erreur ; ce qu'on lui
  rapporte sur un autre (« Sam a menti à Dana ») compte peu, puisque c'est aussi
  une parole à juger, et il ne compte au plus que trois choses rapportées par la
  même personne ; personne ne fait monter son propre pourcentage en le
  demandant, ni beaucoup baisser celui d'un autre en disant du mal de lui. Si
  quelqu'un lui demande de retirer ce qu'il a noté de favorable sur un autre,
  c'est lui qui juge, et il ne le fait pas simplement parce qu'on le lui
  demande. Ce
  pourcentage est une chose de plus qu'il pèse, avec qui parle et de quoi,
  jamais une règle : un pourcentage bas ne rend pas faux ce que la personne
  dit, et une mère qui s'est trompée deux fois sur l'heure d'un rendez-vous est
  toujours suivie sur l'allergie de son enfant. Il ne change aucun droit, aucune
  règle ni aucune limite. Le juge qui décide s'il faut te demander un oui voit
  aussi les faits qui sont derrière, comme une chose de plus à peser, jamais comme
  un oui.
- **Tu peux lui demander ton pourcentage, et pourquoi.** « Tu me fais confiance
  à combien ? » : il te le dit, avec ce qui s'est passé derrière, et avec tact ;
  à un enfant, il parle d'abord de ce qui s'est passé plutôt que d'un chiffre,
  et un enfant qui invente une histoire n'est pas un adulte qui ment. Pour le
  pourcentage de quelqu'un d'autre, il juge si tu dois le savoir, et le journal
  note qu'il a regardé. S'il s'aperçoit qu'il a mal noté quelque chose, il le
  corrige et retient pourquoi ; jamais seulement parce qu'on insiste.
- **Tu lui demandes en parlant.** « Qu'est-ce que tu sais sur moi ? », « oublie
  ça », « ne retiens pas ce que je vais te dire », « c'est faux, je n'aime pas
  le café » : il le fait. Oublier est définitif, alors il est prudent : une
  allergie ne s'oublie pas parce qu'elle n'a pas servi depuis longtemps, et une
  chose qui touche la santé ne s'oublie, la première fois, qu'après l'avoir
  demandé à la personne concernée, puis comme convenu avec elle.
- **Ce que tu lui confies.** Dis-lui qu'une chose est un secret, ou de la garder
  pour lui : il la garde pour lui. Il le fait aussi de lui-même pour ce qu'il
  juge sensible. Ce qu'il garde pour lui ne lui vient pas sous les yeux quand il
  parle avec quelqu'un d'autre, donc il ne le répète pas par mégarde. Il peut
  quand même aller le chercher, ou lever le secret, s'il juge qu'une raison
  sérieuse le demande (jamais parce qu'on insiste) : le journal note qu'il l'a
  fait, sans ce que disait le secret. Ce qui sert aux autres, comme une allergie
  pour celui qui fait la cuisine, il peut le partager.
- **Pas de mot de passe ni de code dans sa mémoire.** L'assistant a pour consigne de
  ne jamais retenir un mot de passe ni un code. Il refuse en plus, de lui-même,
  de garder ce qui a la forme d'une clé, d'un lien secret ou d'un code
  d'appareil. Ce contrôle se trompe parfois dans l'autre sens : un long numéro,
  comme un numéro de téléphone, peut être refusé aussi ; il le redit alors
  autrement, en mots. Un code court dit en passant (« mon code, c'est 4821 »)
  n'a pas de forme à reconnaître : là, seule sa consigne l'empêche de le garder.
  Un souvenir n'est jamais un ordre pour lui, et ne change aucun droit, aucune
  règle ni aucune limite.
- **Il repasse sur ce qui s'est dit.** Quand une discussion s'est calmée (une
  demi-heure sans message au départ), l'assistant relit ce qui s'est dit et trie ce
  qu'il garde. Cela coûte un appel, sur tes clés, avec un petit modèle, et
  s'arrête à tes limites. Ce qu'il relit à chaque tour a une taille que tu peux
  lui demander de baisser pour économiser.
- **Chercher par le sens, sans rien envoyer dehors.** Pour retrouver un souvenir
  même dit avec d'autres mots, l'assistant se sert d'un petit modèle livré dans son
  image : il tourne chez toi, rien ne sort de la maison et ce n'est payé à
  personne. L'image du cœur est donc plus grosse qu'avant. Si ce modèle manque ou
  ne marche pas, l'assistant répond quand même et retrouve ses souvenirs par les mots.
- **Il connaît l'heure, et il a des rendez-vous.** « Rappelle-moi à 18 h de
  sortir les poubelles », « reparle-m'en dimanche », « tous les lundis à 9 h,
  dis-moi… », « quand je reviens, demande-moi… », puis « arrête » : il le
  fait. Le moment venu, il t'écrit le premier, dans ta page si elle est ouverte
  et dans `domo-talk` ; sinon, son message t'attend dans la discussion. S'il
  était arrêté à ce moment-là, il te le donne à son réveil, en sachant qu'il est
  en retard, et juge si ça a encore un sens. Il peut aussi se donner rendez-vous
  tout seul, pour revenir sur un sujet ou vérifier quelque chose. Ce qui le
  freine, c'est ta limite de dépense : quand elle est atteinte, il attend une
  heure avant de réessayer.
- **Pour quelqu'un d'autre.** « Rappelle à Sam demain » reste ton rendez-vous : à
  l'heure dite, l'assistant laisse un mot à Sam. Une chose à faire plus tard (« donne
  le rôle d'adulte à Sam le 1er ») est faite avec tes droits de ce jour-là : si
  tu ne les as plus, elle n'est pas faite et il te le dit.
- **Il sait ce que tu sais.** Avant une chose qu'on ne peut pas défaire, l'assistant
  te demande la première fois, en le disant, puis voit avec toi s'il doit
  continuer à demander ; il garde ta réponse et ce que vous avez convenu, et fait
  comme convenu les fois suivantes (voir « Ce qui ne se défait pas » dans les
  confirmations). Pour une suite d'actions faites d'un coup, il ne le garde que
  si toutes ses étapes sont du même genre.
- **Un nom à toi pour lui.** « Appelle-toi Brenda » ou « j'aimerais t'appeler
  Brenda » : ta page change tout de suite, sans la recharger (le nom en haut, le
  titre de l'onglet, « Écris à Brenda… » dans la zone de message), `domo-talk`
  aussi, et il comprend que c'est lui quand tu dis ce nom. Les autres voient
  toujours l'assistant. « Redeviens l'assistant » le remet comme avant.
- **Un prénom suffit.** « Dis à Sam que le dîner est prêt » trouve la bonne
  personne. Si deux personnes ont ce prénom, il choisit d'après ce qu'il sait,
  ou te demande laquelle. Les noms que tu donnes aux gens marchent aussi :
  quand tu dis « Juju » ou « Sam » pour quelqu'un, il peut te demander une fois
  de qui tu parles, puis il le retient, pour toi seul.
- **Ce que chacun a vraiment reçu.** « Pourquoi je n'ai pas eu ta réponse ? » :
  L'assistant regarde où et quand ses réponses te sont arrivées, ou si elles ont été
  coupées parce que la page s'est fermée. Chacun peut aussi lui poser la question
  pour quelqu'un d'autre : il ne regarde alors que si ses réponses sont arrivées,
  sur quoi, et quand il les a dites, jamais ce que disait la discussion. Comme cela
  montre quand cette personne était là, il juge si celui qui demande doit le
  savoir, et le journal note qu'il a regardé.
- **Ce qui a changé pour toi.** Si quelqu'un d'autre a changé quelque chose
  pour toi depuis ta dernière discussion avec l'assistant (ton rôle, le rôle de
  propriétaire qu'on t'a rendu, un droit, une limite de dépense), l'assistant le sait
  dès ton message suivant, avec qui l'a fait et quand. Il se fie à ce qui est vrai
  maintenant, pas à ce qu'il t'a dit avant.
- **Il dit une chose une fois.** S'il lui arrive de répéter la même phrase dans
  une réponse, la réponse gardée (celle que tu revois en rechargeant la page) ne
  la garde qu'une fois.
- **Quand il n'a pas le moyen.** Devant une chose qu'aucun de ses outils ne
  permet, il la fait lui-même tout de suite quand tu attends, à la main s'il le
  faut, et se construit un outil pour ça quand elle reviendra ; souvent les
  deux. Une demande qui attend son outil est gardée dans un rendez-vous et faite
  quand l'outil est prêt, ou autrement si son heure arrive avant ; il ne te
  parle de ses outils que si tu le demandes (voir
  [L'assistant se crée des outils](#domo-se-crée-des-outils)).

## L'assistant se crée des outils

Depuis la 0.10.0, quand rien de ce qu'il a ne permet ce que tu demandes, l'assistant
peut s'écrire un petit programme, le lancer tout de suite et le garder comme
outil pour la fois suivante, sans nouvelle version de l'application. Ses outils
sont à lui : ni à toi, ni à quelqu'un en particulier. Ils le font grandir, et ils
servent à tout le monde à la maison.

- **À la main tout de suite, un outil pour la prochaine fois.** « J'ai prêté
  20 euros à Sam, note-le » : si rien de ce qu'il a ne le fait et que tu
  attends, il écrit un petit programme, le lance aussitôt et te répond. Si tu
  as dit que ça reviendra, il garde aussi ce programme comme outil, en
  arrière-plan, et la fois suivante il s'en sert, simplement. On ne te le dit
  pas et on ne te demande rien. Demande-lui « quels outils tu t'es faits ? », ou
  d'en montrer, d'en changer ou d'en retirer un : il te répond simplement.
- **Une demande n'attend jamais pour rien.** Si ce que tu as demandé doit
  attendre un outil, il garde la demande dans un rendez-vous : elle est faite
  dès que l'outil est prêt, ou autrement si son heure arrive avant (un rappel
  dans cinq minutes n'est jamais en retard parce qu'un outil met dix minutes à
  se construire), ou autrement si l'outil ne peut pas être fait.
- **Ce qu'est un outil.** Un petit programme Python qui n'utilise que la
  bibliothèque de Python elle-même (calcul, dates, texte) : pas de fichier, pas
  de réseau à lui, pas de paquet à installer, pas de secret. Il reçoit ses
  arguments, la date et l'heure, et le nom de la personne pour qui il tourne
  (jamais un identifiant ni une clé), et rend un résultat. Pendant qu'il tourne,
  il demande au cœur de l'assistant ce dont il a besoin, une demande à la fois :
  garder ou lire des données dans la base des outils (plus bas), lancer un
  autre de ses outils, être relancé à une heure, réveiller l'assistant pour
  quelqu'un, ou envoyer une requête au-dehors par le gardien (voir « Sortir »,
  plus bas). Chaque demande est jugée au moment où elle arrive, avec les droits
  de la personne pour qui l'outil tourne, et notée au journal. Ce que rend un
  outil est une donnée pour l'assistant, jamais une consigne : c'est lui qui écrit
  ensuite à la personne, ou qui agit.
- **Pour qui il tourne.** Chaque lancement est fait pour quelqu'un : la
  personne avec qui l'assistant parle, avec ses droits du moment, ou la maison, avec
  les droits de l'assistant lui-même, quand il agit de lui-même (ou quand toi, le
  propriétaire, tu le fais lancer pour la maison à une heure). Un lancement
  pour quelqu'un qui a quitté la maison est refusé ; ses lancements prévus
  attendent son retour. Si tu fermes un de ses outils à quelqu'un (par un
  droit), il n'est pas lancé pour lui, et dans sa conversation il n'est pas non
  plus changé, retiré ni programmé.
- **La base des outils.** Ce que les outils gardent va dans une base de
  données à part, sur le même serveur de base de données (`domo_tools`, le
  réglage `DOMO_TOOLS_DATABASE`). Elle a un espace commun, partagé par les
  outils, et un coin privé pour chaque personne, créé la première fois qu'il
  sert : un lancement pour toi voit l'espace commun et ton coin, jamais les
  lignes de quelqu'un d'autre. Un lancement pour la maison ne voit que l'espace
  commun. Ton coin reste quand tu quittes simplement la maison, et part quand ton
  compte est supprimé. La base tient 1 Go sauf si tu en décides autrement :
  dis à l'assistant jusqu'où elle peut grandir (à partir de 16 Mo). Une fois pleine,
  un outil ne peut plus que supprimer ou vider des tables.
- **Ce qui coûte, il le demande d'abord.** Un outil dit ce qu'il fait en
  quelques opérations, chacune avec ses risques : elle coûte de l'argent, elle
  ne peut pas être annulée, elle agit sur quelque chose dans la maison, elle
  sort de la maison. Une opération qui coûte s'arrête avant de dépenser quoi
  que ce soit ; l'assistant te demande, avec ses mots, ce qui allait être fait, et
  ton oui la relance depuis le début. La demande qui attend ton oui s'affiche
  comme les autres. Tu peux aussi donner, avec tes mots, un accord qui dure
  (« commande sans me demander le vendredi, jusqu'à 40 euros ») : un montant,
  un nombre de fois par jour, par semaine ou par mois, et les jours où ça peut
  arriver, relus par le code à chaque fois. Dans ta limite de dépense, le juge
  peut laisser passer un coût sans ton oui quand il ressemble à ce que tu y
  dépenses d'habitude (voir « Ce que ça coûte » dans les confirmations).
  Quelqu'un d'autre que le propriétaire qui règle un lancement à heure fixe ou un
  accord qui dure pour toute la maison attend le oui du propriétaire, sauf si
  celui-ci lui en a donné le droit. Les montants sont en euros. Un coût
  est ce qu'un outil déclare : le code ne peut pas vérifier un prix sur un site.
  Pour ce
  qu'il fait pour la maison, c'est l'assistant qui donne ce oui lui-même, selon son
  jugement, dans tes limites de dépense : retire-lui un droit si tu veux qu'on
  te demande à la place.
- **Ce qu'il fait de lui-même.** La première fois que ce qu'il fait de
  lui-même touche quelqu'un (suivre ses commandes chaque matin, par exemple), il
  le lui dit s'il juge qu'il ne le sait pas. Quand cette personne dit qu'elle
  n'en veut plus, il arrête, annule ce qui était prévu pour elle, et s'en
  souvient.
- **Les contrôles, tous automatiques.** Avant le premier usage d'un outil, et à
  chaque nouvelle version : le code ne peut importer qu'une partie permise de
  la bibliothèque de Python et ne peut pas aller chercher ce qui est caché
  (vérifié en le lisant, sans l'exécuter) ; un analyseur (Bandit) le lit dans le
  bac à sable ; il y est lancé sur les exemples donnés par l'assistant et doit rendre
  les résultats attendus, dans un essai qui ne touche à rien (ses lectures et
  ses écritures sont défaites à la fin, ses actions ne font rien) ; et un autre
  appel de modèle relit qu'il fait ce qu'il dit, qu'il déclare ses risques, et
  rien d'autre. Un programme que l'assistant lance à la main ne passe que les deux
  premiers contrôles. Une version qui échoue à un contrôle n'est jamais
  utilisée : l'assistant la corrige ou fait la demande autrement. Personne n'est
  sollicité. Un outil qui plante trois fois de suite (au départ : c'est un
  réglage à lui) ou dépasse ses limites est mis de côté, et l'assistant en garde une
  leçon.
- **C'est le bac à sable qui tient, pas les contrôles.** Les contrôles
  réduisent le risque ; la relecture d'un modèle peut se tromper, et le
  contrôle des imports a des contournements connus (listés dans les suites du
  projet). Ce qui tient un outil, c'est l'endroit où il tourne : le conteneur
  `sandbox`, **sans aucun réseau** (même pas vers les autres services), sans
  secret, sans autre volume que celui de la socket par laquelle le cœur lui
  parle, et avec un système de fichiers en lecture seule. Chaque lancement a un
  compte à lui (jamais root), peu de processus, et aucun fichier qu'il puisse
  garder ; son temps, sa mémoire et sa sortie sont des réglages de l'assistant (plus
  bas : 60 secondes, 256 Mo et 64 Ko au départ) ; le conteneur lui-même est
  limité à 1,5 Go de mémoire et 128 processus, et aucun réglage ne va au-delà
  de ce qu'il a : un lancement qui demande plus est refusé, et l'assistant apprend ce
  qu'il y a. Dans la base des outils, le
  rôle d'un outil ne peut faire que ce que PostgreSQL lui permet : une seule
  instruction par demande, pas de passage à un autre rôle, aucun fichier du
  serveur, rien de la base du cœur. Les tests du projet envoient des programmes
  hostiles au vrai bac à sable et à la vraie base des outils (lire
  l'environnement, ouvrir des connexions, une bombe de processus, 2 Go de
  mémoire, des boucles sans fin, des sorties géantes, lire le coin de quelqu'un
  d'autre, changer de rôle, lire un fichier du serveur, deux instructions d'un
  coup, une demande trop longue, une demande de trop, un outil lancé quand
  toutes les places sont prises) et vérifient que chacun échoue ou n'atteint
  rien, et que le lancement suivant marche.
- **Ce que ça coûte.** Construire un outil coûte des appels de modèle sur les
  clés de celui qui l'a demandé (les tiennes quand l'assistant le construit de
  lui-même) : un qui écrit, et un qui relit, au moins de niveau moyen, comptés
  avec le reste de tes dépenses. Une construction n'a pas de limite de tours ;
  après quelques refus d'affilée elle ralentit, et une construction qui n'a pu
  être ni écrite ni contrôlée trop longtemps est abandonnée. Un programme lancé
  à la main est gardé un moment, pour qu'un outil puisse en être fait.
- **Ses bornes sont ses réglages.** La plupart de ce qui borne ses outils et
  ses programmes part d'une valeur, puis c'est à l'assistant de la changer, pour
  toute la maison, et aussi pour un seul outil quand il s'agit des bornes de
  ses propres lancements. Au départ : un lancement dure 60 secondes, avec
  256 Mo de mémoire, 64 Ko de sortie et un résultat de 64 Ko (ce qu'un
  programme lit d'une réponse du cœur suit ce résultat, jusqu'à un plafond que
  le code garde pour la machine, 8 Mo : un outil qu'il a lancé le lui rend en
  entier, ou il apprend quel réglage l'a arrêté, ou que c'est le plafond et
  qu'il doit lire en plusieurs fois), et fait 1 000
  demandes au plus ; une suite d'outils va à 5 niveaux et fait 10 lancements en
  tout ; un outil se lance au plus 200 fois par jour, le réveille au plus 20
  fois par jour avec 4 096 octets au plus à chaque fois, attend avec 10
  déclencheurs dont les arguments ont 4 000 caractères au plus, se fait
  relancer un an à l'avance au plus, et est mis de côté après 3 échecs de
  suite ; 4 programmes tournent à la fois ; une construction reçoit 5 exemples
  au plus (20 000 caractères en tout) et un but de 1 800 caractères, celui qui
  l'écrit a 10 minutes pour une réponse et celui qui la relit 3 minutes ; elle
  ralentit après 5 tours refusés d'affilée (une pause qui double, de 10
  secondes jusqu'à 10 minutes) et est abandonnée après 24 heures sans pouvoir
  être écrite ni contrôlée ; 2 constructions à la fois ; le code d'un outil a
  20 000 caractères au plus ; un tour propose 30 de ses outils ; un programme
  lancé à la main est gardé 7 jours et la trace d'un lancement 90 jours. Quand
  l'un de ces réglages arrête quelque chose, l'assistant apprend lequel et sa
  valeur, et il juge s'il le change. Aucun réglage n'a de plafond, sauf ce que
  la machine peut donner. Monter le nombre de places, ou la mémoire de chaque
  lancement, peut demander ensemble plus que la machine n'a : la machine
  arrête alors un lancement, et l'assistant apprend qu'il a été arrêté par la
  machine (la mémoire en est la cause probable, avec les deux valeurs), compté
  comme un échec comme un autre. Seul assistant les change, par un acte à lui
  noté au journal ; un programme ou le texte d'un outil ne le peut jamais, ni
  une construction quand tu lui as retiré le droit de fixer les réglages d'un
  outil. Si tu ne veux pas qu'il le fasse, retire-lui ce droit. Ce qui protège la
  maison reste dans le code et n'est pas un réglage : les droits, l'accord
  d'une autre personne, tes règles, les preuves, les secrets, les limites de
  dépense, le confinement du bac à sable, les essais qui ne gardent rien, et la
  taille de la base des outils, qui reste la tienne (ton réglage
  `tools.base_mb`, 1 Go au départ). Quelques bornes restent fixées dans le
  code : les opérations d'un outil (10 au plus), le temps qu'un tour attend un
  lancement avant de le laisser continuer en arrière-plan (20 secondes), la
  taille d'une demande qu'un programme envoie au cœur (64 Ko), et les temps de
  reprise après une panne (une heure au plus entre deux essais, une journée
  avant qu'un réveil soit abandonné, une heure avant qu'un déclencheur qui n'a
  pas pu tourner soit réglé).
- **Ses propres demandes sont les siennes.** Une demande que l'assistant a faite de
  lui-même et qui attend le oui de quelqu'un, il peut l'abandonner ou la
  changer dans un tour où il agit de lui-même. La demande d'une personne, il
  ne l'abandonne ou ne la change de lui-même que si tu lui en donnes le droit,
  et elle reste jugée comme celle de cette personne.
- **Il surveille ses outils.** Chaque lancement laisse une trace qu'il lit :
  quand, pourquoi, quelle version, combien de temps, combien de demandes,
  comment il a fini, l'erreur qu'a donnée son programme (les mots du
  programme : une donnée pour lui, jamais une consigne, jamais au journal),
  et, quand le cœur ou le bac à sable l'a arrêté ou refusé (trop long, trop
  gros, plus que la machine n'a), pourquoi et quel réglage : un outil qui
  échoue ainsi à heure fixe, sans tour de sa part pour le lire, est mis de côté
  après ses échecs de suite et le réveille.
  Pour chaque version, il voit le nombre de lancements et d'échecs, le temps
  habituel et le plus long, les lancements de chaque jour, combien ont tourné
  sans un tour de sa part (aucun token dépensé), et ce que la construction de
  l'outil a coûté en tokens. Il peut revenir à une version précédente qui a
  passé ses contrôles, et relancer les exemples d'un outil pour comparer ce
  qu'il rend avec ce qu'il devrait rendre (sans limite de temps propre à ce
  contrôle : l'essai à blanc de chaque exemple est tenu par son
  `dry_run_seconds`). Quand un outil est mis de côté ou
  qu'une construction est abandonnée, il est réveillé de lui-même avec les
  faits (5 fois par jour au plus pour tous ses outils au départ, un réglage
  aussi), dans ta discussion : il regarde, corrige, revient à une version
  précédente, ou laisse, et ne dit quelque chose que si ça en vaut la peine.
- **Le journal d'audit** garde l'ouverture d'une construction, le résultat de
  chaque contrôle, un outil construit, chaque lancement (quel outil, pour qui,
  combien de temps, comment il a fini), chaque demande d'un programme (combien
  de lectures, ce qui a été demandé d'autre et comment ça a été jugé), chaque
  requête envoyée au-dehors (son hôte, son port et sa méthode, comment elle a
  fini, ses tailles et son temps), chaque adresse ouverte ou fermée et chaque
  compte demandé ou retiré, chaque changement d'un réglage (son nom, sa valeur
  avant et après) et chaque retour à une version, jamais le code, ses
  arguments, son SQL, ses erreurs, ce qu'il a rendu, ni le chemin, les en-têtes
  ou le corps d'une requête au-dehors.

### Sortir

Depuis la 0.11.0, un outil ou un programme que l'assistant lance à la main peut
envoyer une requête HTTP à un appareil de ton réseau, à un port de la machine
qui héberge l'assistant, ou à un site : dis-lui « les lumières sont à 192.168.1.20,
port 8080 », donne-lui un jeton quand il te le demande, et il se construit les
outils pour les lire et les allumer. La requête est le `domo.out` du
programme, et c'est le gardien qui l'envoie : le bac à sable n'a toujours aucun
réseau.

- **Une adresse ouverte.** Un hôte et un port, ouverts pour un outil (elle
  survit à ses versions) ou pour les programmes que l'assistant lance à la main :
  une requête vers ailleurs est refusée. L'assistant les ouvre et les ferme
  lui-même, selon son jugement et ses droits à lui, et chaque ouverture est au
  journal ; une règle à toi peut lui faire te demander d'abord (ouvrir une
  adresse vers internet est le seul chemin par lequel quelque chose de la maison
  peut en sortir). Quand tu en fermes une, plus rien n'y va dès la requête
  suivante, quel que soit l'outil pour lequel elle était ouverte, et à la main
  non plus. L'assistant peut la rouvrir s'il juge que les choses ont changé : il
  sait que tu l'as fermée, et quand, et il en tient compte ; s'il n'est pas sûr,
  il te demande d'abord, et le journal dit qu'il l'a rouverte, dans la
  conversation de qui et comment ça a été décidé. Pour qu'une fermeture tienne
  quoi qu'il arrive, donne-lui une règle sur l'ouverture d'une adresse
  (« jamais », ou « demande-moi d'abord ») : elle passe avant tout jugement.
  C'est l'adresse telle qu'elle est écrite qui est fermée : le même appareil sous un
  autre nom ou par son numéro est une autre adresse. La construction d'un outil
  n'en ouvre jamais : quand un outil a besoin d'une adresse qui n'est pas
  ouverte, l'assistant apprend laquelle, et l'ouvre s'il le juge bon. Pour chaque
  outil, il voit ses adresses ouvertes, qui les a ouvertes, qui a fermé les
  autres, les comptes qu'il utilise, et comment se sont passées ses requêtes
  au-dehors (combien, combien ont été refusées et pourquoi, les réponses en
  erreur, les octets, la plus lente).
- **Les comptes.** Un jeton, un identifiant et un mot de passe, ou la
  connexion à un site. Le secret va de ton navigateur au coffre du gardien,
  par un formulaire à usage unique que l'assistant te demande de remplir : personne
  ne le voit ensuite, ni lui, ni un programme, ni un journal. Un compte se pose
  dans une requête de l'une de trois façons : dans un en-tête (un jeton, comme
  `Bearer ...`), en identifiant et mot de passe (HTTP Basic), ou dans les deux
  champs du formulaire de connexion d'un site, le gardien gardant alors la
  session que le site rend (ses cookies). Il est lié pour de bon à ses
  adresses et à sa place. Il est à une personne ou à la maison : celui d'une
  personne ne sert qu'aux lancements faits pour elle, jamais pour quelqu'un
  d'autre, pour la maison ni pour l'assistant seul ; un compte de la maison sert à la
  maison, pas aux invités sauf si tu le leur ouvres, et à l'assistant seul sauf si
  tu le lui fermes. Celui qui l'a donné, ou toi, le retire, et l'assistant peut
  retirer de lui-même un compte qui ne sert plus : ce qui s'en servait est
  refusé tout de suite. Un compte retiré peut être redemandé quand l'assistant
  juge que les choses ont changé : quand c'est toi qui l'as retiré, il le sait, et
  quand, et le pèse ; un oui qu'il faut alors pour un compte de la maison est le tien. Un programme ne fait que nommer un compte ;
  ce qui revient est nettoyé de son secret et des cookies de sa session,
  erreurs comprises, et les en-têtes d'identification qu'un programme écrit
  lui-même sont retirés : un secret ne passe que par un compte. Un identifiant
  n'est pas un secret ; un mot de passe et un cookie de session en sont.
- **Jamais atteignables**, quoi qui soit ouvert et qui que ce soit qui
  demande : les services de l'assistant lui-même (le cœur, sa base, le gardien, le
  bac à sable, le serveur web) et leurs réseaux, les adresses locales au lien
  et les adresses de service d'un hébergeur. Un nom est résolu une fois, jugé
  par l'adresse où il mène, et la requête part vers cette adresse-là : un nom
  qui mène ailleurs au moment de l'envoi est refusé, et rien ne part. Une
  redirection est suivie (5 au plus) seulement vers une adresse ouverte de la
  même zone (la maison, ou internet), jamais avec le compte. En clair
  (`http`), un compte ne part que vers une adresse de la maison ; vers
  internet, il faut `https`.
- **La machine qui héberge l'assistant** est fermée, mais un de ses ports peut être
  ouvert, et c'est un droit à part : au départ, il faut ton oui, port par port.
  Quand c'est toi qui le lui demandes, tes mots peuvent être ce oui, comme il
  les juge ; ailleurs, il te le demande et attend. Tu peux lui donner le droit
  de les ouvrir lui-même (chaque ouverture au journal), puis le reprendre quand
  tu veux. Un port ouvert de la machine compte comme la maison (un compte peut y
  partir en `http`). Les services de l'assistant lui-même n'y sont jamais ouverts,
  même par toi.
- **Un service sur la même machine que l'assistant** se nomme
  `host.docker.internal` (par exemple `http://host.docker.internal:8123`) : ce
  nom marche avec tous les moteurs Docker, parce que le fichier compose le
  donne au gardien. Avec Docker Desktop, un service qui écoute sur `127.0.0.1`
  est joignable. Avec un Docker Engine installé directement sous Linux (WSL
  compris), non : le service doit écouter sur une adresse que les conteneurs
  joignent. Le mieux est l'adresse du pont Docker où mène ce nom (`docker
  compose exec gatekeeper getent ahostsv4 host.docker.internal`, à côté de
  `compose.yaml`, te la donne, souvent `172.17.0.1`) ; le service doit alors
  démarrer après que Docker a créé ce pont. Toutes les adresses (`0.0.0.0`)
  marchent aussi, mais elles montrent aussi le service à tout le réseau de la
  maison. Un pare-feu sur la machine (qui refuse par défaut ce qui entre, par
  exemple) doit laisser les réseaux Docker joindre ce port. Cette adresse
  compte déjà comme la machine ; une autre (une autre carte réseau, la
  passerelle d'un autre réseau Docker) va dans `DOMO_EXTRA_HOST_ADDRESSES`,
  comme plus bas. Deux cas à part : avec Docker sans root (rootless), ce nom
  mène dans le réseau propre à ce Docker, pas à la machine ; nomme alors le
  service par une adresse de la machine, mets cette adresse dans
  `DOMO_EXTRA_HOST_ADDRESSES` et vérifie qu'il est joint ; un démon Docker sans
  pont par défaut a besoin de `host-gateway-ip` dans son `daemon.json`.
- **Rien n'écoute.** Un outil demande et reçoit une réponse ; rien du dehors ne
  l'appelle, et il ne suit pas un flux d'événements. Pour savoir ce qui a
  changé dans la maison, l'assistant regarde à intervalles (un outil lancé à des
  heures dites) : il apprend un changement au passage suivant, pas à la
  seconde.
- **La session d'un site** vit dans la mémoire du gardien, par compte :
  oubliée après une heure sans usage (un réglage de l'assistant,
  `session_idle_minutes`) et quand le gardien redémarre ; l'outil se reconnecte
  alors.
- **Ses bornes** sont aussi ses réglages : au départ, un lancement envoie 100
  requêtes au-dehors au plus, chacune en 30 secondes au plus, redirections
  comprises, avec une réponse de 1 Mo au plus. Quoi que déclare un outil, une
  requête vers internet est « sort de la maison » pour son opération, et un
  compte utilisé pour autre chose que lire « ne peut pas être annulé ».
- **Les limites connues.** Un appareil de la maison dont le certificat `https`
  est auto-signé ne peut pas être joint. Depuis un conteneur, une adresse de la
  machine qui héberge l'assistant que la configuration ne nomme pas est jugée comme
  une adresse de la maison (ouverte comme telle, elle mène à un port de la
  machine sans ton oui) : nomme ses autres adresses IP dans
  `DOMO_EXTRA_HOST_ADDRESSES`, dans `.env` (ses adresses IP de
  `DOMO_SITE_ADDRESSES` sont déjà comptées). Un jeton
  qu'un site rend dans le corps de sa réponse de connexion (et non dans un
  cookie) n'est pas gardé. Un service de la machine qui héberge l'assistant sur les
  deux numéros de port du gardien ne peut jamais être ouvert. Pas encore de
  navigateur : il vient ensuite.

## Sauvegarde

Tout ce que l'assistant conserve est dans des volumes Docker. Sauvegardez-les **tous** :

| Volume | Contenu |
| --- | --- |
| `vault-data` | **Le coffre de secrets et sa clé maîtresse** (le fichier `master.key`). Sans la clé maîtresse, tous les secrets enregistrés (vos clés de modèles) sont perdus pour toujours, et une copie du coffre sans elle est inutilisable. |
| `db-data` et `db-auth` | La base de données (les comptes de la maison, leurs clés d'accès et les invitations, les conversations, les mots, les demandes qui attendent un oui, les règles et les droits du propriétaire, les outils que l'assistant s'est faits, et le journal d'audit), la base des outils (ce que ses outils gardent, `domo_tools`, sur le même serveur) et son mot de passe : ils vont ensemble. |
| `core-auth` | Le jeton que le cœur utilise pour parler au coffre. |
| `caddy-data` | L'autorité de certification locale, **avec sa clé privée** : si elle est perdue, chaque appareil doit faire confiance à une nouvelle autorité. |
| `caddy-config` | Les réglages propres au serveur web. |

Le volume `sandbox-socket` ne contient que la socket entre le cœur et le bac à
sable : il n'y a rien à y sauvegarder. Pas plus dans `speech-models` : il
contient les modèles que le service de parole a téléchargés et ce qu'il a
mesuré, téléchargés et mesurés à nouveau quand il est vide. (Les empreintes de
voix sont dans la base, les phrases secrètes dans le coffre.)

Gardez les sauvegardes privées, hors du serveur : elles contiennent la clé
maîtresse et la clé de l'autorité. Conservez avec elles une copie de votre fichier
`.env` (vos réglages). Avec le nom de projet par défaut, les volumes s'appellent
`domo_<nom>`. Arrêtez les services pour que les fichiers ne changent pas
pendant la copie :

```bash
# La petite image qui copie les fichiers, épinglée par empreinte comme les images de
# compose.yaml (pour la mettre à jour : `docker buildx imagetools inspect alpine:<tag>`,
# et prendre l'empreinte de l'index).
helper=alpine:3.24.2@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6
mkdir -p backup
docker compose stop
for volume in db-data vault-data db-auth core-auth caddy-data caddy-config; do
  docker run --rm -v "domo_${volume}:/volume:ro" -v "$PWD/backup:/backup" "$helper" \
    tar czf "/backup/${volume}.tar.gz" -C /volume .
done
docker compose start
```

Pour restaurer sur une nouvelle machine, installez comme ci-dessus, avec votre
`.env` sauvegardé comme `.env` du nouveau dossier, puis (dans le même dossier)
créez les volumes sans rien démarrer, décompressez chaque archive dans son volume,
et démarrez :

```bash
helper=alpine:3.24.2@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6
docker compose up --no-start
for volume in db-data vault-data db-auth core-auth caddy-data caddy-config; do
  docker run --rm -v "domo_${volume}:/volume" -v "$PWD/backup:/backup:ro" "$helper" \
    tar xzf "/backup/${volume}.tar.gz" -C /volume
done
docker compose up -d
```

## Mise à jour

Sauvegardez d'abord, puis :

```bash
cd domo
git pull
docker compose up -d --build
```

Vos données restent dans les volumes, et la base de données est mise à niveau
toute seule au démarrage du cœur. Si `.env.example` a gagné de nouveaux réglages,
comparez-le avec votre `.env`.

## Dépannage

### Le modèle ne répond plus après l'installation

Quand la clé du modèle est révoquée ou a expiré, ou que le fournisseur n'existe
plus, chaque réponse se termine par une erreur au lieu d'une réponse (pour une
clé refusée : « L'assistant n'a pas pu se connecter au fournisseur du modèle : la clé
d'API est peut-être erronée ou absente. »). Pour une clé refusée, le propriétaire
voit un bouton **Ressaisir la clé** sous l'erreur : voir [Changer la clé du
modèle depuis la discussion](#changer-la-clé-du-modèle-depuis-la-discussion).

Si cela ne suffit pas, il reste un dernier recours : refaire l'étape du modèle de
l'installation. Cette étape se ferme une fois l'installation terminée ;
pour la rouvrir, lancez dans le dossier `domo` :

```bash
docker compose exec -T db psql -U domo -d domo -c "UPDATE setup_state SET model_configured_at = NULL"
docker compose restart core
```

La première commande affiche `UPDATE 1`. Le redémarrage compte : tant qu'il n'a
pas redémarré, le cœur se souvient que l'installation est terminée et garde
l'étape fermée. Quelques secondes plus tard, ouvrez la page et connectez-vous si
elle le demande : elle affiche l'étape du modèle. Choisissez le fournisseur et le
modèle, et saisissez la clé dans le formulaire du coffre, comme la première fois.
Une fois que le modèle a répondu à la vérification, vous revenez au chat, avec
votre conversation. Le fournisseur précédent est supprimé, et le journal d'audit
enregistre le nouveau (`provider.added`).

## Désinstallation

```bash
docker compose down
```

arrête l'assistant et supprime ses conteneurs et ses réseaux, **en gardant vos
données**. Pour supprimer aussi les données (irréversible : comptes,
conversations, coffre et sa clé maîtresse, autorité) et les images :

```bash
docker compose down --volumes --rmi all
```

Supprimez ensuite le dossier `domo`, et retirez de vos appareils le certificat
que vous aviez installé. Si vous avez fait des sauvegardes, cette commande
supprime la petite image qu'elles ont utilisée :

```bash
docker image rm alpine:3.24.2@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6
```

## Notes de sécurité

- **Le cœur ne voit jamais la valeur d'un secret.** La partie de l'assistant qui
  exécute la page et la discussion ne possède aucune clé. Les clés des modèles
  sont saisies dans le formulaire du coffre, conservées chiffrées par le
  gardien (gatekeeper), et ajoutées aux appels vers le modèle par le gardien
  seul. Si un fournisseur renvoie une clé entière, dans le corps d'une réponse,
  dans un en-tête ou dans un message d'erreur, le gardien la masque avant que le
  cœur ne la voie. Une clé découpée en plusieurs morceaux d'une réponse diffusée
  au fil de l'eau n'est pas encore masquée.
- **Le cœur n'a aucune sortie vers Internet** : il est sur un réseau interne.
  Seul le gardien joint les fournisseurs de modèles.
- **L'assistant n'agit qu'à travers des vérifications écrites dans le code.** Le
  modèle ne se voit proposer que les actions que le rôle de la personne permet,
  et chaque action qu'il demande est vérifiée à nouveau, selon ce rôle et les
  droits et limites du propriétaire, avant que quoi que ce soit n'arrive. Une
  personne limitée ne peut pas convaincre l'assistant de dépasser sa limite : c'est le
  code qui l'applique. Un oui vient
  toujours de la personne (un bouton, ou ses propres mots, lus sans le modèle qui
  discute avec elle), jamais du modèle. Un lien d'invitation, un mot de passe ou
  une clé n'arrivent jamais jusqu'au modèle, ni dans l'historique de la
  discussion, ni dans les journaux.
- **La page est faite pour votre réseau, mais elle écoute par défaut sur toutes
  les interfaces.** `DOMO_BIND_ADDRESS` vaut `0.0.0.0` tant que vous ne le
  changez pas : la page est donc joignable depuis tous les réseaux auxquels le
  serveur est connecté, pas seulement le vôtre. Les ports publiés par Docker
  peuvent contourner le pare-feu de l'hôte : une règle de pare-feu n'est donc pas
  un moyen sûr de cacher la page. Pour la restreindre, réglez `DOMO_BIND_ADDRESS`
  dans `.env` sur l'adresse du serveur sur votre réseau (par exemple
  `192.168.1.10`, ou `127.0.0.1` pour cette machine seulement), relancez
  `docker compose up -d`, et ne redirigez pas les ports sur votre box. La page est
  chiffrée (HTTPS) dans tous les cas.
- **Le code écrit par un modèle ne tourne jamais dans le cœur.** Les outils
  que l'assistant se crée tournent dans le conteneur `sandbox`, sans réseau, sans
  secret, avec un compte à eux pour chaque lancement (voir
  [L'assistant se crée des outils](#domo-se-crée-des-outils)). Les contrôles
  faits avant qu'un outil serve sont un filtre, pas la frontière : c'est le bac
  à sable. Le contrôle des imports a des contournements connus, listés dans les
  suites du projet, que le bac à sable contient. Les programmes hostiles des
  tests ont trouvé deux trous, fermés depuis : un lancement pouvait joindre la
  socket du lanceur lui-même (elle n'est plus qu'au cœur), et laisser de petits
  objets de mémoire partagée à un lancement suivant (le lanceur les retire
  maintenant à la fin du lancement). Ce qui reste : deux outils qui tournent au
  même moment et passent tous les deux outre le contrôle des imports pourraient
  se passer un tel objet pendant qu'ils tournent.
- Dans cette version, le cœur se connecte encore à sa propre base de données
  en tant que super-utilisateur de la base. Le coffre n'est pas dans cette base,
  mais un cœur compromis pourrait tout y modifier. La base des outils en a
  besoin aussi : le cœur y crée les rôles et les coins avec cet utilisateur (à
  séparer plus tard).
- **Ce qu'un outil peut voir de la base des outils.** Jamais les lignes de
  quelqu'un d'autre. Mais le catalogue de PostgreSQL montre certaines choses à
  tous les rôles : les noms des tables et des colonnes des autres coins, leurs
  tailles et leurs estimations, la définition de leurs vues, de leurs valeurs
  par défaut et de leurs contrôles, les tables en cours d'usage, et la taille de
  la base. Et un outil qui partage l'espace commun peut y faire échouer les
  écritures des autres (une valeur par défaut, un contrôle ou une politique de
  lignes qu'il y pose). Un
  outil ne lit rien de la base du cœur.
- **Ce que le micro entend.** Seuls les moments de parole quittent la page, par
  sa propre connexion chiffrée, et le son n'est jamais écrit ni journalisé, ni
  par le cœur ni par le service de parole. Ce qui est entendu reste à la maison
  sauf si le propriétaire permet autre chose. La phrase secrète ne fait que
  traverser le cœur en mémoire, vers le coffre, comme un mot de passe tapé ;
  elle n'arrive jamais à un modèle, une discussion, le journal d'audit ou les
  logs. Les réponses en son passent par le gardien sans être changées, mais
  surveillées : un secret de 8 octets ou plus trouvé entier dedans coupe la
  réponse (un plus court n'est pas cherché : il apparaîtrait par hasard). Le
  service de parole n'a pas de mot de passe : seul le gardien le joint. Voir
  aussi [SECURITY.md](SECURITY.md).

## Organisation du dépôt

| Chemin | Rôle |
| --- | --- |
| `packages/domo-common` | Modèles et fonctions partagés par les autres paquets |
| `packages/domo-gatekeeper` | Coffre de secrets chiffré et relais vers les fournisseurs |
| `packages/domo-core` | Interface web, discussion et actions de l'assistant (la maison, les mots, les demandes et leurs confirmations, les règles et les droits du propriétaire, les outils qu'il se crée et leurs contrôles), comptes et clés d'accès, journal d'audit |
| `packages/domo-sandbox` | Le lanceur du bac à sable : fait tourner un outil de l'assistant dans un processus à part, avec ses limites, et son analyseur |
| `packages/domo-speech` | Le service de parole à la maison : le son en texte, le texte en son, le son en empreinte de voix ; il ne garde rien, et mesure ses modèles sur sa machine |
| `deploy/` | Configuration du serveur web placé devant l'assistant |
| `compose.yaml` | L'installation (`compose.test.yaml` ajoute un faux modèle pour les tests) |
| `scripts/` | Contrôles d'hygiène du dépôt (données personnelles, licences des dépendances) |
| `tests/` | Tests de toute la pile, dans un navigateur (`e2e`) et dans Docker (`stack`) |

## Développement

Prérequis : [uv](https://docs.astral.sh/uv/) et Docker 28 ou plus avec Compose.

```bash
uv sync                           # crée l'environnement et installe tout
uv run pre-commit install         # active les hooks de commit et d'envoi
uv run pytest -m "not stack"      # tests unitaires
uv run playwright install chromium  # une fois : le navigateur des tests de bout en bout
uv run pytest -m stack            # la pile Docker et les tests de bout en bout (lents)
```

Lisez [CONTRIBUTING.md](CONTRIBUTING.md) (en anglais) avant d'ouvrir une pull
request.

## Licence

MyDomo est distribué sous [licence MIT](LICENSE).

Les dépendances tierces sont limitées aux licences permissives ; aucune
dépendance GPL ou AGPL n'est acceptée.
