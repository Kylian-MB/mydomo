# Itération 1a — suites à donner

Points relevés pendant les relectures du plan 1a et volontairement reportés. Le tri a été fait par la relecture finale ; ils alimentent l'écriture des plans 1b à 1e. Texte d'origine des relectures (en anglais).

## À décider par le propriétaire avant fusion

- certifi 2026.7.22 (imposé par le délai de 14 jours) fait de nouveau confiance à trois racines emSign retirées en amont ; recommandation : exception `exclude-newer-package` pour certifi.
- L'image de base python:3.12-slim (3.12.16, 2026-10-25) et le hook pre-commit ruff v0.17.3 (2026-10-27) ont moins de 14 jours ; le ruff verrouillé pour le développement est 0.17.0.
- Vérification sur un vrai serveur Linux propre (README à la lettre, `uv run pytest -m stack`, un vrai fournisseur, une mise à jour `git pull` + `up -d --build`) et première exécution de la CI : jamais faites (seulement Docker Desktop).

## À traiter tôt dans les plans suivants

- 1b : saturation possible par connexions à noms d'utilisateur variés (argon2 + connexion BDD tenue pendant la vérification) — sémaphore, ne pas tenir la connexion.
- Prochain plan : journaliser les GatekeeperError (opération, statut, code) dans l'étape modèle ; indicateur d'attente pendant le test du modèle.
- Avant la première mise à jour livrée : versionner les fichiers statiques (cache).
- Avant la première publication : URL du dépôt dans les README.
- 1e : filtrage côté cœur du texte reconstitué (une clé découpée entre plusieurs événements SSE n'est pas masquée) ; conception AAD v2 et lecture des lignes v1 ; séparation réseau edge ↔ gatekeeper:8081/db ; origine distincte pour le formulaire du coffre ; jeton du formulaire connu du cœur ; routes sans clé = sortie générale du cœur.

## Constaté en essai réel (2026-10-29, vrai modèle)

- Le prompt système ne décrit pas ce que cette version sait faire : sans mention de l'absence de mémoire durable, le modèle improvise (il conseille de « vérifier la config du serveur »). Le corriger avec le cadrage « nouveau-né » ci-dessous, et non comme une liste de limites techniques : « je viens d'arriver, je ne sais encore rien de vous, ni de moi, ni de ma maison ; je découvre », tout en restant honnête sur ce qu'il retient (historique de la conversation oui, mémoire durable pas encore, aucun outil branché).

## Vision précisée par le propriétaire (2026-10-29) — à intégrer à la conception

L'assistant doit se comporter comme une vraie personne/assistant (mi-IA, mi-personne). Au démarrage il n'a rien et commence immédiatement à apprendre, **y compris qui il est, à quoi il servira, son environnement**. C'est un nouveau-né dans un système en perpétuel changement, qui doit s'adapter à tout.

Conséquences à reprendre au brainstorming du plan 1c (mémoire), avec mise à jour de la spec :

- **Connaissance de soi** : un type de mémoire pour ce que l'assistant apprend de lui-même (son rôle, sa raison d'être telle que le foyer la définit, sa manière d'être), en plus des personnes et de l'environnement. Le ton de base du brief (chaleureux, humour pince-sans-rire, langue de l'interlocuteur) reste l'inné ; le reste se construit.
- **Apprentissage dès la première conversation** : la première discussion est une découverte mutuelle (qui est le foyer, ce qu'on attend de lui), pas une configuration ; ce qu'il apprend est enregistré, consultable et corrigeable.
- **Adaptation continue** : les faits et la connaissance de soi évoluent, se remplacent et s'oublient avec trace, comme le prévoit déjà la spec (§9).
- **Garde-fous inchangés** : ce que l'assistant apprend de lui-même ou de son rôle ne peut jamais élargir ses droits ni lever un garde-fou (spec §9.4) ; un utilisateur ne peut pas lui « apprendre » qu'il n'a plus de règles.

## Manière de parler (précisée par le propriétaire, 2026-10-29)

Le ton de l'essai réel était « encore trop robotique » : listes, gras, registre formel, vouvoiement alors que l'utilisateur tutoyait. L'assistant a les connaissances et aide, mais parle **simplement, avec des mots de tous les jours**, adaptés à la personne. Il n'emploie de termes techniques que **dans un domaine où la personne a elle-même des connaissances avancées, et seulement dans celui-là**.

- **Prompt système (correction déjà prévue)** : registre simple par défaut, phrases courtes et naturelles, pas de jargon ni de mise en forme de rapport dans une discussion ordinaire, calquer le registre de l'interlocuteur (tutoiement ou vouvoiement compris).
- **Mémoire (plan 1c)** : retenir, par personne et par domaine, le niveau d'expertise observé dans les échanges (une préférence apprise, consultable et corrigeable), et ajuster le vocabulaire en conséquence. À intégrer à la spec avec la vision « nouveau-né ».
- Les réponses en Markdown s'affichent brutes (`**gras**`), la page montrant du texte simple par sécurité. Court terme : demander au modèle d'écrire sans mise en forme ; plus tard : rendu Markdown sûr (assaini), y compris pendant le flux.

## Remarques mineures reportées (par tâche)

- Task 1: minor (deferred): OWN_PACKAGES hard-coded in check_licenses.py (extend for sandbox package in plan 1e)
- Task 1: minor (deferred): check_licenses main() hides piplicenses stderr on failure; exception lookup not name-canonicalized; checker deps scanned too
- Task 1: minor (deferred): check_personal_data skips unreadable files silently; empty-but-present denylist passes silently
- Task 1: minor (deferred): thin tests for parse_report branches and main(); pip-licenses duplicated in dev and licenses groups; README.md has one French sentence
- Task 2: minor (deferred → carried to Task 3): Vault.put leaks value in UnicodeEncodeError repr for lone surrogates; raise ValueError from None.
- Task 2: minor (deferred → carried to Task 4): relay must use one authenticated read (add Vault.reveal_bound(ref) -> (value, bindings)) instead of metadata()+reveal(); ProviderRoute base_url must reject ASCII control chars/whitespace (urlsplit strips them).
- Task 2: minor (deferred): corrupt-row errors not uniform (TypeError on TEXT nonce; raw ValidationError in list_metadata); master key temp+link+dir fsync; test helpers don't close sqlite connections (contextlib.closing); vault parent dir default perms.
- Task 2: minor (deferred → carried to Task 4, same file vault.py): rotation UPDATE must also rewrite bindings_json (else AAD mismatch after normalization change); catch RecursionError in _stored_associated_data → VaultDecryptError; add test pinning Binding/InjectionLocation field sets.
- Task 2: out-of-scope (deferred): metadata()/list_metadata()/put mismatch check still validate stored JSON with current models (fails closed); v1-after-v2 read policy for plan 1e.
- Task 3: minor (deferred → carried to Task 5): public_app imports starlette.concurrency directly — use fastapi.concurrency.
- Task 3: minor (deferred): positional row handling/duplicated token lookup in secret_requests.py (NamedTuple + helper); dynamic i18n keys in secret_notice.html; double-submit UX wording on 404; caplog-only logging test; _transaction rollback masking.
- Task 3: design note (for final review): core knows form_path token → a compromised core (or anything on core_net) could fulfil its own request (junk rotation = DoS, or values for bindings it chose); cannot read secrets. Consider restricting gatekeeper public port to edge (shared edge→gatekeeper header) later.
- Task 4: minor (deferred → carried to Task 5): pin httpcore logger at INFO+ in gatekeeper (DEBUG logs response headers unscrubbed); explicit upstream timeouts (httpx default 5 s breaks long completions); never log locals / serialize RelayResponse.upstream; validate caller header names (ASCII token) before build_request (internal API).
- Task 4: minor (deferred): smarter suffix-prefix holdback (latency; long unrelated secret → bursty streaming); _decrypt raises inside except (keeps __context__); %252e double-encoding passes; ':' in first path segment refused (Gemini paths with base ending /models); base_url with empty ? or # accepted; scrub_text rebuilds variants per header; holdback test name; chunk-size claim; vault-in-thread untested; keyless routes make gatekeeper a general egress proxy for core (whole-branch look).
- Task 4: minor (deferred): cleartext_allowed treats non-ASCII dot-free host as single label (add isascii guard); IPv6 literal hosts impossible in Binding (decide if IPv6 bindings wanted); IDN punycode binding never matches httpx decoded host (fails closed); test helper typing (list of tuples).
- Task 5: minor (deferred → carried to Task 13): .dockerignore add .env, **/*.key, **/*.db; init token length ≥ 32 (gatekeeper could also enforce).
- Task 5: minor (deferred): fixtures/helpers in test modules instead of conftest (duplication, HTTP_LOGGERS redefined); no tests for HEAD/OPTIONS auth and /llm/.. path probes; PermissionError/sqlite DatabaseError at startup → raw traceback; ClientDisconnect during upload → 500 + ERROR log; second Ctrl+C force-quit lost; uv_build fetched without hash; pydantic-settings locked same-day release (cooldown); commit trailer attribution on 764b981.
- Task 6: minor (deferred → carried to Task 7): empty DOMO_DATABASE_URL must count as unset (env_ignore_empty or explicit check).
- Task 6: minor (deferred → carried to Task 9): setup_state reset in conftest should upsert / assert rowcount.
- Task 6: minor (deferred): drift test ignores column lengths + permanent compare_metadata==[] test; order-dependent isolation test; run_async docstring vs direct asyncio.run; no alembic.ini test, offline --sql needs password; no db marker for docker-less runs; downgrade drops vector unconditionally; audit append-only not enforced in schema (Task 8?); pytest basenames must be unique (consider --import-mode=importlib); message order tie-breaker; nits (unused session params, module-level state, three connection styles).
- Task 7: minor (deferred → carried to Task 8): Cache-Control: no-store on all dynamic responses outside /static/ (middleware).
- Task 7: minor (deferred → carried to Task 9): extract set_session_cookie/clear_session_cookie helpers; revoke the presented session on (re)login; purge expired sessions in create_session.
- Task 7: minor (deferred → carried to Task 12): hx-trigger filters (e.g. keyup[key=='Enter']) are silently always-true with allowEval=false — don't use them; document in VENDORED.md.
- Task 7: minor (deferred): login flood with distinct usernames (argon2 memory, DB connection held during verify) — semaphore or edge rate limit; _DUMMY_HASH computed at import; Origin check ignores scheme; current_person commits last_seen on every request; test nits (normalized traversal path, CSS grep, no re-login-with-cookie test).
- Task 8: minor (deferred): no lock_timeout on global advisory lock (key 4242 bare constant); _logged_username docstring wrong + belongs in audit module; oversized ints accepted by _clean then fail after lock; floats hashed with Python repr (language-neutral format); apply_cache_control reads scope path lazily; test hygiene (no reorder case, monkeypatch.undo, raw U+FFFD glyph, lenient_client dup); full-chain verify on event loop per page view.
- Task 8: minor (deferred): audit/routes.py _details_text json.dumps unguarded (RecursionError on ~10k-deep hand-edited jsonb → 500).
- Task 9: minor (deferred → carried to Task 13): filter /healthz from uvicorn access log (setup-code line buried); validate DOMO_TRUSTED_PROXIES (reject * , IP/CIDR only).
- Task 9: minor (deferred): gate path-class regression tests (HEAD, trailing slash, dot-segments, encoded slashes, //x, /setup); DB down at start → traceback; DRY (setup_state missing error x3, LoginThrottle naming, HOME_PATH in router module, long create_owner); theoretical purge/revoke lock-order deadlock; gate ignores non-HTTP scopes; throttle counts correct code on later failure; order-dependent test pair in test_db.py.
- Task 10: minor (deferred): every 409 → auth (provider's own 409 too); create_secret_request param named request vs req; duplication (_EmptyStream x3, _classify, _count) → _support.py; transport keyword test seam on production signature (ignored for Google); token read twice at startup; test gaps (trust_env assert, duplicate key-location test, RED was ImportError); Google builds genai.Client per call; deps.py imports all SDKs at import time.
- Task 11: minor (deferred → note to Task 14): model-step.js 400 swap has no committed browser test (E2E should hit an invalid-input path if cheap); discard control is <a> in the no-provider failure path.
- Task 11: minor (deferred): possible deadlock when two providers finish concurrently (lock setup_state first) + duplicate polls probe twice; keyless 'auth' message says re-enter key / transient errors promise a retry not offered; polling continues on 404/5xx; finalization on GET; swallowed GatekeeperErrors not logged; discard/cleanup not audited; abandoned secrets/routes stay (vault management later); base_url not trimmed; IPv6+key generic message; no in-flight indicator during probe; _ConcurrentFinish not real concurrency; structure (model_page dup, ProviderRoute used as validator, hard-coded /setup/model, 508-line module).
- Task 12: minor (deferred): missing provider at stream time → bare 409 (stream error+done instead); server event named 'error' also fires EventSource onerror (rename to 'failure' needs plan change); no 'interrupted' note on cut replies / streaming bubble in history; partial text lost if process killed; no tie-breaker on message order; _Turn logic could move to chat/turn.py; no SSE keep-alive during long first-token waits; no static cache busting; timing-dependent tests.
- Task 12: minor (deferred): claim taken before ownership check (theoretical 204 for owner); no hard turn deadline (claim held while provider pings without content); chat.turn 'partial' with zero tokens when reader leaves before any model call.
- Task 13: minor (deferred, later iteration): edge can reach gatekeeper:8081 and db:5432 over core_net (split edge↔core/gatekeeper:8082 from core↔gatekeeper:8081/db; needs per-network binding) — defence in depth.
- Task 13: minor (deferred): vault token may reach Caddy error log on edge-generated 5xx; edge has Internet egress via lan (disable masquerade?), edge runs as uid 0; db has no cap_drop while core is DB superuser (COPY TO PROGRAM); init never repairs modes / keeps short values silently, writes temp secret even when target exists; /ca.crt traversal test normalized by httpx; no direct X-Forwarded-Proto assert; teardown TimeoutExpired can mask failure; bare pytest runs stack tests (deselect by default?); init/core build same image twice.
- Task 13: minor (deferred): .env.example says FIRST but example lists localhost first (rule is first IPv4); non-overlap test covers only default vs test.env; union of networks covering everything not detected.
- Task 14: DONE_WITH_CONCERNS (a03e610). Notes: e2e fixtures in tests/e2e/conftest.py + running_stack_with_fake_llm in stack_support.py, helpers tests/e2e/support.py; e2e tests order-dependent (completed_setup self-heals when run alone); fresh stack own project/ports/prefix, port clash fixed + guard test; CI playwright install --with-deps chromium; README clone URL placeholder (no remote). DESIGN LIMIT (for final review / plan 1e): gatekeeper byte-level scrubber cannot mask a key split across SSE events (JSON-wrapped deltas) → core should also scrub reassembled text via gatekeeper scrub service (spec §12.3). Reply stays streaming if browser leaves before stream opens → 409 for 2 min. README walk-through on Docker Desktop: 4 gaps fixed; not verified: Linux host, CA install on devices, real provider, update path.
- Task 14: minor (deferred): docker image rm alpine in README; PytestRemovedIn10Warning from pytest-base-url (filterwarnings); leftover images (--rmi local or CONTRIBUTING note); e2e order dependence; CI Playwright cache + traces/screenshots on failure; port constants imported from other test modules; support module loaded twice; dead _send_error close logic; README clone URL placeholder before release.
- Task 14: minor (deferred): README restore with old .env should say update DOMO_SITE_ADDRESSES if address changed; FR/EN setup-code paragraph line wrapping; test_stack_support.py lacks require_docker.
