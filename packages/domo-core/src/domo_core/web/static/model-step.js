/* The model step of the first launch. htmx does not show the answer to a request that failed with
   a 4xx, and the step answers 400 to a form that has something wrong in it, with the form again
   and the explanation: show that answer. It is limited to the step's own container, so that
   nothing else on the page changes. The script is deferred, so the page is already parsed. */
(function () {
  var step = document.getElementById("model-step");
  if (!step) {
    return;
  }
  step.addEventListener("htmx:beforeSwap", function (event) {
    var xhr = event.detail.xhr;
    if (xhr && xhr.status === 400) {
      event.detail.shouldSwap = true;
      event.detail.isError = false;
    }
  });
})();
