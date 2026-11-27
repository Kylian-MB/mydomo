# Security

Please do not report a vulnerability in a public issue or pull request.

Use GitHub's private report instead: in the **Security** tab of this repository,
choose **Report a vulnerability**. Describe the problem, how to reproduce it and
what it allows. The fix is prepared in private and published once it is ready.

## What the voice hears and keeps

When the voice is on, the page sends only the moments where someone speaks, over
its own encrypted socket (`/chat/voice`, which checks its `Origin` and the session
itself). The sound lives in memory while it is heard and is never written or
logged, by the core or by the speech service; it is heard at home unless the
owner allows a provider outside. What is said to the assistant is kept as text in
the conversation of the person who spoke; what is not said to him is kept only if
he chooses to remember it, marked as overheard; voice prints are vectors, never
sound, kept in the database by voice and by microphone. The spoken phrase lives
only in the gatekeeper's vault, encrypted, and crosses the core in memory only,
like a typed password. The proof window protects it this way: while a request
waits for a person's proof and the assistant spoke to them at a microphone in the
last 90 seconds, every sentence heard there is heard at home and compared by code
first, so the phrase reaches no model, discussion, audit entry or log; a near miss
is dropped, and five near misses or fifty comparisons in fifteen minutes hold the
phrase back. It does not protect the phrase said at a microphone where the
assistant did not just speak to that person, nor inside a longer sentence: that is
an ordinary sentence. Anyone in the room can hear the phrase, and can make it
unusable by voice there for a while by talking a lot or failing on purpose. A
voice alone never establishes who speaks. A voice nobody knows is answered apart,
and what it says reaches the page's person as a word of the assistant's own, marked
as coming from an unknown source: before that word can make him stop listening,
change or remove a spoken phrase, or forget a voice, his judge weighs it, told
where it comes from (a silent judge asks the person a proof). A program's, a
site's or a tool's text beside a person's words or a note is weighed the same way,
the judge told which kind of text it is; such text alone, with no words of anyone,
never does. Only its holder agrees or changes a spoken phrase; the owner, or an
adult for a child, may have someone else's removed: the judge weighs it every
time (when he cannot, nothing is removed and no proof stands in for him), and
that person is told. A "no" said by a voice the assistant is not sure
of is weighed too: unless the judge takes it for the person's, the request keeps
waiting (it does nothing by itself). One exception
is the price of never making a hoarse person a stranger by the score alone: at a
signed-in page, a stranger whose sentence the quick judgement takes for the page's
person is that person, "not sure". Ordinary things can then be done with that
person's rights, and the stranger hears answers drawn from this visit's
conversation; anything sensitive still goes to the judge, who sees the voice
weighs against it, and nothing of that voice is learnt. Acting in the house is
ordinary only for what the assistant declared harmless when he built the tool,
checked by its review (a light, never a lock or an alarm). The speech
service has no password: in the stack only the gatekeeper reaches it; run outside
Docker, keep it on the house's network, never reachable from the Internet.
