# Un nom pour toute la maison — conception courte

Date : 2026-11-26. Petit changement, sur la 0.12.1.

## Pourquoi

Aujourd'hui chacun peut donner à l'assistant un nom pour soi (« Brenda »), mais pour tous les
autres il s'appelle « Domo », écrit dans le code. Tu veux pouvoir choisir une fois un nom qui
remplace « Domo » pour tout le monde.

## Ce que tu as décidé

- **Qui le change** : « Uniquement le propriétaire ».
- **Domo peut proposer** un nom ou te demander comment l'appeler (« Oui, il peu ») ; le nom ne
  change que sur ton accord.
- **L'ordre** : le nom que la personne lui a donné, sinon le nom de la maison, sinon « Domo ».
- Accepté d'un bloc (« C'est ok ») :
  - quelqu'un qui a son propre nom le garde partout ; Domo sait qu'il a les deux et répond aux
    deux ;
  - un invité, un enfant voient et entendent le nom de la maison, et peuvent lui donner le leur ;
  - rien du passé n'est réécrit ; Domo sait comment la maison l'appelait avant ;
  - tu le dis avec tes mots ; tu peux demander quel est son nom, le changer encore, revenir à
    « Domo » ;
  - refusés : le nom de quelqu'un de la maison ou d'une invitation en attente, un nom de plus
    de 40 caractères ;
  - la clé d'accès et l'application installée sur un téléphone montrent le nom de la maison
    (une application déjà installée garde l'ancien jusqu'à sa réinstallation).

- **Quelqu'un arrive avec le même nom que lui** (tes mots) : « Si une nouvelle personne arrive
  dans la maison avec le même nom que DOMO, domo peu proposer au propriétaire de changer son nom
  pour éviter la confusion, ou si le nom est que pour une personne, il le propose a cette
  personne uniquement ». Donc rien n'est refusé à la personne qui arrive : c'est Domo qui
  propose de changer le sien, à toi pour le nom de la maison, à la seule personne concernée
  pour un nom qu'elle lui a donné.
- **Après un non** (tes mots) : il repropose, « c'est oui si la confusion arrive souvent et que
  ça "agace" domo qui se fait réveiller inutilement trop souvent ». Sinon il n'y revient pas.

## Buts, et ce qu'on ne fait pas

- But : partout où le code écrit son nom pour quelqu'un, c'est le bon des trois.
- Pas dans ce chantier : le mot de réveil à la voix et sur le téléphone (ils liront le même
  endroit du code le moment venu) ; réécrire les anciennes discussions ; changer les noms
  techniques (`domo`, `domo-talk`).
- À ne surtout pas faire : qu'une autre personne que toi change ce nom, ou que Domo le change
  de lui-même.

## Façons de faire

1. **Retenue** (ton « Oui ») : le nom est un réglage de la maison, à côté de la langue et du
   fuseau horaire (`policy_settings`), et une seule fonction décide du nom à montrer. Aucune
   étape de base.
2. Écartée : une place à part dans la base. Rien de plus, et une étape de base à attendre.

## Pour le constructeur (mes choix, pas les tiens)

- Réglages `house.domo_name` (le nom) et `house.domo_name_before` (le nom d'avant, écrit par le
  code à chaque changement, « Domo » compris), dans `policy/settings.py`. Même règle que le nom
  par personne (`tools.profile.domo_name_from` : une ligne simple, nettoyée, 40 au plus) ;
  `null` ou vide = « Domo » à nouveau.
- Outil `house_name` (action `house.name_domo`) : le propriétaire seul, par son rôle, dans un
  tour à lui. Pas `house.set` : ce droit peut se donner, et tu as dit « uniquement ». Domo dans
  un tour à lui seul ne peut pas l'appeler. Refus si le nom est celui d'une personne ou d'une
  invitation (`household.service.name_in_use`). Entrée au journal, sans le nom (comme
  `profile.rename`).
- Une fonction `name_shown(calls_domo, house_name)` ; tous les lecteurs d'aujourd'hui passent
  par elle : pages (`web/templating.py`, `texts_middleware.py`, `base.html`, `message.html`),
  discussion (`channels/page`), programme sur ordinateur (`channels/api` : `domo_name` envoie
  le nom résolu, `domo-talk` ne change pas), mots laissés (`tools/notes.py`), demandes de Domo
  montrées à quelqu'un (`confirmations`), journal et exemples de droits (`audit.who.system`,
  `rights.example.as_domo`), clé d'accès (`RP_NAME`), application installée (`MANIFEST`).
- Consignes : une ligne « la maison t'appelle … » (donnée, pas instruction) pour tout le monde
  quand le nom existe, en plus de « ils t'appellent … » ; une ligne « avant, la maison
  t'appelait … ». La description de `domo_name` ne dit plus « chacun choisit le sien » : au
  propriétaire, `house_name` ; à un autre, que c'est le propriétaire qui choisit.
- Même nom qu'une personne : le code le constate (nom de la maison, ou nom donné par la
  personne du tour, égal au nom d'une personne active ou d'une invitation en attente, comparé
  comme `name_in_use`) et le dit à Domo en une ligne de consignes : dans tes tours pour le nom
  de la maison, dans les tours de la personne pour le nom qu'elle a donné. Domo juge quand et
  comment le proposer ; rien n'est changé sans un oui. Tant que ça dure, la ligne reste, et
  elle dit aussi : après un non, n'y revenir que si la confusion arrive vraiment souvent (on
  l'appelle pour rien, il répond à la place de la personne) ; il retient le non lui-même
  (sa mémoire).
- Restent fixes : le nom de Domo dans les faits lus par une limite (`policy.facts.DOMO_NAME` :
  une limite déjà écrite peut le comparer), l'image `icon.svg`, le titre interne de
  l'application.
- Tests : les trois cas de l'ordre à chaque lecteur ; poser, changer, relire, effacer ; refus
  (pas le propriétaire, Domo seul, nom d'une personne, d'une invitation, trop long, saut de
  ligne) ; le nom d'avant.

## Ce que ça coûte si je me trompe

- Un seul nom d'avant est gardé : après deux changements, Domo ne connaît que le dernier.
- Après un non, le code ne compte pas les confusions : c'est Domo qui juge, d'après ce qu'il a
  vécu et retenu, si elles reviennent assez souvent pour reproposer. Il peut mal juger (trop
  tôt, ou jamais).
- Une page de discussion ouverte pendant le changement garde l'ancien nom jusqu'au prochain
  chargement, si le plan ne trouve pas mieux.
