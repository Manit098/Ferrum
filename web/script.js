/* ferrum — session playback + copy buttons */

(function () {
  "use strict";

  var reduceMotion = window.matchMedia(
    "(prefers-reduced-motion: reduce)"
  ).matches;

  /* --- the hero session plays once, line by line --- */

  var session = document.getElementById("session");
  if (session) {
    var lines = session.querySelectorAll(".t-line");
    // Beats: prompt first, investigation at a steady pace, diff quick,
    // a beat of suspense before the confirmation, then the verdict.
    var delays = [500, 700, 650, 650, 450, 250, 250, 350, 950, 550, 800];

    if (reduceMotion) {
      lines.forEach(function (line) {
        line.classList.add("shown");
      });
    } else {
      var step = 0;
      function showNext() {
        if (step >= lines.length) return;
        lines[step].classList.add("shown");
        step += 1;
        setTimeout(showNext, delays[step - 1] || 400);
      }
      setTimeout(showNext, 350);
    }
  }

  /* --- copy buttons --- */

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve, reject) {
      var area = document.createElement("textarea");
      area.value = text;
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      try {
        document.execCommand("copy") ? resolve() : reject();
      } catch (err) {
        reject(err);
      }
      document.body.removeChild(area);
    });
  }

  document.querySelectorAll("[data-copy]").forEach(function (button) {
    button.addEventListener("click", function () {
      copyText(button.getAttribute("data-copy")).then(
        function () {
          var label = button.querySelector(".copy-label") || button;
          var original = label.textContent;
          button.classList.add("copied");
          label.textContent = "Copied";
          setTimeout(function () {
            button.classList.remove("copied");
            label.textContent = original;
          }, 1600);
        },
        function () {
          var label = button.querySelector(".copy-label") || button;
          label.textContent = "Press Ctrl+C";
          setTimeout(function () {
            label.textContent = button.hasAttribute("data-copy")
              ? "Copy"
              : label.textContent;
          }, 1600);
        }
      );
    });
  });
})();
