/* The chat page. htmx does the work (posting the message, filling the reply from the server-sent
   events); this file is what it cannot do without code, which the content security policy keeps
   out of the page:
   - an answer to a refusal (400, 409) is shown, in the notice area: htmx swaps nothing for a 4xx,
     nor does it for the answer of a link already shown or no longer there (410), shown in the
     place of the button that asked for it;
   - Enter sends the message and Shift+Enter starts a new line, as in a messaging app; nothing is
     sent while a composition is under way (accents, the keyboard of an Asian language), nor for
     a box that holds nothing but spaces. On a touch screen (no hover, a coarse pointer: a phone,
     whose keyboard has no Shift+Enter), Enter starts a new line and the button sends, as in a
     phone's messaging app;
   - the text box is emptied once its message was accepted; nothing holds a message back while the
     assistant writes a reply (only the same message, while it is being sent): one sent then is
     kept for that reply (202, its bubble alone, put at the end), and what answers it comes as any
     reply the assistant starts, with what is new;
   - a reply that has ended (the "done" event) stops being "busy", and its stream is not opened
     again: the htmx extension would otherwise try to reconnect to a stream that was closed by the
     server, for ever. A stream that stopped short (the browser reconnected and the server answered
     204 because the assistant still writes the reply on the server, or the reply was over; or it refused
     the stream) is not opened again either: the bubble shows the assistant writing (data-writing) until
     the reply as stored takes its place;
   - the page follows the reply as it grows, unless the reader scrolled up;
   - when a turn changed the name the person calls the assistant by, the stream says so before its end
     (the "frame" event: the new brand, title and placeholder, as plain texts), and the page puts
     them in place as text;
   - the connection errors of the browser, which arrive as events named "error" like the server's
     own, are not swapped into the page (they have no text);
   - every SINCE_EVERY_MS, when no reply of the page is streaming and no message is being sent,
     the page asks for the bubbles that came after its last one, or after the one just before the
     first bubble being written when there is one (a message may have been sent after that bubble,
     once its reply was over) ("/chat/since/<id>": what the assistant said when a button woke him, what
     another device of the person sent, and a bubble that shows the assistant writing for a reply that a
     turn still writes on the server, spec 5.7.2), or for the whole conversation when there is no
     such bubble ("/chat/since": a page opened on an empty conversation, where the assistant may write
     first, conversation.starts), adds those it does not have yet, puts a reply
     that is over in the place of the bubble that showed it being written, and follows them if the
     reader was at the end. A bubble shown as being written that the server neither writes nor has
     any more (a wake that said nothing is taken away) loses its dots, or goes when it is
     empty;
   - that request says whose page this is ("X-Domo-Page-Person", the person the page was
     rendered for, as htmx says it with its own requests): when the browser's session became
     someone else's since (they joined or logged in, in another tab), the server does nothing and
     answers "HX-Refresh", and the tab reloads in full, as htmx does on that answer. */
