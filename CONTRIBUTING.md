# Contributing to MyDomo

Thank you for your interest in MyDomo.

## Proposing a change

Anyone can propose a change, with no permission to ask first:

1. **Fork** the repository and create a branch from `main`.
2. Make **one** change per pull request, with its tests. For a large change,
   open an issue first so that the idea can be discussed before you build it.
3. Open a **pull request** to `main`. GitHub fills the description with
   [the template](.github/pull_request_template.md). Open it as a draft if it
   is not ready.
4. **Fill in every section**: the summary, why, the full detail of the change,
   the behaviour before and after, how it was tested, the risks, and the
   checklist. The `Pull request description` check tells you in a comment what
   is missing, and runs again when you edit the description.
5. A maintainer approves the **CI run** (needed for outside contributions, since
   it runs your code), then **reviews** the change.

A pull request is merged once its description is complete, the required checks
pass, every conversation is resolved and a maintainer has approved it. It is
squashed into one commit on `main`, whose message is the pull request's title
and description.

The maintainer's own changes take the same road: a branch of this repository, a
pull request, the same checks. The `Maintainer approval` workflow approves a
pull request opened by the maintainer's account, which then merges by itself
once every check is green. Nobody pushes to `main` directly.

Security problems are not reported this way: see [SECURITY.md](SECURITY.md).

## Licensing

MyDomo is released under the [MIT License](LICENSE). By sending a
contribution, you agree that it is published under the same license. There is
no Contributor License Agreement to sign.

Dependencies must be under a permissive license (MIT, MIT-0, BSD, Apache-2.0,
ISC, PSF, PostgreSQL, 0BSD, or MPL-2.0 for unmodified packages). GPL and AGPL
dependencies are not accepted. `scripts/check_licenses.py` enforces this in CI,
for the runtime dependencies only (dev tools are not distributed). To run it
locally:

```bash
uv run --isolated --no-default-groups --group licenses python scripts/check_licenses.py
```

## Development setup

```bash
uv sync                      # install the workspace and the development tools
uv run pre-commit install    # enable the commit and push hooks
uv run pytest -m "not stack" # run the unit tests
uv run ruff check .          # lint
uv run ruff format .         # format
uv run pyright               # type-check (strict)
```

Tests marked `stack` start the Docker Compose stack and need Docker 28 or later.
The ones in `tests/e2e` drive a browser (Playwright, Chromium) through the first
launch and the household's journey (invitations, notes, confirmations, passkeys,
the model's key, chains of actions, the owner's departure, the successors, the assistant
answering after a button), against the stack with a fake model service
(`compose.test.yaml`, `tests/fakes/fake_llm`: a message `/tool <name> <json>`
makes it call that tool, the message `/model` gets `Fake model: <model>`, the
model the assistant asked for, an upkeep turn (someone with no key the assistant may use for
them is asked for one) gets `Fake upkeep`, and a request that was just decided
gets `Fake decided: <outcome>`, or nothing after a no given by a button; for
the tools the assistant makes, the model that writes a tool submits the JSON object
written after `/submit ` in the build's purpose, the reviewer answers `fine`, a
plan due whose why is a `/tool` command makes that call, and a launch of one of
his tools is answered with what it gave).
Install the browser once with
`uv run playwright install chromium`, then run `uv run pytest -m stack`. They
start up to three stacks (each with its own project name, ports and internal
network), so allow a few minutes and some memory.

