/*
 * Website chat widget. Add one line to any page of the firm's website:
 *   <script src="https://<app address>/embed.js" async></script>
 * It adds a chat button in the bottom-right corner that opens the assistant in a frame.
 * Optional attributes: data-label="Chat with us" data-color="#1F7A6B" data-position="left".
 */
(function () {
  if (window.__consultChat) return;
  window.__consultChat = true;
  var script = document.currentScript;
  var origin = new URL(script.src).origin;
  var label = script.getAttribute("data-label") || "Chat with us";
  var color = script.getAttribute("data-color") || "#1F7A6B";
  var side = script.getAttribute("data-position") === "left" ? "left" : "right";
  var open = false;
  var frame = null;

  var css = document.createElement("style");
  css.textContent =
    ".cc-launch{position:fixed;bottom:20px;" + side + ":20px;z-index:2147483000;display:flex;align-items:center;gap:8px;" +
    "height:52px;padding:0 18px 0 14px;border:0;border-radius:26px;background:" + color + ";color:#fff;" +
    "font:600 15px/1 system-ui,-apple-system,'Segoe UI',sans-serif;box-shadow:0 6px 24px rgba(16,40,44,.25);cursor:pointer}" +
    ".cc-launch:focus-visible{outline:3px solid #10282C;outline-offset:3px}" +
    ".cc-frame{position:fixed;bottom:84px;" + side + ":20px;z-index:2147483000;width:380px;height:min(620px,calc(100vh - 110px));" +
    "border:0;border-radius:16px;box-shadow:0 12px 48px rgba(16,40,44,.28);background:#fff}" +
    "@media (max-width:480px){.cc-frame{inset:0;width:100%;height:100%;border-radius:0}}";
  document.head.appendChild(css);

  var button = document.createElement("button");
  button.className = "cc-launch";
  button.type = "button";
  button.setAttribute("aria-expanded", "false");
  button.innerHTML =
    '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M4 5.5A2.5 2.5 0 0 1 6.5 3h11A2.5 2.5 0 0 1 20 5.5v8a2.5 2.5 0 0 1-2.5 2.5H10l-4.2 3.6c-.5.4-1.3.1-1.3-.6V16A2.5 2.5 0 0 1 4 13.5z" fill="currentColor"/></svg>';
  button.appendChild(document.createTextNode(label));

  function toggle(show) {
    open = show;
    button.setAttribute("aria-expanded", String(open));
    if (open && !frame) {
      frame = document.createElement("iframe");
      frame.className = "cc-frame";
      frame.title = label;
      frame.allow = "clipboard-write";
      frame.src = origin + "/widget?embedded=1&page=" + encodeURIComponent(location.href.slice(0, 500));
      document.body.appendChild(frame);
    }
    if (frame) frame.style.display = open ? "block" : "none";
    if (open && window.innerWidth <= 480) button.style.display = "none";
    if (!open) {
      button.style.display = "flex";
      button.focus();
    }
  }

  button.addEventListener("click", function () { toggle(!open); });
  window.addEventListener("message", function (e) {
    if (e.origin === origin && e.data && e.data.type === "consult-chat:close") toggle(false);
  });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && open) toggle(false); });
  document.body.appendChild(button);
})();
