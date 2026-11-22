/* Passkeys (WebAuthn): "Sign in with a passkey" on the login page, "Add a passkey" on the
   account page, and "Use my passkey" on the card of a request that asks a proof that it is the
   person (the chat page). The server checks everything (accounts/passkeys.py,
   proofs/kinds.py); this file carries bytes between it and the browser's authenticator,
   which a page cannot do without code, and the content security policy keeps code out of the
   page:
   - on a form's submit, it asks the server for the options: a POST to the form's
     data-options-url (with the password field, on the account page), answered in JSON. That
     request says whose page this is ("X-Domo-Page-Person", the person the page was rendered
     for, as htmx and chat.js say it): when the browser's session became someone else's since,
     the server gives no options and answers "HX-Refresh", and the tab reloads in full. So a
     page left open never adds a passkey to the account of whoever the session became;
   - it turns their base64url strings into the bytes navigator.credentials wants, and lets the
     browser ask the person;
   - it turns the browser's answer back into JSON (base64url), puts it in the form's hidden
     "credential" field, empties the password field (the server checked it already) and posts
     the form: the server answers with a page, as for any form. The form of a proof
     (data-passkey-form="prove") is posted by its own htmx request instead (form.requestSubmit():
     htmx takes it by its hx-post, and puts the card that comes back in the place of the card).
   The forms are taken by delegation (one listener on the document, in the capture phase, so that
   it comes before htmx's own): a card that htmx brings in later (the stream, the approvals zone,
   the answer to a proof) works as the page's own. The buttons that need this script
   ([data-passkey-only]) are hidden in the page and shown here only when the browser can use
   passkeys: when the page is loaded, and whenever htmx brings content in (htmx:load).
   An answer to a proof that was not checked (429: too many wrong passwords; 503: too many
   passwords to check at once) is shown where the server says (the notice area): htmx swaps no
   error by itself. The approvals zone, which reloads itself, waits while a proof is being given
   in it (htmx:beforeRequest is cancelled).
   Messages come from the form's data-message-* attributes, or from the server's "error", already
   in the person's language; they are written as text, never as HTML. The script is deferred, so
   the page is already parsed. */