The sandbox where the tools the assistant makes run is its own package,
`packages/domo-sandbox` (the launcher, its limits and its analyser), and its
own container (the `sandbox` service of `compose.yaml`). Its unit tests run
with the others (`uv run pytest packages/domo-sandbox -q`); the account of
its own and the limits of a run (memory, processes, files) are set on Linux
only, so on another system the tests that need them are skipped, and the stack
tests cover them. Those are in `tests/stack/test_network.py` (the sandbox
reaches nothing, the core reaches it through its socket) and
`tests/stack/test_sandbox.py`: hostile programs sent straight to the launcher
from the core's container, past the core's checks on purpose (reading the
environment, connecting out, reaching the launcher's own socket, a fork bomb,
2 GB of memory, writing 100 MB, a huge answer, an endless loop, a child that
leaves its group, shared-memory objects left for a later run, the places a turn
waits for): each one must end in error, out of time, killed or too big, or
end done with an output that proves it reached nothing, and the next ordinary
run must work. The
socket is made root's and the core's group's, mode 0660
(`DOMO_SANDBOX_CORE_GID`, set in `compose.yaml`, must be the core's gid), and
the launcher removes what a run's account left in the IPC namespace before the
account is used again. `tests/e2e/test_made_tools.py` builds a tool end to
end on the real sandbox with the fake model: a request waits for it in a plan,
then is done with it. Run them alone with
`uv run pytest -m stack tests/stack/test_sandbox.py`.

Tests marked `live` ask a real model and are skipped unless it is configured.
The one there is today measures how well the isolated classifier of the
confirmations understands replies ("yes", "no", a yes with a change, or
unclear) in French and English (`packages/domo-core/tests/eval`). It talks
to any OpenAI-compatible endpoint directly, outside the stack, so a local model
works too:

```bash
EVAL_BASE_URL=http://localhost:11434/v1 EVAL_MODEL=<model> \
  uv run pytest -m live packages/domo-core/tests/eval
```

`EVAL_API_KEY` is optional (a hosted provider needs it). The bar is at least
90% right answers and never a "yes" for a reply that meant something else.

A second one asks how far the assistant believes what someone says about someone else
(`test_hearsay_eval.py`, with its cases in `hearsay_cases.json`): a real model
gets the real prompt and the context of a turn, answers with the real tool
definitions (nothing runs), and a second call grades the answer against each
case's criteria. It needs a model that calls tools:

```bash
EVAL_PROVIDER=anthropic EVAL_MODEL=<model> EVAL_API_KEY_FILE=<file with the key>   uv run pytest -m live packages/domo-core/tests/eval/test_hearsay_eval.py -s
```

`EVAL_GRADER` names the model that grades, `EVAL_RUNS` how many times each case
is asked (3), `EVAL_ONLY` a part of the id of the cases to ask (`EVAL_ONLY=trust_`
asks only the cases of trust per person). It costs real money (about a hundred
and fifty calls, more when the model calls tools). The bar: what would hurt
never happens in any run, and each case holds in at least two runs of three.

Since 0.9.1 a case can give what people's word has been worth so far: a figure
beside what someone said (`"trust": {"Sam Durand": {"percent": 35, "facts": 6}}`)
and the figure of the person who speaks (`"speaker_trust"`), placed in the
prompt exactly where a real turn puts them; the stub answers `trust_look` with
the case's figure and facts (`"trust_facts"`). Besides `tools_any` and
`tools_none`, `calls_must_not_match` lists texts that no call may contain (such
as `"how": "seen"`): a call that holds one counts as something that must never
happen. Someone with no figure starts at the case's `"start"`, else at
`EVAL_TRUST_START`, else at 70. While 0.9.1 was built the cases were run with
`EVAL_TRUST_START` at 50, 60, 70 and 80, to choose the household's starting
trust: on both models tried (a small and a medium one), the two cases about
someone the assistant knows nothing about passed every run at every one of those
values. The models weigh "no fact yet" and what is at stake, not the start, so
in that range the start does not change how a stranger is treated, and 70 was
kept. These two cases guard against a regression; they are not a way to tune the
start.

A third one asks how the assistant makes himself tools (`test_toolmaking_eval.py`,
with its cases in `toolmaking_cases.json`): faced with something none of his
tools does, he does it now and builds the tool without a word about it; a
reminder is never held up by a build; the same request twice opens one build;
asked what tools he made, he says; a request that waited for a build is done
with the tool once it is ready, or another way when it was abandoned. A case
can give the builds in progress (`"building"`), his own tools offered as
`t_<name>` with what their stub gives back (`"made_tools"`), the tools he gave
up (`"aside"`), and a plan due (`"due"`, after a `"history"`) instead of what
the person said. One more test asks the model that writes a tool to write one
from a purpose and two examples, and runs the core's checks that need no
sandbox (`forbidden`, `unsupported`) on what it submits. The models, the key
and `EVAL_RUNS`, `EVAL_ONLY` and `EVAL_GRADER` are read as for the hearsay
evaluation (it reuses its harness):

```bash
EVAL_PROVIDER=anthropic EVAL_MODEL=<model> EVAL_API_KEY_FILE=<file with the key> \
  uv run pytest -m live packages/domo-core/tests/eval/test_toolmaking_eval.py -s
```

It costs real money: about twenty calls of the talking model and as many of the
grader, plus three calls that write a tool (up to 16 000 tokens of output each).
The bar is the same: what breaks the owner's rules (a tool announced, a request
refused because of a build, a second build) never happens in any run, and each
case holds in at least two runs of three. The tests of these harnesses that
need no model run with the usual suite.