(function () {
  "use strict";

  var messages = document.getElementById("messages");
  var form = document.getElementById("chat-form");
  if (!messages || !form) {
    return;
  }
  var text = form.querySelector("textarea");
  var stickToEnd = true;
  var SINCE_EVERY_MS = 5000;
  // A message being sent, and how many were: an answer to "since" asked for before a message was
  // sent may hold that message, which its own answer adds.
  var sending = false;
  var sent = 0;
  // A touch screen (a phone): its keyboard has no Shift+Enter, so Enter starts a new line there.
  var touch = window.matchMedia("(hover: none) and (pointer: coarse)");
  // The person this page was rendered for.
  var pagePerson = document.body.getAttribute("data-page-person");

  function atEnd() {
    var root = document.documentElement;
    return window.innerHeight + window.scrollY >= root.scrollHeight - 160;
  }

  function scrollToEnd() {
    window.scrollTo(0, document.documentElement.scrollHeight);
  }

  // Answers to refusals: show them (the server says where, with HX-Retarget; the answer to a
  // "Show the link" button says it in the place of the button).
  document.body.addEventListener("htmx:beforeSwap", function (event) {
    var xhr = event.detail.xhr;
    if (xhr && (xhr.status === 400 || xhr.status === 409 || xhr.status === 410)) {
      event.detail.shouldSwap = true;
      event.detail.isError = false;
    }
  });

  // Enter sends, as the button does (htmx posts the form); Shift+Enter keeps the browser's new
  // line, and so does Enter on a touch screen, where the button sends. The Enter that ends a
  // composition is the composition's (229 is what some browsers give its key instead of
  // isComposing). While a message is being sent, Enter does nothing, as the button, which htmx
  // disables meanwhile.
  text.addEventListener("keydown", function (event) {
    if (event.key !== "Enter" || event.shiftKey || event.isComposing || event.keyCode === 229) {
      return;
    }
    if (touch.matches) {
      return;
    }
    event.preventDefault();
    if (!sending && text.value.trim() !== "") {
      form.requestSubmit();
    }
  });

  form.addEventListener("htmx:beforeRequest", function (event) {
    if (event.target === form) {
      sending = true;
      sent += 1;
    }
  });

  // The message was accepted: the text box is free for the next one, and the new pair is in view.
  // (Not "successful": the handler above made the refusals count as successes for htmx, and a
  // message that was refused must stay in the box.)
  form.addEventListener("htmx:afterRequest", function (event) {
    var xhr = event.detail.xhr;
    if (event.target === form) {
      sending = false;
    }
    if (event.target === form && xhr && xhr.status >= 200 && xhr.status < 300) {
      form.reset();
      scrollToEnd();
      text.focus();
    }
  });

  // The reply's stream is over: nothing is coming, nothing may be reopened.
  function endReply(event) {
    var bubble = event.target;
    if (bubble && bubble.removeAttribute) {
      bubble.removeAttribute("sse-connect");
      bubble.removeAttribute("aria-busy");
    }
  }

  // The reply's stream stopped before its end: the assistant may still be writing it on the server. The
  // stream is not reopened; the bubble, still busy, shows him writing, and the page asks for what
  // is new from the bubble before it (askSince), which brings the reply once it is over.
  function stillWriting(event) {
    var bubble = event.target;
    if (bubble && bubble.setAttribute) {
      bubble.removeAttribute("sse-connect");
      bubble.setAttribute("data-writing", "true");
    }
  }

  document.body.addEventListener("htmx:sseClose", endReply);
  document.body.addEventListener("htmx:sseError", function (event) {
    var source = event.detail && event.detail.source;
    // A connection that is closed for good (EventSource.CLOSED: the server answered with
    // something else than a stream, 204 for a reply it writes or has finished) would be retried
    // by the extension; one that is only interrupted (CONNECTING) is retried by the browser
    // itself.
    if (source && source.readyState === 2) {
      stillWriting(event);
    }
  });

  // The turn changed the name the person calls the assistant by: the brand, the title of the tab and the
  // placeholder of the text box follow it, put in as text (never as HTML).
  function followTheName(data) {
    var frame;
    try {
      frame = JSON.parse(data);
    } catch (error) {
      return;
    }
    if (!frame || typeof frame !== "object") {
      return;
    }
    var brand = document.querySelector(".topbar .brand");
    if (brand && typeof frame.brand === "string") {
      brand.textContent = frame.brand;
    }
    if (typeof frame.title === "string") {
      document.title = frame.title;
    }
    if (typeof frame.placeholder === "string") {
      text.setAttribute("placeholder", frame.placeholder);
    }
  }

  // Follow the reply while the reader is at the end of the conversation.
  document.body.addEventListener("htmx:sseBeforeMessage", function (event) {
    // The browser's own "error" event (a connection that dropped or was refused) reaches the
    // listener of the server's "error" event too, and has no text: there is nothing to show.
    if (typeof event.detail.data !== "string") {
      event.preventDefault();
      return;
    }
    if (event.detail.type === "frame") {
      // Nothing is swapped: the texts go in their places.
      event.preventDefault();
      followTheName(event.detail.data);
      return;
    }
    stickToEnd = atEnd();
  });
  document.body.addEventListener("htmx:sseMessage", function () {
    if (stickToEnd) {
      scrollToEnd();
    }
  });

  // The bubbles that came after the last one of the page, once they are over. A bubble that shows
  // The assistant writing elsewhere (data-writing) does not make the page busy: it waits for them.
  function busy() {
    return sending || messages.querySelector('[aria-busy="true"]:not([data-writing])') !== null;
  }

  function bubbleOf(id) {
    var bubbles = messages.querySelectorAll("[data-message-id]");
    for (var i = 0; i < bubbles.length; i += 1) {
      if (bubbles[i].getAttribute("data-message-id") === id) {
        return bubbles[i];
      }
    }
    return null;
  }

  function addNew(fragment) {
    var holder = document.createElement("template");
    holder.innerHTML = fragment;
    var follow = atEnd();
    var changed = false;
    var given = {};
    Array.prototype.slice.call(holder.content.children).forEach(function (bubble) {
      var id = bubble.getAttribute("data-message-id");
      if (!id) {
        return;
      }
      given[id] = true;
      var shown = bubbleOf(id);
      if (shown) {
        // A reply the page showed being written is over: the reply takes its place.
        if (shown.hasAttribute("data-writing") && !bubble.hasAttribute("data-writing")) {
          shown.replaceWith(bubble);
          htmx.process(bubble);
          changed = true;
        }
        return;
      }
      messages.appendChild(bubble);
      htmx.process(bubble);
      changed = true;
    });
    // Shown as being written, but the server neither writes it nor has it any more (a wake that
    // said nothing is taken away, a turn stopped with the core): no more dots, and an empty
    // bubble goes.
    var writing = messages.querySelectorAll("[data-writing]");
    Array.prototype.slice.call(writing).forEach(function (bubble) {
      if (given[bubble.getAttribute("data-message-id")]) {
        return;
      }
      var words = bubble.querySelector(".message-text");
      if (!words || words.textContent === "") {
        bubble.remove();
      } else {
        bubble.removeAttribute("data-writing");
        bubble.removeAttribute("aria-busy");
      }
      changed = true;
    });
    if (changed && follow) {
      scrollToEnd();
    }
  }

  // The bubble the page asks from: the one just before the first bubble being written, so that
  // the answer holds that reply (a message sent once it was over comes after it, and the page
  // already has it), else the last one; none when there is no bubble, or when a bubble being
  // written comes first: the page then asks for the whole conversation.
  function askFrom() {
    var bubbles = messages.querySelectorAll("[data-message-id]");
    var from = null;
    for (var i = 0; i < bubbles.length; i += 1) {
      if (bubbles[i].hasAttribute("data-writing")) {
        return from;
      }
      from = bubbles[i];
    }
    return from;
  }

  function askSince() {
    var from = askFrom();
    if (busy()) {
      return;
    }
    var url =
      from === null
        ? "/chat/since"
        : "/chat/since/" + encodeURIComponent(from.getAttribute("data-message-id"));
    var asked = sent;
    // As an htmx request: a session that ended is then answered 401, not the login page.
    var headers = { "HX-Request": "true" };
    if (pagePerson) {
      headers["X-Domo-Page-Person"] = pagePerson;
    }
    fetch(url, {
      credentials: "same-origin",
      headers: headers
    })
      .then(function (response) {
        if (response.headers.get("HX-Refresh") === "true") {
          // The session is someone else's now: this page is no longer theirs to use.
          window.location.reload();
          return null;
        }
        return response.ok && !response.redirected ? response.text() : null;
      })
      .then(function (fragment) {
        // An answer that did not come (null) says nothing of what is being written.
        if (fragment !== null && asked === sent && !busy()) {
          addNew(fragment);
        }
      })
      .catch(function () {
        // The server is away for a moment: the next round asks again.
      });
  }

  window.setInterval(askSince, SINCE_EVERY_MS);

  scrollToEnd();
})();
