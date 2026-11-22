# domo-talk

A small program to talk with the assistant from a terminal, with no browser. It holds no logic of
Domo: it shows the sentences the assistant gives, and sends what you type. It is the same conversation
as on his page: what is said on one side is found on the other, and what the assistant says on his own
reaches you while the program is open.

## Install

From the repository:

    uv tool install ./packages/domo-talk

or, inside the workspace, run it without installing:

    uv run domo-talk

## Entrust this computer to the assistant

1. Ask the assistant, on his page, to entrust this computer to him. He shows a short code, once.
2. Trust his certificate if you have not yet (see the main README: his address serves it at
   `<url>/ca.crt`), then pair:

       domo-talk pair https://domo.example --ca path/to/ca.crt

   The program asks for the code and keeps a secret of its own in one file in your configuration
   folder (`%APPDATA%\domo-talk` on Windows, else `$XDG_CONFIG_HOME/domo-talk` or
   `~/.config/domo-talk`). The secret is never printed.

Without `--ca`, your system's trust store is used. There is no way to turn the verification of
the certificate off.

## Talk

    domo-talk

Your recent conversation is shown, then a prompt. Type to talk to the assistant. When a request waits for
a proof that can be given here, type `/prove` to give your password (it is read without echo and
sent to the assistant only). `/quit` leaves.

If the assistant no longer knows this computer, the program says so (exit code 2): pair it again.

## Forget

    domo-talk forget

deletes the saved secret from this computer.

The saved secret is worth a session of yours: whoever holds this computer can talk to the assistant as
you, and even entrust another computer. If it is lost or stolen, ask the assistant at once, from your
page, to take it back, then to show your devices, and take back any you do not know.