(function () {
  "use strict";

  var supported = Boolean(
    window.PublicKeyCredential && navigator.credentials && window.fetch && window.JSON
  );
  var FORMS = "form[data-passkey-form]";

  // base64url (no padding) -> ArrayBuffer.
  function toBytes(text) {
    var base64 = text.replace(/-/g, "+").replace(/_/g, "/");
    while (base64.length % 4) {
      base64 += "=";
    }
    var binary = window.atob(base64);
    var bytes = new Uint8Array(binary.length);
    for (var i = 0; i < binary.length; i += 1) {
      bytes[i] = binary.charCodeAt(i);
    }
    return bytes.buffer;
  }

  // ArrayBuffer -> base64url (no padding).
  function toText(buffer) {
    var bytes = new Uint8Array(buffer);
    var binary = "";
    for (var i = 0; i < bytes.length; i += 1) {
      binary += String.fromCharCode(bytes[i]);
    }
    return window.btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function descriptors(list) {
    return (list || []).map(function (item) {
      var descriptor = { type: item.type, id: toBytes(item.id) };
      if (item.transports) {
        descriptor.transports = item.transports;
      }
      return descriptor;
    });
  }

  function creationOptions(options) {
    var publicKey = Object.assign({}, options);
    publicKey.challenge = toBytes(options.challenge);
    publicKey.user = Object.assign({}, options.user, { id: toBytes(options.user.id) });
    publicKey.excludeCredentials = descriptors(options.excludeCredentials);
    return { publicKey: publicKey };
  }

  function requestOptions(options) {
    var publicKey = Object.assign({}, options);
    publicKey.challenge = toBytes(options.challenge);
    publicKey.allowCredentials = descriptors(options.allowCredentials);
    return { publicKey: publicKey };
  }

  // The JSON form of a credential (what PublicKeyCredential.toJSON() gives, where it exists).
  function credentialJson(credential) {
    var response = credential.response;
    var json = {
      id: credential.id,
      rawId: toText(credential.rawId),
      type: credential.type,
      clientExtensionResults: {},
      response: { clientDataJSON: toText(response.clientDataJSON) }
    };
    if (credential.authenticatorAttachment) {
      json.authenticatorAttachment = credential.authenticatorAttachment;
    }
    if (response.attestationObject) {
      json.response.attestationObject = toText(response.attestationObject);
      json.response.transports =
        typeof response.getTransports === "function" ? response.getTransports() : [];
    } else {
      json.response.authenticatorData = toText(response.authenticatorData);
      json.response.signature = toText(response.signature);
      if (response.userHandle) {
        json.response.userHandle = toText(response.userHandle);
      }
    }
    return json;
  }

  function messageBox(form) {
    return form.querySelector("[data-passkey-message]");
  }

  function show(form, text, role) {
    var box = messageBox(form);
    if (!box || !text) {
      return;
    }
    box.textContent = text;
    box.setAttribute("role", role || "alert");
    box.hidden = false;
  }

  function hideMessage(form) {
    var box = messageBox(form);
    if (box) {
      box.hidden = true;
      box.textContent = "";
      box.removeAttribute("role");
    }
  }

  // The buttons of the forms inside ``root`` are shown where the browser can use passkeys;
  // elsewhere the form says why, if it has words for it.
  function reveal(root) {
    var forms = [];
    if (root.matches && root.matches(FORMS)) {
      forms.push(root);
    }
    if (root.querySelectorAll) {
      Array.prototype.push.apply(forms, root.querySelectorAll(FORMS));
    }
    forms.forEach(function (form) {
      if (!supported) {
        show(form, form.getAttribute("data-message-unsupported"), "status");
        return;
      }
      Array.prototype.forEach.call(form.querySelectorAll("[data-passkey-only]"), function (button) {
        button.hidden = false;
      });
    });
  }

  // A refusal the server explained: its text is shown as it is.
  function ServerRefusal(text) {
    this.text = text;
  }

  function askOptions(form) {
    var body = new URLSearchParams();
    var password = form.elements.namedItem("current");
    if (password) {
      body.set("current", password.value);
    }
    var headers = { Accept: "application/json" };
    // Whose page this is (none on the login page: nobody is logged in there).
    var pagePerson = document.body.getAttribute("data-page-person");
    if (pagePerson) {
      headers["X-Domo-Page-Person"] = pagePerson;
    }
    return window
      .fetch(form.getAttribute("data-options-url"), {
        method: "POST",
        credentials: "same-origin",
        headers: headers,
        body: body
      })
      .then(function (response) {
        if (response.headers.get("HX-Refresh") === "true") {
          // The session is someone else's now: this page is no longer theirs to use. Nothing
          // was asked of the authenticator; the tab reloads and shows the session's person.
          window.location.reload();
          throw new DOMException("Not this person's page", "AbortError");
        }
        return response
          .json()
          .catch(function () {
            return {};
          })
          .then(function (data) {
            if (!response.ok) {
              throw new ServerRefusal(data.error);
            }
            return data;
          });
      });
  }

  function start(form) {
    var kind = form.getAttribute("data-passkey-form");
    return askOptions(form)
      .then(function (options) {
        return kind === "register"
          ? navigator.credentials.create(creationOptions(options))
          : navigator.credentials.get(requestOptions(options));
      })
      .then(function (credential) {
        if (!credential) {
          throw new DOMException("No credential", "NotAllowedError");
        }
        form.elements.namedItem("credential").value = JSON.stringify(credentialJson(credential));
        var password = form.elements.namedItem("current");
        if (password) {
          password.value = "";
        }
        if (kind === "prove") {
          // Its own htmx request posts it: the submit that follows is let through (``ready``).
          form.setAttribute("data-passkey-ready", "");
          form.requestSubmit();
        } else {
          form.submit();
        }
      });
  }

  function explain(form, error) {
    if (error instanceof ServerRefusal && error.text) {
      show(form, error.text);
    } else if (error && (error.name === "NotAllowedError" || error.name === "AbortError")) {
      // The person said no, closed the prompt, or let it time out.
      show(form, form.getAttribute("data-message-cancelled"));
    } else {
      show(form, form.getAttribute("data-message-failed"));
    }
  }

  function running(form, yes) {
    var button = form.querySelector("[data-passkey-only]");
    if (yes) {
      form.setAttribute("data-passkey-running", "");
    } else {
      form.removeAttribute("data-passkey-running");
    }
    if (button) {
      button.disabled = yes;
    }
  }

  function onSubmit(event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.matches(FORMS)) {
      return;
    }
    if (form.hasAttribute("data-passkey-ready")) {
      // The ceremony is over and its answer is in the form: htmx posts it.
      form.removeAttribute("data-passkey-ready");
      return;
    }
    // Neither the browser nor htmx posts the form before the ceremony.
    event.preventDefault();
    event.stopPropagation();
    if (!supported || form.hasAttribute("data-passkey-running")) {
      return;
    }
    running(form, true);
    hideMessage(form);
    start(form)
      .then(function () {
        running(form, false);
      })
      .catch(function (error) {
        running(form, false);
        explain(form, error);
      });
  }

  // In the capture phase: before htmx's own listener on the form.
  document.addEventListener("submit", onSubmit, true);

  // The answer to a proof that was not checked: shown where the server says (HX-Retarget).
  document.addEventListener("htmx:beforeSwap", function (event) {
    var xhr = event.detail.xhr;
    var elt = event.detail.requestConfig && event.detail.requestConfig.elt;
    var proof = elt && elt.closest && elt.closest(".confirmation-proof");
    if (proof && xhr && (xhr.status === 429 || xhr.status === 503)) {
      event.detail.shouldSwap = true;
      event.detail.isError = false;
    }
  });

  // Whether a proof is being given in a form of ``root``: its passkey's ceremony runs, its post
  // is under way (htmx's own class), the focus is in it, or a password is typed in it.
  function proofInUse(root) {
    return Array.prototype.some.call(
      root.querySelectorAll(".confirmation-proof form"),
      function (form) {
        var password = form.elements.namedItem("password");
        return (
          form.hasAttribute("data-passkey-running") ||
          form.classList.contains("htmx-request") ||
          form.contains(document.activeElement) ||
          Boolean(password && password.value)
        );
      }
    );
  }

  // The approvals zone asks for itself again every 15 seconds (chat.html), which would replace a
  // proof being given there (the typed password, the ceremony, the note of a failed proof). Its
  // request waits while one is (a trigger's filter cannot: the page runs htmx without eval), and
  // goes again at the next tick once the form is left alone. The same holds for any element
  // whose request would replace a proof in use, but the proof's own forms.
  document.addEventListener("htmx:beforeRequest", function (event) {
    var elt = event.detail.elt;
    if (
      elt &&
      elt.querySelectorAll &&
      !(elt.closest && elt.closest(".confirmation-proof")) &&
      proofInUse(elt)
    ) {
      event.preventDefault();
    }
  });

  document.addEventListener("htmx:load", function (event) {
    reveal(event.target);
  });
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      reveal(document);
    });
  } else {
    reveal(document);
  }
})();