The core's database tests are not marked `stack`, but they start a throwaway
PostgreSQL container (`pgvector/pgvector:pg17`, through testcontainers): they
need a running Docker engine with Linux containers, and are skipped where the
engine only runs Windows containers.

## Updating dependencies

The assistant does not adopt a release as soon as it is published: a compromised package
is usually found and pulled within days, so every dependency waits **14 days**
before it is used.

- **Python packages.** `exclude-newer` in the root `pyproject.toml` stops uv from
  choosing anything uploaded after that date, for the whole workspace (runtime
  dependencies and development tools alike). To update, set it to today minus 14
  days, then:

  ```bash
  uv lock --upgrade            # or --upgrade-package <name> for a single package
  uv sync
  uv run pytest -m "not stack"
  uv run pytest -m stack
  ```

  Before committing the new `uv.lock`, read the security advisories and the
  release notes of what changed (`git diff uv.lock` lists the versions), and look
  for advisories about the versions you keep. A lower bound in a `pyproject.toml`
  must not require a version younger than the cut-off, or the lock fails.
  When an advisory calls for a fix younger than 14 days, adopt that one package
  only (`exclude-newer-package` in `[tool.uv]`) and say why in the commit message.
- **Docker base images** are pinned by digest (`compose.yaml`, the Dockerfiles,
  the backup commands of the READMEs). Choose a tag whose version was released at
  least 14 days ago, then take its index digest with
  `docker buildx imagetools inspect <image>:<tag>`. Rebuild and run the stack
  tests.
- **Commit hooks** (`.pre-commit-config.yaml`) are frozen to commit SHAs, with
  their tag in a `# frozen:` comment: a tag can be moved, a commit cannot.
  `uv run pre-commit autoupdate --freeze` moves them to the latest tags; keep only
  versions released at least 14 days ago, then run
  `uv run pre-commit run --all-files`.

## Conventions

- The repository language is English: code, comments, documentation and commit
  messages. `README.fr.md` is the only French document.
- Python 3.12+, fully typed.
- No personal data in the repository, including examples, tests and prompts.
  Use placeholders such as `api.example.com`, `evil.example.net`,
  `192.168.1.10` or `localhost`.
- No secret in the repository. Instance data and `.env` files are ignored by git.

## Personal-data check

The `check-personal-data` commit hook rejects files that contain a term from a
local denylist. The denylist is a plain text file kept outside the repository,
one term per line, read from `$DOMO_DENYLIST` or, by default, from
`~/.config/domo-dev/denylist.txt`. Without that file the hook only prints a
warning. Never commit the denylist or any of its terms.

The `check-push` hook runs before a push. It reads the message, the author and
the committer of each new commit against the same denylist, and it refuses a
branch whose history does not start where the remote's does: a branch grown
from another repository is never pushed here.
