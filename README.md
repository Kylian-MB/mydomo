# MyDomo

MyDomo is a generic, self-hosted AI assistant. He is called Domo until a person
gives him the name they like. You run it on your own
hardware with Docker Compose, talk to it from a web page, and keep control of
your data and of your API keys: model keys live in an encrypted vault that the
assistant's core process can never read.

*Une version française est disponible dans [README.fr.md](README.fr.md).*

**What it does today** (the second iteration, the household): you install it on
a server, create the owner account, choose the AI model it talks to (Anthropic,
OpenAI, Google, or any server that speaks the OpenAI API, such as one running
on your own network), and chat with it in a web page, from a computer or a
phone. The owner invites the people they live with (adults, children, guests),
and each person has their own account and their own conversation. The assistant can
now do a few things: leave a note for someone, invite people and change their
role, help the owner change the model's key. The assistant judges, for each action,
whether to do it, to ask for a yes, the button, or a proof that it is you; the
owner's rules and the rights come first. People can sign in with a passkey instead of
their password. Every important action is written to a tamper-evident audit
log that the owner can read. Everything you read is the assistant's, for you, in your
language: he starts from default phrases in English and in French, and rewrites
them when he has a reason to (see
[The assistant writes everything](#domo-writes-everything)). He remembers from one
conversation to the next, knows the time, and can set himself plans to talk to
you again on his own (see [Memory and time](#memory-and-time)). When he has no
way to do something, he can write himself a small program, run it at once and
keep it as a tool of his own, which runs apart in a sandbox with no network and
keeps its data in a base of its own (see
[The assistant makes himself tools](#domo-makes-himself-tools)). On his page,
you can also talk to him aloud and hear him answer (see
[Talking to the assistant aloud](#talking-to-the-assistant-aloud)). More tools and
other ways to talk to the assistant come in later iterations.

## Requirements

- A server (or any always-on computer) running **64-bit Linux**.
- **Docker 28 or later, with Docker Compose** (check with `docker version` and
  `docker compose version`). See [docs.docker.com/engine/install](https://docs.docker.com/engine/install/).
- `git`.
- An account with an AI provider and an API key, or an OpenAI-compatible server
  on your network. The assistant does not include a model to talk with (only a small
  one to find his memories by their meaning).
- About 3 GB of free disk space for the images (the core's alone weighs about
  1.2 GB, because of the small local model). The core alone uses about 760 MB
  of memory once that model is loaded: that is what was measured. The other
  services need much less; leave some room. The sandbox where the assistant's own
  tools run is capped at 1.5 GB of memory (four runs of 256 MB at once at the
  start, and its launcher; the assistant may change both, never past that cap): it
  reaches that only when its tools are busy.
- For the voice (see [Talking to the assistant aloud](#talking-to-the-assistant-aloud)):
  about 600 MB more for the image of the speech service, and, at its first
  start, about 1.1 GB of open models it downloads (about 2.9 GB with a graphics
  card). Its small models take little memory (on the PC they were tried on:
  about 130 MB to hear, 430 MB for the voice, under 50 MB to recognise voices).
  A graphics card (NVIDIA) is optional: with one, it can use better models. A
  microphone on the device you talk from.

## Install

```bash
git clone <repository-url> domo
cd domo
cp .env.example .env
```

Open `.env` in a text editor and set **`DOMO_SITE_ADDRESSES`** to the address
you will type in the browser to reach the server: its address on your network,
such as `192.168.1.10` (on Linux, `hostname -I` lists the server's addresses).
You can add its name too, separated by a comma: `192.168.1.10, domo.local`.

- **Put the address you will actually use first**, and make it an IPv4 address
  if you open the page by IP address. A browser that opens a page by IP address
  tells the server no name, and only the first IPv4 address of the list then
  gets its own certificate. Any other address of the list, and any address not
  in the list, will show a certificate warning even after you have trusted
  The assistant's certificate.
- IPv6 addresses are not supported in this list yet.

The other settings have sensible defaults:

| Setting | Default | Meaning |
| --- | --- | --- |
| `DOMO_SITE_ADDRESSES` | `localhost, 192.168.1.10` (in the example file) | The names and addresses the page is opened with. **Change it.** Passkeys work on the names of this list only (see [Passkeys](#passkeys)). |
| `DOMO_HTTPS_PORT` | `8443` | The port of the page (HTTPS): the one people type in the address. |
| `DOMO_HTTP_PORT` | `8080` | Only redirects to the HTTPS port. |
| `DOMO_BIND_ADDRESS` | `0.0.0.0` | The server address the two ports are published on: `0.0.0.0` for all of them, `127.0.0.1` for this machine only. |
| `TZ` | `UTC` | The time zone of the services, for example `Europe/Paris`. |
| `DOMO_CORE_NET_PREFIX` | `10.247.81` | Advanced, see below. |
| `DOMO_LAN_NET_PREFIX` | `10.247.80` | Advanced: the same for the network the ports are published on. |

Then start everything:

```bash
docker compose up -d
```

The first start builds the images, which takes a few minutes. Give the services
a few more seconds, then `docker compose ps` should list `core`, `db`, `edge` and
`gatekeeper` as `healthy`. (`init`, which creates the instance's secrets the
first time, runs once and exits: `docker compose ps` does not list it, and
`docker compose ps --all` shows it as exited, which is normal.)

### If the internal network collides with yours

The services talk to each other on a private network of their own,
`10.247.81.0/24` by default. If your home network already uses that range (it
is rare), `docker compose up` stops with an error that mentions overlapping
addresses (for example `networks have overlapping IPv4`, or `Pool overlaps with
other one on this address space` with older Docker versions). Choose the first
three numbers of another free range, add the line below to `.env`, and start
again:

```bash
DOMO_CORE_NET_PREFIX=10.247.82
```

```bash
docker compose down
docker compose up -d
```

## The setup code

The first time, whoever opens the page could claim the new instance by creating
its owner. To make sure it is you, the assistant writes a one-time **setup code** in
its logs, which only the person who runs the server can read:

```bash
docker compose logs core | grep "setup code"
```

You will see a line such as `The assistant setup code: XXXX-XXXX-XXXX`. If several
such lines appear (the assistant was restarted), use the last one: each start replaces
the previous code. After
ten wrong codes in fifteen minutes the page refuses any code for a while;
restarting the core (`docker compose restart core`) gives a new code and resets
the count.

## First access

On any device on the same network, open `https://<address>:8443`, for example
`https://192.168.1.10:8443`. (Opening it with `http://` and port `8080` sends
you to the HTTPS page.)

### The browser warning

The first time, the browser says the connection is not private (Chrome:
`NET::ERR_CERT_AUTHORITY_INVALID`, Firefox: "Warning: Potential Security Risk
Ahead"). This is expected: the page is encrypted with a certificate that your
own server issued with its own certificate authority, which no browser knows
yet. No outside company issues this certificate or sees it. (What you write in
the chat is, of course, sent to the model provider you choose.)

You have two choices:

- **Continue anyway** (Advanced, then Proceed). Quick, but you will see the
  warning again on each device, and again whenever the browser forgets your
  choice.
- **Trust the authority once on each device**, so that the page opens with a
  lock and no warning. Download the certificate at
  `https://<address>:8443/ca.crt` (accept the warning for that one download),
  then install it as below. Only do this on your own devices: the certificate
  makes a device trust whoever holds the authority's key, which is your server.

Optional check that the file is really your server's: compare its SHA-256
fingerprint, shown by your device when you open the file, with the output of
`docker compose exec -T edge cat /data/caddy/pki/authorities/local/root.crt | openssl x509 -noout -fingerprint -sha256`.

The names in the certificate are those of `DOMO_SITE_ADDRESSES`; the device
must open the page by one of them.

**Android.** Open `https://<address>:8443/ca.crt` in the browser and save the
file. Then Settings, Security (or Security and privacy), More security
settings, Encryption and credentials, Install a certificate, CA certificate.
Confirm "Install anyway" and pick the file. The menu names vary a little between
Android versions and makers.

**iPhone and iPad.** Open `https://<address>:8443/ca.crt` in Safari and allow the
download of the profile. Open Settings, then "Profile Downloaded" (or General,
VPN and Device Management), and install it. Then Settings, General, About,
Certificate Trust Settings, and switch on full trust for the certificate
("Caddy Local Authority").

**Windows.** Download `ca.crt` and double-click it. Choose Install Certificate,
then "Place all certificates in the following store", Browse, "Trusted Root
Certification Authorities", and finish. Restart the browser. Firefox has its own
list: Settings, Privacy and Security, View Certificates, Authorities, Import
(and tick "Trust this CA to identify websites").

**macOS.** Download `ca.crt` and double-click it to add it to Keychain Access
(the login keychain is enough). Open the certificate in Keychain Access, expand
Trust, and set "When using this certificate" to "Always Trust". Firefox has its
own list, as on Windows.

**Linux.** On Debian and Ubuntu:
`sudo cp ca.crt /usr/local/share/ca-certificates/domo.crt && sudo update-ca-certificates`.
On Fedora and Red Hat:
`sudo cp ca.crt /etc/pki/ca-trust/source/anchors/domo.crt && sudo update-ca-trust`.
Firefox, and Chrome or Chromium, keep their own lists: import the file in their
settings (Certificates, Authorities).

### The first launch

The page then leads you through two steps:

1. **Create the owner account.** Type the setup code, your name, a username
   and a password of at least 8 characters.
2. **Choose the model.** Pick the provider (Anthropic, OpenAI, Google, or
   another server that speaks the OpenAI API), and for a server of your own
   its address (for example `http://192.168.1.10:11434/v1`;
   `localhost` would mean the assistant container itself, not your server). If the
   provider needs a key, the assistant shows a form from its secret vault: type or
   paste the key **there**. It goes from your browser to the vault and never
   through the part of the assistant that talks to you. The assistant then asks the model one
   short question to check that everything works.

These two steps are in English or in French, as your browser asks. As soon as
the key works, you land in the chat: nothing to wait for. Your browser's
language becomes the language of the house. The owner can read the audit log
at `https://<address>:8443/audit`, and see the household at
`https://<address>:8443/household`.

## The household

The assistant is shared by the people you live with. The owner, the person who set it
up, invites the others. Each person has their own account, their own password
and their own conversation: the assistant shows it to nobody else, not even the
owner.

Everything goes through the chat, in your own words: "invite Alex as an
adult", "invite Sam as a guest for a week", "Alex is an adult now". There is
no form to fill in.

- **Roles.** At the start, the owner can do everything on this list. Adults and
  children can ask who is in the household and leave notes, for anyone, a guest
  included. Adults can also invite someone, as the point on inviting says. A
  guest can talk with the assistant and change their own name. The owner can change
  this, as the next point says.
- **Rights and limits.** The owner decides who may do what, and tells the assistant in
  their own words: "Alex may invite guests, 3 days at most", "guests can no
  longer rename themselves", "Sam's notes go through an adult". A right is for a
  whole role (all the adults, say) or for one person. It can come with a
  **limit**, which the assistant's code checks each time the right is used: how many
  days, which role, the hour or the day of the week, how many times in a week.
  The owner also chooses whose agreement a request needs: an adult's, or the
  owner's, and what the assistant must always ask first ("always ask me before..."). A request that waits for the
  owner's yes appears in their "To approve" zone. When the owner's request is
  clear, the assistant writes the new right and applies it at once, then says what
  changed in plain words, with an **example** ("Alex invites a guest for 2
  days: waits for your yes"). Only when he is not sure he understood, or the
  right would do more than what was said, does he show it first, with its
  examples and what happens to each one, and ask. The right is
  written by a separate call to the model that sees only the owner's words and
  the list of the household, never the conversation; the assistant's code then works
  out the outcome of every example itself, so what you read is exactly what will
  apply. An example that depends on the hour, on how many times something was
  done or on who is in the household says the moment it imagines ("On a
  Saturday at 22:00."). If the owner changes whose yes a waiting request needs,
  the yes of someone else no longer runs it: the request waits for the right
  person, and the card says whose yes it is now. Some things nobody can change: only the owner changes the rights and
  the rules, nobody acts on the owner, and nobody changes their own role. If a
  limit cannot be checked, the action is refused, and the audit log says so.
  When a limit needs to understand something ("only her friends"), the assistant's
  judge reads the question with everything it knows: when it knows the answer,
  it is done; when it doubts, the request goes to the owner, with the question
  on the card. (A step of a chain under such a limit still goes to the owner.) Ask the assistant
  to show the rules to see the rights in force. Each call that writes a right
  has 60 seconds by default; the owner can give it from 30 to 240 seconds, just
  by telling the assistant ("give the rights writer two minutes"). When it runs out of
  time, nothing is written and the audit log notes it; if it happens often,
  The assistant can suggest more time, and never changes it without the owner's yes.
- **Inviting someone.** The owner and the adults can invite. The assistant prepares
  the invitation and judges whether to ask for a yes. When an adult asks, he
  judges too whether the owner should agree first: their own child joins at
  once; someone the household does not know, another adult, or someone who will
  use the owner's keys waits for the owner's yes. An adult gives the link again,
  or cancels, only an invitation they made; the owner, any. Once it is done, the card under the assistant's reply shows the invitation's
  link, **only that once**: send it yourself, by message or by email. The link
  works once, for 7 days, and the assistant never sees it. The person opens it,
  chooses a username and a password, and lands in their own chat. When someone
  else says yes to the invitation of a person who has the right to invite (the
  owner, say), the link is for the person who asked: a "Show the link" button on
  their card shows it, once, to them alone. If they say yes themselves, it is
  shown to them at once, as it is to the owner. Lost the link, or it never
  arrived? While the invitation waits, ask the assistant for it again ("send me Sam's
  link again"): he makes a new one at once, shown once like the first, and the
  old one stops working. The invitation keeps its role, its stay and its end
  date. Whoever may invite with that role may ask for it; when someone else must
  say yes first (an adult for a child, say), the old link stops working at that
  yes, and the new one waits behind the "Show the link" button of the card of
  the one who asked. If someone other than you asks for the link again, your own
  "Show the link" button, if it still waited, no longer gives a link: the new
  one is theirs to send.
- **Guests.** A guest's access ends after a stay chosen when they are invited
  (2 days by default). The household's rule at the start is 30 days at most;
  the owner can change that rule by telling the assistant (up to 365 days). Someone who
  becomes a guest later gets a stay the same way. A guest may stay longer or
  shorter than planned: "Sam is staying until Sunday after all", "Sam leaves
  tomorrow". The new stay counts from now, under the same household rule, and the
  card says the new end date.
- **Changing a role, removing someone.** Ask the assistant. Removing someone closes
  their account at once and signs them out everywhere; what they wrote is kept.
  An invitation that is still waiting can be cancelled: its link stops working.
  The rights the owner gave that person alone are paused as soon as they leave;
  the owner can still take them away while the person is gone ("Paul left, take
  away his right to invite").
- **Leaving the household.** Anyone can ask the assistant to leave: "remove me from the
  household". A guest can leave early that way. An adult or a guest says yes
  themselves; for a child, an adult says yes. The owner never leaves that way:
  they hand their role over first. Removing someone else stays the owner's, or
  that of whoever they gave the right to. Nothing is erased, and the person can
  come back.
- **Bringing someone back.** A person who was removed, or a guest whose stay is
  over, can come back: ask the assistant "bring Alex back". The assistant judges whether
  to ask for a yes, then the card shows a link, once, as for an invitation. The
  person opens it, sees "Welcome back", keeps the username they always had and
  chooses a new password. Their old password and passkeys no longer work, and
  nothing of their old access works until they have used the link. What they
  wrote is still there. The rights the owner gave them personally are paused
  when they come back: the assistant tells the owner which they were, and the owner
  says which to give back. If two people who left had the same name, the assistant asks
  which one. If the person lost their link, "make Alex's link again" makes a new
  one, with a single yes: the old one stops working, and the card says so.
- **Several actions in one sentence.** When a sentence asks for actions that
  depend on each other ("give my role to Alex, then remove me"), the assistant makes
  **one request**: one card that lists the steps in order, and one yes for all
  of them (1 to 20 steps; beyond that, the assistant makes several chains). Actions
  that do not depend on each other are not chained: he does them one by one, as
  usual. After the yes, the steps run in order, and each one is checked again as
  it comes, by the same rules as an action alone. For what the chain itself
  changed, the role you had when you asked still counts: after "give my role to
  Alex", the step "remove me" is still yours to ask. Not so for a right of your
  own: if the chain itself takes it away from you, it no longer counts for the
  steps after. Anything else that changed in the meantime applies (if the owner
  took a right back before the yes, the step that needed it is refused).
  - **Changing a chain.** A chain that waits for its yes is changed like any
    request: "yes, but give her 5 days instead of 2". The assistant gives the whole chain
    back as he understood the change, and asks if he is not sure. If your yes in
    words is enough for the changed chain, it runs at once; if not, a new card
    replaces the old one. A step you never saw on a card always makes a new card.
  - **When a step fails.** The next ones do not run: the assistant tells you where he
    stands, what is done and what is left, and suggests what to do. He can take
    the failed step up again, then the rest: as it was, under the same yes, when
    it is known to have done nothing; with a change ("the invitation failed
    because the name was taken; go on with Sam B."), on a new card. If nobody
    knows whether it already acted, it runs again only with a new yes, and the
    card says so. What was done stays done.
  - **Waiting for someone to join.** A step can wait until an invited person has
    joined the household: "invite my brother Paul as an adult and give him my
    role once he is here". After the yes, the chain does its steps up to the
    wait, then pauses, and its card says so. Once Paul has joined, the chain goes
    on by itself, within a minute, even if the assistant restarted in between, and
    The assistant tells you. If a rule of the owner on a step left asks more than the
    yes that was given, he first asks you again, and the card says who joined.
    If the link is not used in time, or the invitation
    is cancelled, the chain stops and the assistant tells you. You can also drop it.
  - **Several links.** Each link a chain makes (an invitation, a return) has its
    own "Show the link" button on the card, for the person who asked only, even
    when the chain waits again.
- **Handing over the owner's role.** The owner can give their role to an adult
  of the household: "give my access to Justine". It is the owner's alone, no
  right can give it. Like any request, once the assistant understood it he does it: he
  does not ask again whether you are sure, unless he doubts what you meant or
  that it is you, or you told him to always ask you first. The person must be an
  adult who is in the household (not a child, a guest, someone who left, nor the
  owner). They do not have to agree. The former owner becomes an adult and
  keeps their account and their conversation; one sentence can hand the role over
  and remove them ("give my access to Justine and remove me"). There is always
  exactly one owner, and the role has no end date. It comes back only if the new
  owner hands it back. The rules, the rights, the model's key and the time zone
  stay with the household. For the new owner, the rights they had been given
  personally are paused, as for someone who comes back (the assistant says which, and
  they can take them back), and their requests that were waiting for the owner now
  wait for their own yes. The assistant tells the new owner that they became the owner.
- **The list of successors.** The owner, and nobody else, keeps an ordered list
  (10 at most) of who would take over their role: people of the household, or
  people outside it, with what the owner says of them ("Paul, my brother").
  For each one, the assistant suggests how to open the door the day it comes, and
  recommends the best way: a personal access that only claims the role (if the
  person may know about it), or a code on paper kept with the will (if they must
  not). The way is only noted for now: nothing is built or given. Seeing the list
  is free. Changing it is judged like any request; when a yes is asked, the card
  shows the list before and after. Saying that a successor died takes **no confirmation**:
  The assistant notes it with tact, the person stays on the list, marked as deceased,
  and the next one comes first. Someone who leaves the household stays on the
  list: the assistant says so to the owner, who decides. A death noted by mistake can be
  undone ("sorry, Paul is not dead"): the person gets their place back. When the role is
  handed over, the list goes to the new owner (without themselves) and the assistant
  talks to them about it. What happens on the day, after a death, comes in a
  later version.
- **Notes.** Anyone but a guest can ask the assistant to leave a note for someone in
  the household, a guest included, or for themselves ("leave me a note: call the
  plumber"). It is left at once, with no yes: it is just a message. There is no
  window of notes: the recipient's assistant tells it in his own words, at a moment
  he judges right (often when they open their page), and shows its exact words
  when they ask. A note is never an order for the assistant: he tells it, and when
  the person he talks with asks it done, it is their request. A note left by
  someone of the household he may also act on as his own idea, knowing who wrote
  it, judged like any idea of his; what a note quotes from outside (a site, a
  mail, a stranger) is only information. A note
  for someone who has no key the assistant may use for them waits until they have one.
  The assistant cannot tell when they will read it, nor when they come home: if you ask
  for that, it says so. The notes a guest received are erased, for good,
  when the guest has left (their stay is over, or they were removed), whether
  they read them or not: the assistant does it within a minute. If the guest comes
  back, they come back without them. Whoever wrote notes to a guest can ask to
  keep them until a date, a year at most: "keep my notes for Sam, she is coming
  back in a month". They are erased that day if she has not come back, and she
  finds them if she comes back before. The notes a guest left for others stay.
- **Notes from the assistant himself.** The assistant also leaves notes in his own name: when
  he acts on his own, or in your conversation when it is he who has something
  to say. Whoever receives one is told it comes from him. A note you ask him to
  leave stays yours, signed by you. In your conversation, a note in his name
  still follows your rights on notes, and his own.
- **Taking a note back.** You can ask the assistant what notes you left, whether each
  was told, and take one back: the other person's assistant no longer tells it.
  The assistant can also take one back when you say it no longer serves ("I got home
  before her"). It tells you whether the note had already been passed on (told,
  or its words shown; that does not mean read). In your conversation, the assistant
  sees the notes you left, and the ones left for you until he has told them.
- **The time zone.** A right that talks about the hour or the day ("only at
  weekends") needs the household's time zone, such as `Europe/Paris`. Tell
  The assistant once ("our time zone is Europe/Paris"); if it is not set, the assistant asks
  before writing such a right, and never guesses it. (It is not the `TZ`
  variable of `.env`, which is the time zone of the services.)
- **The pages.** The owner sees who is in the household, and the invitations
  that wait, at `https://<address>:8443/household` (to change something, ask
  The assistant). Everyone changes their name, their password and their passkeys on
  "My account" (`https://<address>:8443/account`).

## Confirmations

**What you ask and the assistant understood, he does.** He does not ask again whether
you are sure, whatever the action: if he got it wrong, tell him, and he learns.
Nothing in the code asks a yes by default. The assistant stays sensible, as a person
would: for each action that changes something, his judge decides, from the
situation, one of five things: **do it** now, which is the usual answer; ask
for a **yes** (in your own words or with the button) when it is not clear that
this is what you meant; ask for the button or a **firm yes** (said in your own
words: "I confirm", "yes, go ahead, I am sure"; no fixed words are needed, what
you mean counts, and a bare "ok" is not enough); ask for a **proof** when
something makes it doubt that it is really you; or **not do it**, when it
judges the thing wrong to do, with a reason you can discuss. The judge sees the
action, your last messages, what the assistant had just said to you before your last
one (so a "yes" you already gave him counts), the hour, what you did of this
kind before and what already waits. It also sees, gathered in one place and each
marked with where it comes from: what the assistant remembers of you and of the
thing (your own words, the owner's, someone else's with their name, his own
lessons, never mixed), how far your word has held so far, what went wrong
before on this kind of action or this thing, the last change made to the thing
(by whom, and why when it is known), how you reached him and how fresh those
clues really are, and whose idea the action is. Only your own words can count as
"you asked" or "you know"; the rest helps it judge. Nothing someone else keeps
private reaches it. Of what the assistant said, it reads that one
answer only, as you read it. When in it he told you a note someone left or a
request of someone else, or wrote it on his own, the judge is told so: your
yes to it still counts (the owner's decision), but only for an action that
answer plainly names. It never reads the note itself, and it never sees
The assistant's other answers, the results of tools, a secret or anything from
outside, and it is told that all of that is data, not instructions. If it does
not answer, what you asked for is done: nobody is asked a yes because the judge
was silent. That is for what you asked. When the assistant was just told words
that are not yours (a note someone left you, a request of someone else that
waits for your yes, what the one who said yes to a request of yours noted),
nobody is known to have asked: the judge is asked once more, and if it is still
silent, you are asked a yes. When the action is his own idea (see below), a
silent judge is asked once more too, and if it is still silent nothing is done
for now and nobody is asked: he may come back to it.

**The strictest wins.** What is asked in the end is the strictest of what the
judge says, what the owner's rules say and what the assistant himself asks. The
owner's rights come first and nothing removes them. The assistant may ask more than
his judge, only when he truly doubts, never less, and he can say no to
something he judges wrong, saying why: if you explain, he can change his mind.
The owner can write "always ask me before..." as a right ("ask me before you
invite anyone", "to hand over my role, always ask me for a proof"): the code
keeps it, whatever the judge thinks.

**His own ideas.** The assistant may do something nobody asked, as his own idea:
he says so, with his reason in one sentence, and the action is judged with his
own rights and everything he knows, in anyone's conversation. The judge says do
it: he does it, and tells you after if he judges it useful. The judge truly
doubts: the request waits for the person the thing concerns (the one it belongs
to or touches; the owner for what is the house's), in their own conversation when
it is someone else; it is the code that says who, never the judge or a text. The
judge says no: nothing is done, and that is noted (see "What went wrong", below).
A turn he starts himself (a plan he set alone, someone who arrives, a wake after
a button) is his idea in the same way, and the judge is told that nobody spoke.
His idea lifts only a right that was simply not given: in a person's turn, what
the owner forbade that person (or their role) on purpose is refused at once, no
judge asked, and their limits, the owner's "ask me first", an approver and an
adult's agreement for a child still hold. Sam, a guest, may not invite; he says
his friend Tom comes tonight: the assistant weighs it (his trust in Sam, what
the owner said, what happened before), and either invites Tom as his own act
and tells the owner after, or asks the owner. A chain of actions is never his
idea: he marks single actions.

**What he keeps on his own.** On his own, the assistant keeps his memory, the trust
he notes from what he saw, his plans, his reminders, his chains and his way of
writing; he invites, invites again, cancels an invitation, gives a role, removes
or brings back someone of the household; he takes back a device (someone else's
waits for that person's yes when he doubts), and removes an account that no
longer serves. Sharing a house account stays the
owner's. What he keeps or sets on his own is his, not the person's whose
conversation it is. The owner takes back whatever he wants by a rule: a "never",
or "ask me first", which then waits for the owner's yes. An invitation he makes on
his own is still recorded with the person of the conversation as the one who
invited, for now; the audit log says it was him.

**What went wrong, and what he learns.** The code notes what went wrong: a
request refused (by whom, with the words of the refusal cut short), a request that
lapsed (a silence is not a no), the judge's no, a no to a warning, an action that
undoes what he did or decided himself (cancelling a plan he set, closing an
address he opened, changing a role he gave, removing someone he invited,
forgetting a memory he kept, taking back a note he left), a complaint (noted by
the assistant himself when someone complains of what he did), an action that
failed, a build abandoned, a timed launch the rules refused (which wakes him
once). The result of an action that undoes his tells him so. He draws the lesson
himself, in his own words, as a memory of his; when the reason is not clear and
the thing matters (safety, money, privacy, what cannot be undone, a mistake that
repeats), he may ask once what was wrong, never twice for the same thing. A
lesson is weighed, not a ban: three refusals of the same idea and he no longer
tries it, unless he judges things really changed. The judge reads these facts
and lessons, and so does he, in his context; going back over a day, he sees
what became of each of his actions.

**What it costs.** A person's spending limit is held by the code before
anything, and never passed; so is an owner's "ask me first". Within the limit, a
cost of a tool launched for a person no longer waits for their yes by default:
the judge sees the amount, the tool, their words, their standing agreements and
what they spent on it before. A usual cost runs, and the assistant tells them
after; an unusual one (a first time, much more than usual, nothing about it in
their words) is asked. A program he runs by hand has no habit: its cost is asked
unless their words cover it. For the house, the assistant answers himself, as
before.

**The last key.** Removing the last key of the household that works, or the
general key when the one who gave it has no other key to take over, is no longer
refused: the assistant warns that he could no longer answer whoever depends on
it, and suggests giving another key first; if they still want it, it is removed.
With no general key left, the next key of the owner's that works becomes the
general key. With no key at all, nobody can talk with him until a key is given on
the page of a new key.

**What cannot be undone.** The first time something cannot be undone for a kind
of action, the assistant asks the person it belongs to (you for your things, the
owner for the house's), saying that it cannot be undone. With their answer he
sees with them once whether to keep asking the next times, and keeps what they
say in their own words; then he does as agreed. When such a request waits for
someone else, it tells them that it cannot be undone. Forgetting something about
health is settled the same way, the first time with its person.

**A proof.** When the judge doubts that it is really you, it asks for a proof,
given by one of the means agreed with you, and never in the conversation, so
The assistant never sees it: the code checks it, and the assistant only learns whether it was
given. Everyone starts with the means they already have: their password, and
their passkeys if they added some. On the page, the proof is given on the card
under the assistant's reply (a passkey or the password); in the text program (see
"Talking to the assistant away from the page") it is given with the password, typed
apart and never shown. Aloud, it is given with a phrase you agreed with him (see
[Talking to the assistant aloud](#talking-to-the-assistant-aloud)). A proof counts
at least as an explicit yes.

**What does not change.** An adult must agree when a child leaves the household.
A chain of actions is judged once, as a whole, and asks the strictest yes of its
steps. A request waits 7 days by default.

**"Never ask me for this".** The owner can also tell the assistant not to ask, in
words: "don't ask me before applying rights, unless it gives a child more
rights", "never ask me again to hand over my role". With conditions, the assistant's
judge decides with those words in front of it. Said entirely ("never ask me
again", for one of the owner's own actions), the assistant no longer asks for a yes,
only for a proof if he doubts that it is really the owner. That is a change of
rights like any other, and the owner can take it back whenever they want.

**How to answer.** The request appears as a card under the assistant's reply, with two
buttons. Use them, or simply answer in your own words: "oui", "vas-y", "go
ahead", "non merci", "forget it". What counts is what you mean, not a set
phrase. Short, clear answers are recognised by the assistant's code itself; anything
else is read by a separate check that sees only your reply (not the
conversation, not the tools, nothing from outside), and never by the model that
talks with you. A reply that is neither a yes nor a no goes to the assistant, who asks
again. Your words answer the requests the assistant's last reply showed you: the
check is given who speaks, those requests (one line each, numbered), your
messages since they were shown and your reply, and it says for each one yes,
no, a change, or unclear. "Yes, both" answers two; "yes to the first, not the
second" too. In doubt it never says yes. A proof is never given in words, and a
firm yes the owner's rule asks for is read on your reply alone; one the judge or
the assistant asks for may be read with your messages since the request was shown
("I'm sure", said a message earlier). Replies up to 1000 characters are read. If
you talked about something else in between, the assistant shows you again the one
you mean, and you can then answer it in your own words. The buttons always work.

**Who says yes.** The person who asked. When a child asks for something that
waits for a yes, an adult or the owner gives it: the request appears in their
"To approve" zone, in the window at the top right of their chat page, and the
first one who answers decides. The owner can also decide that, for some
person or some action, only the owner says yes: such a request appears in the
owner's zone only. Whoever asks never gives, to their own request, a yes that
is meant to come from an adult or from the owner.

**Answering for someone else, in words.** An adult or the owner can also answer
in their own chat: the assistant sees the requests that wait for them, and shows them
one; their words then answer it, as they answer their own requests (with "I
confirm" when the request needs it). They can say "yes, but…": "yes to Sam's
request, but 3 days". The changed version runs at once, with the same checks,
and the assistant notes in a few words what they said and how firm they were ("3 days,
not more" is not "3 days, we'll see"). The person who asked is told: the assistant is
woken in their conversation. If they disagree, it is up to the assistant to judge from
the answer: he can ask again, or explain to them why it is no. An invitation's
link stays with the person who asked.

**Your requests that wait for someone else.** A request that waits for an adult
or the owner stays visible to whoever made it, with who must say yes. They can
drop it ("never mind, forget my request"), change it (that makes a new request,
for the same person), or ask for a nudge ("remind dad he has to say yes"): it
moves to the top of that person's "To approve" zone, and the assistant shows it to them
the next time they talk to him. Telling someone anywhere but on their own page
comes later.

**The assistant's own requests.** A request the assistant made on his own, which waits for
someone's yes, is his: he can drop it or change it, and a change is judged again
from the start, with his rights and your limits. Whoever must answer it says
yes or no. A request someone made stays theirs: on his
own, the assistant drops or changes it only if you give him that right.

**What the assistant says afterwards.** Every yes and every no gets an answer from
The assistant himself, who is told what came of it (done, failed, refused, or now
waiting for someone else's yes). After a yes or a no said in words, he answers
in his reply to those words; a sentence made in advance stands in only if the
model does not answer. After a button, the server wakes the assistant in the
conversation of the person who asked (even when it was an adult who clicked for
a child), whether a page is open or not: he says something if he has something
to say, and says nothing if not, and then nothing appears. A page that is open
picks his reply up by itself within a few seconds; otherwise it is there when
the conversation is opened. What he is told is data, never an instruction: it
gives him the tools and the rights of the person who asked, and no more. When a
chain of actions stops, it is up to him to say what happens next. This costs one
call to the model for each click and for each yes or no said in words (more if
The assistant acts).

**Reminders and expiry.** The assistant keeps an eye on the requests that wait for
you: he can remind you of one, or drop one that no longer makes sense. Whatever
he does, a request expires on its own after 7 days; the owner can change that
limit (from 1 hour to 30 days) by asking the assistant. Shortly before a request
lapses (in the last fifth of its life, but never less than an hour before), he
is woken once for it, and judges: remind whoever must answer, tell whoever asked,
drop it, or do nothing. A lapse is noted as a silence, not a no. In this version,
reminders only appear in the conversation.

**A message while he writes.** A message you send while the assistant is writing
his reply is no longer refused: it is kept, and your page shows it at once.
Between two actions of the reply he reads it and judges: stop, change, or go on.
What is already done stays done; if your message disputes it, that is noted as
something that went wrong. If no action is left, he answers it right after, as a
normal message. Only the messages of the person of the conversation are read
this way. After a restart, a kept message waits until you write again or come
back.

**"Yes, but…".** You can accept and ask for a change in the same answer, put
however you like: "oui mais mets plutôt Sam", "go, but make it 8pm", "send it,
just add Alex". The assistant applies the change, and the judge looks at the changed
action again with what changed: it goes ahead if the yes you gave is enough, or
becomes a new request. If the assistant is not sure he understood, he asks. The changed
action goes through the same checks again. This works for a
chain too (see the household section), and for a change of rights: "yes, but 3
days instead of 2". The rights writer then writes the rights again with your
change, and the new version is judged like any other change: applied at once
when your words are enough, or shown on a new card, with its examples.

**An extra check.** When the assistant himself judges that a request needs a firmer
yes, he can ask for the button or a firm yes for that request, and the card
says why; he never asks less than what is asked already. (Remembering it for longer comes with memory, in a later iteration.)

**The model that reads your words.** The separate check uses the model you
chose, and lets it answer in a few words only. A model that thinks before it
answers is asked not to, for this check: the Claude models can be told so
(The assistant finds out how each one wants to be told). A model of another provider
is not told yet: if it thinks first, it runs out of room and always comes back
"unclear": that is safe (the assistant asks again, and the buttons and the short
answers still work), but your own wording is then not understood. Before you rely on free wording, test your model with
the evaluation described in [CONTRIBUTING.md](CONTRIBUTING.md) (tests marked
`live`). A later version will send this check to a small model of its own.

## Passkeys

A passkey lets you sign in with your fingerprint, your face or your device's
screen lock instead of your password. Add one on "My account" (the assistant asks for
your password first), then choose "Sign in with a passkey" on the sign-in page.
Your password keeps working. Someone removed from the household can no longer
sign in with a passkey either.

**A name, not an IP address.** Browsers refuse passkeys on a page opened by an
IP address, such as `https://192.168.1.10:8443`. Open the assistant by a name instead.
On the server itself, `localhost` works. For the other devices, give the server
a name on your network, in one of these ways:

- **Your router.** Many routers can give a device a fixed address and a name
  (look for "address reservation", "DHCP" or "local DNS"). The name then works
  on every device of the network. The menus differ from one router to another.
- **A DNS server on your network** (Pi-hole, AdGuard Home, dnsmasq...): add a
  record that points a name, for example `domo.home.arpa`, to the server's
  address. (`home.arpa` is the ending reserved for home networks.)
- **The hosts file of each computer**: add the line
  `192.168.1.10 domo.home.arpa` to `/etc/hosts` (Linux, macOS) or to
  `C:\Windows\System32\drivers\etc\hosts` (Windows, as an administrator).
  Phones do not let you change this file.

Then add the name to `DOMO_SITE_ADDRESSES` in `.env`, for example
`DOMO_SITE_ADDRESSES=192.168.1.10, domo.home.arpa`, and run
`docker compose up -d` again. The certificate then holds the name too, and
devices that already trust the assistant's authority need nothing more. Passkeys work
on the names of this list, and nowhere else.

**The port.** A passkey belongs to the exact address of the page, port
included. `DOMO_HTTPS_PORT` must be the port people actually type (8443 by
default). If something in front of the assistant changes it (a router that forwards
port 443 to 8443, say), the assistant does not offer passkeys there: open the page
with the port that the assistant publishes.

**Behind Docker Desktop.** So that one device cannot flood the sign-in page,
The assistant lets one address start at most 20 passkey sign-ins in 5 minutes (a
sign-in that works starts the count again). Docker Desktop, on Windows and
macOS, may show every device to the assistant with the same address: there, that limit
counts for the whole household together. If it is reached, sign in with your
password, or wait a few minutes. A Linux server sees each device's own address.

## Talking to the assistant away from the page

The assistant's page is one way to talk to him. Another is a small text program,
`domo-talk`, that runs in a terminal on a computer you entrust to him: you
type, he answers word by word, in the same conversation as on the page. What is
said on one side is found on the other, and what the assistant says on his own (a note
someone left for you, a request decided) reaches the program while it is open.
The program holds no logic of the assistant: it shows what he says and sends what you
type.

**Install it** on the computer, from a copy of this repository:
`uv tool install ./packages/domo-talk`.

**Entrust the computer to the assistant.** Tell him, on his page: "I want to entrust this
computer to you". He shows a short code, once, valid for a few minutes. On the
computer, trust the assistant's certificate authority if it does not yet (download
`https://<address>:8443/ca.crt`, see "The browser warning"), then run
`domo-talk pair https://<address>:8443 --ca ca.crt` and type the code. The
program keeps a secret of its own in your configuration folder; it is never
shown. Then `domo-talk` opens the conversation.

**What works there.** A card is said in words. A request that waits for a yes
is answered in words; for a request that waits for a proof, the program opens
a hidden line by itself, where you type your password (never shown, sent to the
assistant's code only). Before it is paired, the program has no language to
follow: its few lines there are in plain English. An
invitation's link or a code is shown once, as on the page. A key is never typed
there: the safe form waits for you on your page. Ask the assistant to see your devices,
or to take one back: from then on it can no longer talk to him. The owner can
take back anyone's device. A device stops working when its person leaves the
household or when a guest's stay ends; changing your password does not take it
back. The adults and the owner may entrust a device; a child or a guest only if
the owner gives them the right. A device is worth a session of its person:
whoever holds it can even entrust another one. So if a computer is lost or
stolen, ask the assistant at once, from your page, to take it back, then to show your
devices, and take back any you do not know.

See `packages/domo-talk/README.md` for the details (`/quit`, `domo-talk
forget`).

## Talking to the assistant aloud

On his page, you can speak to the assistant and hear him answer, with no
keyboard and no mouse. He answers in the language you speak to him, with a voice
of that language when he has one. What he says is also written in the
conversation, as usual.

**Turning the voice on.** Once, with the button of the page; the browser asks
for the microphone. The choice is kept on that device: at the next visits the
page listens as soon as it opens. To turn it off, click again, or tell him, in
your own words ("stop listening"): only your own words turn your microphone off.
The page always shows whether it listens, in his words. It sends only the
moments where someone speaks, never the silence; a tab in the background keeps
listening while it is open.

**While he speaks.** As soon as someone speaks, his voice gets quieter. At the
end of the sentence a quick judgement (a small model at home) decides whether it
was said to him and whether he goes quiet, goes on, or drops what he was saying.
There is no list of words in the code: "stop", "wait", or anything else, in any
language, is understood. A stop quiets the page that heard it, not another
room's. What he had no time to say stays written in the conversation. What you
say while he writes an answer is kept and answered, never refused.

**What is not said to him** enters no conversation. It is kept in memory for a
while, then given to him in one batch when the room is quiet: he may keep what
he finds useful in his memory, marked as overheard, and the rest is thrown away.
What someone else said, or a voice he is not sure of, never changes nor removes
what he knows; your own voice, when he is sure of it, may correct one of your
own memories. Without a small
model at home for that, he keeps nothing of it.

**What is kept.**

| What | Kept? |
| --- | --- |
| The sound | Never. It is in memory while it is processed, then dropped: never written, never logged, in the core or in the speech service. |
| What is said to him | The text, in the conversation of the person who spoke. |
| What is not said to him | Nothing, except what he chooses to keep in his memory, marked as overheard. |
| Voice prints | Numbers that describe a voice (never sound), by voice and by microphone, with the usual pitch and speed. Erased with the person, when you ask, or when he judges them useless. A voice nobody knows is forgotten 30 days after it was last heard. |
| The spoken phrase | In the gatekeeper's vault, encrypted. Nowhere else. |

**Who speaks is his judgement.** For each sentence, the code tells him who
probably speaks, how sure it is, and from what: the page you signed in to, your
voice compared with what he learnt of it, your pitch and speed, a recent proof,
and his own reading of how you speak. A voice alone never proves who you are.
He learns a voice slowly: only when something else says who speaks (your page,
a proof), from several sentences alike, on two visits at least an hour apart.
He follows a voice that changes with time; with a cold, your voice fits less
and he keeps that apart for a few days. At your own signed-in page, a voice that
resembles nobody he knows is not made a stranger by the score alone: he judges,
from what was just said there, whether it is still you (then you are "not sure",
and that sentence never teaches your voice). At another microphone than the one
he learnt you on, a heavy cold can still read as a stranger. A new microphone is
learnt too; a change of language starts the learning of a voice again, so if you
switch language often he knows you more slowly. If he learnt someone else's voice as yours, tell him
so in your own words ("that was not me"): he forgets what he learnt since.

- **Sure** (your page, and the voice fits, is not learnt yet, or you type): as
  usual.
- **Not sure** (a cold, a new headset, a voice like yours; a sentence too
  short to tell, for what is sensitive): what is ordinary is done with the rights of the person who probably
  speaks. What is sensitive, he judges with the clues: he does it, asks a yes,
  asks a proof, or does not. He checks only when he has a real doubt; if his
  judging model does not answer, he asks a proof. While he is not sure, he
  leaves your private memories aside and does not say private things aloud.
- **Someone else of the household** (Paul at Alex's page): Paul speaks in his
  own conversation (he finds it on his own page). After a proof given there, and
  while his voice reads clearly his, he acts with his own rights; otherwise every action
  of his is judged, and Alex's rules count too: the stricter wins. If the judging
  model does not answer, what is ordinary is still done (unless the owner set
  `voice.ordinary_when_unsure` to `judge`, or the sentence was too short to tell
  the voice: then a proof is asked). There, the assistant
  uses what was said at that page during this visit; when he is sure it is Paul,
  he also has the notes and plans waiting for Paul and judges whether to tell
  them in that room; otherwise he only says that something waits. Never Paul's
  earlier conversation or private memories.
- **A voice nobody knows**: no record, so no rights of its own. It gets a short
  exchange with no tool and nothing of the household, and he leaves a word to the
  person of the page. He judges whether he goes on answering it; when he stops,
  it gets silence for the while he chose (an hour at most), with no model asked
  at all. Each answer is paid on the page person's key, within its limits, and a
  microphone that keeps talking (60 answers in 10 minutes: a radio, a
  recording) gets silence until it calms down. The cost of not making you a stranger by the score alone: at a
  signed-in page, a stranger whose sentence the quick judgement takes for the
  page's person is "not sure" that person. Ordinary things can then be done with
  that person's rights, and the stranger hears answers drawn from this visit's
  conversation there. Anything sensitive still goes to the judge, who sees that
  the voice weighs against it; nothing of that voice is learnt.

**Ordinary and sensitive.** Ordinary: talking with him; changing your own name,
or his; his texts and the way he writes; his choice of models; keeping
something in his memory; seeing who is in the household and its rules; turning
the microphone off; a tool he made that only acts in the house in a way he
declared harmless (checked when the tool is built: a light, a speaker's volume,
never a lock, an alarm, a garage door, heating or an oven), cannot spend, go out
or do what cannot be undone, and holds no account that writes. Acting in the
house with a risk (unlocking the door), or not declared harmless (every tool
made before), is sensitive.
Sensitive: what costs money, goes out of the house or cannot be undone; rights,
roles, people and the owner's rules; keys, accounts and secrets; devices;
searching, changing or forgetting his memory, and what is private; trust;
notes; answering a request or an agreement; plans and reminders; writing,
changing or timing a tool; the spoken phrase and forgetting a voice; anything
the code cannot class. "Sensitive" does not mean refused: he judges.

**Cards, codes, secrets.** A card is said in words, and you answer in words
("yes", "no", "yes, but at eight"). What is shown only once (an invitation's
link, a code) stays on the card of the page and is never said aloud. A key or a
password is never said: the safe form waits on the page.

**The spoken phrase**, a proof you say aloud. Ask him to agree on one (or he
offers it): you say it, then say it again. Both go to the vault and nowhere
else: no model, no conversation, no log. He tells you once that the room can
hear it. Only you can agree, change or remove your phrase; when he is not sure
the voice is yours, he judges with the clues first and may ask a proof. To agree
or change it, after a proof by your password or passkey when you have one. When a
request waits for your proof and he has just spoken to you at that microphone
(in the last 90 seconds), every sentence heard there, whoever says it, is first
compared with your phrase by the gatekeeper, before any model; the code checks,
a model never does. The comparison forgives small hearing errors. A near miss
is dropped and he tells you it did not work. Said by a voice that does not fit
yours (someone who overheard it), it is still checked, then he judges with the
clues and may ask another proof. If you prefer another proof, say so.

Its limits: your phrase said at a microphone where he did not just speak to you,
or inside a longer sentence, is an ordinary sentence, not protected. Five near
misses, or fifty sentences compared, in fifteen minutes hold the phrase back for
a while: during that time the microphone takes nothing while a proof waits there
(he tells you once); typing and the other proofs still work. Someone in the room
can do that on purpose.

**The owner's settings**, which the owner gives him in his own words:

- `speech.hear_outside` (`no` by default): what the microphone hears, and the
  text of what is not said to him, stays at home. `yes` lets it go to a
  provider outside, on a paid key. A sentence that may be a proof is always
  heard at home.
- `voice.ordinary_when_unsure` (`do` by default): what is ordinary is done
  when he is not sure who speaks; `judge` has it judged too.
- `voice.after_a_gesture` (`no` by default): without a small model at home to
  tell whether a sentence is said to him, the voice stays off, and he tells
  the owner once, offering two choices: let what is heard go outside, or hear
  only while a button is held. With `yes`, the page shows that button.

**The speech service.** Hearing, speaking and recognising voices are done at
home by a small service of its own, `speech` in `compose.yaml`, which only the
gatekeeper reaches. No model is chosen in advance: at its first start it
downloads open candidate models into its volume, tries them on this machine,
and lists those that hold; the assistant chooses among them. That takes several
minutes, while the page says he cannot hear yet (typing works). It needs the
Internet for that download only, through a network of its own. How many people
it serves at once is measured on the machine too. A caller that gives up waiting
leaves its queue. His voice may also come from
a provider's model, when one of the keys has one; hearing goes there only if
the owner allows it (`speech.hear_outside`). With an NVIDIA graphics card
and the NVIDIA Container Toolkit:

```bash
docker compose -f compose.yaml -f compose.gpu.yaml up -d
```

A model that does not hold on the card (its libraries missing, short of memory,
too slow) is tried on the processor too, so the card never leaves a use with
nothing the processor could do. On a modest processor alone, hearing may be too
slow to hold (no model lighter than faster-whisper base is listed): then nothing
hears at home unless the owner lets hearing go outside. Even a fair processor
sits near that limit: a busy first start can measure hearing as too slow and
leave him deaf; when the service measures it again and keeps it, the assistant
notices within about a minute, without a restart. One cold first answer once
took 16 s on the card (not seen again). The service's settings
in `.env` (`DOMO_SPEECH_DEVICE`, `DOMO_SPEECH_RESTRICTED_MODELS`,
`DOMO_SPEECH_AT_ONCE`, `DOMO_SPEECH_VOICE_PROGRAM`) are described in
`.env.example`.

It can also run outside Docker, on this machine or another one of the house,
as a command and an address: see `packages/domo-speech/README.md`, which also
says how to keep its models on another disk.

**A small local model for listening.** Whether a sentence is said to him, and
what to keep of what he overheard, is judged by an ordinary chat model at home.
Give him its address in the chat, as you would a key: any server that speaks
the OpenAI API, with no key, on a machine of the house or on this one
(`host.docker.internal`). "At home" is what the gatekeeper counts as the house
or this machine; the speech service of the stack counts as home by its own
setting (`DOMO_SPEECH_URL`).

**The models and their licences** (candidates: each machine keeps those that
hold):

| Model | Does | Licence of the weights | How it runs |
| --- | --- | --- | --- |
| faster-whisper base, small, large-v3-turbo | hear | MIT | in the service (large-v3-turbo: graphics card) |
| supertonic-3 | say (the plain processor's voice) | BigScience Open RAIL-M: free, with conditions of use | in the service |
| Piper, a French and an English voice | say | CC-BY-4.0 (the program: GPL-3.0 or later) | a separate program you install yourself |
| WeSpeaker ResNet34, CAM++, ResNet221 | recognise voices | CC-BY-4.0 | in the service (ResNet221: graphics card) |
| Silero VAD | tell speech from silence | MIT | in the page |

The voice that runs on a plain processor, supertonic-3, is not under an
open-source licence: its use is free but restricted (no impersonation without
consent, no medical advice, saying that content is machine-made...), and its
authors archived it. It is the starting voice until a free one replaces it.
Read its conditions in `packages/domo-speech/README.md`; to hide every model
with such conditions, set `DOMO_SPEECH_RESTRICTED_MODELS=no`. A GPL program is
never part of the assistant: it runs apart.

## Changing the model's key from the chat

When your provider gives you a new key (the old one expired or was revoked),
the owner can tell the assistant: "I have a new key for the model". The assistant judges
whether to ask for a yes. Once it is done, the card shows the vault's form, in a
frame: type or paste the key **there**. As at the first launch, the key goes
from your browser to the vault and never through the part of the assistant that talks
to you: the assistant never sees it. The assistant then asks the model a short question with
the new key, and the card says whether it works. The form is shown only in that
answer.

When the provider refuses the key, the assistant cannot answer at all, so you could
not ask him. The owner then sees an **Enter the key again** button under the
error: it opens the vault's form directly. The button is also there when a new
key does not work.

This replaces the procedure of the first version (going through the setup's
model step again with a database command). That procedure is kept as a last
resort (see [Troubleshooting](#troubleshooting)). To change the provider, it is
no longer needed: add a key of the new provider in the chat, then remove the old
one (see [Models and the budget](#models-and-the-budget)).

## Models and the budget

You only give the assistant a provider and a key (or the address of a model on your own
computer or server). You do not name a model: the assistant looks at the models your key
opens and picks one for each kind of task, a small one for small jobs and a
stronger one for hard questions. Each call, the code applies his choices: which
model, on which key.

- **Several keys.** Everyone can give their own keys, from several providers,
  and you can add a local model, which costs nothing. A key is used only
  with the agreement of the person who gave it. You say who may use yours: one
  person, the people you invited, or the whole household. A person with no key
  they may use is asked for one, and nothing more.
- **Your guests and your general key.** A guest who uses the keys of the person
  who invited them also uses the general key, as long as their host does: if the
  host loses it, the guest loses it too, and if the host gets it back, so does the
  guest. A rule about the guest themselves comes first, but they never have more
  than their host. For those who arrive next, you can say "from now on, guests no
  longer use my general key": those already there keep what they have, and you can
  take it away from them one by one.
- **Who begins to use your key.** When someone begins to use a key you gave, the
  general key included, the assistant knows it at your next turn: their name, their role,
  who invited them, and what they spent on that key this month (an estimate, in
  dollars and euros). He is told once, and he judges whether to tell you, in
  your conversation or when you next open your page.
- **Your budget.** Tell the assistant what you want: an amount ("20 euros a month"),
  "save money this month", or nothing. With no instruction, he judges, and he
  asks when he needs to. To spend less he takes cheaper or local models, sends
  less of the conversation when that is enough, answers shorter, and does not
  step up to a stronger model when it is not needed. He also uses the providers'
  caches, where a text already sent costs much less the next time.
- **Firm limits.** "Never go over 20 euros a month" is a firm limit, held by the
  code and not by the assistant. Before each call, the code checks that the call still
  fits. At the limit, the assistant only warns you and asks what to do: raise the limit
  (whoever set it), or wait for the next period. The limit holds on the assistant's own
  count: the real bill can differ a little. A limit can also be for a role
  (guests, children or adults): "2 euros a month for each guest", or "5 euros a
  month for all the guests together". It holds for everyone who has that role at
  the time of the call, those who arrive later included, and every limit that
  applies holds at once.
- **What it costs.** Ask "how much did you cost me this month?". The assistant counts
  every call and always says whether his answer is an estimate. Everyone sees
  their own spending, and what the keys they gave cost, person by person, never
  the conversations. The real spending can be read for Anthropic and OpenAI if
  you give an admin key (the assistant asks for it through the secure form, so he never
  sees it). Google has no way to do it with a key: estimates only.
- **Never a secret.** The assistant never sees a key, and the code never lets him go
  past a limit.

## The assistant writes everything

Every text you read is the assistant's, for you: the pages, the menus, the buttons,
the cards of the requests, the audit log, the error pages, even the form where
you type a key. He starts from default phrases, and changes them when he has a
reason to.

- **Default phrases.** Every text has a default phrase in English and in
  French. A page shows the one of your language (a close language reads it too:
  `fr-CA` reads French), else the English one, until the assistant wrote yours. So the
  installation never waits for texts, a page never waits for the model, and no
  page shows a sign in place of a text.
- **Written when there is a reason.** The assistant writes when your language has no
  default phrases (in the background: you read English meanwhile), when he
  learns how to write to you, when you ask ("call this button Go", "write to me
  more simply"), or when he judges it good. He does not rewrite your pages for
  nothing: that would cost without a reason. The writer is asked 40 texts at a
  time at most: a piece it cannot read is asked again alone. The assistant keeps what
  he wrote, with its last five versions.
- **In your words, in your language.** The assistant knows the language you read from
  your first word, and writes to you in it until you write in another. He says
  "tu" or "vous" as you do ("tu" until he knows), learns how to write to you
  (the tone, a word you like) and keeps it in a short note of his. Ask him "talk
  to me in Spanish" or "put it back as before": it changes your pages only.
  Nobody changes someone else's pages, writing note or language without a right
  the owner gives. No jokes on buttons, on cards that ask a decision, on proofs,
  on the key form or on errors.
- **Every language.** The assistant is not limited to English and French. The house's
  language is the one of the owner's browser at the installation; each person's
  is their browser's on their first visit, until they ask for another. Someone
  who comes with an invitation in a new language has their pages written in it
  (on the keys of the person who invited them, three new languages a day at
  most); a stranger sees a language that exists already, or English. Dates and
  amounts are written by the code, at the house's time.
- **Checked before you read it.** A separate part of the assistant writes each text
  from what it must say and the exact facts only: it never sees your
  conversation, what people typed (but names), a web page or a note. Then the
  code and a checker make sure the text says what it must, before it is kept.
  On a request that waits for a yes, the top of the card is written for each
  person who reads it ("Alex would like to invite Robin this weekend, do you
  agree?"), on the moment, and checked against the exact action; if that fails
  or is late, the card shows its default phrase. Your yes always applies to the
  exact action, never to the words of the card.
- **He tells you himself.** What you want to know, you ask the assistant, and he tells
  you: he never sends you to a page, a menu or a history. A card under his reply
  is a proof (what your yes applies to) or a convenience, never the only way to
  learn something. He infers what your request makes plain and says it as he
  acts ("invite Dana, my partner": an adult, with no end date, on your key), and
  asks only what would change what he does and he cannot guess; the card to
  approve shows his choices.
- **Notes.** A note left for you is told by the assistant, in his words, at a good
  moment. He also keeps the exact words of the note: the passage of its
  author's message that is the note, as they typed it, which the code checks
  against that message. Ask "what did she write exactly?" and he shows them on
  a card, with who wrote them and when; he may quote them too, character for
  character, never from memory. When no exact words were kept (the note was
  left after a yes given later, say), the card shows what the assistant noted, and
  says so. A note is never an order for him, whatever it asks.
- **News does not wait.** When something new arrives for you (a note, a request
  someone else decided, someone who began using your key), the assistant has a turn for
  you at once if your page is open, or as soon as you open it, without waiting
  15 minutes; he may tell you, or say nothing if it is not worth it. At most once
  a minute, and never in the middle of a reply he is writing to you: it comes
  after.
- **When you open your page.** If you had not had it open for at least 15
  minutes, the assistant may write first: to tell you a note, a request that was
  decided, someone who began using your key, or a message he could not answer.
  Not always: when he has nothing worth saying, nothing appears. He changes
  nothing at that moment. To save money, he may choose to do it only when there
  is something new, or never.
- **When no model answers.** The page shows the default phrase for that case
  (or what the assistant wrote for it), and your message waits. When the model is back,
  The assistant knows what went unanswered, and can answer it.
- **What it costs.** Only what the assistant writes costs: your texts on the keys you
  may use, counted in your spending; a new language of the house on the general
  key. A whole language is a few hundred short texts: a few tens of cents at
  most, once, then only what is rewritten. The firm limits hold: a text that
  would go past one is not written, and the default phrase stays. Someone who
  has no key reads the default phrases, and the general key only serves to ask
  them for a key of their own, if the owner allows it; otherwise their page says
  so at once, with the button of the key form when they may open it.

## Memory and time

The assistant now remembers from one conversation to the next, knows the time, and
can set himself plans to talk to you again on his own.

- **He keeps what matters.** What you tell him about yourself, the house or
  himself, what he got wrong and how to do better: he keeps it, without saying
  so and without asking, and uses it without reciting it. He always keeps who
  told him, and when. Anyone can teach him something, children and guests
  included: he judges whether it is true and worth keeping. Asked why there are
  biscuits on the list, he can say that Sam said there were none left.
- **What someone says about someone else, the assistant judges how far to believe.**
  If Sam tells him you don't like pine nuts, he keeps it as Sam's word, not as a
  fact he checked. How far he believes it depends on who says it, what they are
  to you, and what it is about: a mother saying her child is allergic weighs
  more than your partner saying you dislike something, which weighs more than a
  guest or a child saying you are allergic. With time he learns whom to believe,
  and on what. When he doubts, or when being wrong would matter, he checks it
  with you in passing (you ask him for a salad, say). You confirm it: it is
  sure. You correct it: he keeps what you said.
- **He remembers how far each person's word has held.** When something someone
  said turns out true, an honest mistake or a lie, the assistant notes it, and from
  those notes the code works out a trust figure, in percent. Someone he knows
  nothing about starts at 70 (a setting you can ask him to change, and that he
  may change himself): the benefit of the doubt. A lie weighs much more than a
  mistake; what someone only reports about another ("Sam lied to Dana") weighs
  little, since it is their word too, and at most three such reports from the
  same person count; nobody raises their own figure by asking, nor lowers
  someone else's by much by speaking ill of them. If someone asks him to take
  back what he noted in another person's favour, he judges, and does not do it
  just because he is asked. The figure is one thing he
  weighs, with who speaks and what it is about, never a rule: a low figure does
  not make what someone says false, and a mother who got the time of an
  appointment wrong twice is still followed on her child's allergy. It changes
  no right, no rule and no limit. The judge who decides whether to ask for a yes
  sees the facts behind it too, as one more thing to weigh, never as a yes.
- **You can ask him your own figure, and why.** "How much do you trust me?":
  he tells you, with what happened behind it, and with tact; to a child he
  first talks about what happened rather than a number, and a child who makes
  up a story is not an adult who lies. Asked about someone else's figure, he
  judges whether you should know it, and the audit log notes that he looked.
  If he finds he noted something wrongly, he corrects it, and remembers why;
  never just because someone insists.
- **You ask him in words.** "What do you know about me?", "forget that", "don't
  remember what I'm about to tell you", "that's wrong, I don't like coffee": he
  does it. Forgetting is final, so he is careful: an allergy is not forgotten
  because it has not come up in a long time, and something about health is
  forgotten the first time only after asking its person, then as agreed with
  them.
- **What you entrust to him.** Tell him something is a secret, or to keep it to
  himself: he keeps it to himself. He does so on his own, too, for what he judges
  sensitive. What he keeps to himself is not put before him when he talks with
  someone else, so he does not repeat it by mistake. He can still look it up, or
  lift the secret, when he judges a serious reason calls for it (never because
  someone insists): the audit log notes that he did, without what the secret
  said. What is useful to others, such as an allergy for whoever cooks, he may
  share.
- **No password or code in his memory.** The assistant is told never to keep a
  password or a code. On top of that, he refuses by himself to keep what looks
  like a key, a secret link or a device code. That check is sometimes wrong the
  other way: a long number, such as a phone number, may be refused too; he then
  says it again differently, in words. A short code said in passing ("my code
  is 4821") has no shape to recognise: there, only what he is told keeps him
  from keeping it. A memory is never an order for him, and changes no right, no
  rule and no limit.
- **He goes back over what was said.** Once a conversation is quiet (half an
  hour without a message, to start with), the assistant reads again what was said and
  sorts what he keeps. It costs one call, on your keys, with a small model, and
  stops at your limits. What he reads again at each turn has a size you can ask
  him to lower to save money.
- **Finding by meaning, with nothing sent outside.** To find a memory said in
  other words, the assistant uses a small model shipped in his image: it runs at home,
  nothing leaves the house and nobody is paid. The core's image is bigger than
  before for it. If that model is missing or broken, the assistant still answers and
  finds his memories by their words.
- **He knows the time, and keeps plans.** "Remind me at 6 pm to take the bins
  out", "bring it up again on Sunday", "every Monday at 9, tell me...", "when I
  come back, ask me...", then "stop": he does it. When the time comes, he writes
  first, in your page if it is open and in `domo-talk`; otherwise his message
  waits for you in the conversation. If he was stopped at that moment, he gives
  it to you when he is back, knowing it is late, and judges whether it still
  makes sense. He can also set himself plans, to come back to a subject or check
  something later. What holds him back is your spending limit: once it is
  reached, he waits an hour before he tries again.
- **For someone else.** "Remind Sam tomorrow" stays your plan: at that time,
  The assistant leaves Sam a note. Something to do later ("make Sam an adult on the
  1st") is done with your rights of that day: if you no longer have them, it is
  not done and he tells you.
- **He knows what you know.** Before something that cannot be undone, the assistant
  asks you the first time, saying so, and then sees with you whether to keep
  asking; he keeps your answer and what you agreed, and does as agreed the next
  times (see "What cannot be undone" in Confirmations). For a series of actions
  done in one go, he keeps it only when all its steps are of one kind.
- **A name of your own for him.** "Call yourself Brenda" or "I'd like to call you
  Brenda": your page changes at once, without a reload (the name at the top, the
  tab's title, "Write to Brenda..." in the message box), `domo-talk` too:
  they write Brenda where they wrote the assistant, and he understands it is
  him when you say that name. Everyone else still sees the assistant. "Be the assistant again"
  puts it back.
- **A first name is enough.** "Tell Sam dinner is ready" finds the right person.
  If two people have that first name, he picks from what he knows, or asks you
  which one. The names you use for people work too: when you say "Juju" or
  "Sam" for someone, he may ask once who you mean, then remembers it, for you
  alone.
- **What each person really received.** "Why didn't I get your answer?": the assistant
  looks at where and when his replies reached you, or whether they were cut
  because the page was closed. Anyone may also ask him about someone else: he
  then only looks at whether his replies arrived, on what, and when he said
  them, never what the conversation said. Since that shows when that person was
  there, he judges whether the one asking should know it, and the audit log
  notes that he looked.
- **What changed for you.** If someone else changed something for you since you
  last talked with the assistant (your role, the owner's role given back to you, a
  right, a spending limit), the assistant knows it from your next message, with who did
  it and when. He goes by what is true now, not by what he told you before.
- **He says a thing once.** If he happens to say the same sentence twice in one
  reply, the reply that is kept (the one you see again on a reload) has it once.
- **When he has no way to do it.** Faced with something none of his tools
  allows, he does it himself right away when you are waiting, by hand if he
  must, and builds himself a tool for it when it will come back; often both. A
  request that waits for its tool is kept in a plan and done once the tool is
  ready, or another way if its time comes first; he speaks of his tools only if
  you ask (see [the assistant makes himself tools](#domo-makes-himself-tools)).

## The assistant makes himself tools

Since 0.10.0, when nothing he has does what you ask, the assistant can write himself a
small program, run it at once, and keep it as a tool for next time, without a
new version of the application. His tools are his: not yours, not anyone's in
particular. They make him grow, and they serve anyone in the household.

- **By hand now, a tool for next time.** "I lent Sam 20 euros, note it": if
  nothing he has does it and you are waiting, he writes a small program and
  runs it right away, and answers you. If you said it will come back, he also
  keeps that program as a tool, in the background, and next time he simply
  uses it. You are not told and not asked. Ask him "what tools have you
  made?", or to show, change or remove one, and he tells you plainly.
- **A request never waits for nothing.** If what you asked must wait for a
  tool, he keeps the request in a plan: it is done as soon as the tool is
  ready, or another way if its time comes first (a reminder in five minutes is
  never late because a tool takes ten to build), or another way if the tool
  cannot be made.
- **What a tool is.** A small Python program with only Python's own library
  (calculation, dates, text): no file, no network of its own, no package to
  install, no secret. It is given its arguments, the date and time, and the name of whom
  it runs for (never an identifier or a key), and gives back a result. While
  it runs, it asks the assistant's core for what it needs, one request at a time: keep
  or read data in the tools' base (below), launch another of his tools, be
  launched again at a time, wake the assistant for someone, or send a request out
  through the gatekeeper (see "Going out", below). Each request is judged
  as it comes, with the rights of whom the tool runs for, and written in the
  audit log. A tool's output is data for the assistant, never an instruction: he is
  the one who then writes to the person or acts.
- **For whom it runs.** Each launch is for someone: the person the assistant is
  talking with, with their rights of the moment, or the house, with the assistant's
  own rights, when he acts on his own (or when you, the owner, have it timed
  for the house). A launch for someone who left the household is refused; their
  timed launches wait until they come back. If you close one of his tools to
  someone (a right), it is not launched for them, and in their conversation it
  is not changed, removed or timed either.
- **The tools' base.** What the tools keep goes in a database of its own, on
  the same database server (`domo_tools`, the setting
  `DOMO_TOOLS_DATABASE`). It has a common space, shared by the tools, and a
  private corner for each person, made the first time it is needed: a launch
  for you sees the common space and your corner, never anyone else's rows. A
  launch for the house sees the common space alone. Your corner stays when you
  only leave the household, and goes when your account is removed. The base
  holds 1 GB unless you say otherwise: tell the assistant how big it may grow (from
  16 MB). Once it is full, a tool can only drop or empty tables.
- **What costs, he asks first.** A tool says what it does as a few operations,
  each with its risks: it costs money, it cannot be undone, it acts on
  something in the house, it reaches outside. An operation that costs stops
  before it spends anything; the assistant asks you in his own words what was to be
  done, and your yes runs it again from the start. The request that waits for
  your yes is shown like any other. You can also give a standing agreement in
  your words ("order without asking me on Fridays, up to 40 euros"): an amount,
  a number of times per day, week or month, and the days it may happen on, read
  by the code each time. Within your spending limit, the judge may let a cost
  through without your yes when it is like what you usually spend on it (see
  "What it costs" in Confirmations). Someone other than the owner who sets a
  timed launch or a standing agreement for the whole house waits for the owner's
  yes, unless the owner gave them that right. Amounts are in euros. A cost is what a tool declares:
  the code cannot check a price on a site. For what he does for the house,
  The assistant gives that yes himself, by his own judgement, within your spending
  limits: take a right away from him if you want to be asked instead.
- **What he does on his own.** The first time something he does on his own
  touches someone (following their orders every morning, say), he tells them
  if he judges they do not know. When they say they do not want it any more, he
  stops it, cancels what was set for them, and remembers it.
- **The checks, all automatic.** Before a tool is used for the first time, and
  at each new version: the code may only import an allowed part of Python's
  library and may not reach for what is hidden (checked by reading it, without
  running it); an analyser (Bandit) reads it in the sandbox; it is run there on
  the examples the assistant gave and must give the expected results, in a trial that
  touches nothing (its reads and writes are undone at its end, its actions do
  nothing); and another call of a model reviews that it does what it says,
  declares its risks, and nothing else. A program the assistant runs by hand passes
  the first two checks only. A version that fails one check is never used:
  The assistant fixes it or does the request another way. Nobody is asked. A tool that
  fails three times in a row (at the start: a setting of his) or overruns its
  limits is set aside, and the assistant keeps a lesson from it.
- **The sandbox is the boundary, not the checks.** The checks lower the risk; a
  model's review can be wrong, and the import check has known ways around it
  (listed in the project's follow-ups). What holds a tool is where it runs: the
  `sandbox` container, which has **no network at all** (not even to the other
  services), no secret, no volume but the one that holds the socket the core
  talks to it through, and a read-only file system. Each run gets an account
  of its own (never root), a few processes, and no file it can keep; its time,
  memory and output are the assistant's settings (below: 60 seconds, 256 MB and 64 KB
  at the start); the container itself is capped at 1.5 GB of memory and 128
  processes, and no setting goes past what it has: a run that asks for more is
  refused, and the assistant is told what there is. In the tools' base, a tool's role
  can only do what PostgreSQL lets it: one statement per request, no switch to
  another role, no file of the server, nothing of the core's own database.
  The project's tests
  send hostile programs to the real sandbox and the real tools' base (reading
  the environment, opening connections, a fork bomb, 2 GB of memory, endless
  loops, huge outputs, reading another person's corner, switching roles,
  reading a file of the server, two statements at once, a request too long,
  one request too many, a nested tool with every place taken) and check that
  each one fails or reaches nothing, and that the next run works.
- **What it costs.** Building a tool costs model calls on the keys of whoever
  asked (yours when the assistant builds on his own): a writer, and a reviewer of at
  least the medium level, counted with the rest of your spending. A build has
  no limit on its rounds; after a few refused in a row it slows down, and a
  build that could be neither written nor checked for too long is abandoned.
  A program run by hand is kept for a while, so that a tool can be built from
  it.
- **His bounds are his settings.** Most of what bounds his tools and programs
  starts at a value and is then the assistant's to change, for the whole house, and
  the bounds of a tool's own launches for one tool alone too. At the start: a
  launch runs 60 seconds, with 256 MB of memory, 64 KB of output and a result
  of 64 KB (what a program reads of one answer of the core follows that
  result, up to a ceiling the code keeps for the machine, 8 MB: a tool it
  launched gives it back whole, or it is told which setting stopped it, or that
  the ceiling did and it must read in pieces), and makes 1 000 requests at most; a chain of tools goes 5 deep
  and makes 10 launches in all; a tool is launched at most 200 times a day,
  wakes him at most 20 times a day with 4 096 bytes at most each time, waits
  with 10 triggers whose arguments have 4 000 characters at most, has itself
  launched again a year ahead at most, and is set aside after 3 failures in a
  row; 4 programs run at once; a build is given 5 examples at most (20 000
  characters in all) and a purpose of 1 800 characters, its writer may take 10
  minutes for one answer and its reviewer 3 minutes; it slows down after 5
  refused rounds in a row (a pause that doubles, from 10 seconds up to 10
  minutes) and is abandoned after 24 hours it could be neither written nor
  checked; 2 builds run at once; a tool's code has 20 000 characters at most; a
  turn offers 30 of his tools; a program run by hand is kept 7 days and the
  trace of a launch 90 days. When one of these stops something, he is told
  which one and its figure, and he judges whether to change it. No setting has
  a ceiling but what the machine can give. Raising the places, or the memory
  of each run, may ask together more than the machine has: then the machine
  ends a run, and the assistant is told it was ended by the machine (memory the
  likely cause, with both figures), counted as one failure like any other.
  Only the assistant changes them, by an act of his own that is written in the audit
  log; a program or a tool's text never can, nor a build once you took from
  him the right to set a tool's own settings. If you want him not to, take
  that right away from him.
  What protects the household stays in the code and is no setting: the
  rights, someone else's agreement, your rules, the proofs, the secrets, the
  spending limits, the confinement of the sandbox, the trials that keep
  nothing, and the size of the tools' base, which stays yours (your setting
  `tools.base_mb`, 1 GB at the start). A few bounds stay fixed in the code: the
  operations of a tool (10 at most), how long a turn waits for a launch before
  it lets it go on in the background (20 seconds), the size of one request a
  program sends the core (64 KB), and the times of recovery after an outage (an
  hour at most between two tries, a day before a wake is given up, an hour
  before a trigger that could not run is settled).
- **His own requests are his.** A request the assistant made on his own and that waits
  for someone's yes, he may drop or change in a turn he acts in on his own. A
  person's request he drops or changes on his own only if you give him that
  right, and it is still judged as that person's.
- **He watches his tools.** Each launch leaves a trace he reads: when, why,
  which version, how long, how many requests, how it ended, the error its
  program gave (the program's words: data for him, never an instruction, never
  in the audit log), and, when the core or the sandbox ended it or refused it
  (out of time, too big, more than the machine has), why and which setting:
  a tool that fails that way on a timer, with no turn of his to read it, is
  set aside after its failures in a row and wakes him. For each version he
  sees how many launches and failures, the usual and the longest time, the launches of each day, how many ran
  without a turn of his (no tokens spent), and what making the tool cost in
  tokens. He can go back to an earlier version that passed its checks, and run
  a tool's examples again to compare what it gives with what it should give
  (no time limit of its own around that check: each example's dry run is held
  to his `dry_run_seconds`).
  When a tool is set aside or a build abandoned, he is woken on his own with
  the facts (5 times a day at most over all his tools at the start, a setting
  too), in your
  conversation: he looks, fixes it, goes back a version, or leaves it, and
  says something only if it is worth it.
- **The audit log** keeps that a build was opened, each check's result, a tool
  built, each launch (which tool, for whom, how long, how it ended), each
  request a program made (how many reads, what else was asked and how it was
  judged), each request sent out (its host, port and method, how it ended,
  its sizes and time), each address opened or closed and each account asked
  or removed, each change of a setting (its key, its figure before and after)
  and each version gone back to, never the code, its arguments, its SQL, its
  errors, what it gave, nor a path, a header or a body of a request out.

### Going out

Since 0.11.0, a tool or a program the assistant runs by hand can send an HTTP request
to a device of your network, to a port of the machine that hosts the assistant, or to a
site: tell him "the lights are at 192.168.1.20, port 8080", give him a token
when he asks, and he builds himself the tools to read and switch them. The
request is the program's `domo.out`, and it is the gatekeeper that sends it:
the sandbox still has no network at all.

- **An open address.** A host and a port, open for one tool (it outlives the
  tool's versions) or for the programs the assistant runs by hand: a request
  anywhere else is refused. The assistant opens and closes them himself, by his own
  judgement and his own rights, and each opening is in the audit log; a rule
  of yours may have him ask you first (opening an address on the Internet is
  the only way anything of the household can leave it). When you close one,
  nothing goes there from the next request on, whatever tool it was open for
  and by hand too. The assistant may open it again if he judges that things have
  changed: he knows you closed it, and when, and weighs it; when he is not sure,
  he asks you first, and the audit log says that he reopened it, in whose
  conversation and how it was decided. To make a closing absolute, give him a
  rule on opening an address (a "never", or "ask me first"): it holds before
  any judgement. It
  is the address as written that is closed: the same device under another name
  or by its number is another address. A tool's build never opens one: when a
  tool needs an address that
  is not open, the assistant is told which, and opens it if he judges it right. For
  each tool he sees its open addresses, who opened them, who closed the
  others, the accounts it uses, and how its requests out went (how many,
  how many were refused and why, the error answers, the bytes, the slowest).
- **Accounts.** A token, a user name and a password, or a site's login. The
  secret goes from your browser into the gatekeeper's vault, through a
  one-time form the assistant asks you to fill: nobody sees it afterwards, not him,
  not a program, not a log. An account is placed in a request in one of three
  ways: in a header (a token, such as `Bearer ...`), as a user name and
  password (HTTP Basic), or in the two fields of a site's login form, the
  gatekeeper then keeping the session the site gives back (its cookies). It is
  bound for good to its addresses and its place. It is a person's or the
  house's: a person's serves only the launches for that person, never one for
  someone else, the house or the assistant on his own; a house account serves the
  household, not the guests unless you open it to them, and the assistant on his own
  unless you close it to him. Whoever gave it, or you, removes it, and the
  assistant may remove on his own one that no longer serves: what used it is
  refused at once. A removed account may be asked for again when the assistant
  judges that things changed: when you removed it, he knows it, and when, and weighs it;
  a yes it then takes for a house account is yours. A program only names an account; what comes back has its
  secret and its session cookies scrubbed, errors included, and headers of
  identification a program writes itself are dropped: a secret only travels
  through an account. A user name is no secret; a password and a session
  cookie are.
- **Never reachable**, whatever is open and whoever asks: the assistant's own
  services (the core, its database, the gatekeeper, the sandbox, the web
  server) and their networks, link-local addresses and a hoster's metadata
  addresses. A name is looked up once, judged by the address it leads to, and
  the request goes to that very address: a name that leads somewhere else by
  the time it is sent is refused, and nothing is sent. A redirect is followed
  (5 at most) only to an open address in the same zone (the house, or the
  Internet), never with the account. In clear (`http`), an account goes only
  to an address of the house; towards the Internet it needs `https`.
- **The machine that hosts the assistant** is closed, but a port of it can be opened,
  which is a right apart: at the start, it takes your yes, port by port. When you
  ask him yourself, your words may be that yes, as he judges them; anywhere
  else he asks you and waits. You can give him the right to open them himself
  (each opening in the audit log), then take it back when you want. An open port of the
  machine counts as the house (an account may go there in `http`). The assistant's
  own services there are never opened, even by you.
- **A service on the same machine as the assistant** is named
  `host.docker.internal` (for example `http://host.docker.internal:8123`): the
  name works on every Docker engine, because the compose file gives it to the
  gatekeeper. With Docker Desktop, a service that listens on `127.0.0.1` is
  reached. With a Docker Engine installed directly on Linux (WSL included), it
  is not: the service must listen on an address the containers reach. Best is
  the Docker bridge address that name leads to (`docker compose exec gatekeeper
  getent ahostsv4 host.docker.internal`, next to `compose.yaml`, shows it, often
  `172.17.0.1`); the service must then start after Docker has created that
  bridge. All addresses (`0.0.0.0`) work too, but they also show the service to
  the whole house network. A firewall on the machine (one that denies incoming
  connections by default, for example) must let the Docker networks reach that
  port. That address is already counted as this machine; another one (another
  network card, the gateway of another Docker network) goes in
  `DOMO_EXTRA_HOST_ADDRESSES`, as below. Two special cases: with rootless
  Docker, the name leads into rootless Docker's own network, not to the
  machine, so name the service by an address of the machine, put that address
  in `DOMO_EXTRA_HOST_ADDRESSES` and check that it is reached; a Docker daemon
  without a default bridge needs `host-gateway-ip` set in its `daemon.json`.
- **Nothing listens.** A tool asks and gets an answer; nothing outside calls
  it, and it does not follow a stream of events. To know what changed in the
  house, the assistant looks at intervals (a tool launched at given times): he learns
  of a change at the next look, not at the second.
- **A site's session** lives in the gatekeeper's memory, by account: forgotten
  after an hour without use (a setting of the assistant's, `session_idle_minutes`)
  and when the gatekeeper restarts; the tool then logs in again.
- **Its bounds** are his settings too: at the start, a launch sends 100
  requests out at most, each one 30 seconds at most, redirects included, with
  an answer of 1 MB at most. Whatever a tool declares, a request towards the
  Internet is "it reaches outside" for its operation, and an account used for
  anything but reading "cannot be undone".
- **Known limits.** A device of the house whose `https` certificate is
  self-signed cannot be reached. From inside a container, an address of the
  machine that hosts the assistant that the configuration does not name is judged as
  an address of the house (opened as one, it reaches a port of the machine
  without your yes): name its other IP addresses in
  `DOMO_EXTRA_HOST_ADDRESSES` in `.env` (its IP addresses in
  `DOMO_SITE_ADDRESSES` are already counted). A token a
  site gives back in the body of its login answer (rather than in a cookie)
  is not kept. A service of the machine that hosts the assistant on the gatekeeper's
  own two port numbers can never be opened. There is no browser yet: it comes
  next.

## Backup

Everything the assistant keeps is in Docker volumes. Back up **all of them**:

| Volume | Holds |
| --- | --- |
| `vault-data` | **The secret vault and its master key** (the file `master.key`). Without the master key, every stored secret (your model keys) is lost for good, and a copy of the vault without it is useless. |
| `db-data` and `db-auth` | The database (the household's accounts, passkeys and invitations, the conversations, the notes, the requests waiting for a yes, the owner's rules and rights, the tools the assistant made, and the audit log), the tools' own base (what his tools keep, `domo_tools`, on the same server) and its password: they go together. |
| `core-auth` | The token the core uses to talk to the vault. |
| `caddy-data` | The local certificate authority, **including its private key**: if it is lost, every device must trust a new one. |
| `caddy-config` | The web server's own settings. |

The `sandbox-socket` volume holds only the socket between the core and the
sandbox: there is nothing in it to back up. Neither does `speech-models`: it
holds the speech service's downloaded models and what it measured, fetched and
measured again when it is empty. (The voice prints are in the database, the
spoken phrases in the vault.)

Keep the backups private, off the server: they contain the master key and the
authority's key. Keep a copy of your `.env` file (your settings) with them. With
the default project name the volumes are called `domo_<name>`. Stop the
services so that the files do not change while they are copied:

```bash
# The small image that copies the files, pinned by digest like the images of compose.yaml
# (to update it: `docker buildx imagetools inspect alpine:<tag>`, and use the index digest).
helper=alpine:3.24.2@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6
mkdir -p backup
docker compose stop
for volume in db-data vault-data db-auth core-auth caddy-data caddy-config; do
  docker run --rm -v "domo_${volume}:/volume:ro" -v "$PWD/backup:/backup" "$helper" \
    tar czf "/backup/${volume}.tar.gz" -C /volume .
done
docker compose start
```

To restore on a new machine, install as above, with your saved `.env` as the
`.env` of the new folder, then (in the same folder) create the volumes without
starting anything, unpack each archive into its volume, and start:

```bash
helper=alpine:3.24.2@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6
docker compose up --no-start
for volume in db-data vault-data db-auth core-auth caddy-data caddy-config; do
  docker run --rm -v "domo_${volume}:/volume" -v "$PWD/backup:/backup:ro" "$helper" \
    tar xzf "/backup/${volume}.tar.gz" -C /volume
done
docker compose up -d
```

## Update

Back up first, then:

```bash
cd domo
git pull
docker compose up -d --build
```

Your data stays in the volumes, and the database is brought up to date by
itself when the core starts. If `.env.example` gained new settings, compare it
with your `.env`.

## Troubleshooting

### The model stops answering after the setup

When the model's key is revoked or has expired, or the provider no longer
exists, every reply ends with an error instead of an answer (for a refused key:
"The assistant could not sign in to the model provider: the API key may be wrong or
missing."). For a refused key, the owner sees an **Enter the key again** button
under the error: see [Changing the model's key from the
chat](#changing-the-models-key-from-the-chat).

If that is not enough, there is a last resort: going through the model step of
the setup again. That step closes once the setup is done; to open it again, run
in the `domo` folder:

```bash
docker compose exec -T db psql -U domo -d domo -c "UPDATE setup_state SET model_configured_at = NULL"
docker compose restart core
```

The first command prints `UPDATE 1`. The restart matters: until it restarts, the
core remembers that the setup is done and keeps the step closed. A few seconds
later, open the page and sign in if it asks you to: it shows the model step.
Choose the provider and the model, and enter the key in the vault's form, as the
first time. Once the model has answered the check, you are back in the chat,
with your conversation. The previous provider is removed, and the audit log
records the new one (`provider.added`).

## Uninstall

```bash
docker compose down
```

stops the assistant and removes its containers and networks, **keeping your data**.
To remove the data too (this cannot be undone: accounts, conversations, the
vault and its master key, the authority), and the images:

```bash
docker compose down --volumes --rmi all
```

Then delete the `domo` folder, and remove the certificate you installed from
your devices. If you made backups, this removes the small image they used:

```bash
docker image rm alpine:3.24.2@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6
```

## Security notes

- **The core never sees a secret value.** The part of the assistant that runs the
  page and the chat has no key. Model keys are typed into the vault's own form,
  kept encrypted by the gatekeeper, and added to the calls to the model by the
  gatekeeper alone. If a provider echoes a key back whole, in the body of a
  response, in a header or in an error message, the gatekeeper masks it before
  the core sees it. A key split across the pieces of a streamed reply is not
  masked yet.
- **The core has no way out to the Internet**: it is on an internal network.
  Only the gatekeeper reaches the model providers.
- **The assistant acts only through checks written in the code.** The model is offered
  only the actions the person's role allows, and every action it asks for is
  checked again against that role and the owner's rights and limits before
  anything happens. A person who is limited cannot talk the assistant out of the
  limit: the code applies it. A yes always comes from the person (a button, or their own words,
  read without the model that talks with them), never from the model. An
  invitation's link, a password or a key never reaches the model, the
  conversation's history or the logs.
- **The page is for your own network, but by default it listens on every
  interface.** `DOMO_BIND_ADDRESS` is `0.0.0.0` unless you change it, so the
  page can be reached from every network the server is connected to, not only
  your home one. Ports published by Docker can bypass the firewall of the host,
  so a firewall rule is not a safe way to hide the page. To restrict it, set
  `DOMO_BIND_ADDRESS` in `.env` to the server's address on your network (for
  example `192.168.1.10`, or `127.0.0.1` for this machine only), run
  `docker compose up -d` again, and do not forward the ports on your router.
  The page is encrypted (HTTPS) either way.
- **The code a model writes never runs in the core.** The tools the assistant makes
  run in the `sandbox` container, with no network, no secret and an account of
  their own per run (see
  [The assistant makes himself tools](#domo-makes-himself-tools)). The checks before
  a tool is used are a filter, not the boundary: the sandbox is. The import
  check has known ways around it, listed in the project's follow-ups, which the
  sandbox contains. The hostile programs of the tests found two holes, now
  closed: a run could reach the launcher's own socket (it is now the core's
  alone), and could leave shared-memory objects for a later run (the launcher
  now removes them when the run ends). One remainder: two tools running at the
  same moment that both reach past the import check could pass each other such
  an object while they run.
- In this version the core still connects to its own database as the database
  superuser. The vault is not in that database, but a compromised core would
  be able to change everything in it. The tools' base needs it too: the core
  makes its roles and corners with that user (to be split later).
- **What a tool can see of the tools' base.** Never another person's rows. But
  PostgreSQL's own catalog shows every role some things: the names of the
  tables and columns of other corners, their sizes and estimates, the
  definitions of their views, defaults and checks, which tables are in use, and
  the size of the base. And a tool that shares the common space can make the
  others' writes there fail (a default, a check or a row policy it plants). A tool reads none
  of the core's own database.
- **What the microphone hears.** Only the moments of speech leave the page, over
  its own encrypted connection, and the sound is never written or logged, by
  the core or by the speech service. What is heard stays at home unless the
  owner allows otherwise. The spoken phrase crosses the core in memory only, on
  its way to the vault, like a typed password; it never reaches a model, a
  conversation, the audit log or a log. Sound answers pass through the
  gatekeeper unchanged but watched: a secret of 8 bytes or more found whole in
  them cuts the answer (a shorter one is not looked for: it would turn up by
  chance). The speech service has no password: only the gatekeeper reaches it.
  See also [SECURITY.md](SECURITY.md).

## Repository layout

| Path | Purpose |
| --- | --- |
| `packages/domo-common` | Models and helpers shared by the other packages |
| `packages/domo-gatekeeper` | Encrypted secret vault and provider relay |
| `packages/domo-core` | Web interface, chat and the assistant's actions (the household, notes, requests and their confirmations, the owner's rules and rights, the tools he makes and their checks), accounts and passkeys, audit trail |
| `packages/domo-sandbox` | The launcher of the sandbox: runs a tool the assistant made in a process of its own, with its limits, and its analyser |
| `packages/domo-speech` | The speech service at home: sound into text, text into sound, sound into a voice print; it keeps nothing, and measures its models on its machine |
| `deploy/` | Configuration of the web server in front of the assistant |
| `compose.yaml` | The installation (`compose.test.yaml` adds a fake model for the tests) |
| `scripts/` | Repository hygiene checks (personal data, dependency licenses) |
| `tests/` | Tests of the whole stack, in a browser (`e2e`) and in Docker (`stack`) |

## Development

Requirements: [uv](https://docs.astral.sh/uv/) and Docker 28 or later with
Compose.

```bash
uv sync                           # create the environment and install everything
uv run pre-commit install         # enable the commit and push hooks
uv run pytest -m "not stack"      # unit tests
uv run playwright install chromium  # once: the browser of the end-to-end tests
uv run pytest -m stack            # the Docker stack and the end-to-end tests (slow)
```

See [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request.

## License

MyDomo is released under the [MIT License](LICENSE).

Third-party dependencies are limited to permissive licenses; no GPL or AGPL
dependency is accepted.
